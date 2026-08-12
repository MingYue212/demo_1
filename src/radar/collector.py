"""候选项目发现和每日快照采集编排。

本模块只负责协调 GitHub 客户端与存储层，不把数据库实现细节泄漏到
采集流程中，因此 SQLite 和 PostgreSQL 都可以复用同一套采集逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from radar.github import GitHubClient
from radar.store import SnapshotStoreProtocol


DEFAULT_QUERIES = (
    # 使用 topic 查询覆盖当前 V0.1 关注的四类 AI 工程方向。
    "topic:llm archived:false fork:false",
    "topic:rag archived:false fork:false",
    "topic:ai-agents archived:false fork:false",
    "topic:model-context-protocol archived:false fork:false",
)


@dataclass(frozen=True)
class CollectionResult:
    """一次采集任务的摘要结果，供 CLI 和定时任务输出。"""

    candidates: int
    unique_repositories: int
    saved: int
    rate_limit_remaining: int | None


def collect(
    client: GitHubClient,
    store: SnapshotStoreProtocol,
    *,
    queries: tuple[str, ...] = DEFAULT_QUERIES,
    snapshot_day: date | None = None,
    per_query: int = 25,
) -> CollectionResult:
    """发现候选项目，并为当天的每个唯一仓库幂等写入一条快照。

    Args:
        client: 提供 GitHub 搜索能力的客户端。
        store: 实现统一存储协议的 SQLite 或 PostgreSQL 存储。
        queries: 要执行的 GitHub 搜索表达式。
        snapshot_day: 快照日期；省略时使用本地当天日期。
        per_query: 每个搜索返回的最大仓库数量。

    Returns:
        包含候选数、去重后仓库数、写入数和剩余配额的摘要。
    """
    # 固定快照日期使补采和测试具有可重复性。
    day = snapshot_day or date.today()
    candidates = 0
    saved = 0
    # 以 GitHub repository ID 去重，避免同一仓库命中多个 topic 时重复写入。
    repositories: dict[int, tuple[dict, str]] = {}

    for query in queries:
        response = client.search_repositories(query, per_page=per_query)
        items = response.data.get("items", [])
        if not isinstance(items, list):
            raise ValueError("GitHub search response has invalid items")
        candidates += len(items)
        for repository in items:
            # 忽略无法识别的响应项，让单个脏数据不会中断整批采集。
            if isinstance(repository, dict) and isinstance(repository.get("id"), int):
                repositories.setdefault(repository["id"], (repository, f"github_search:{query}"))

    for repository, source in repositories.values():
        # 存储层使用 (repository_id, snapshot_date) 保证同日重复运行可更新而不复制。
        store.save_repository(repository, source, day)
        saved += 1

    return CollectionResult(candidates, len(repositories), saved, client.rate_limit_remaining)
