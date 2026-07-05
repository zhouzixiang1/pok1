#!/usr/bin/env python3
"""Build JSONL training rows from multi-action counterfactual outputs."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
from typing import Any


LABELS = ("fold", "call", "raise_half", "raise_pot", "raise_2pot", "allin")


def _iter_inputs(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.glob("*.json")))
        else:
            files.append(path)
    return files


def _features(row: dict[str, Any], feature_set: str) -> list[float] | None:
    if feature_set == "advantage":
        values = row.get("advantage_features")
    elif feature_set == "state":
        values = row.get("state_features")
    else:
        raise ValueError(f"unknown feature set: {feature_set}")
    if not isinstance(values, list):
        return None
    return [float(value) for value in values]


def _target_values(row: dict[str, Any], target: str) -> list[float | None] | None:
    values = row.get(target)
    if not isinstance(values, list):
        return None
    out: list[float | None] = []
    for value in values:
        out.append(float(value) if value is not None else None)
    return out


def _row_from_target(
    row: dict[str, Any],
    source: Path,
    feature_set: str,
    target: str,
    require_full_vector: bool,
) -> dict[str, Any] | None:
    if row.get("status") != "ok":
        return None
    features = _features(row, feature_set)
    targets = _target_values(row, target)
    if features is None or targets is None:
        return None
    if len(targets) != len(LABELS):
        return None
    target_mask = [1 if value is not None else 0 for value in targets]
    if require_full_vector and sum(target_mask) != len(LABELS):
        return None
    filled = [float(value) if value is not None else 0.0 for value in targets]
    legal_mask = row.get("legal_mask")
    if not isinstance(legal_mask, list) or len(legal_mask) != len(LABELS):
        legal_mask = target_mask
    legal_mask = [1 if int(value) else 0 for value in legal_mask]
    magnitude = max(abs(value) for value in filled) if filled else 0.0
    return {
        "features": features,
        "targets": filled,
        "target_mask": target_mask,
        "legal_mask": legal_mask,
        "weight": max(0.05, min(5.0, magnitude / 1000.0)),
        "source": str(source),
        "feature_set": feature_set,
        "target": target,
        "labels": list(LABELS),
        "row_index": row.get("row_index"),
        "shard": row.get("shard"),
        "side": row.get("side"),
        "stage": row.get("stage"),
        "hand": row.get("hand"),
        "rule_final": row.get("rule_final"),
        "rule_value": row.get("rule_value"),
        "rule_final_in_menu": bool(row.get("rule_final_in_menu", True)),
        "best_label_id": row.get("best_label_id"),
        "best_label": row.get("best_label"),
        "unique_final_action_count": row.get("unique_final_action_count"),
        "evaluated_branch_count": row.get("evaluated_branch_count"),
    }


def _summary(
    rows: list[dict[str, Any]],
    scanned: int,
    skipped: int,
    inputs: list[Path],
    feature_set: str,
    target: str,
    require_full_vector: bool,
) -> dict[str, Any]:
    target_values: list[float] = []
    by_label: dict[str, list[float]] = {label: [] for label in LABELS}
    best_counts = Counter(str(row.get("best_label")) for row in rows if row.get("best_label"))
    stage_counts = Counter(str(row.get("stage")) for row in rows)
    for row in rows:
        for idx, value in enumerate(row["targets"]):
            if row["target_mask"][idx]:
                target_values.append(float(value))
                by_label[LABELS[idx]].append(float(value))
    dim = len(rows[0]["features"]) if rows else 0
    return {
        "inputs": [str(path) for path in inputs],
        "feature_set": feature_set,
        "target": target,
        "require_full_vector": require_full_vector,
        "scanned_rows": scanned,
        "skipped_rows": skipped,
        "rows": len(rows),
        "input_dim": dim,
        "label_count": len(LABELS),
        "target_samples": len(target_values),
        "target_mean": statistics.mean(target_values) if target_values else 0.0,
        "target_median": statistics.median(target_values) if target_values else 0.0,
        "positive_targets": sum(1 for value in target_values if value > 0.0),
        "zero_targets": sum(1 for value in target_values if value == 0.0),
        "negative_targets": sum(1 for value in target_values if value < 0.0),
        "off_menu_rule_rows": sum(1 for row in rows if not row.get("rule_final_in_menu", True)),
        "best_label_counts": dict(sorted(best_counts.items())),
        "stage_counts": dict(sorted(stage_counts.items())),
        "target_samples_by_label": {
            label: {
                "samples": len(values),
                "mean": statistics.mean(values) if values else 0.0,
            }
            for label, values in by_label.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--feature-set", choices=["advantage", "state"], default="advantage")
    parser.add_argument(
        "--target",
        choices=["delta_vs_rule", "regret_vs_mean", "action_values"],
        default="delta_vs_rule",
    )
    parser.add_argument("--allow-incomplete-vector", action="store_true")
    args = parser.parse_args()

    inputs = _iter_inputs(args.input)
    rows: list[dict[str, Any]] = []
    scanned = 0
    skipped = 0
    for path in inputs:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in payload.get("rows", []):
            scanned += 1
            out = _row_from_target(
                row,
                path,
                args.feature_set,
                args.target,
                require_full_vector=not args.allow_incomplete_vector,
            )
            if out is None:
                skipped += 1
                continue
            rows.append(out)

    if not rows:
        raise SystemExit("no usable multi-action value rows")
    dim = len(rows[0]["features"])
    bad_dims = [len(row["features"]) for row in rows if len(row["features"]) != dim]
    if bad_dims:
        raise SystemExit(f"inconsistent feature dimensions: expected {dim}, got {bad_dims[:5]}")

    summary = _summary(
        rows,
        scanned,
        skipped,
        inputs,
        args.feature_set,
        args.target,
        require_full_vector=not args.allow_incomplete_vector,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
