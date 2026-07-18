#!/usr/bin/env python3
"""Inspect or execute the one-way strict Reviewer rejection reconciliation."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web" / "core"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--acknowledge-completed-review-rejection",
        action="store_true",
    )
    args = parser.parse_args()
    from terminal_gate_reconcile import (
        inspect_terminal_gate_reconciliation,
        reconcile_terminal_gate,
    )

    if args.execute:
        if not args.acknowledge_completed_review_rejection:
            parser.error(
                "--execute requires "
                "--acknowledge-completed-review-rejection"
            )
        payload = asyncio.run(reconcile_terminal_gate())
    else:
        inspected = inspect_terminal_gate_reconciliation()
        payload = {
            key: value
            for key, value in inspected.items()
            if key not in {
                "checkpoint",
                "call",
                "gate",
                "outcome",
                "role_result",
                "route",
            }
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if (not args.execute or payload.get("abandoned") is True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
