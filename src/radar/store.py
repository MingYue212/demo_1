"""SQLite and PostgreSQL persistence for repository snapshots.

SQLite remains useful for local experiments; PostgreSQL is the persistent V0.1
backend. Both stores expose the same small interface to the collector.
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
"""

POSTGRES_SCHEMA = files("radar.migrations").joinpath("001_initial.sql").read_text(encoding="utf-8")
POSTGRES_MIGRATIONS = (
    POSTGRES_SCHEMA,
    files("radar.migrations").joinpath("002_trend_scores.sql").read_text(encoding="utf-8"),
)


@dataclass(frozen=True)
class Snapshot:
    repository_id: int
    snapshot_date: date
    stars: int
    forks: int
    open_issues: int
    watchers: int
    commit_count_30d: int | None = None
    release_count_30d: int | None = None
    contributor_count_approx: int | None = None


class SnapshotStoreProtocol(Protocol):
    def save_repository(self, repository: dict[str, Any], source: str, snapshot_day: date) -> None:
        """Persist repository metadata and its snapshot for a day."""

    def repository_ids(self) -> list[int]:
        """Return repositories that can be scored."""

    def snapshot_history(
        self,
        repository_id: int,
        *,
        end_date: date | None = None,
        limit: int = 8,
    ) -> list[Snapshot]:
        """Return recent snapshots in ascending date order."""

    def save_trend_score(self, score: "TrendScore") -> None:
        """Persist one versioned score for a repository and day."""


class SnapshotStore:
    """SQLite snapshot store used by local runs and CI without a database."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            _ensure_sqlite_snapshot_columns(connection)

    def save_repository(self, repository: dict[str, Any], source: str, snapshot_day: date) -> None:
        required = {"id", "full_name", "html_url", "created_at"}
        missing = required.difference(repository)
        if missing:
            raise ValueError(f"repository is missing required fields: {', '.join(sorted(missing))}")

        collected_at = datetime.now(timezone.utc).isoformat()
        payload_hash = _payload_hash(repository)
        with self.connect() as connection:
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
        with self.connect() as connection:
            repositories = connection.execute("SELECT count(*) FROM repositories").fetchone()[0]
            snapshots = connection.execute("SELECT count(*) FROM repository_snapshots").fetchone()[0]
        return repositories, snapshots

    def repository_ids(self) -> list[int]:
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
            query += " AND snapshot_date <= ?"
            parameters.append(end_date.isoformat())
        query += " ORDER BY snapshot_date DESC LIMIT ?"
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_snapshot_from_row(row) for row in reversed(rows)]

    def save_trend_score(self, score: "TrendScore") -> None:
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
    import hashlib

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _ensure_sqlite_snapshot_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(repository_snapshots)").fetchall()
    }
    for column in ("commit_count_30d", "release_count_30d", "contributor_count_approx"):
        if column not in columns:
            connection.execute(f"ALTER TABLE repository_snapshots ADD COLUMN {column} INTEGER")


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _validate_history_limit(limit: int) -> None:
    if not 1 <= limit <= 366:
        raise ValueError("history limit must be between 1 and 366")


def _snapshot_from_row(row: Any) -> Snapshot:
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
    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError(
            "PostgreSQL support requires `python -m pip install 'ai-tech-radar[postgres]'`"
        ) from error
    return psycopg.connect(dsn)


def _github_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError("GitHub timestamp must be an ISO-8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class PostgresSnapshotStore:
    """PostgreSQL snapshot store with the same contract as :class:`SnapshotStore`."""

    def __init__(
        self,
        dsn: str,
        *,
        connection_factory: PostgresConnectionFactory | None = None,
    ) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL database URL cannot be empty")
        self.dsn = dsn
        self.connection_factory = connection_factory or _default_postgres_connection

    @contextmanager
    def connect(self) -> Iterator[Any]:
        connection = self.connection_factory(self.dsn)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            for migration in POSTGRES_MIGRATIONS:
                connection.execute(migration)

    def save_repository(self, repository: dict[str, Any], source: str, snapshot_day: date) -> None:
        required = {"id", "full_name", "html_url", "created_at"}
        missing = required.difference(repository)
        if missing:
            raise ValueError(f"repository is missing required fields: {', '.join(sorted(missing))}")

        collected_at = datetime.now(timezone.utc)
        payload_hash = _payload_hash(repository)
        with self.connect() as connection:
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
        with self.connect() as connection:
            repositories = connection.execute("SELECT count(*) FROM repositories").fetchone()[0]
            snapshots = connection.execute("SELECT count(*) FROM repository_snapshots").fetchone()[0]
        return repositories, snapshots

    def repository_ids(self) -> list[int]:
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
            query += " AND snapshot_date <= %s"
            parameters.append(end_date)
        query += " ORDER BY snapshot_date DESC LIMIT %s"
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_snapshot_from_values(row) for row in reversed(rows)]

    def save_trend_score(self, score: "TrendScore") -> None:
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
