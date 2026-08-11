from datetime import date

from radar.collector import collect
from radar.github import Response
from radar.store import SnapshotStore


def repository(repo_id=1, name="org/project", stars=10):
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
    rate_limit_remaining = 100

    def search_repositories(self, query, *, per_page):
        return Response({"items": [repository()]}, {})


def test_collection_deduplicates_queries_and_is_idempotent(tmp_path):
    store = SnapshotStore(tmp_path / "radar.db")
    store.initialize()

    first = collect(FakeClient(), store, queries=("one", "two"), snapshot_day=date(2026, 8, 11))
    second = collect(FakeClient(), store, queries=("one", "two"), snapshot_day=date(2026, 8, 11))

    assert first.candidates == 2
    assert first.unique_repositories == 1
    assert first.saved == 1
    assert second.saved == 1
    assert store.counts() == (1, 1)


def test_second_day_creates_new_snapshot(tmp_path):
    store = SnapshotStore(tmp_path / "radar.db")
    store.initialize()
    collect(FakeClient(), store, queries=("one",), snapshot_day=date(2026, 8, 10))
    collect(FakeClient(), store, queries=("one",), snapshot_day=date(2026, 8, 11))

    assert store.counts() == (1, 2)
