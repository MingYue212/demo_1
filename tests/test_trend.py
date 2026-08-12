from datetime import date, datetime, timezone

from radar.store import Snapshot, SnapshotStore
from radar.trend import score_histories, score_store


def snapshots(repository_id, stars, *, optional=False):
    result = []
    for offset, star_count in enumerate(stars):
        result.append(
            Snapshot(
                repository_id=repository_id,
                snapshot_date=date(2026, 8, 1 + offset),
                stars=star_count,
                forks=1,
                open_issues=1,
                watchers=star_count,
                commit_count_30d=10 if optional else None,
                release_count_30d=1 if optional else None,
                contributor_count_approx=4 if optional else None,
            )
        )
    return result


def test_score_requires_a_seven_day_history():
    scores = score_histories(
        {1: snapshots(1, [1, 2, 3, 4, 5, 6])},
        score_date=date(2026, 8, 6),
        calculated_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
    )

    assert scores[0].total_score is None
    assert scores[0].reasons["status"] == "warming_up"
    assert scores[0].reasons["warming_reason"] == "fewer_than_7_days_of_history"


def test_accelerating_repository_ranks_above_linear_repository():
    scores = score_histories(
        {
            1: snapshots(1, [0, 1, 2, 3, 4, 5, 6, 7]),
            2: snapshots(2, [0, 0, 0, 0, 1, 3, 6, 10], optional=True),
        },
        score_date=date(2026, 8, 8),
        calculated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    by_repository = {score.repository_id: score for score in scores}
    assert by_repository[1].total_score is not None
    assert by_repository[2].total_score is not None
    assert by_repository[2].total_score > by_repository[1].total_score
    assert by_repository[2].reasons["status"] == "ready"
    assert "stars_delta_7d" in by_repository[2].reasons


def test_missing_optional_signals_are_reported_without_blocking_a_score():
    scores = score_histories(
        {1: snapshots(1, [0, 1, 2, 3, 4, 5, 6, 7])},
        score_date=date(2026, 8, 8),
        calculated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    score = scores[0]
    assert score.total_score is not None
    assert "activity" in score.reasons["missing_signals"]
    assert "contributors" in score.reasons["missing_signals"]


def test_score_store_reads_history_and_persists_idempotently(tmp_path):
    store = SnapshotStore(tmp_path / "radar.db")
    store.initialize()
    repository = {
        "id": 1,
        "full_name": "org/project",
        "html_url": "https://github.com/org/project",
        "created_at": "2026-01-01T00:00:00Z",
        "archived": False,
        "fork": False,
    }
    for offset, stars in enumerate(range(8)):
        store.save_repository(
            {**repository, "stargazers_count": stars},
            "fixture",
            date(2026, 8, 1 + offset),
        )

    first = score_store(store, score_date=date(2026, 8, 8))
    second = score_store(store, score_date=date(2026, 8, 8))

    assert first[0].total_score is not None
    assert second[0].total_score == first[0].total_score
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM trend_scores").fetchone()[0] == 1
