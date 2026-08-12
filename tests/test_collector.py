"""采集编排、候选去重和每日快照幂等性的测试。"""

from datetime import date

from radar.collector import collect
from radar.github import Response
from radar.store import SnapshotStore


def repository(repo_id=1, name="org/project", stars=10):
    """构造最小但接近 GitHub 响应格式的仓库 fixture。"""
    return {
        "id": repo_id,
        "full_name": name,
        "html_url": f"https://github.com/{name}",
        "description": "An AI project",
        "language": "Python",
        "created_at": "2026-01-01T00:00:00Z",
        "pushed_at": "2026-08-10T00:00:00Z",
        "archived": False,
        "fork": False,
        "stargazers_count": stars,
        "forks_count": 2,
        "open_issues_count": 3,
        "watchers_count": stars,
    }


class FakeClient:
    """返回固定候选项的 GitHub 客户端替身。"""

    rate_limit_remaining = 100

    def search_repositories(self, query, *, per_page):
        """无论查询内容如何，都返回同一个仓库以验证去重。"""
        return Response({"items": [repository()]}, {})


def test_collection_deduplicates_queries_and_is_idempotent(tmp_path):
    """多个查询命中同一仓库时只写一条同日快照。"""
    store = SnapshotStore(tmp_path / "radar.db")
    store.initialize()

    # 两次查询制造候选重复；第二次 collect 验证同日 upsert。
    first = collect(FakeClient(), store, queries=("one", "two"), snapshot_day=date(2026, 8, 11))
    second = collect(FakeClient(), store, queries=("one", "two"), snapshot_day=date(2026, 8, 11))

    assert first.candidates == 2
    assert first.unique_repositories == 1
    assert first.saved == 1
    assert second.saved == 1
    assert store.counts() == (1, 1)


def test_second_day_creates_new_snapshot(tmp_path):
    """同一仓库在不同日期应产生两条历史快照。"""
    store = SnapshotStore(tmp_path / "radar.db")
    store.initialize()
    collect(FakeClient(), store, queries=("one",), snapshot_day=date(2026, 8, 10))
    collect(FakeClient(), store, queries=("one",), snapshot_day=date(2026, 8, 11))

    assert store.counts() == (1, 2)


def test_snapshot_history_is_returned_in_ascending_order(tmp_path):
    """存储层应将内部倒序查询结果恢复为时间升序。"""
    store = SnapshotStore(tmp_path / "radar.db")
    store.initialize()
    collect(FakeClient(), store, queries=("one",), snapshot_day=date(2026, 8, 10))
    collect(FakeClient(), store, queries=("one",), snapshot_day=date(2026, 8, 11))

    # limit 较大但实际只返回两天，验证日期顺序而非数量截断。
    history = store.snapshot_history(1, end_date=date(2026, 8, 11), limit=8)

    assert [snapshot.snapshot_date for snapshot in history] == [
        date(2026, 8, 10),
        date(2026, 8, 11),
    ]
