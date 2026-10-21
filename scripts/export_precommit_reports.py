#!/usr/bin/env python3
"""Backfill seed-free precommit evaluation reports from candidates.jsonl."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web" / "core"))

from precommit_report import export_precommit_reports  # noqa: E402


def _default_results_dir() -> Path:
    runtime = ROOT / ".evolution_pok" / "web" / "core" / "results"
    if runtime.is_dir():
        return runtime
    return ROOT / "web" / "core" / "results"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=None,
        help="results directory containing candidates.jsonl (default: runtime if present)",
    )
    parser.add_argument(
        "--version",
        type=int,
        action="append",
        default=None,
        help="only export this version (repeatable)",
    )
    args = parser.parse_args(argv)
    results_dir = args.results_dir or _default_results_dir()
    paths = export_precommit_reports(results_dir, versions=args.version)
    print(f"exported {len(paths)} precommit report(s) from {results_dir}")
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
