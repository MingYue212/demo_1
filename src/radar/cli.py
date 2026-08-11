"""Command-line entry point for local and scheduled collection."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import date

from radar.collector import DEFAULT_QUERIES, collect
from radar.github import GitHubClient, GitHubError
from radar.store import SnapshotStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar")
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect_parser = subparsers.add_parser("collect", help="collect GitHub repository snapshots")
    collect_parser.add_argument("--database", default="radar.db")
    collect_parser.add_argument("--date", type=date.fromisoformat)
    collect_parser.add_argument("--per-query", type=int, default=25)
    collect_parser.add_argument("--query", action="append", dest="queries")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "collect":
        if not 1 <= args.per_query <= 100:
            raise SystemExit("--per-query must be between 1 and 100")
        store = SnapshotStore(args.database)
        store.initialize()
        client = GitHubClient(os.environ.get("GITHUB_TOKEN"))
        try:
            result = collect(
                client,
                store,
                queries=tuple(args.queries or DEFAULT_QUERIES),
                snapshot_day=args.date,
                per_query=args.per_query,
            )
        except (GitHubError, ValueError) as error:
            raise SystemExit(str(error)) from error
        print(json.dumps(asdict(result), sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
