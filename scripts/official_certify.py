#!/usr/bin/env python3
"""Manage official Windows-platform certification for native national bots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = ROOT / "web" / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(1, str(ROOT))

from official_certification import (  # noqa: E402
    build_spec,
    select_official_opponent,
    status_payload,
)
from official_certification_job import job_snapshot, reconcile_jobs, start_or_poll_job  # noqa: E402
from official_certificate_signing import signing_environment_report  # noqa: E402
from official_platform_harness import check_environment  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    mode_help = {
        "smoke": "Run or queue a short official quality-gate smoke.",
        "compliance": "Run or queue a short official protocol-compliance check.",
        "full": "Run or queue the manual 5+3, 70-hand official certification suite.",
    }
    for name in ("smoke", "compliance", "full"):
        p = sub.add_parser(name, help=mode_help[name])
        p.add_argument("candidate", help="Candidate bot directory or script.")
        p.add_argument(
            "--opponent",
            default=None,
            help="Preferred eligible opponent. Invalid preferences never bypass policy.",
        )
        p.add_argument("--force", action="store_true", help="Ignore a valid certification cache entry.")
        p.add_argument(
            "--wait-if-busy",
            action="store_true",
            help="Poll the durable job until it reaches a terminal state.",
        )
    p = sub.add_parser("status", help="Read certification status for a bot.")
    p.add_argument("candidate", help="Candidate bot directory or script.")
    p = sub.add_parser("queue-status", help="Show pending official certification queue entries.")
    sub.add_parser("doctor", help="Check official EXE and certificate-signing prerequisites.")
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
        print(json.dumps(job_snapshot(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "doctor":
        platform = check_environment()
        signing = signing_environment_report()
        payload = {
            "ok": bool(platform.get("ok")) and bool(signing.get("ok")),
            "platform": platform,
            "signing": signing,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 1
    if args.cmd == "process-queue":
        payload = reconcile_jobs(limit=args.limit)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not payload.get("errors") else 1

    selection = select_official_opponent(
        args.candidate,
        preferred=args.opponent,
        allow_bootstrap_grandfather=False,
    )
    if not selection.get("selected"):
        print(json.dumps({
            "status": "opponent-selection-blocked",
            "candidate": args.candidate,
            "opponent_selection": selection,
        }, ensure_ascii=False, indent=2))
        return 2
    spec = build_spec(
        args.cmd,
        args.candidate,
        opponent=selection["opponent"]["path"],
    )
    payload = start_or_poll_job(
        spec,
        opponent_selection=selection,
        retry_terminal=bool(args.force),
    )
    if args.wait_if_busy:
        while payload.get("pending"):
            time.sleep(5)
            payload = start_or_poll_job(
                spec,
                opponent_selection=selection,
                retry_terminal=False,
            )
    payload["opponent_selection"] = selection
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload.get("pending"):
        return 0
    if payload.get("state") != "completed":
        return 2
    status = payload.get("status") or {}
    return 0 if status.get("status") in {
        "official-smoke-pass",
        "official-compliance-pass",
        "official-certified",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
