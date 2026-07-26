#!/usr/bin/env python3
"""Inspect or explicitly rotate authoritative rating data identity."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = ROOT / "web" / "core"
# When this script is invoked by path (e.g. `python scripts/foo.py`), Python
# prepends the script's own directory (`scripts/`) to sys.path[0]. If a parent
# process also exports PYTHONPATH containing CORE_DIR/ROOT, the idempotency
# guard below would skip the insert and leave `scripts/` first — causing the
# subsequent `from evaluation_data_identity import ...` to resolve back to this
# very file (circular import). Always force CORE_DIR to the front, and ROOT
# right after it, regardless of whether PYTHONPATH already lists them.
sys.path.insert(0, str(CORE_DIR))
sys.path.insert(1, str(ROOT))

from evaluation_data_identity import (  # noqa: E402
    archive_and_initialize,
    ensure_evaluation_data_identity,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect the national_tcp_policy_v1 rating-data identity, or "
            "explicitly archive incompatible authoritative rating payloads "
            "and initialize an empty identity. Archived ratings, H2H, match "
            "history, and generation evidence are never migrated into the new "
            "identity."
        ),
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=ROOT / "web" / "core" / "results",
        help="Authoritative rating results directory to inspect (default: %(default)s).",
    )
    parser.add_argument(
        "--archive-and-initialize",
        action="store_true",
        help=(
            "Archive incompatible identity-bound payloads and create an empty "
            "current identity; do not carry old strength evidence forward."
        ),
    )
    parser.add_argument(
        "--reason",
        default="operator-approved evaluator identity migration",
        help="Operator reason recorded in the archive manifest.",
    )
    args = parser.parse_args()
    if args.archive_and_initialize:
        payload = archive_and_initialize(args.results_dir, reason=args.reason)
    else:
        payload = ensure_evaluation_data_identity(args.results_dir)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
