"""只读 FastAPI 路由的 SQLite fixture 集成测试。"""

from datetime import date

from fastapi.testclient import TestClient

from radar.api import create_app
from radar.store import SnapshotStore
from radar.trend import score_store


SCORE_DATE = date(2026, 8, 8)


def repository(repository_id: int, name: str) -> dict:
    """构造 API fixture 使用的仓库元数据。"""
    return {
        "id": repository_id,
        "full_name": f"org/{name}",
        "html_url": f"https://github.com/org/{name}",
        "description": f"{name} project",
        "language": "Python",
        "created_at": "2026-01-01T00:00:00Z",
        "pushed_at": "2026-08-08T00:00:00Z",
        "archived": False,
        "fork": False,
    }


def seed_store(tmp_path):
    """写入线性、加速和 warming_up 三类项目并预先计算分数。"""
    store = SnapshotStore(tmp_path / "radar.db")
    store.initialize()
    histories = {
        1: ("linear", [0, 1, 2, 3, 4, 5, 6, 7]),
        2: ("accelerating", [0, 0, 0, 0, 1, 3, 6, 10]),
        3: ("warming", [0, 1, 2, 3, 4, 5]),
    }
    for repository_id, (name, stars) in histories.items():
        for offset, star_count in enumerate(stars):
            # 三类历史长度不同，用于同时验证正式分数和预热状态。
            store.save_repository(
                {
                    **repository(repository_id, name),
                    "stargazers_count": star_count,
                    "forks_count": 2,
                    "open_issues_count": 1,
                    "watchers_count": star_count,
                },
                "fixture",
                date(2026, 8, 1 + offset),
            )
    # API 只读数据库，因此 fixture 需要先执行一次评分写入。
    score_store(store, score_date=SCORE_DATE)
    return store


def test_read_only_api_exposes_health_trending_and_repository_views(tmp_path):
    """健康、排名、详情、历史和单项目分数接口应返回稳定结构。"""
    store = seed_store(tmp_path)
    client = TestClient(create_app(store))

    # 健康检查同时验证存储后端名称和 fixture 数量。
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "backend": "SnapshotStore",
        "repositories": 3,
        "snapshots": 22,
    }

    trending = client.get(
        "/trending",
        params={"score_date": SCORE_DATE.isoformat(), "limit": 10},
    )
    assert trending.status_code == 200
    trending_body = trending.json()
    assert trending_body["algorithm_version"] == "trend-v0.1"
    assert [item["repository"]["repository_id"] for item in trending_body["items"]] == [2, 1]
    assert all(item["score"]["total_score"] is not None for item in trending_body["items"])

    detail = client.get(
        "/repositories/2",
        params={"score_date": SCORE_DATE.isoformat(), "history_limit": 3},
    )
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["repository"]["full_name"] == "org/accelerating"
    assert detail_body["score"]["total_score"] is not None
    assert [snapshot["snapshot_date"] for snapshot in detail_body["history"]] == [
        "2026-08-06",
        "2026-08-07",
        "2026-08-08",
    ]

    history = client.get(
        "/repositories/2/history",
        params={"score_date": "2026-08-05", "limit": 2},
    )
    assert history.status_code == 200
    assert [snapshot["snapshot_date"] for snapshot in history.json()["snapshots"]] == [
        "2026-08-04",
        "2026-08-05",
    ]

    score = client.get(
        "/repositories/2/score",
        params={"score_date": SCORE_DATE.isoformat()},
    )
    assert score.status_code == 200
    assert score.json()["repository_id"] == 2


def test_trending_can_include_warming_up_and_returns_not_found(tmp_path):
    """Trending 可选返回 warming_up 项目，未知仓库返回 404。"""
    store = seed_store(tmp_path)
    client = TestClient(create_app(store))

    trending = client.get(
        "/trending",
        params={
            "score_date": SCORE_DATE.isoformat(),
            "include_warming_up": "true",
            "limit": 10,
        },
    )
    assert trending.status_code == 200
    # include_warming_up=true 时，历史不足的项目也应出现在列表中。
    items = trending.json()["items"]
    warming = next(item for item in items if item["repository"]["repository_id"] == 3)
    assert warming["score"]["total_score"] is None
    assert warming["score"]["reasons"]["status"] == "warming_up"

    missing = client.get("/repositories/999")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "repository 999 was not found"
