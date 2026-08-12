"""本地运行和定时任务使用的命令行入口。

``radar collect`` 负责采集 GitHub 快照，``radar score`` 负责读取历史快照
并持久化确定性趋势分数；两个命令共享数据库参数和后端选择逻辑。
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import date

from radar.collector import DEFAULT_QUERIES, collect
from radar.github import GitHubClient, GitHubError
from radar.store import PostgresSnapshotStore, SnapshotStore
from radar.trend import ALGORITHM_VERSION, score_store


def _add_storage_args(parser: argparse.ArgumentParser) -> None:
    """为子命令添加 SQLite 路径和 PostgreSQL URL 参数。"""
    parser.add_argument("--database", default="radar.db")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("RADAR_DATABASE_URL"),
        help="PostgreSQL URL; defaults to RADAR_DATABASE_URL when set",
    )


def _build_store(args: argparse.Namespace) -> SnapshotStore | PostgresSnapshotStore:
    """根据命令行参数优先选择 PostgreSQL，否则使用 SQLite。"""
    # RADAR_DATABASE_URL 可通过环境变量注入，便于 GitHub Actions 使用 secrets。
    return PostgresSnapshotStore(args.database_url) if args.database_url else SnapshotStore(args.database)


def build_parser() -> argparse.ArgumentParser:
    """构造 collect/score 两个子命令及其参数校验器。"""
    parser = argparse.ArgumentParser(prog="radar")
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect_parser = subparsers.add_parser("collect", help="collect GitHub repository snapshots")
    _add_storage_args(collect_parser)
    collect_parser.add_argument("--date", type=date.fromisoformat)
    collect_parser.add_argument("--per-query", type=int, default=25)
    collect_parser.add_argument("--query", action="append", dest="queries")
    score_parser = subparsers.add_parser("score", help="calculate deterministic cross-day trend scores")
    _add_storage_args(score_parser)
    score_parser.add_argument("--date", type=date.fromisoformat)
    score_parser.add_argument("--algorithm-version", default=ALGORITHM_VERSION)
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行命令行任务并输出机器可读的 JSON 摘要。"""
    args = build_parser().parse_args(argv)
    if args.command == "collect":
        # GitHub 搜索接口最多接受 100 条，提前在 CLI 层给出清晰错误。
        if not 1 <= args.per_query <= 100:
            raise SystemExit("--per-query must be between 1 and 100")
        try:
            store = _build_store(args)
            store.initialize()
            # token 从环境变量读取，避免把凭据写进命令行历史或代码仓库。
            client = GitHubClient(os.environ.get("GITHUB_TOKEN"))
            result = collect(
                client,
                store,
                queries=tuple(args.queries or DEFAULT_QUERIES),
                snapshot_day=args.date,
                per_query=args.per_query,
            )
        except (GitHubError, RuntimeError, ValueError) as error:
            raise SystemExit(str(error)) from error
        print(json.dumps(asdict(result), sort_keys=True))
        return 0
    if args.command == "score":
        # 未指定日期时评分当天已有的最新快照。
        score_date = args.date or date.today()
        try:
            store = _build_store(args)
            store.initialize()
            scores = score_store(
                store,
                score_date=score_date,
                algorithm_version=args.algorithm_version,
            )
        except (RuntimeError, ValueError) as error:
            raise SystemExit(str(error)) from error
        print(
            json.dumps(
                {
                    "algorithm_version": args.algorithm_version,
                    "repositories": len(scores),
                    "score_date": score_date.isoformat(),
                    "scored": sum(score.total_score is not None for score in scores),
                    "warming_up": sum(score.total_score is None for score in scores),
                },
                sort_keys=True,
            )
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
