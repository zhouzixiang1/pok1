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
from official_bootstrap import (  # noqa: E402
    authorize_operator_bootstrap_selection,
    select_signed_v5_ledger_bootstrap_root,
)
from official_certification_job import job_snapshot, reconcile_jobs, start_or_poll_job  # noqa: E402
from official_certificate_signing import signing_environment_report  # noqa: E402
from official_platform_harness import check_environment  # noqa: E402
from official_verdict_ledger import (  # noqa: E402
    initialize_verdict_ledger,
    ledger_head_path,
    ledger_integrity,
    ledger_path,
)


def _ledger_report() -> dict:
    path = ledger_path()
    try:
        integrity = ledger_integrity()
    except Exception as exc:
        integrity = {
            "valid": False,
            "issues": [
                f"official_verdict_ledger_validation_error:{type(exc).__name__}:{str(exc)[:240]}"
            ],
            "entry_count": 0,
            "head": None,
        }
    return {
        **integrity,
        "path": str(path),
        "head_path": str(ledger_head_path(path)),
        "init_command": "python3 scripts/official_certify.py init-ledger",
    }


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
    bootstrap = sub.add_parser(
        "bootstrap-full",
        help=(
            "Explicit one-time 5+3, 70-hand full certification using the "
            "pinned signed-ledger bootstrap root."
        ),
    )
    bootstrap.add_argument("candidate", help="New native national bot directory.")
    bootstrap.add_argument(
        "--root-id",
        required=True,
        help="Repository-pinned signed-ledger root id; no active-pool fallback is used.",
    )
    bootstrap.add_argument(
        "--acknowledge-one-time-ledger-bootstrap",
        action="store_true",
        help="Required acknowledgement that one successful run permanently consumes this root.",
    )
    bootstrap.add_argument("--force", action="store_true", help="Retry a terminal durable job.")
    bootstrap.add_argument(
        "--wait-if-busy",
        action="store_true",
        help="Poll the durable job until it reaches a terminal state.",
    )
    p = sub.add_parser("status", help="Read certification status for a bot.")
    p.add_argument("candidate", help="Candidate bot directory or script.")
    p = sub.add_parser("queue-status", help="Show pending official certification queue entries.")
    sub.add_parser(
        "doctor",
        help="Check official EXE, certificate signing, and verdict-ledger prerequisites.",
    )
    sub.add_parser(
        "init-ledger",
        help="Explicitly create the signed verdict-ledger genesis (idempotent when valid).",
    )
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
    if args.cmd == "init-ledger":
        signing = signing_environment_report()
        if not signing.get("ok"):
            payload = {
                "ok": False,
                "initialized": False,
                "signing": signing,
                "ledger": _ledger_report(),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1
        try:
            initialized = initialize_verdict_ledger()
        except Exception as exc:
            payload = {
                "ok": False,
                "initialized": False,
                "signing": signing,
                "ledger": _ledger_report(),
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1
        ledger = _ledger_report()
        payload = {
            "ok": bool(ledger.get("valid")),
            **initialized,
            "signing": signing,
            "ledger": ledger,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 1
    if args.cmd == "doctor":
        platform = check_environment(require_formal_sandbox=True)
        signing = signing_environment_report()
        ledger = _ledger_report()
        payload = {
            "ok": (
                bool(platform.get("ok"))
                and bool(signing.get("ok"))
                and bool(ledger.get("valid"))
            ),
            "platform": platform,
            "signing": signing,
            "ledger": ledger,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 1
    if args.cmd == "process-queue":
        payload = reconcile_jobs(limit=args.limit)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not payload.get("errors") else 1

    if args.cmd == "bootstrap-full" and not args.acknowledge_one_time_ledger_bootstrap:
        print(json.dumps({
            "status": "bootstrap-acknowledgement-required",
            "reason": "acknowledge_one_time_ledger_bootstrap_required",
            "candidate": args.candidate,
            "root_id": args.root_id,
        }, ensure_ascii=False, indent=2))
        return 2

    if args.cmd in {"full", "bootstrap-full"}:
        ledger = _ledger_report()
        if not ledger.get("valid"):
            print(json.dumps({
                "status": "formal-preflight-blocked",
                "reason": "official_verdict_ledger_unavailable",
                "candidate": args.candidate,
                "ledger": ledger,
            }, ensure_ascii=False, indent=2))
            return 2

    if args.cmd == "bootstrap-full":
        selection = select_signed_v5_ledger_bootstrap_root(
            args.root_id,
            candidate_path=args.candidate,
        )
    else:
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
    if args.cmd == "bootstrap-full":
        authorization = authorize_operator_bootstrap_selection(
            selection,
            args.root_id,
            args.candidate,
        )
        if authorization.get("valid") is not True:
            print(json.dumps({
                "status": "bootstrap-authorization-blocked",
                "candidate": args.candidate,
                "root_id": args.root_id,
                "authorization": authorization,
            }, ensure_ascii=False, indent=2))
            return 2
        selection = authorization["selection"]
    spec = build_spec(
        "full" if args.cmd == "bootstrap-full" else args.cmd,
        args.candidate,
        opponent=selection["opponent"]["path"],
        bootstrap_root_id=(args.root_id if args.cmd == "bootstrap-full" else None),
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
