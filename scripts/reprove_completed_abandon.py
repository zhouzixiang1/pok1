#!/usr/bin/env python3
"""Read-only reproof of one finalized, checkpoint-free abandon transaction.

This command is for the autonomous ``.evolution_pok`` checkout after a
source-only fast-forward.  It does not create a checkpoint, reconstruct old
provider input, clear a path, or start evolution.  It only verifies immutable
schema-2 transaction evidence and the current fetched-main source lineage.
"""

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

from tool_bot_management import reprove_historical_completed_abandon  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "transaction_id",
        help="64-character transaction id from policy_epoch_abandon_transactions",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = reprove_historical_completed_abandon(args.transaction_id)
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)}",
        }, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, "proof": result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
