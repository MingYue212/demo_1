"""Command-line entry point for local and scheduled collection."""

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
    parser.add_argument("--database", default="radar.db")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("RADAR_DATABASE_URL"),
        help="PostgreSQL URL; defaults to RADAR_DATABASE_URL when set",
    )


def _build_store(args: argparse.Namespace) -> SnapshotStore | PostgresSnapshotStore:
    return PostgresSnapshotStore(args.database_url) if args.database_url else SnapshotStore(args.database)


def build_parser() -> argparse.ArgumentParser:
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
    args = build_parser().parse_args(argv)
    if args.command == "collect":
        if not 1 <= args.per_query <= 100:
            raise SystemExit("--per-query must be between 1 and 100")
        try:
            store = _build_store(args)
            store.initialize()
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
