"""PostgreSQL schema、类型转换和趋势分数写入的契约测试。"""

from datetime import date, datetime, timezone

from radar.store import POSTGRES_MIGRATIONS, POSTGRES_SCHEMA, PostgresSnapshotStore
from radar.trend import TrendScore


def repository():
    """构造供 PostgreSQL fake connection 使用的仓库 payload。"""
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
    """提供最小 fetchone 接口的数据库结果替身。"""

    def __init__(self, row):
        """保存预设查询结果。"""
        self.row = row

    def fetchone(self):
        """返回预设单行结果。"""
        return self.row


class FakeConnection:
    """记录 SQL 调用而不连接真实 PostgreSQL 的连接替身。"""

    def __init__(self):
        """初始化调用、事务和连接状态记录。"""
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, statement, params=()):
        """记录 SQL，并为健康检查返回固定计数。"""
        self.calls.append((statement, params))
        if statement.startswith("SELECT count(*) FROM repositories"):
            return FakeResult((1,))
        if statement.startswith("SELECT count(*) FROM repository_snapshots"):
            return FakeResult((1,))
        return FakeResult(None)

    def commit(self):
        """记录提交动作。"""
        self.commits += 1

    def rollback(self):
        """记录回滚动作。"""
        self.rollbacks += 1

    def close(self):
        """记录连接关闭动作。"""
        self.closed = True


def test_postgres_schema_is_packaged():
    """migration SQL 应随 Python 包一起提供。"""
    assert "CREATE TABLE IF NOT EXISTS repositories" in POSTGRES_SCHEMA
    assert "PRIMARY KEY (repository_id, snapshot_date)" in POSTGRES_SCHEMA
    assert any("CREATE TABLE IF NOT EXISTS trend_scores" in migration for migration in POSTGRES_MIGRATIONS)


def test_postgres_store_initializes_and_upserts_with_typed_values():
    """PostgreSQL 写入应使用日期和带时区 datetime 等正确类型。"""
    connection = FakeConnection()
    store = PostgresSnapshotStore("postgresql://example/radar", connection_factory=lambda _: connection)

    # initialize 和 save_repository 各自打开一次短连接并提交。
    store.initialize()
    store.save_repository(repository(), "github_search:topic:llm", date(2026, 8, 11))

    assert connection.commits == 2
    assert connection.closed is True
    repository_insert = next(call for call in connection.calls if "INSERT INTO repositories" in call[0])
    snapshot_insert = next(call for call in connection.calls if "INSERT INTO repository_snapshots" in call[0])
    assert "ON CONFLICT (id) DO UPDATE SET" in repository_insert[0]
    assert repository_insert[1][5] == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert snapshot_insert[1][1] == date(2026, 8, 11)
    assert "ON CONFLICT (repository_id, snapshot_date) DO UPDATE SET" in snapshot_insert[0]


def test_postgres_store_persists_versioned_score():
    """趋势分数写入应包含算法版本和 JSON reasons。"""
    connection = FakeConnection()
    store = PostgresSnapshotStore("postgresql://example/radar", connection_factory=lambda _: connection)
    score = TrendScore(
        repository_id=1,
        score_date=date(2026, 8, 11),
        algorithm_version="trend-v0.1",
        total_score=75.5,
        star_velocity_score=100.0,
        acceleration_score=50.0,
        activity_score=None,
        freshness_score=100.0,
        quality_score=50.0,
        reasons={"status": "ready"},
        calculated_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )

    store.save_trend_score(score)

    trend_insert = next(call for call in connection.calls if "INSERT INTO trend_scores" in call[0])
    assert "ON CONFLICT (repository_id, score_date, algorithm_version) DO UPDATE SET" in trend_insert[0]
    assert trend_insert[1][2] == "trend-v0.1"
    assert '"status":"ready"' in trend_insert[1][9]


def test_postgres_store_counts_are_read_from_database():
    """健康检查计数应直接来自数据库查询。"""
    connection = FakeConnection()
    store = PostgresSnapshotStore("postgresql://example/radar", connection_factory=lambda _: connection)

    assert store.counts() == (1, 1)
