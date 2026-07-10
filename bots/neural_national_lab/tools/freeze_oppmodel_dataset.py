#!/usr/bin/env python3
"""Freeze a completed collection into four opponent-disjoint model splits."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any


TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from audit_oppmodel_dataset import audit  # noqa: E402


SOURCE_SPLITS = ("train", "val", "held_out")
OUTPUT_SPLITS = ("train", "val", "calibration", "held_out")
PREFIXES = ("cf", "opponent_actions")
DEFAULT_REQUIRED_ALTERNATIVES = {
    "fold", "call", "raise_half", "raise_pot", "raise_2pot", "allin"
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{path}:{line_number}: invalid JSON") from exc
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def _opponent(row: dict[str, Any]) -> str:
    return str(row.get("_opponent_label") or row.get("opponent") or "")


def _completed_passes(path: Path) -> int:
    pass_numbers = set()
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                number = int(json.loads(line).get("pass", 0) or 0)
                if number > 0:
                    pass_numbers.add(number)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    completed = 0
    while completed + 1 in pass_numbers:
        completed += 1
    return completed


def freeze_dataset(
    source_dir: Path,
    output_dir: Path,
    *,
    calibration_opponents: set[str],
    selection_val_opponents: set[str] | None = None,
    min_value_train: int,
    min_behavior_train: int,
    required_alternative_labels: set[str],
    allow_incomplete: bool = False,
) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()
    if not calibration_opponents:
        raise ValueError("at least one calibration opponent is required")
    if source_dir == output_dir:
        raise ValueError("source and output directories must differ")
    if output_dir.exists():
        if any(output_dir.iterdir()):
            raise FileExistsError(f"output already exists and is not empty: {output_dir}")
        output_dir.rmdir()
    selection_val_opponents = set(selection_val_opponents or set())
    partition_mode = (
        "validation_to_calibration"
        if selection_val_opponents
        else "train_to_calibration"
    )
    overlap = selection_val_opponents & calibration_opponents
    if overlap:
        raise ValueError(
            f"selection/calibration opponents overlap: {sorted(overlap)}"
        )

    collection_manifest_path = source_dir / "collection_manifest.json"
    snapshots_path = source_dir / "pool_snapshots.jsonl"
    if not collection_manifest_path.exists() or not snapshots_path.exists():
        raise FileNotFoundError("collection manifest or pool snapshots are missing")
    collection_manifest = json.loads(
        collection_manifest_path.read_text(encoding="utf-8")
    )
    collection_manifest_sha256 = _sha256(collection_manifest_path)
    pool_snapshots_sha256 = _sha256(snapshots_path)
    requested_passes = int(collection_manifest.get("passes_requested", 0) or 0)
    completed_passes = _completed_passes(snapshots_path)
    if not allow_incomplete and completed_passes < requested_passes:
        raise RuntimeError(
            f"collection incomplete: {completed_passes}/{requested_passes} passes"
        )
    if not allow_incomplete and any(source_dir.glob("_tmp*")):
        raise RuntimeError("collection contains unfinished temporary probe files")

    data: dict[str, dict[str, list[dict[str, Any]]]] = {}
    input_files: dict[str, dict[str, Any]] = {}
    for prefix in PREFIXES:
        data[prefix] = {}
        source_rows = {}
        for split in SOURCE_SPLITS:
            path = source_dir / f"{prefix}_{split}.jsonl"
            if not path.exists():
                raise FileNotFoundError(path)
            input_files[path.name] = {
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            rows = _read_jsonl(path)
            if _sha256(path) != input_files[path.name]["sha256"]:
                raise RuntimeError(f"source changed while reading: {path}")
            source_rows[split] = rows
        if selection_val_opponents:
            observed_val = {_opponent(row) for row in source_rows["val"]}
            unassigned = (
                observed_val - selection_val_opponents - calibration_opponents
            )
            missing = (
                selection_val_opponents | calibration_opponents
            ) - observed_val
            if unassigned or missing:
                raise RuntimeError(
                    f"invalid val partition for {prefix}: "
                    f"unassigned={sorted(unassigned)} missing={sorted(missing)}"
                )
            data[prefix]["train"] = [
                dict(row) for row in source_rows["train"]
            ]
            data[prefix]["val"] = [
                {**row, "_split": "val"}
                for row in source_rows["val"]
                if _opponent(row) in selection_val_opponents
            ]
            data[prefix]["calibration"] = [
                {**row, "_split": "calibration"}
                for row in source_rows["val"]
                if _opponent(row) in calibration_opponents
            ]
        else:
            data[prefix]["train"] = [
                {**row, "_split": "train"}
                for row in source_rows["train"]
                if _opponent(row) not in calibration_opponents
            ]
            data[prefix]["calibration"] = [
                {**row, "_split": "calibration"}
                for row in source_rows["train"]
                if _opponent(row) in calibration_opponents
            ]
            data[prefix]["val"] = [dict(row) for row in source_rows["val"]]
        data[prefix]["held_out"] = [
            dict(row) for row in source_rows["held_out"]
        ]

    for prefix in PREFIXES:
        present = {_opponent(row) for row in data[prefix]["calibration"]}
        missing = calibration_opponents - present
        if missing:
            raise RuntimeError(
                f"{prefix} has no calibration rows for opponents: {sorted(missing)}"
            )
    checked_splits = (
        ("train", "held_out")
        if selection_val_opponents
        else ("val", "held_out")
    )
    for split in checked_splits:
        opponents = {
            _opponent(row)
            for prefix in PREFIXES
            for row in data[prefix][split]
        }
        overlap = calibration_opponents & opponents
        if overlap:
            raise RuntimeError(
                f"calibration opponents already occur in {split}: {sorted(overlap)}"
            )

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=f".{output_dir.name}.freeze-", dir=output_dir.parent
    ))
    try:
        if _sha256(collection_manifest_path) != collection_manifest_sha256:
            raise RuntimeError("collection manifest changed during freeze")
        if _sha256(snapshots_path) != pool_snapshots_sha256:
            raise RuntimeError("pool snapshots changed during freeze")
        for prefix in PREFIXES:
            for split in OUTPUT_SPLITS:
                _write_jsonl(
                    temporary / f"{prefix}_{split}.jsonl",
                    data[prefix][split],
                )
        shutil.copyfile(
            collection_manifest_path, temporary / collection_manifest_path.name
        )
        shutil.copyfile(snapshots_path, temporary / snapshots_path.name)
        if _sha256(temporary / collection_manifest_path.name) != collection_manifest_sha256:
            raise RuntimeError("copied collection manifest hash mismatch")
        if _sha256(temporary / snapshots_path.name) != pool_snapshots_sha256:
            raise RuntimeError("copied pool snapshots hash mismatch")
        report = audit(
            temporary,
            min_value_rows=min_value_train,
            min_behavior_rows=min_behavior_train,
            required_alternative_labels=required_alternative_labels,
            require_cross_hand_sequence=True,
        )
        (temporary / "dataset_audit.json").write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )
        if not report["passed"]:
            raise RuntimeError(
                "frozen dataset audit failed: " + "; ".join(report["errors"][:10])
            )
        output_files = {}
        legacy_outputs = {}
        for prefix in PREFIXES:
            for split in OUTPUT_SPLITS:
                path = temporary / f"{prefix}_{split}.jsonl"
                output_files[path.name] = {
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                    "rows": len(data[prefix][split]),
                }
                legacy_outputs[f"{prefix}_{split}"] = {
                    "path": str((output_dir / path.name).resolve()),
                    **output_files[path.name],
                    "opponents": sorted({
                        _opponent(row)
                        for row in data[prefix][split]
                        if _opponent(row)
                    }),
                }
        manifest = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_dir": str(source_dir),
            "source_completed_passes": completed_passes,
            "source_requested_passes": requested_passes,
            "allow_incomplete": bool(allow_incomplete),
            "partition_mode": partition_mode,
            "selection_val_opponents": sorted(selection_val_opponents),
            "calibration_opponents": sorted(calibration_opponents),
            "split_opponents": report["opponents"],
            "input_files": input_files,
            "output_files": output_files,
            "source_files": {
                name: details["sha256"] for name, details in input_files.items()
            },
            "outputs": legacy_outputs,
            "collection_manifest_sha256": collection_manifest_sha256,
            "pool_snapshots_sha256": pool_snapshots_sha256,
            "freeze_tool_sha256": _sha256(Path(__file__)),
            "dataset_audit_sha256": _sha256(temporary / "dataset_audit.json"),
        }
        (temporary / "freeze_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        temporary.replace(output_dir)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument(
        "--output-dir", "--out-dir", dest="output_dir", required=True, type=Path
    )
    parser.add_argument("--selection-val-opponent", action="append", default=[])
    parser.add_argument(
        "--calibration-opponent", action="append", default=[], required=True
    )
    parser.add_argument("--min-value-train", type=int, default=500)
    parser.add_argument("--min-behavior-train", type=int, default=2000)
    parser.add_argument("--require-alternative-label", action="append")
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    required_labels = set(
        args.require_alternative_label or DEFAULT_REQUIRED_ALTERNATIVES
    )
    try:
        manifest = freeze_dataset(
            args.source_dir,
            args.output_dir,
            calibration_opponents=set(args.calibration_opponent),
            selection_val_opponents=set(args.selection_val_opponent),
            min_value_train=args.min_value_train,
            min_behavior_train=args.min_behavior_train,
            required_alternative_labels=required_labels,
            allow_incomplete=args.allow_incomplete,
        )
    except (FileNotFoundError, FileExistsError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
