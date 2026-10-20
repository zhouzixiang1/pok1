#!/usr/bin/env python3
"""One-shot operator entry to tighten the candidates-ledger retention window.

The disk-hygiene janitor (``web/core/disk_hygiene.py``) compacts
``results/candidates.jsonl`` every cycle with the window resolved from
``POK_CANDIDATE_LEDGER_KEEP_VERSIONS`` (default
``candidate_store.DEFAULT_KEEP_VERSIONS`` = 50). After a window tighten, the
steady janitor reclaims the accumulated backlog only as new versions push the
floor forward; this script applies the tighter window once, through the exact
canonical ``candidate_store.compact_candidate_ledger`` transaction (append
sidecar lock, streamed atomic rewrite, sha256 summary rows, paired SQLite
DELETE+VACUUM, receipt append). It is an operator convenience wrapper around
that one function — never a second compaction implementation.

Governance contract:

- dry run by default: a lock-free, read-only plan — the ledger bytes are
  byte-identical afterwards and no receipt is written;
- ``--execute`` performs the real compaction and prints the receipt;
- only the named ledger, its paired SQLite database, and
  ``candidates_compaction_receipt.jsonl`` beside it are ever touched;
- the window value shares one resolver with the janitor
  (``disk_hygiene._candidate_ledger_keep_versions``): explicit
  ``--keep-versions`` wins, else the env knob, else the library default 50;
  values < 1 clamp to 1.

Run it in the runtime checkout during an idle generation window (no active
generation / handoff):

    cd /home/ubuntu/pok1/.evolution_pok
    python scripts/compact_candidate_ledger.py --keep-versions 20
    # review the printed plan, then:
    python scripts/compact_candidate_ledger.py --keep-versions 20 --execute
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "web" / "core"
for entry in list(sys.path):
    try:
        resolved = Path(entry or ".").resolve()
    except OSError:
        continue
    if resolved in {CORE, ROOT}:
        sys.path.remove(entry)
sys.path.insert(0, str(CORE))
sys.path.insert(1, str(ROOT))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=CORE / "results" / "candidates.jsonl",
        help=(
            "candidates.jsonl to compact (default: the results ledger of the "
            "checkout this script lives in)"
        ),
    )
    parser.add_argument(
        "--keep-versions",
        type=int,
        default=None,
        help=(
            "retention window in distinct versions (default: env "
            "POK_CANDIDATE_LEDGER_KEEP_VERSIONS, else the library default 50; "
            "clamped to >= 1)"
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually compact (without it, print a read-only plan only)",
    )
    return parser


def _resolve_window(requested: int | None) -> int:
    if requested is not None:
        return max(1, int(requested))
    from disk_hygiene import _candidate_ledger_keep_versions

    return _candidate_ledger_keep_versions()


def _plan(ledger: Path, keep_versions: int) -> dict:
    """Lock-free read-only plan using candidate_store's own classification.

    The numbers are advisory (a concurrent appender can add lines while the
    scan runs); ``--execute`` re-derives everything under the sidecar lock.
    """
    import candidate_store

    rows_total = 0
    versions: set[int] = set()
    bytes_to_compact = 0
    rows_to_compact = 0
    bytes_total = 0
    with ledger.open("rb") as handle:
        for line in handle:
            if not line.strip():
                continue
            rows_total += 1
            bytes_total += len(line)
            item = candidate_store._parse_ledger_line(line)
            version = candidate_store._line_version(item)
            if version is not None:
                versions.add(version)
    floor = candidate_store._retention_version_floor(versions, keep_versions)
    if floor is not None:
        with ledger.open("rb") as handle:
            for line in handle:
                if not line.strip():
                    continue
                if candidate_store._is_compaction_candidate(
                    candidate_store._parse_ledger_line(line), floor
                ):
                    rows_to_compact += 1
                    bytes_to_compact += len(line)
    db_path = candidate_store._db_path_for_events(ledger)
    try:
        sqlite_bytes = int(db_path.stat().st_size) if db_path.exists() else 0
    except OSError:
        sqlite_bytes = 0
    return {
        "mode": "dry-run",
        "ledger": str(ledger),
        "sqlite_db": str(db_path),
        "keep_versions": keep_versions,
        "version_floor": floor,
        "distinct_versions": len(versions),
        "rows_total": rows_total,
        "rows_to_compact": rows_to_compact,
        "estimated_jsonl_bytes_to_compact": bytes_to_compact,
        "ledger_bytes": bytes_total,
        "sqlite_bytes": sqlite_bytes,
        "note": (
            "advisory lock-free estimate; --execute re-derives under the "
            "sidecar lock"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ledger = Path(args.ledger).resolve()
    keep_versions = _resolve_window(args.keep_versions)

    if not ledger.is_file():
        print(f"error: candidates ledger not found: {ledger}", file=sys.stderr)
        return 2
    if ledger.is_symlink():
        print(f"error: candidates ledger is a symlink: {ledger}", file=sys.stderr)
        return 2

    print(f"ledger       : {ledger}")
    print(f"keep_versions: {keep_versions}")
    if not args.execute:
        plan = _plan(ledger, keep_versions)
        print(
            "PLAN " + json.dumps(plan, sort_keys=True, ensure_ascii=False),
        )
        print(
            "dry run only — ledger untouched, no receipt written; "
            "re-run with --execute to compact"
        )
        return 0

    from candidate_store import compact_candidate_ledger

    receipt = compact_candidate_ledger(ledger, keep_versions=keep_versions)
    print("RECEIPT " + json.dumps(receipt, sort_keys=True, ensure_ascii=False))
    print(
        "compacted: rows={rows_compacted} jsonl_bytes_reclaimed={bytes_reclaimed} "
        "sqlite_events_deleted={sqlite_events_deleted} "
        "sqlite_bytes_reclaimed={sqlite_bytes_reclaimed} "
        "receipt_persisted={receipt_persisted}".format(**receipt)
    )
    if not receipt.get("receipt_persisted"):
        print(
            "note: nothing met the compaction window (receipt not persisted)"
        )
    return 0


if __name__ == "__main__":
    os.environ.setdefault("POK_CLOUD_RUNTIME", "1")
    sys.exit(main())
