#!/usr/bin/env python3
"""Freeze a collected dataset and split selection from risk calibration."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _read(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _opponent(row: dict[str, Any]) -> str:
    return str(row.get("_opponent_label") or row.get("opponent") or "")


def _write(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    payload = "".join(
        json.dumps(row, separators=(",", ":")) + "\n" for row in rows
    )
    path.write_text(payload, encoding="utf-8")
    return {
        "path": str(path.resolve()),
        "rows": len(rows),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "opponents": sorted({_opponent(row) for row in rows if _opponent(row)}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--selection-val-opponent", action="append", required=True)
    parser.add_argument("--calibration-opponent", action="append", required=True)
    args = parser.parse_args()

    source = args.source_dir.resolve()
    out = args.out_dir.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"freeze output must be empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    selection = set(args.selection_val_opponent)
    calibration = set(args.calibration_opponent)
    overlap = selection & calibration
    if overlap:
        raise SystemExit(f"selection/calibration opponents overlap: {sorted(overlap)}")

    source_value_val = _read(source / "cf_val.jsonl")
    source_behavior_val = _read(source / "opponent_actions_val.jsonl")
    observed_val = {
        _opponent(row) for row in source_value_val + source_behavior_val if _opponent(row)
    }
    unassigned = observed_val - selection - calibration
    missing = (selection | calibration) - observed_val
    if unassigned or missing:
        raise SystemExit(
            f"invalid val partition: unassigned={sorted(unassigned)} missing={sorted(missing)}"
        )

    outputs = {}
    for prefix in ("cf", "opponent_actions"):
        train = _read(source / f"{prefix}_train.jsonl")
        held_out = _read(source / f"{prefix}_held_out.jsonl")
        val_source = source_value_val if prefix == "cf" else source_behavior_val
        val = [row for row in val_source if _opponent(row) in selection]
        calibration_rows = [
            row for row in val_source if _opponent(row) in calibration
        ]
        outputs[f"{prefix}_train"] = _write(out / f"{prefix}_train.jsonl", train)
        outputs[f"{prefix}_val"] = _write(out / f"{prefix}_val.jsonl", val)
        outputs[f"{prefix}_calibration"] = _write(
            out / f"{prefix}_calibration.jsonl", calibration_rows
        )
        outputs[f"{prefix}_held_out"] = _write(
            out / f"{prefix}_held_out.jsonl", held_out
        )

    split_opponents = {
        "train": set(outputs["cf_train"]["opponents"])
        | set(outputs["opponent_actions_train"]["opponents"]),
        "val": set(outputs["cf_val"]["opponents"])
        | set(outputs["opponent_actions_val"]["opponents"]),
        "calibration": set(outputs["cf_calibration"]["opponents"])
        | set(outputs["opponent_actions_calibration"]["opponents"]),
        "held_out": set(outputs["cf_held_out"]["opponents"])
        | set(outputs["opponent_actions_held_out"]["opponents"]),
    }
    names = list(split_opponents)
    for left_index, left in enumerate(names):
        for right in names[left_index + 1:]:
            leaked = split_opponents[left] & split_opponents[right]
            if leaked:
                raise SystemExit(f"opponent leakage {left}/{right}: {sorted(leaked)}")

    source_files = {}
    for path in sorted(source.glob("*.jsonl")):
        source_files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "source_dir": str(source),
        "source_files": source_files,
        "selection_val_opponents": sorted(selection),
        "calibration_opponents": sorted(calibration),
        "split_opponents": {
            split: sorted(opponents) for split, opponents in split_opponents.items()
        },
        "outputs": outputs,
    }
    manifest_path = out / "freeze_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
