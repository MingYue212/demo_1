"""SQLite persistence for the collection spike.

The schema mirrors the intended PostgreSQL model while remaining runnable in CI.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator


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


class SnapshotStore:
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
