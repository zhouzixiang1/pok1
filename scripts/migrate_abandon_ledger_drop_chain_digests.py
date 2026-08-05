#!/usr/bin/env python3
"""One-time migration: strip the legacy chain-digest fields from abandon receipts.

The abandoned-versions ledger was radically simplified: receipts are now
immutable structured records (version/source/stage/reason/timestamp/envelope)
and no longer carry per-row ``receipt_digest`` / ``previous_receipt_digest``
chain fields, nor are they re-validated against live git on every load.  The
allocation CAS now uses a holistic sha256 over all rows (computed at read
time) instead of the fragile chain head.

This script rewrites an existing ``abandoned_versions.jsonl`` in place,
dropping the two legacy fields from every row and re-serializing each row as
canonical JSON (sorted keys, compact separators).  It is idempotent: rows that
already lack the fields are emitted unchanged.

Usage (run from the autonomous runtime checkout, service stopped)::

    python scripts/migrate_abandon_ledger_drop_chain_digests.py \\
        --ledger web/core/results/abandoned_versions.jsonl \\
        --execute

Omit ``--execute`` for a read-only dry run that reports what would change.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

LEGACY_FIELDS = ("receipt_digest", "previous_receipt_digest")


def _canonical_row(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def migrate(ledger_path: Path, *, execute: bool) -> int:
    if not ledger_path.is_file() or ledger_path.is_symlink():
        print(f"error: ledger not a regular file: {ledger_path}", file=sys.stderr)
        return 2
    raw = ledger_path.read_text(encoding="utf-8")
    if not raw:
        print(f"ledger is empty: {ledger_path}")
        return 0
    if not raw.endswith("\n"):
        print("error: ledger final row lacks trailing newline", file=sys.stderr)
        return 2

    out_lines: list[str] = []
    changed = 0
    total = 0
    for line_no, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            print(f"error: blank row at line {line_no}", file=sys.stderr)
            return 2
        row = json.loads(line)
        total += 1
        stripped = {k: v for k, v in row.items() if k not in LEGACY_FIELDS}
        if set(stripped) != set(row):
            changed += 1
        out_lines.append(_canonical_row(stripped))

    print(f"ledger: {ledger_path}")
    print(f"rows: {total}, rows with legacy fields stripped: {changed}")
    if not changed:
        print("no changes needed (already migrated)")
        return 0
    if not execute:
        print("dry run only — re-run with --execute to apply")
        return 0

    # Atomic publish: temp file + rename, same parent dir.
    parent = ledger_path.parent
    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".migrate_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out_lines) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, ledger_path)
        # fsync the parent directory so the rename is durable.
        dir_fd = os.open(str(parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    print(f"migrated {changed} rows in place (atomic)")
    return 0


if __name__ == "__main__":
    import tempfile  # noqa: E402  (local import keeps --help fast)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger",
        default="web/core/results/abandoned_versions.jsonl",
        help="path to abandoned_versions.jsonl",
    )
    parser.add_argument("--execute", action="store_true", help="apply the migration")
    args = parser.parse_args()
    raise SystemExit(migrate(Path(args.ledger), execute=args.execute))
