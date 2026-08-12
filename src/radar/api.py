"""仓库快照和趋势分数的只读 HTTP API。

FastAPI 是可选依赖，因此采集和评分仍可在零第三方运行时依赖的基础安装
中执行；安装 ``api`` extra 后即可通过 Uvicorn 暴露本模块的 ``app``。
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from radar.store import (
    PostgresSnapshotStore,
    RepositoryRecord,
    Snapshot,
    SnapshotStore,
    SnapshotStoreProtocol,
    TrendScoreRecord,
    TrendingItem,
)
from radar.trend import ALGORITHM_VERSION

try:
    from fastapi import Depends, FastAPI, HTTPException, Query
    from pydantic import BaseModel
except ImportError as error:  # pragma: no cover - 基础安装会覆盖此分支
    # 基础安装仍允许导入模块，但只有安装 api extra 后才能创建应用。
    _FASTAPI_IMPORT_ERROR = error
    app = None

    def create_app(store: SnapshotStoreProtocol | None = None) -> Any:
        """在缺少可选依赖时给出可操作的安装提示。"""
        del store
        raise RuntimeError(
            "API support requires `python -m pip install 'ai-tech-radar[api]'`"
        ) from _FASTAPI_IMPORT_ERROR

else:

    class HealthResponse(BaseModel):
        """健康检查返回的数据库摘要。"""

        status: str
        backend: str
        repositories: int
        snapshots: int


    class RepositoryResponse(BaseModel):
        """仓库元数据的公开响应模型。"""

        repository_id: int
        full_name: str
        url: str
        description: str | None
        primary_language: str | None
        created_at: str
        pushed_at: str | None
        archived: bool
        is_fork: bool
        discovered_at: str
        discovery_source: str


    class SnapshotResponse(BaseModel):
        """单日仓库指标的公开响应模型。"""

        repository_id: int
        snapshot_date: date
        stars: int
        forks: int
        open_issues: int
        watchers: int
        commit_count_30d: int | None
        release_count_30d: int | None
        contributor_count_approx: int | None


    class TrendScoreResponse(BaseModel):
        """趋势分数及其可解释原因的公开响应模型。"""

        repository_id: int
        score_date: date
        algorithm_version: str
        total_score: float | None
        star_velocity_score: float | None
        acceleration_score: float | None
        activity_score: float | None
        freshness_score: float | None
        quality_score: float | None
        reasons: dict[str, Any]
        calculated_at: str


    class TrendingItemResponse(BaseModel):
        """一个仓库和对应分数的联结响应。"""

        repository: RepositoryResponse
        score: TrendScoreResponse


    class TrendingResponse(BaseModel):
        """Trending 列表及其查询上下文。"""

        score_date: date
        algorithm_version: str
        items: list[TrendingItemResponse]


    class RepositoryHistoryResponse(BaseModel):
        """单仓库历史快照响应。"""

        repository_id: int
        snapshots: list[SnapshotResponse]


    class RepositoryDetailResponse(BaseModel):
        """仓库详情、当前分数和历史快照的组合响应。"""

        repository: RepositoryResponse
        score: TrendScoreResponse | None
        history: list[SnapshotResponse]


    def _build_store() -> SnapshotStore | PostgresSnapshotStore:
        """根据环境变量创建 API 使用的数据库存储。"""
        database_url = os.environ.get("RADAR_DATABASE_URL")
        if database_url:
            return PostgresSnapshotStore(database_url)
        # 本地开发默认使用 radar.db，也允许用 RADAR_DATABASE 指定路径。
        return SnapshotStore(os.environ.get("RADAR_DATABASE", "radar.db"))


    def _repository_response(repository: RepositoryRecord) -> RepositoryResponse:
        """将存储层仓库记录映射为 Pydantic 响应。"""
        return RepositoryResponse(
            repository_id=repository.repository_id,
            full_name=repository.full_name,
            url=repository.url,
            description=repository.description,
            primary_language=repository.primary_language,
            created_at=repository.created_at,
            pushed_at=repository.pushed_at,
            archived=repository.archived,
            is_fork=repository.is_fork,
            discovered_at=repository.discovered_at,
            discovery_source=repository.discovery_source,
        )


    def _snapshot_response(snapshot: Snapshot) -> SnapshotResponse:
        """将领域快照映射为 API 快照响应。"""
        return SnapshotResponse(
            repository_id=snapshot.repository_id,
            snapshot_date=snapshot.snapshot_date,
            stars=snapshot.stars,
            forks=snapshot.forks,
            open_issues=snapshot.open_issues,
            watchers=snapshot.watchers,
            commit_count_30d=snapshot.commit_count_30d,
            release_count_30d=snapshot.release_count_30d,
            contributor_count_approx=snapshot.contributor_count_approx,
        )


    def _score_response(score: TrendScoreRecord) -> TrendScoreResponse:
        """将存储层分数映射为 API 分数响应。"""
        return TrendScoreResponse(
            repository_id=score.repository_id,
            score_date=score.score_date,
            algorithm_version=score.algorithm_version,
            total_score=score.total_score,
            star_velocity_score=score.star_velocity_score,
            acceleration_score=score.acceleration_score,
            activity_score=score.activity_score,
            freshness_score=score.freshness_score,
            quality_score=score.quality_score,
            reasons=score.reasons,
            calculated_at=score.calculated_at,
        )


    def _trending_item_response(item: TrendingItem) -> TrendingItemResponse:
        """将 Trending 联结记录映射为公开响应。"""
        return TrendingItemResponse(
            repository=_repository_response(item.repository),
            score=_score_response(item.score),
        )


    def _not_found(repository_id: int) -> HTTPException:
        """构造统一的仓库不存在错误。"""
        return HTTPException(status_code=404, detail=f"repository {repository_id} was not found")


    def create_app(store: SnapshotStoreProtocol | None = None) -> FastAPI:
        """创建 API 应用；测试和嵌入式运行可注入已有存储。"""

        resolved_store = store
        initialized = False

        def get_store() -> SnapshotStoreProtocol:
            """懒加载并初始化存储，避免模块导入时创建数据库文件。"""
            nonlocal resolved_store, initialized
            if resolved_store is None:
                resolved_store = _build_store()
            if not initialized:
                # 初始化只执行一次，避免每个请求重复跑 migration。
                resolved_store.initialize()
                initialized = True
            return resolved_store

        api = FastAPI(
            title="AI Tech Radar API",
            version="0.1.0",
            description="AI Tech Radar 的仓库快照和确定性趋势分数只读接口。",
        )

        @api.get("/health", response_model=HealthResponse, tags=["system"])
        def health(active_store: SnapshotStoreProtocol = Depends(get_store)) -> HealthResponse:
            """检查数据库连通性，并返回当前数据规模。"""
            repositories, snapshots = active_store.counts()
            return HealthResponse(
                status="ok",
                backend=type(active_store).__name__,
                repositories=repositories,
                snapshots=snapshots,
            )

        @api.get("/trending", response_model=TrendingResponse, tags=["trending"])
        def trending(
            score_date: date | None = Query(default=None),
            algorithm_version: str = Query(default=ALGORITHM_VERSION, min_length=1),
            limit: int = Query(default=20, ge=1, le=100),
            include_warming_up: bool = Query(default=False),
            active_store: SnapshotStoreProtocol = Depends(get_store),
        ) -> TrendingResponse:
            """返回指定评分日的 Trending 排名。"""
            selected_date = score_date or date.today()
            items = active_store.list_trending(
                selected_date,
                algorithm_version=algorithm_version,
                limit=limit,
                include_warming_up=include_warming_up,
            )
            return TrendingResponse(
                score_date=selected_date,
                algorithm_version=algorithm_version,
                items=[_trending_item_response(item) for item in items],
            )

        @api.get(
            "/repositories/{repository_id}/history",
            response_model=RepositoryHistoryResponse,
            tags=["repositories"],
        )
        def repository_history(
            repository_id: int,
            score_date: date | None = Query(default=None),
            limit: int = Query(default=30, ge=1, le=366),
            active_store: SnapshotStoreProtocol = Depends(get_store),
        ) -> RepositoryHistoryResponse:
            """返回指定仓库截至某日的快照历史。"""
            if active_store.get_repository(repository_id) is None:
                raise _not_found(repository_id)
            snapshots = active_store.snapshot_history(
                repository_id,
                end_date=score_date,
                limit=limit,
            )
            return RepositoryHistoryResponse(
                repository_id=repository_id,
                snapshots=[_snapshot_response(snapshot) for snapshot in snapshots],
            )

        @api.get(
            "/repositories/{repository_id}/score",
            response_model=TrendScoreResponse,
            tags=["repositories"],
        )
        def repository_score(
            repository_id: int,
            score_date: date | None = Query(default=None),
            algorithm_version: str = Query(default=ALGORITHM_VERSION, min_length=1),
            active_store: SnapshotStoreProtocol = Depends(get_store),
        ) -> TrendScoreResponse:
            """返回指定仓库在某个评分日的趋势分数。"""
            if active_store.get_repository(repository_id) is None:
                raise _not_found(repository_id)
            score = active_store.get_trend_score(
                repository_id,
                score_date or date.today(),
                algorithm_version=algorithm_version,
            )
            if score is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"no score found for repository {repository_id}",
                )
            return _score_response(score)

        @api.get(
            "/repositories/{repository_id}",
            response_model=RepositoryDetailResponse,
            tags=["repositories"],
        )
        def repository_detail(
            repository_id: int,
            score_date: date | None = Query(default=None),
            algorithm_version: str = Query(default=ALGORITHM_VERSION, min_length=1),
            history_limit: int = Query(default=30, ge=1, le=366),
            active_store: SnapshotStoreProtocol = Depends(get_store),
        ) -> RepositoryDetailResponse:
            """返回仓库元数据、当前版本分数和历史快照。"""
            repository = active_store.get_repository(repository_id)
            if repository is None:
                raise _not_found(repository_id)
            selected_date = score_date or date.today()
            # 详情页允许没有分数的仓库存在，例如刚被发现或仍在 warming_up。
            score = active_store.get_trend_score(
                repository_id,
                selected_date,
                algorithm_version=algorithm_version,
            )
            history = active_store.snapshot_history(
                repository_id,
                end_date=selected_date,
                limit=history_limit,
            )
            return RepositoryDetailResponse(
                repository=_repository_response(repository),
                score=_score_response(score) if score is not None else None,
                history=[_snapshot_response(snapshot) for snapshot in history],
            )

        return api


    app = create_app()
