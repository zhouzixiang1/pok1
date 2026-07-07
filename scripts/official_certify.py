#!/usr/bin/env python3
"""Manage official Windows-platform certification for native national bots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = ROOT / "web" / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(1, str(ROOT))

from official_certification import (  # noqa: E402
    build_spec,
    process_certification_queue,
    queue_snapshot,
    run_certification,
    status_payload,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("smoke", "full"):
        p = sub.add_parser(name, help=f"Run or queue official {name} certification.")
        p.add_argument("candidate", help="Candidate bot directory or script.")
        p.add_argument("--opponent", default="bots/national_v76", help="Opponent bot directory or script.")
        p.add_argument("--force", action="store_true", help="Ignore a valid certification cache entry.")
        p.add_argument(
            "--wait-if-busy",
            action="store_true",
            help="Wait on the official EXE lock instead of leaving the request pending.",
        )
    p = sub.add_parser("status", help="Read certification status for a bot.")
    p.add_argument("candidate", help="Candidate bot directory or script.")
    p = sub.add_parser("queue-status", help="Show pending official certification queue entries.")
    p = sub.add_parser("process-queue", help="Process pending official certification queue entries.")
    p.add_argument("--limit", type=int, default=1, help="Maximum queued entries to process in this run.")
    p.add_argument("--force", action="store_true", help="Ignore valid cache entries when processing.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(status_payload(args.candidate), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "queue-status":
        print(json.dumps(queue_snapshot(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "process-queue":
        payload = process_certification_queue(limit=args.limit, force=bool(args.force))
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not payload.get("errors") else 1

    spec = build_spec(args.cmd, args.candidate, opponent=args.opponent)
    payload = run_certification(
        spec,
        force=bool(args.force),
        queue_on_busy=not bool(args.wait_if_busy),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("status") not in {"official-failed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
