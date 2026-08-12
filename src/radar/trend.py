"""Deterministic cross-day trend scoring from repository snapshots."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from radar.store import Snapshot, SnapshotStoreProtocol


ALGORITHM_VERSION = "trend-v0.1"
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
    """Score a cohort of repositories using only information available by ``score_date``.

    Cross-repository percentile ranking is intentionally performed in this pure
    function so a score can be reproduced from a frozen fixture without a
    database or network call.
    """
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
    velocity_values = [feature.star_velocity for feature in ready if feature.star_velocity is not None]
    acceleration_values = [feature.acceleration for feature in ready if feature.acceleration is not None]
    activity_values = [feature.activity for feature in ready if feature.activity is not None]
    contributor_values = [feature.contributors for feature in ready if feature.contributors is not None]
    calculated_at = calculated_at or datetime.now(timezone.utc)

    scores: list[TrendScore] = []
    for feature in features:
        if feature.star_velocity is None:
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
        total_score = sum(
            float(components[signal]) * weight
            for signal, weight in _SIGNAL_WEIGHTS.items()
            if components[signal] is not None
        ) / available_weight
        reasons = dict(feature.reasons)
        reasons["status"] = "ready"
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
    """Read recent snapshots, calculate scores, and persist them idempotently."""
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
        store.save_trend_score(score)
    return scores


def _extract_features(
    repository_id: int,
    snapshots: Sequence[Snapshot],
    score_date: date,
    *,
    algorithm_version: str,
) -> _TrendFeatures:
    ordered = sorted(
        (snapshot for snapshot in snapshots if snapshot.snapshot_date <= score_date),
        key=lambda snapshot: snapshot.snapshot_date,
    )
    current = ordered[-1] if ordered else None
    if current is None:
        return _warming_features(
            repository_id,
            score_date,
            "no_snapshot",
            algorithm_version=algorithm_version,
        )

    target_date = current.snapshot_date - timedelta(days=7)
    baseline = _latest_at_or_before(ordered, target_date)
    if baseline is None:
        return _warming_features(
            repository_id,
            score_date,
            "fewer_than_7_days_of_history",
            algorithm_version=algorithm_version,
            current=current,
        )

    observed_days = (current.snapshot_date - baseline.snapshot_date).days
    if observed_days < 7 or observed_days > 10:
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
        activity = float(current.commit_count_30d or 0) + 10.0 * float(current.release_count_30d or 0)
    contributors = (
        float(current.contributor_count_approx)
        if current.contributor_count_approx is not None
        else None
    )
    quality = 50.0 + 50.0 * sum(value is not None for value in optional_values) / len(optional_values)
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
    reasons: dict[str, Any] = {
        "status": "warming_up",
        "warming_reason": reason,
        "algorithm_version": algorithm_version,
    }
    if current is not None:
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
    candidates = [snapshot for snapshot in snapshots if snapshot.snapshot_date <= target]
    return max(candidates, key=lambda snapshot: snapshot.snapshot_date) if candidates else None


def _percentile(value: float, values: Sequence[float | None]) -> float:
    usable = sorted(float(item) for item in values if item is not None)
    if not usable:
        return 50.0
    if len(usable) == 1 or usable[0] == usable[-1]:
        return 50.0
    lower = sum(item < value for item in usable)
    upper = sum(item <= value for item in usable)
    return 100.0 * ((lower + upper) / 2.0) / (len(usable) - 1)


def _rounded(value: float | None, *, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None
