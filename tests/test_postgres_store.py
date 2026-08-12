from datetime import date, datetime, timezone

from radar.store import POSTGRES_SCHEMA, PostgresSnapshotStore


def repository():
    return {
        "id": 1,
        "full_name": "org/project",
        "html_url": "https://github.com/org/project",
        "description": "An AI project",
        "language": "Python",
        "created_at": "2026-01-01T00:00:00Z",
        "pushed_at": "2026-08-10T00:00:00Z",
        "archived": False,
        "fork": False,
        "stargazers_count": 10,
        "forks_count": 2,
        "open_issues_count": 3,
        "watchers_count": 10,
    }


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, statement, params=()):
        self.calls.append((statement, params))
        if statement.startswith("SELECT count(*) FROM repositories"):
            return FakeResult((1,))
        if statement.startswith("SELECT count(*) FROM repository_snapshots"):
            return FakeResult((1,))
        return FakeResult(None)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def test_postgres_schema_is_packaged():
    assert "CREATE TABLE IF NOT EXISTS repositories" in POSTGRES_SCHEMA
    assert "PRIMARY KEY (repository_id, snapshot_date)" in POSTGRES_SCHEMA


def test_postgres_store_initializes_and_upserts_with_typed_values():
    connection = FakeConnection()
    store = PostgresSnapshotStore("postgresql://example/radar", connection_factory=lambda _: connection)

    store.initialize()
    store.save_repository(repository(), "github_search:topic:llm", date(2026, 8, 11))

    assert connection.commits == 2
    assert connection.closed is True
    assert "ON CONFLICT (id) DO UPDATE SET" in connection.calls[1][0]
    assert connection.calls[1][1][5] == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert connection.calls[2][1][1] == date(2026, 8, 11)
    assert "ON CONFLICT (repository_id, snapshot_date) DO UPDATE SET" in connection.calls[2][0]


def test_postgres_store_counts_are_read_from_database():
    connection = FakeConnection()
    store = PostgresSnapshotStore("postgresql://example/radar", connection_factory=lambda _: connection)

    assert store.counts() == (1, 1)
