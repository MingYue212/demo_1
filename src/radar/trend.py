"""基于仓库快照的确定性跨日趋势评分。

评分器只读取指定评分日之前的数据，不访问网络，也不依赖数据库查询顺序。
因此同一批快照、同一算法版本和同一评分日可以得到可复现的排名结果。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from radar.store import Snapshot, SnapshotStoreProtocol


ALGORITHM_VERSION = "trend-v0.1"
# 权重会在可用信号不足时重新归一化，避免缺失数据把项目直接判为低分。
_SIGNAL_WEIGHTS = {
    "star_velocity": 0.35,
    "acceleration": 0.20,
    "activity": 0.15,
    "freshness": 0.10,
    "quality": 0.10,
    "contributors": 0.10,
}


@dataclass(frozen=True)
class TrendScore:
    """一个仓库在某个评分日、某个算法版本下的完整分数记录。"""

    repository_id: int
    score_date: date
    algorithm_version: str
    total_score: float | None
    star_velocity_score: float | None
    acceleration_score: float | None
    activity_score: float | None
    freshness_score: float | None
    quality_score: float | None
    reasons: dict[str, Any]
    calculated_at: datetime


@dataclass(frozen=True)
class _TrendFeatures:
    """评分前的中间特征，包含可解释原因和原始快照引用。"""

    repository_id: int
    score_date: date
    current: Snapshot | None
    baseline: Snapshot | None
    star_velocity: float | None
    acceleration: float | None
    activity: float | None
    contributors: float | None
    freshness: float | None
    quality: float | None
    reasons: dict[str, Any]


def score_histories(
    histories: Mapping[int, Sequence[Snapshot]],
    *,
    score_date: date,
    algorithm_version: str = ALGORITHM_VERSION,
    calculated_at: datetime | None = None,
) -> list[TrendScore]:
    """用评分日可获得的信息为一组仓库计算趋势分数。

    跨仓库百分位排名故意放在这个纯函数中完成，使评分可以脱离数据库和
    网络，用冻结 fixture 独立复现。

    Args:
        histories: 按仓库 ID 分组的快照历史。
        score_date: 评分截止日期，未来快照会被忽略。
        algorithm_version: 写入解释信息和持久化键的算法版本。
        calculated_at: 可选的计算时间；测试和回放时可固定该值。

    Returns:
        按仓库 ID 排序的趋势分数列表；历史不足的项目返回 warming_up 记录。
    """
    # 先提取每个仓库的原始特征，再在整个 cohort 内计算百分位。
    features = [
        _extract_features(
            repository_id,
            snapshots,
            score_date,
            algorithm_version=algorithm_version,
        )
        for repository_id, snapshots in sorted(histories.items())
    ]
    ready = [feature for feature in features if feature.star_velocity is not None]
    # 百分位分母来自所有可评分项目，避免单个项目的绝对量级主导结果。
    velocity_values = [feature.star_velocity for feature in ready if feature.star_velocity is not None]
    acceleration_values = [feature.acceleration for feature in ready if feature.acceleration is not None]
    activity_values = [feature.activity for feature in ready if feature.activity is not None]
    contributor_values = [feature.contributors for feature in ready if feature.contributors is not None]
    calculated_at = calculated_at or datetime.now(timezone.utc)

    scores: list[TrendScore] = []
    for feature in features:
        if feature.star_velocity is None:
            # 没有 7 日基线时保留 warming_up，而不是伪造一个正式分数。
            scores.append(
                TrendScore(
                    repository_id=feature.repository_id,
                    score_date=score_date,
                    algorithm_version=algorithm_version,
                    total_score=None,
                    star_velocity_score=None,
                    acceleration_score=None,
                    activity_score=None,
                    freshness_score=None,
                    quality_score=None,
                    reasons=feature.reasons,
                    calculated_at=calculated_at,
                )
            )
            continue

        # 不同信号先转换为 0~100 组件，再按当前可用权重求加权平均。
        components: dict[str, float | None] = {
            "star_velocity": _percentile(feature.star_velocity, velocity_values),
            "acceleration": (
                _percentile(feature.acceleration, acceleration_values)
                if feature.acceleration is not None
                else None
            ),
            "activity": (
                _percentile(feature.activity, activity_values)
                if feature.activity is not None
                else None
            ),
            "freshness": feature.freshness,
            "quality": feature.quality,
            "contributors": (
                _percentile(feature.contributors, contributor_values)
                if feature.contributors is not None
                else None
            ),
        }
        available_weight = sum(
            weight for signal, weight in _SIGNAL_WEIGHTS.items() if components[signal] is not None
        )
        # available_weight 至少包含 star_velocity，因此不会出现除零。
        total_score = sum(
            float(components[signal]) * weight
            for signal, weight in _SIGNAL_WEIGHTS.items()
            if components[signal] is not None
        ) / available_weight
        reasons = dict(feature.reasons)
        reasons["status"] = "ready"
        # 将可用和缺失信号写入 reasons，供 API 和后续编辑分析解释排名。
        reasons["available_signals"] = [signal for signal, value in components.items() if value is not None]
        reasons["missing_signals"] = [signal for signal, value in components.items() if value is None]
        scores.append(
            TrendScore(
                repository_id=feature.repository_id,
                score_date=score_date,
                algorithm_version=algorithm_version,
                total_score=round(total_score, 2),
                star_velocity_score=_rounded(components["star_velocity"]),
                acceleration_score=_rounded(components["acceleration"]),
                activity_score=_rounded(components["activity"]),
                freshness_score=_rounded(components["freshness"]),
                quality_score=_rounded(components["quality"]),
                reasons=reasons,
                calculated_at=calculated_at,
            )
        )
    return scores


def score_store(
    store: SnapshotStoreProtocol,
    *,
    score_date: date,
    algorithm_version: str = ALGORITHM_VERSION,
) -> list[TrendScore]:
    """读取最近快照、计算分数，并以幂等方式持久化结果。"""
    # 每个仓库只读取评分日前最近 8 条快照，覆盖 7 日基线和短期加速度窗口。
    histories = {
        repository_id: store.snapshot_history(repository_id, end_date=score_date, limit=8)
        for repository_id in store.repository_ids()
    }
    scores = score_histories(
        histories,
        score_date=score_date,
        algorithm_version=algorithm_version,
    )
    for score in scores:
        # 存储层的复合主键保证重复评分只更新同一版本的记录。
        store.save_trend_score(score)
    return scores


def _extract_features(
    repository_id: int,
    snapshots: Sequence[Snapshot],
    score_date: date,
    *,
    algorithm_version: str,
) -> _TrendFeatures:
    """从一个仓库的快照序列提取评分特征。"""
    # 过滤未来数据并排序，避免调用方传入无序或超前快照造成数据泄漏。
    ordered = sorted(
        (snapshot for snapshot in snapshots if snapshot.snapshot_date <= score_date),
        key=lambda snapshot: snapshot.snapshot_date,
    )
    current = ordered[-1] if ordered else None
    if current is None:
        # 仓库存在但没有可用快照时，仍返回可展示的 warming_up 原因。
        return _warming_features(
            repository_id,
            score_date,
            "no_snapshot",
            algorithm_version=algorithm_version,
        )

    target_date = current.snapshot_date - timedelta(days=7)
    baseline = _latest_at_or_before(ordered, target_date)
    if baseline is None:
        # 没有 7 日前基线，无法计算 star velocity。
        return _warming_features(
            repository_id,
            score_date,
            "fewer_than_7_days_of_history",
            algorithm_version=algorithm_version,
            current=current,
        )

    observed_days = (current.snapshot_date - baseline.snapshot_date).days
    if observed_days < 7 or observed_days > 10:
        # 允许 7~10 天窗口，兼容周末或定时任务延迟，但拒绝过大的间隔。
        return _warming_features(
            repository_id,
            score_date,
            "snapshot_window_is_not_7_to_10_days",
            algorithm_version=algorithm_version,
            current=current,
            baseline=baseline,
            observed_days=observed_days,
        )

    star_delta = current.stars - baseline.stars
    star_velocity = star_delta / observed_days
    # 使用最近 3 天与前一段时间的速度差，近似衡量增长是否在加速。
    recent_baseline = _latest_at_or_before(ordered, current.snapshot_date - timedelta(days=3))
    acceleration = None
    if recent_baseline is not None:
        recent_days = (current.snapshot_date - recent_baseline.snapshot_date).days
        previous_days = (recent_baseline.snapshot_date - baseline.snapshot_date).days
        if recent_days >= 2 and previous_days >= 2:
            recent_velocity = (current.stars - recent_baseline.stars) / recent_days
            previous_velocity = (recent_baseline.stars - baseline.stars) / previous_days
            acceleration = recent_velocity - previous_velocity

    lag_days = max(0, (score_date - current.snapshot_date).days)
    # 每滞后一天扣 25 分，四天没有新快照时新鲜度降为 0。
    freshness = max(0.0, 100.0 - lag_days * 25.0)
    optional_values = (
        current.commit_count_30d,
        current.release_count_30d,
        current.contributor_count_approx,
    )
    activity_values = [
        value
        for value in (
            current.commit_count_30d,
            current.release_count_30d,
        )
        if value is not None
    ]
    activity = None
    if activity_values:
        # release 的稀缺性通常高于单次 commit，因此给予更高的简单系数。
        activity = float(current.commit_count_30d or 0) + 10.0 * float(current.release_count_30d or 0)
    contributors = (
        float(current.contributor_count_approx)
        if current.contributor_count_approx is not None
        else None
    )
    quality = 50.0 + 50.0 * sum(value is not None for value in optional_values) / len(optional_values)
    # quality 只反映数据完整度，不把缺失可选信号误当成项目质量差。
    reasons = {
        "status": "warming_up",
        "current_snapshot_date": current.snapshot_date.isoformat(),
        "baseline_snapshot_date": baseline.snapshot_date.isoformat(),
        "observed_days": observed_days,
        "stars_delta_7d": star_delta,
        "star_velocity_per_day": round(star_velocity, 4),
        "acceleration_per_day": _rounded(acceleration, digits=4),
        "data_lag_days": lag_days,
        "algorithm_version": algorithm_version,
    }
    return _TrendFeatures(
        repository_id=repository_id,
        score_date=score_date,
        current=current,
        baseline=baseline,
        star_velocity=star_velocity,
        acceleration=acceleration,
        activity=activity,
        contributors=contributors,
        freshness=freshness,
        quality=quality,
        reasons=reasons,
    )


def _warming_features(
    repository_id: int,
    score_date: date,
    reason: str,
    *,
    algorithm_version: str,
    current: Snapshot | None = None,
    baseline: Snapshot | None = None,
    observed_days: int | None = None,
) -> _TrendFeatures:
    """构造历史不足项目的中间特征和可解释原因。"""
    reasons: dict[str, Any] = {
        "status": "warming_up",
        "warming_reason": reason,
        "algorithm_version": algorithm_version,
    }
    if current is not None:
        # 尽可能保留当前快照日期，帮助 API 使用者定位为何仍在预热。
        reasons["current_snapshot_date"] = current.snapshot_date.isoformat()
    if baseline is not None:
        reasons["baseline_snapshot_date"] = baseline.snapshot_date.isoformat()
    if observed_days is not None:
        reasons["observed_days"] = observed_days
    return _TrendFeatures(
        repository_id=repository_id,
        score_date=score_date,
        current=current,
        baseline=baseline,
        star_velocity=None,
        acceleration=None,
        activity=None,
        contributors=None,
        freshness=None,
        quality=None,
        reasons=reasons,
    )


def _latest_at_or_before(snapshots: Sequence[Snapshot], target: date) -> Snapshot | None:
    """返回不晚于目标日期的最新快照。"""
    candidates = [snapshot for snapshot in snapshots if snapshot.snapshot_date <= target]
    return max(candidates, key=lambda snapshot: snapshot.snapshot_date) if candidates else None


def _percentile(value: float, values: Sequence[float | None]) -> float:
    """计算带并列处理中位秩的百分位，结果范围为 0~100。"""
    usable = sorted(float(item) for item in values if item is not None)
    if not usable:
        # 没有 cohort 对照时使用中性分，避免人为放大单项目结果。
        return 50.0
    if len(usable) == 1 or usable[0] == usable[-1]:
        return 50.0
    lower = sum(item < value for item in usable)
    upper = sum(item <= value for item in usable)
    return 100.0 * ((lower + upper) / 2.0) / (len(usable) - 1)


def _rounded(value: float | None, *, digits: int = 2) -> float | None:
    """统一保留展示和持久化所需的小数位数。"""
    return round(value, digits) if value is not None else None
