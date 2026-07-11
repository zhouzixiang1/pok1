#!/usr/bin/env python3
"""Inspect or explicitly rotate authoritative rating data identity."""

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
    # ``evaluation_data_identity`` imports the native evaluator identity, which
    # in turn imports the repository-level ``sever`` package.  Executing this
    # script by path sets sys.path[0] to ``scripts/``, not the repository root.
    sys.path.insert(1, str(ROOT))

from evaluation_data_identity import (  # noqa: E402
    archive_and_initialize,
    ensure_evaluation_data_identity,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=ROOT / "web" / "core" / "results",
    )
    parser.add_argument("--archive-and-initialize", action="store_true")
    parser.add_argument("--reason", default="operator-approved evaluator identity migration")
    args = parser.parse_args()
    if args.archive_and_initialize:
        payload = archive_and_initialize(args.results_dir, reason=args.reason)
    else:
        payload = ensure_evaluation_data_identity(args.results_dir)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
