#!/usr/bin/env python3
"""One-time backfill of direction_ledger.jsonl from surviving generation trees.

The durable direction ledger (web/core/recent_directions.py) starts at plan
acceptance going forward; this script backfills the SEALED
``proposal_binding.change_symbol`` for every generation whose logs still
exist, so the audit's mechanical repetitive veto regains true history
immediately after deployment instead of after N more generations.

Extraction order per version:
1. ``logs/master_plan_audit_io.txt`` — the FIRST ``"proposal_binding"``
   object's ``change_symbol`` (that is the sealed selected plan the audit
   received; verified 2026-10-28 to beat the tail scan on all of v630-634);
2. fallback ``logs/master_io.txt`` accepted-slot form
   ``"final_plan": { ... "proposal_binding" ... }`` if present;
3. otherwise the version is skipped (never the wrong tail symbol).

Dry-run by default; ``--execute`` writes. Idempotent: re-running rewrites
sealed rows for the same versions. Never touches archive/, never executes
candidate code.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "web" / "core"))

_SYMBOL_RE = re.compile(r'"change_symbol"\s*:\s*"(policy\.py:[A-Za-z_][A-Za-z0-9_]*)"')
_BINDING_RE = re.compile(r'"proposal_binding"')


def _sealed_from_audit_io(path: Path) -> "str | None":
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # Only ever look INSIDE a proposal_binding object: a bare tail scan hits
    # the per-proposal compilation-contract block and records the wrong symbol.
    for m in _BINDING_RE.finditer(text):
        window = text[m.end():m.end() + 4000]
        sm = _SYMBOL_RE.search(window)
        if sm:
            return sm.group(1)
    return None


def collect(results_dir: Path) -> "dict[int, str]":
    rows: "dict[int, str]" = {}
    for d in sorted(results_dir.iterdir()):
        if not (d.is_dir() and re.fullmatch(r"v\d+", d.name)):
            continue
        version = int(d.name[1:])
        symbol = _sealed_from_audit_io(d / "logs" / "master_plan_audit_io.txt")
        if symbol is None:
            symbol = _sealed_from_audit_io(d / "logs" / "master_io.txt")
        if symbol:
            rows[version] = symbol
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-dir",
        default=str(REPO / ".evolution_pok" / "web" / "core" / "results"),
    )
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    results_dir = Path(args.results_dir)
    rows = collect(results_dir)
    print(f"sealed symbols recovered for {len(rows)} versions: "
          f"{sorted(rows)[:3]}…{sorted(rows)[-3:] if rows else ''}")
    counts: "dict[str, int]" = {}
    for symbol in rows.values():
        counts[symbol] = counts.get(symbol, 0) + 1
    for symbol, count in sorted(counts.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {count:3d}  {symbol}")
    ledger = results_dir / "direction_ledger.jsonl"
    if not args.execute:
        print(f"dry-run: would write {len(rows)} rows to {ledger}")
        return 0
    with ledger.open("w", encoding="utf-8") as fh:
        for version in sorted(rows):
            fh.write(json.dumps(
                {
                    "version": version,
                    "source_v": None,
                    "change_symbol": rows[version],
                    "selected_proposal_id": "",
                    "ts": 0.0,
                    "backfilled": True,
                },
                ensure_ascii=False,
                sort_keys=True,
            ) + "\n")
        fh.flush()
        import os
        os.fsync(fh.fileno())
    print(f"wrote {len(rows)} rows to {ledger}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
