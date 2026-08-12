"""仓库快照、趋势分数和查询结果的 SQLite/PostgreSQL 持久化层。

SQLite 用于本地实验和 CI fallback；PostgreSQL 是 V0.1 的持久化后端。两个
实现遵守同一套协议，让采集器、评分器和只读 API 不需要感知具体数据库。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Protocol

if TYPE_CHECKING:
    from radar.trend import TrendScore


SCHEMA = """
-- 保存 GitHub 仓库的相对稳定元数据。
CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    description TEXT,
    primary_language TEXT,
    created_at TEXT NOT NULL,
    pushed_at TEXT,
    archived INTEGER NOT NULL,
    is_fork INTEGER NOT NULL,
    discovered_at TEXT NOT NULL,
    discovery_source TEXT NOT NULL
);

-- 每个仓库每天最多一条快照，主键同时提供幂等写入约束。
CREATE TABLE IF NOT EXISTS repository_snapshots (
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    snapshot_date TEXT NOT NULL,
    stars INTEGER NOT NULL CHECK (stars >= 0),
    forks INTEGER NOT NULL CHECK (forks >= 0),
    open_issues INTEGER NOT NULL CHECK (open_issues >= 0),
    watchers INTEGER NOT NULL CHECK (watchers >= 0),
    commit_count_30d INTEGER,
    release_count_30d INTEGER,
    contributor_count_approx INTEGER,
    collected_at TEXT NOT NULL,
    raw_payload_hash TEXT NOT NULL,
    PRIMARY KEY (repository_id, snapshot_date)
);

-- 保存按算法版本区分的趋势分数，total_score 为空表示 warming_up。
CREATE TABLE IF NOT EXISTS trend_scores (
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    score_date TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    total_score REAL,
    star_velocity_score REAL,
    acceleration_score REAL,
    activity_score REAL,
    freshness_score REAL,
    quality_score REAL,
    reasons_json TEXT NOT NULL,
    calculated_at TEXT NOT NULL,
    PRIMARY KEY (repository_id, score_date, algorithm_version)
);

-- API 按日期和分数排序时使用的索引。
CREATE INDEX IF NOT EXISTS idx_trend_scores_date_score
    ON trend_scores (score_date, total_score DESC);

CREATE INDEX IF NOT EXISTS idx_snapshots_repository_date
    ON repository_snapshots (repository_id, snapshot_date DESC);
"""

# PostgreSQL 使用打包在 migrations 目录中的 SQL，避免在 Python 中维护两份 schema。
POSTGRES_SCHEMA = files("radar.migrations").joinpath("001_initial.sql").read_text(encoding="utf-8")
POSTGRES_MIGRATIONS = (
    POSTGRES_SCHEMA,
    files("radar.migrations").joinpath("002_trend_scores.sql").read_text(encoding="utf-8"),
)


@dataclass(frozen=True)
class Snapshot:
    """某个仓库在某一天采集到的客观指标。"""

    repository_id: int
    snapshot_date: date
    stars: int
    forks: int
    open_issues: int
    watchers: int
    commit_count_30d: int | None = None
    release_count_30d: int | None = None
    contributor_count_approx: int | None = None


@dataclass(frozen=True)
class RepositoryRecord:
    """供查询 API 返回的仓库元数据。"""

    repository_id: int
    full_name: str
    url: str
    description: str | None
    primary_language: str | None
    created_at: str
    pushed_at: str | None
    archived: bool
    is_fork: bool
    discovered_at: str
    discovery_source: str


@dataclass(frozen=True)
class TrendScoreRecord:
    """从数据库读取的、可直接序列化为 API 响应的趋势分数。"""

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
    calculated_at: str


@dataclass(frozen=True)
class TrendingItem:
    """一个仓库元数据和对应趋势分数的联结结果。"""

    repository: RepositoryRecord
    score: TrendScoreRecord


class SnapshotStoreProtocol(Protocol):
    """采集器、评分器和 API 共同依赖的最小存储协议。"""

    def initialize(self) -> None:
        """创建或迁移底层数据库 schema。"""

    def counts(self) -> tuple[int, int]:
        """返回仓库数和快照数，供健康检查使用。"""

    def save_repository(self, repository: dict[str, Any], source: str, snapshot_day: date) -> None:
        """保存仓库元数据，并写入指定日期的快照。"""

    def repository_ids(self) -> list[int]:
        """返回可以参与评分的仓库 ID。"""

    def snapshot_history(
        self,
        repository_id: int,
        *,
        end_date: date | None = None,
        limit: int = 8,
    ) -> list[Snapshot]:
        """按日期升序返回最近快照。"""

    def get_repository(self, repository_id: int) -> RepositoryRecord | None:
        """返回仓库元数据；仓库不存在时返回 None。"""

    def list_trending(
        self,
        score_date: date,
        *,
        algorithm_version: str | None = None,
        limit: int = 20,
        include_warming_up: bool = False,
    ) -> list[TrendingItem]:
        """返回与仓库元数据联结后的趋势分数行。"""

    def get_trend_score(
        self,
        repository_id: int,
        score_date: date,
        *,
        algorithm_version: str | None = None,
    ) -> TrendScoreRecord | None:
        """返回指定仓库、日期和算法版本的分数。"""

    def save_trend_score(self, score: "TrendScore") -> None:
        """保存指定仓库和算法版本的趋势分数。"""


class SnapshotStore:
    """用于本地运行和无数据库 CI 环境的 SQLite 存储实现。"""

    def __init__(self, path: str | Path) -> None:
        """记录 SQLite 文件路径；真正的连接在每次操作时短暂创建。"""
        self.path = str(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """创建带外键约束的连接，并在离开上下文时提交和关闭。"""
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        # SQLite 默认不强制外键，显式打开以保证孤立快照不会被写入。
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        """创建当前 schema，并为旧 SQLite 文件补齐新增列。"""
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            # 兼容 Trend Score 功能上线前已经存在的本地数据库。
            _ensure_sqlite_snapshot_columns(connection)

    def save_repository(self, repository: dict[str, Any], source: str, snapshot_day: date) -> None:
        """幂等保存仓库元数据和指定日期的 GitHub 指标快照。"""
        required = {"id", "full_name", "html_url", "created_at"}
        missing = required.difference(repository)
        if missing:
            raise ValueError(f"repository is missing required fields: {', '.join(sorted(missing))}")

        # 原始 payload 哈希用于审计同日数据是否发生变化。
        collected_at = datetime.now(timezone.utc).isoformat()
        payload_hash = _payload_hash(repository)
        with self.connect() as connection:
            # 仓库元数据按 GitHub ID 更新，避免 full_name 改名时产生重复实体。
            connection.execute(
                """
                INSERT INTO repositories (
                    id, full_name, url, description, primary_language, created_at,
                    pushed_at, archived, is_fork, discovered_at, discovery_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    full_name=excluded.full_name, url=excluded.url,
                    description=excluded.description, primary_language=excluded.primary_language,
                    pushed_at=excluded.pushed_at, archived=excluded.archived,
                    is_fork=excluded.is_fork
                """,
                (
                    repository["id"], repository["full_name"], repository["html_url"],
                    repository.get("description"), repository.get("language"),
                    repository["created_at"], repository.get("pushed_at"),
                    bool(repository.get("archived")), bool(repository.get("fork")),
                    collected_at, source,
                ),
            )
            # 快照按 (repository_id, snapshot_date) 更新，保证重复采集幂等。
            connection.execute(
                """
                INSERT INTO repository_snapshots (
                    repository_id, snapshot_date, stars, forks, open_issues,
                    watchers, commit_count_30d, release_count_30d,
                    contributor_count_approx, collected_at, raw_payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repository_id, snapshot_date) DO UPDATE SET
                    stars=excluded.stars, forks=excluded.forks,
                    open_issues=excluded.open_issues, watchers=excluded.watchers,
                    commit_count_30d=excluded.commit_count_30d,
                    release_count_30d=excluded.release_count_30d,
                    contributor_count_approx=excluded.contributor_count_approx,
                    collected_at=excluded.collected_at,
                    raw_payload_hash=excluded.raw_payload_hash
                """,
                (
                    repository["id"], snapshot_day.isoformat(),
                    int(repository.get("stargazers_count", 0)),
                    int(repository.get("forks_count", 0)),
                    int(repository.get("open_issues_count", 0)),
                    int(repository.get("subscribers_count", repository.get("watchers_count", 0))),
                    _optional_int(repository.get("commit_count_30d")),
                    _optional_int(repository.get("release_count_30d")),
                    _optional_int(repository.get("contributor_count_approx")),
                    collected_at, payload_hash,
                ),
            )

    def counts(self) -> tuple[int, int]:
        """返回当前数据库中的仓库数和快照数。"""
        with self.connect() as connection:
            repositories = connection.execute("SELECT count(*) FROM repositories").fetchone()[0]
            snapshots = connection.execute("SELECT count(*) FROM repository_snapshots").fetchone()[0]
        return repositories, snapshots

    def repository_ids(self) -> list[int]:
        """按 ID 升序返回所有已发现仓库。"""
        with self.connect() as connection:
            rows = connection.execute("SELECT id FROM repositories ORDER BY id").fetchall()
        return [int(row[0]) for row in rows]

    def snapshot_history(
        self,
        repository_id: int,
        *,
        end_date: date | None = None,
        limit: int = 8,
    ) -> list[Snapshot]:
        """读取一个仓库的最近历史，并按日期升序返回。"""
        _validate_history_limit(limit)
        query = """
            SELECT repository_id, snapshot_date, stars, forks, open_issues,
                   watchers, commit_count_30d, release_count_30d,
                   contributor_count_approx
            FROM repository_snapshots
            WHERE repository_id = ?
        """
        parameters: list[Any] = [repository_id]
        if end_date is not None:
            # 评分和 API 查询都不能读取评分日之后的未来数据。
            query += " AND snapshot_date <= ?"
            parameters.append(end_date.isoformat())
        query += " ORDER BY snapshot_date DESC LIMIT ?"
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        # SQL 为了 LIMIT 使用降序，返回前翻转为时间序列自然顺序。
        return [_snapshot_from_row(row) for row in reversed(rows)]

    def get_repository(self, repository_id: int) -> RepositoryRecord | None:
        """按 GitHub repository ID 查询元数据。"""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id AS repository_id, full_name, url, description,
                       primary_language, created_at, pushed_at, archived,
                       is_fork, discovered_at, discovery_source
                FROM repositories
                WHERE id = ?
                """,
                (repository_id,),
            ).fetchone()
        return _repository_from_row(row) if row is not None else None

    def list_trending(
        self,
        score_date: date,
        *,
        algorithm_version: str | None = None,
        limit: int = 20,
        include_warming_up: bool = False,
    ) -> list[TrendingItem]:
        """按总分降序返回指定日期的 Trending 项目。"""
        _validate_query_limit(limit)
        query = _trending_query("?", include_warming_up=include_warming_up)
        parameters: list[Any] = [score_date.isoformat()]
        if algorithm_version is not None:
            # 指定算法版本后只返回该版本，避免不同版本混排。
            query += " AND t.algorithm_version = ?"
            parameters.append(algorithm_version)
        query += " ORDER BY (t.total_score IS NULL), t.total_score DESC, r.full_name LIMIT ?"
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_trending_item_from_row(row) for row in rows]

    def get_trend_score(
        self,
        repository_id: int,
        score_date: date,
        *,
        algorithm_version: str | None = None,
    ) -> TrendScoreRecord | None:
        """读取单个仓库在指定日期的最新匹配分数。"""
        query = """
            SELECT repository_id, score_date, algorithm_version, total_score,
                   star_velocity_score, acceleration_score, activity_score,
                   freshness_score, quality_score, reasons_json, calculated_at
            FROM trend_scores
            WHERE repository_id = ? AND score_date = ?
        """
        parameters: list[Any] = [repository_id, score_date.isoformat()]
        if algorithm_version is not None:
            query += " AND algorithm_version = ?"
            parameters.append(algorithm_version)
        query += " ORDER BY calculated_at DESC LIMIT 1"
        with self.connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        return _trend_score_from_row(row) if row is not None else None

    def save_trend_score(self, score: "TrendScore") -> None:
        """幂等保存一条趋势分数及其可解释原因。"""
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO trend_scores (
                    repository_id, score_date, algorithm_version, total_score,
                    star_velocity_score, acceleration_score, activity_score,
                    freshness_score, quality_score, reasons_json, calculated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repository_id, score_date, algorithm_version) DO UPDATE SET
                    total_score=excluded.total_score,
                    star_velocity_score=excluded.star_velocity_score,
                    acceleration_score=excluded.acceleration_score,
                    activity_score=excluded.activity_score,
                    freshness_score=excluded.freshness_score,
                    quality_score=excluded.quality_score,
                    reasons_json=excluded.reasons_json,
                    calculated_at=excluded.calculated_at
                """,
                _trend_score_values(score, calculated_at=score.calculated_at.isoformat()),
            )


def _payload_hash(payload: dict[str, Any]) -> str:
    """对排序稳定的 JSON payload 计算 SHA-256 哈希。"""
    import hashlib

    # separators 去掉无关空白，sort_keys 保证字段顺序不影响哈希。
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _ensure_sqlite_snapshot_columns(connection: sqlite3.Connection) -> None:
    """为旧 SQLite 数据库补充 Trend Score 所需的可选指标列。"""
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(repository_snapshots)").fetchall()
    }
    for column in ("commit_count_30d", "release_count_30d", "contributor_count_approx"):
        # SQLite 不支持在 ADD COLUMN 中使用 IF NOT EXISTS，因此先检查列清单。
        if column not in columns:
            connection.execute(f"ALTER TABLE repository_snapshots ADD COLUMN {column} INTEGER")


def _optional_int(value: Any) -> int | None:
    """把可选数值转换成整数，并保留 None。"""
    if value is None:
        return None
    return int(value)


def _validate_history_limit(limit: int) -> None:
    """校验历史查询上限，避免 API 或内部调用读取过大数据集。"""
    if not 1 <= limit <= 366:
        raise ValueError("history limit must be between 1 and 366")


def _validate_query_limit(limit: int) -> None:
    """校验 Trending 查询上限。"""
    if not 1 <= limit <= 100:
        raise ValueError("query limit must be between 1 and 100")


def _row_value(row: Any, key: str, index: int) -> Any:
    """兼容 SQLite 命名行和 psycopg 元组行的字段读取。"""
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return row[index]


def _iso_timestamp(value: Any) -> str:
    """把数据库返回的 datetime 或字符串统一为 ISO 文本。"""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _repository_from_row(row: Any, *, index_offset: int = 0) -> RepositoryRecord:
    """把仓库查询行转换为跨数据库一致的记录对象。"""
    return RepositoryRecord(
        repository_id=int(_row_value(row, "repository_id", index_offset)),
        full_name=str(_row_value(row, "full_name", index_offset + 1)),
        url=str(_row_value(row, "url", index_offset + 2)),
        description=_row_value(row, "description", index_offset + 3),
        primary_language=_row_value(row, "primary_language", index_offset + 4),
        created_at=_iso_timestamp(_row_value(row, "created_at", index_offset + 5)),
        pushed_at=(
            None
            if _row_value(row, "pushed_at", index_offset + 6) is None
            else _iso_timestamp(_row_value(row, "pushed_at", index_offset + 6))
        ),
        archived=bool(_row_value(row, "archived", index_offset + 7)),
        is_fork=bool(_row_value(row, "is_fork", index_offset + 8)),
        discovered_at=_iso_timestamp(_row_value(row, "discovered_at", index_offset + 9)),
        discovery_source=str(_row_value(row, "discovery_source", index_offset + 10)),
    )


def _parse_reasons(value: Any) -> dict[str, Any]:
    """把 SQLite 文本或 PostgreSQL JSONB 转为原因字典。"""
    if isinstance(value, dict):
        return value
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("trend score reasons must be a JSON object")
    return parsed


def _score_date(value: Any) -> date:
    """兼容 PostgreSQL date/datetime 和 SQLite 字符串日期。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _trend_score_from_row(row: Any, *, index_offset: int = 0, prefix: str = "") -> TrendScoreRecord:
    """把趋势分数查询行转换为 API 可用的记录对象。"""
    def value(name: str, offset: int) -> Any:
        """按字段别名或位置读取一列。"""
        # 联结查询使用 score_ 前缀，单表查询则使用默认字段名。
        return _row_value(row, f"{prefix}{name}", index_offset + offset)

    return TrendScoreRecord(
        repository_id=int(value("repository_id", 0)),
        score_date=_score_date(value("score_date", 1)),
        algorithm_version=str(value("algorithm_version", 2)),
        total_score=_optional_float(value("total_score", 3)),
        star_velocity_score=_optional_float(value("star_velocity_score", 4)),
        acceleration_score=_optional_float(value("acceleration_score", 5)),
        activity_score=_optional_float(value("activity_score", 6)),
        freshness_score=_optional_float(value("freshness_score", 7)),
        quality_score=_optional_float(value("quality_score", 8)),
        reasons=_parse_reasons(value("reasons_json", 9)),
        calculated_at=_iso_timestamp(value("calculated_at", 10)),
    )


def _optional_float(value: Any) -> float | None:
    """把数据库中的可选数值统一为浮点数。"""
    if value is None:
        return None
    return float(value)


def _trending_query(placeholder: str, *, include_warming_up: bool) -> str:
    """生成 SQLite 或 PostgreSQL 共享结构的 Trending 联结 SQL。"""
    # 默认排除 total_score 为空的 warming_up 项目，API 可显式要求包含它们。
    warming_filter = "" if include_warming_up else " AND t.total_score IS NOT NULL"
    return f"""
        SELECT
            r.id AS repository_id, r.full_name, r.url, r.description,
            r.primary_language, r.created_at, r.pushed_at, r.archived,
            r.is_fork, r.discovered_at, r.discovery_source,
            t.repository_id AS score_repository_id, t.score_date AS score_date,
            t.algorithm_version AS score_algorithm_version,
            t.total_score AS score_total_score,
            t.star_velocity_score AS score_star_velocity_score,
            t.acceleration_score AS score_acceleration_score,
            t.activity_score AS score_activity_score,
            t.freshness_score AS score_freshness_score,
            t.quality_score AS score_quality_score,
            t.reasons_json AS score_reasons_json,
            t.calculated_at AS score_calculated_at
        FROM repositories r
        JOIN trend_scores t ON t.repository_id = r.id
        WHERE t.score_date = {placeholder}{warming_filter}
    """


def _trending_item_from_row(row: Any) -> TrendingItem:
    """把仓库和分数联结行拆分为两个领域记录。"""
    return TrendingItem(
        repository=_repository_from_row(row),
        score=_trend_score_from_row(row, index_offset=11, prefix="score_"),
    )


def _snapshot_from_row(row: Any) -> Snapshot:
    """把 SQLite 命名行转换为 Snapshot。"""
    return Snapshot(
        repository_id=int(row["repository_id"]),
        snapshot_date=date.fromisoformat(row["snapshot_date"]),
        stars=int(row["stars"]),
        forks=int(row["forks"]),
        open_issues=int(row["open_issues"]),
        watchers=int(row["watchers"]),
        commit_count_30d=_optional_int(row["commit_count_30d"]),
        release_count_30d=_optional_int(row["release_count_30d"]),
        contributor_count_approx=_optional_int(row["contributor_count_approx"]),
    )


def _snapshot_from_values(values: Any) -> Snapshot:
    """把 PostgreSQL 位置行转换为 Snapshot。"""
    snapshot_date = values[1]
    if isinstance(snapshot_date, str):
        snapshot_date = date.fromisoformat(snapshot_date)
    return Snapshot(
        repository_id=int(values[0]),
        snapshot_date=snapshot_date,
        stars=int(values[2]),
        forks=int(values[3]),
        open_issues=int(values[4]),
        watchers=int(values[5]),
        commit_count_30d=_optional_int(values[6]),
        release_count_30d=_optional_int(values[7]),
        contributor_count_approx=_optional_int(values[8]),
    )


def _trend_score_values(score: "TrendScore", *, calculated_at: Any) -> tuple[Any, ...]:
    """按两个数据库的写入顺序准备趋势分数参数。"""
    return (
        score.repository_id,
        score.score_date.isoformat(),
        score.algorithm_version,
        score.total_score,
        score.star_velocity_score,
        score.acceleration_score,
        score.activity_score,
        score.freshness_score,
        score.quality_score,
        json.dumps(score.reasons, sort_keys=True, separators=(",", ":")),
        calculated_at,
    )


PostgresConnectionFactory = Callable[[str], Any]


def _default_postgres_connection(dsn: str) -> Any:
    """创建默认 psycopg 连接；导入延迟到真正使用 PostgreSQL 时。"""
    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError(
            "PostgreSQL support requires `python -m pip install 'ai-tech-radar[postgres]'`"
        ) from error
    return psycopg.connect(dsn)


def _github_timestamp(value: Any) -> datetime | None:
    """把 GitHub 的 ISO-8601 时间转换为带时区的 datetime。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError("GitHub timestamp must be an ISO-8601 string")
    # Python fromisoformat 需要显式的 UTC 偏移，而 GitHub 常返回 Z 后缀。
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class PostgresSnapshotStore:
    """遵守 SnapshotStoreProtocol 的 PostgreSQL 持久化实现。"""

    def __init__(
        self,
        dsn: str,
        *,
        connection_factory: PostgresConnectionFactory | None = None,
    ) -> None:
        """保存 DSN 和可替换连接工厂，便于测试而不要求真实数据库。"""
        if not dsn.strip():
            raise ValueError("PostgreSQL database URL cannot be empty")
        self.dsn = dsn
        self.connection_factory = connection_factory or _default_postgres_connection

    @contextmanager
    def connect(self) -> Iterator[Any]:
        """创建一次 PostgreSQL 连接，并统一处理提交、回滚和关闭。"""
        connection = self.connection_factory(self.dsn)
        try:
            yield connection
            connection.commit()
        except Exception:
            # 任何 SQL 异常都必须回滚，否则连接无法安全复用或关闭。
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """按顺序执行基础 schema 和趋势分数 migration。"""
        with self.connect() as connection:
            for migration in POSTGRES_MIGRATIONS:
                # migration 文件本身使用 IF NOT EXISTS，可重复执行。
                connection.execute(migration)

    def save_repository(self, repository: dict[str, Any], source: str, snapshot_day: date) -> None:
        """使用 PostgreSQL 类型写入仓库元数据和每日快照。"""
        required = {"id", "full_name", "html_url", "created_at"}
        missing = required.difference(repository)
        if missing:
            raise ValueError(f"repository is missing required fields: {', '.join(sorted(missing))}")

        collected_at = datetime.now(timezone.utc)
        payload_hash = _payload_hash(repository)
        with self.connect() as connection:
            # PostgreSQL 使用 %s 占位符，并把 GitHub 时间转为 TIMESTAMPTZ。
            connection.execute(
                """
                INSERT INTO repositories (
                    id, full_name, url, description, primary_language, created_at,
                    pushed_at, archived, is_fork, discovered_at, discovery_source
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    full_name=EXCLUDED.full_name, url=EXCLUDED.url,
                    description=EXCLUDED.description, primary_language=EXCLUDED.primary_language,
                    pushed_at=EXCLUDED.pushed_at, archived=EXCLUDED.archived,
                    is_fork=EXCLUDED.is_fork
                """,
                (
                    repository["id"], repository["full_name"], repository["html_url"],
                    repository.get("description"), repository.get("language"),
                    _github_timestamp(repository["created_at"]),
                    _github_timestamp(repository.get("pushed_at")),
                    bool(repository.get("archived")), bool(repository.get("fork")),
                    collected_at, source,
                ),
            )
            # 复合主键使同日重跑安全地更新原记录。
            connection.execute(
                """
                INSERT INTO repository_snapshots (
                    repository_id, snapshot_date, stars, forks, open_issues,
                    watchers, commit_count_30d, release_count_30d,
                    contributor_count_approx, collected_at, raw_payload_hash
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (repository_id, snapshot_date) DO UPDATE SET
                    stars=EXCLUDED.stars, forks=EXCLUDED.forks,
                    open_issues=EXCLUDED.open_issues, watchers=EXCLUDED.watchers,
                    commit_count_30d=EXCLUDED.commit_count_30d,
                    release_count_30d=EXCLUDED.release_count_30d,
                    contributor_count_approx=EXCLUDED.contributor_count_approx,
                    collected_at=EXCLUDED.collected_at,
                    raw_payload_hash=EXCLUDED.raw_payload_hash
                """,
                (
                    repository["id"], snapshot_day,
                    int(repository.get("stargazers_count", 0)),
                    int(repository.get("forks_count", 0)),
                    int(repository.get("open_issues_count", 0)),
                    int(repository.get("subscribers_count", repository.get("watchers_count", 0))),
                    _optional_int(repository.get("commit_count_30d")),
                    _optional_int(repository.get("release_count_30d")),
                    _optional_int(repository.get("contributor_count_approx")),
                    collected_at, payload_hash,
                ),
            )

    def counts(self) -> tuple[int, int]:
        """返回 PostgreSQL 中的仓库数和快照数。"""
        with self.connect() as connection:
            repositories = connection.execute("SELECT count(*) FROM repositories").fetchone()[0]
            snapshots = connection.execute("SELECT count(*) FROM repository_snapshots").fetchone()[0]
        return repositories, snapshots

    def repository_ids(self) -> list[int]:
        """按 ID 升序返回所有仓库。"""
        with self.connect() as connection:
            rows = connection.execute("SELECT id FROM repositories ORDER BY id").fetchall()
        return [int(row[0]) for row in rows]

    def snapshot_history(
        self,
        repository_id: int,
        *,
        end_date: date | None = None,
        limit: int = 8,
    ) -> list[Snapshot]:
        """读取评分日之前的最近快照，并按日期升序返回。"""
        _validate_history_limit(limit)
        query = """
            SELECT repository_id, snapshot_date, stars, forks, open_issues,
                   watchers, commit_count_30d, release_count_30d,
                   contributor_count_approx
            FROM repository_snapshots
            WHERE repository_id = %s
        """
        parameters: list[Any] = [repository_id]
        if end_date is not None:
            # 使用 date 参数而不是字符串，让 psycopg 保持数据库类型语义。
            query += " AND snapshot_date <= %s"
            parameters.append(end_date)
        query += " ORDER BY snapshot_date DESC LIMIT %s"
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        # 查询使用倒序和 LIMIT，返回前恢复时间升序。
        return [_snapshot_from_values(row) for row in reversed(rows)]

    def get_repository(self, repository_id: int) -> RepositoryRecord | None:
        """按 GitHub repository ID 查询 PostgreSQL 元数据。"""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id AS repository_id, full_name, url, description,
                       primary_language, created_at, pushed_at, archived,
                       is_fork, discovered_at, discovery_source
                FROM repositories
                WHERE id = %s
                """,
                (repository_id,),
            ).fetchone()
        return _repository_from_row(row) if row is not None else None

    def list_trending(
        self,
        score_date: date,
        *,
        algorithm_version: str | None = None,
        limit: int = 20,
        include_warming_up: bool = False,
    ) -> list[TrendingItem]:
        """执行 PostgreSQL Trending 联结查询，并按分数降序返回。"""
        _validate_query_limit(limit)
        query = _trending_query("%s", include_warming_up=include_warming_up)
        parameters: list[Any] = [score_date]
        if algorithm_version is not None:
            # 算法版本作为可选过滤器，避免同一天多个版本混排。
            query += " AND t.algorithm_version = %s"
            parameters.append(algorithm_version)
        query += " ORDER BY t.total_score DESC NULLS LAST, r.full_name LIMIT %s"
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_trending_item_from_row(row) for row in rows]

    def get_trend_score(
        self,
        repository_id: int,
        score_date: date,
        *,
        algorithm_version: str | None = None,
    ) -> TrendScoreRecord | None:
        """查询指定仓库、日期和算法版本的单条分数。"""
        query = """
            SELECT repository_id, score_date, algorithm_version, total_score,
                   star_velocity_score, acceleration_score, activity_score,
                   freshness_score, quality_score, reasons_json, calculated_at
            FROM trend_scores
            WHERE repository_id = %s AND score_date = %s
        """
        parameters: list[Any] = [repository_id, score_date]
        if algorithm_version is not None:
            query += " AND algorithm_version = %s"
            parameters.append(algorithm_version)
        query += " ORDER BY calculated_at DESC LIMIT 1"
        with self.connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        return _trend_score_from_row(row) if row is not None else None

    def save_trend_score(self, score: "TrendScore") -> None:
        """把趋势分数和 JSONB reasons 幂等写入 PostgreSQL。"""
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO trend_scores (
                    repository_id, score_date, algorithm_version, total_score,
                    star_velocity_score, acceleration_score, activity_score,
                    freshness_score, quality_score, reasons_json, calculated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (repository_id, score_date, algorithm_version) DO UPDATE SET
                    total_score=EXCLUDED.total_score,
                    star_velocity_score=EXCLUDED.star_velocity_score,
                    acceleration_score=EXCLUDED.acceleration_score,
                    activity_score=EXCLUDED.activity_score,
                    freshness_score=EXCLUDED.freshness_score,
                    quality_score=EXCLUDED.quality_score,
                    reasons_json=EXCLUDED.reasons_json,
                    calculated_at=EXCLUDED.calculated_at
                """,
                _trend_score_values(score, calculated_at=score.calculated_at),
            )
