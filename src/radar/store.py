"""SQLite and PostgreSQL persistence for repository snapshots.

SQLite remains useful for local experiments; PostgreSQL is the persistent V0.1
backend. Both stores expose the same small interface to the collector.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol


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
    collected_at TEXT NOT NULL,
    raw_payload_hash TEXT NOT NULL,
    PRIMARY KEY (repository_id, snapshot_date)
);
"""

POSTGRES_SCHEMA = files("radar.migrations").joinpath("001_initial.sql").read_text(encoding="utf-8")


class SnapshotStoreProtocol(Protocol):
    def save_repository(self, repository: dict[str, Any], source: str, snapshot_day: date) -> None:
        """Persist repository metadata and its snapshot for a day."""


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
                    watchers, collected_at, raw_payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repository_id, snapshot_date) DO UPDATE SET
                    stars=excluded.stars, forks=excluded.forks,
                    open_issues=excluded.open_issues, watchers=excluded.watchers,
                    collected_at=excluded.collected_at,
                    raw_payload_hash=excluded.raw_payload_hash
                """,
                (
                    repository["id"], snapshot_day.isoformat(),
                    int(repository.get("stargazers_count", 0)),
                    int(repository.get("forks_count", 0)),
                    int(repository.get("open_issues_count", 0)),
                    int(repository.get("subscribers_count", repository.get("watchers_count", 0))),
                    collected_at, payload_hash,
                ),
            )

    def counts(self) -> tuple[int, int]:
        with self.connect() as connection:
            repositories = connection.execute("SELECT count(*) FROM repositories").fetchone()[0]
            snapshots = connection.execute("SELECT count(*) FROM repository_snapshots").fetchone()[0]
        return repositories, snapshots


def _payload_hash(payload: dict[str, Any]) -> str:
    import hashlib

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


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
            connection.execute(POSTGRES_SCHEMA)

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
                    watchers, collected_at, raw_payload_hash
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (repository_id, snapshot_date) DO UPDATE SET
                    stars=EXCLUDED.stars, forks=EXCLUDED.forks,
                    open_issues=EXCLUDED.open_issues, watchers=EXCLUDED.watchers,
                    collected_at=EXCLUDED.collected_at,
                    raw_payload_hash=EXCLUDED.raw_payload_hash
                """,
                (
                    repository["id"], snapshot_day,
                    int(repository.get("stargazers_count", 0)),
                    int(repository.get("forks_count", 0)),
                    int(repository.get("open_issues_count", 0)),
                    int(repository.get("subscribers_count", repository.get("watchers_count", 0))),
                    collected_at, payload_hash,
                ),
            )

    def counts(self) -> tuple[int, int]:
        with self.connect() as connection:
            repositories = connection.execute("SELECT count(*) FROM repositories").fetchone()[0]
            snapshots = connection.execute("SELECT count(*) FROM repository_snapshots").fetchone()[0]
        return repositories, snapshots
