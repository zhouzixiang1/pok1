#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _iter_inputs(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.glob("*.json")))
        else:
            files.append(path)
    return files


def _row_from_probe(
    probe: dict[str, Any],
    source: Path,
    positive_threshold: float,
    include_zero: bool,
) -> dict[str, Any] | None:
    if probe.get("status") != "ok":
        return None
    features = probe.get("advantage_features")
    delta = probe.get("primary_delta")
    if not isinstance(features, list) or delta is None:
        return None
    delta_f = float(delta)
    if delta_f == 0.0 and not include_zero:
        return None
    target = 1 if delta_f > positive_threshold else 0
    weight = probe.get("advantage_weight")
    if weight is None:
        weight = max(0.05, min(5.0, abs(delta_f) / 100.0))
    return {
        "features": [float(value) for value in features],
        "target": target,
        "weight": float(weight),
        "delta": delta_f,
        "source": str(source),
        "probe_index": probe.get("probe_index"),
        "stage": probe.get("stage"),
        "kind": probe.get("kind"),
        "candidate_label_id": probe.get("candidate_label_id"),
        "rule_label_id": probe.get("rule_label_id"),
        "top_conf": probe.get("top_conf"),
        "advised_final": probe.get("advised_final"),
        "base_final": probe.get("base_final"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--positive-threshold", type=float, default=0.0)
    parser.add_argument("--drop-zero", action="store_true")
    args = parser.parse_args()

    rows: list[dict[str, Any]] = []
    scanned = 0
    skipped = 0
    for path in _iter_inputs(args.input):
        data = json.loads(path.read_text(encoding="utf-8"))
        for probe in data.get("probes", []):
            scanned += 1
            row = _row_from_probe(probe, path, args.positive_threshold, not args.drop_zero)
            if row is None:
                skipped += 1
                continue
            rows.append(row)

    if not rows:
        raise SystemExit("no usable advantage rows; rerun counterfactual probe with advantage feature export")

    dim = len(rows[0]["features"])
    bad_dims = [len(row["features"]) for row in rows if len(row["features"]) != dim]
    if bad_dims:
        raise SystemExit(f"inconsistent feature dimensions: expected {dim}, got {bad_dims[:5]}")

    positives = sum(int(row["target"]) for row in rows)
    negatives = len(rows) - positives
    zero_delta = sum(1 for row in rows if float(row["delta"]) == 0.0)
    summary = {
        "inputs": [str(path) for path in _iter_inputs(args.input)],
        "scanned_probes": scanned,
        "skipped_probes": skipped,
        "rows": len(rows),
        "input_dim": dim,
        "positive": positives,
        "negative": negatives,
        "positive_rate": positives / max(1, len(rows)),
        "zero_delta": zero_delta,
        "positive_threshold": args.positive_threshold,
        "include_zero": not args.drop_zero,
        "mean_delta": sum(float(row["delta"]) for row in rows) / max(1, len(rows)),
    }

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
