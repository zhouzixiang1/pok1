#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = Path(__file__).resolve().parent
for path in (ROOT, TOOL_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from build_outlier_multi_action_value_data import CONTEXT_FEATURE_SET, OPPONENT_CONTEXT_DEFAULTS  # noqa: E402


def _resolve(path: Path | str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def _neutral_context() -> list[float]:
    out: list[float] = []
    for key, default in OPPONENT_CONTEXT_DEFAULTS.items():
        if key == "avg_raise_bb":
            out.append(max(0.0, min(1.0, float(default) / 10.0)))
        else:
            out.append(max(0.0, min(1.0, float(default))))
    return out


def _rows(path: Path, neutral: list[float]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        features = [float(value) for value in row["features"]]
        if row.get("feature_set") == f"advantage+{CONTEXT_FEATURE_SET}":
            rows.append(row)
            continue
        row["features"] = features + neutral
        row["feature_set"] = f"advantage+{CONTEXT_FEATURE_SET}"
        row["opponent_context_features"] = True
        row["opponent_context_source"] = "neutral_prior"
        rows.append(row)
    return rows


def _summary(rows: list[dict[str, Any]], inputs: list[str]) -> dict[str, Any]:
    sources: dict[str, int] = {}
    for row in rows:
        source = str(row.get("source", "unknown"))
        sources[source] = sources.get(source, 0) + 1
    return {
        "mode": "augment_multi_action_context_data_v1",
        "inputs": inputs,
        "rows": len(rows),
        "input_dim": len(rows[0]["features"]) if rows else 0,
        "feature_set": rows[0].get("feature_set") if rows else None,
        "context_dim": len(OPPONENT_CONTEXT_DEFAULTS),
        "source_counts": dict(sorted(sources.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--summary-output", type=Path)
    args = parser.parse_args()

    neutral = _neutral_context()
    rows: list[dict[str, Any]] = []
    inputs: list[str] = []
    for input_arg in args.input:
        input_path = _resolve(input_arg)
        inputs.append(str(input_path.relative_to(ROOT)) if input_path.is_relative_to(ROOT) else str(input_path))
        rows.extend(_rows(input_path, neutral))

    if rows:
        dim = len(rows[0]["features"])
        bad = [len(row["features"]) for row in rows if len(row["features"]) != dim]
        if bad:
            raise SystemExit(f"inconsistent feature dimensions: expected {dim}, got {bad[:3]}")

    out = _resolve(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    summary = _summary(rows, inputs)
    if args.summary_output:
        summary_path = _resolve(args.summary_output)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
