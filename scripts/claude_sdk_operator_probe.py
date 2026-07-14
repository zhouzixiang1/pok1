#!/usr/bin/env python3
"""Run the read-only, multi-tool Claude Agent SDK operator probe."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = PROJECT_ROOT / "web" / "core"
for value in (str(PROJECT_ROOT), str(CORE_DIR)):
    if value not in sys.path:
        sys.path.insert(0, value)


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Exercise the production Claude Agent SDK path with three Read and "
            "two exact-allowlisted Bash calls; print one JSON receipt to stdout."
        )
    )
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--timeout-seconds", type=_positive_float, default=300.0)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    try:
        from operator_sdk_probe import run_operator_probe

        receipt = asyncio.run(
            run_operator_probe(
                repo_root=PROJECT_ROOT,
                timeout_seconds=args.timeout_seconds,
                model=args.model,
            )
        )
    except BaseException as exc:
        receipt = {
            "schema": "pok.claude_sdk_operator_probe_receipt/v1",
            "status": "fail",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "failure": {
                "category": "probe_bootstrap_failure",
                "exception_type": type(exc).__name__,
                "message": str(exc)[:2000],
            },
        }

    print(
        json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
        )
    )
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
