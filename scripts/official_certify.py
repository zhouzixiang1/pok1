#!/usr/bin/env python3
"""Manage signed official-full-v5 certification for strict policy bots.

Formal publication requires five 70-hand self-play rounds plus three 70-hand
eligible-opponent rounds. The EXE verdict is compliance-only and never supplies
Glicko/H2H strength evidence.
"""

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
    DEFAULT_BOOTSTRAP_CONTROL_ID,
    authorize_operator_bootstrap_selection,
    select_first_strict_control,
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
        "smoke": "Start or poll a durable short official quality-gate smoke job.",
        "compliance": "Start or poll a durable short official protocol-compliance job.",
        "full": "Start or poll a durable manual 5+3, 70-hand official certification job.",
    }
    for name in ("smoke", "compliance", "full"):
        p = sub.add_parser(name, help=mode_help[name])
        p.add_argument(
            "candidate",
            help=(
                "Strict national_tcp_policy_v1 bot directory. The low-level "
                "diagnostic harness, not formal full mode, accepts standalone scripts."
            ),
        )
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
        "bootstrap-first-strict",
        help=(
            "Explicit one-time 5+3, 70-hand certification of v143 against "
            "the current system-owned first-strict typed-policy control."
        ),
    )
    bootstrap.add_argument("candidate", help="The in-flight bots/national_v143 directory.")
    bootstrap.add_argument(
        "--control-id",
        default=DEFAULT_BOOTSTRAP_CONTROL_ID,
        help="Repository-owned current control id; archived bots are never selected.",
    )
    bootstrap.add_argument(
        "--acknowledge-one-time-first-strict-control",
        action="store_true",
        help="Required acknowledgement that one successful signed run consumes this control authorization.",
    )
    bootstrap.add_argument("--force", action="store_true", help="Retry a terminal durable job.")
    bootstrap.add_argument(
        "--wait-if-busy",
        action="store_true",
        help="Poll the durable job until it reaches a terminal state.",
    )
    p = sub.add_parser("status", help="Read certification status for a bot.")
    p.add_argument(
        "candidate", help="Strict national_tcp_policy_v1 bot directory/subject."
    )
    sub.add_parser(
        "jobs-status",
        help="Show durable official certification jobs.",
    )
    sub.add_parser(
        "doctor",
        help="Check official EXE, certificate signing, and verdict-ledger prerequisites.",
    )
    sub.add_parser(
        "init-ledger",
        help="Explicitly create the signed verdict-ledger genesis (idempotent when valid).",
    )
    p = sub.add_parser(
        "reconcile-jobs",
        help="Reconcile durable official certification jobs.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=1,
        help="Maximum durable jobs to reconcile in this run.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(status_payload(args.candidate), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "jobs-status":
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
    if args.cmd == "reconcile-jobs":
        payload = reconcile_jobs(limit=args.limit)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not payload.get("errors") else 1

    if (
        args.cmd == "bootstrap-first-strict"
        and not args.acknowledge_one_time_first_strict_control
    ):
        print(json.dumps({
            "status": "bootstrap-acknowledgement-required",
            "reason": "acknowledge_one_time_first_strict_control_required",
            "candidate": args.candidate,
            "control_id": args.control_id,
        }, ensure_ascii=False, indent=2))
        return 2

    if args.cmd in {"full", "bootstrap-first-strict"}:
        ledger = _ledger_report()
        if not ledger.get("valid"):
            print(json.dumps({
                "status": "formal-preflight-blocked",
                "reason": "official_verdict_ledger_unavailable",
                "candidate": args.candidate,
                "ledger": ledger,
            }, ensure_ascii=False, indent=2))
            return 2

    if args.cmd == "bootstrap-first-strict":
        selection = select_first_strict_control(
            args.control_id,
            args.candidate,
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
    if args.cmd == "bootstrap-first-strict":
        authorization = authorize_operator_bootstrap_selection(
            selection,
            args.control_id,
            args.candidate,
        )
        if authorization.get("valid") is not True:
            print(json.dumps({
                "status": "bootstrap-authorization-blocked",
                "candidate": args.candidate,
                "control_id": args.control_id,
                "authorization": authorization,
            }, ensure_ascii=False, indent=2))
            return 2
        selection = authorization["selection"]
    spec = build_spec(
        "full" if args.cmd == "bootstrap-first-strict" else args.cmd,
        args.candidate,
        opponent=selection["opponent"]["path"],
        bootstrap_control_id=(
            args.control_id if args.cmd == "bootstrap-first-strict" else None
        ),
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
