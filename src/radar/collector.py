"""Candidate discovery and snapshot orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from radar.github import GitHubClient
from radar.store import SnapshotStore


DEFAULT_QUERIES = (
    "topic:llm archived:false fork:false",
    "topic:rag archived:false fork:false",
    "topic:ai-agents archived:false fork:false",
    "topic:model-context-protocol archived:false fork:false",
)


@dataclass(frozen=True)
class CollectionResult:
    candidates: int
    unique_repositories: int
    saved: int
    rate_limit_remaining: int | None


def collect(
    client: GitHubClient,
    store: SnapshotStore,
    *,
    queries: tuple[str, ...] = DEFAULT_QUERIES,
    snapshot_day: date | None = None,
    per_query: int = 25,
) -> CollectionResult:
    """Discover candidates and upsert one snapshot per repository for the day."""
    day = snapshot_day or date.today()
    candidates = 0
    saved = 0
    repositories: dict[int, tuple[dict, str]] = {}

    for query in queries:
        response = client.search_repositories(query, per_page=per_query)
        items = response.data.get("items", [])
        if not isinstance(items, list):
            raise ValueError("GitHub search response has invalid items")
        candidates += len(items)
        for repository in items:
            if isinstance(repository, dict) and isinstance(repository.get("id"), int):
                repositories.setdefault(repository["id"], (repository, f"github_search:{query}"))

    for repository, source in repositories.values():
        store.save_repository(repository, source, day)
        saved += 1

    return CollectionResult(candidates, len(repositories), saved, client.rate_limit_remaining)
