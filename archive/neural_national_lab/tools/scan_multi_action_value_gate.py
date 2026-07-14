#!/usr/bin/env python3
"""Scan runtime-style gates for multi-action value heads."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
from typing import Any


LABELS = ("fold", "call", "raise_half", "raise_pot", "raise_2pot", "allin")


def _label_id(raw: str) -> int:
    if raw in LABELS:
        return LABELS.index(raw)
    return int(raw)


def _relu(value: float) -> float:
    return value if value > 0.0 else 0.0


def _predict(model: dict[str, Any], features: list[float]) -> list[float]:
    hidden = []
    for weights, bias in zip(model["w1"], model["b1"]):
        hidden.append(_relu(sum(float(w) * float(x) for w, x in zip(weights, features)) + float(bias)))
    return [
        sum(float(w) * float(x) for w, x in zip(weights, hidden)) + float(bias)
        for weights, bias in zip(model["w2"], model["b2"])
    ]


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "samples": 0,
            "mean": 0.0,
            "median": 0.0,
            "min": None,
            "max": None,
            "positive": 0,
            "negative": 0,
            "zero": 0,
        }
    return {
        "samples": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "positive": sum(1 for value in values if value > 0),
        "negative": sum(1 for value in values if value < 0),
        "zero": sum(1 for value in values if value == 0),
    }


def _thresholds(raw: str) -> list[float]:
    return [float(part) for part in raw.split(",") if part.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--model", action="append", required=True, type=Path)
    parser.add_argument("--label", default="call")
    parser.add_argument("--rule-label", default="raise_pot")
    parser.add_argument("--thresholds", default="0.05,0.10,0.15,0.20,0.25,0.30,0.35,0.40,0.45,0.50,0.60,0.75")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    label_id = _label_id(args.label)
    rule_id = _label_id(args.rule_label)
    thresholds = _thresholds(args.thresholds)
    rows = [
        json.loads(line)
        for line in args.input.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    payload: dict[str, Any] = {
        "input": str(args.input),
        "label": LABELS[label_id],
        "rule_label": LABELS[rule_id],
        "thresholds": thresholds,
        "models": [],
    }
    for model_path in args.model:
        model = json.loads(model_path.read_text(encoding="utf-8"))
        scored = []
        for row in rows:
            target_mask = row.get("target_mask") or []
            targets = row.get("targets") or []
            if label_id >= len(target_mask) or not int(target_mask[label_id]):
                continue
            values = _predict(model, [float(value) for value in row["features"]])
            if label_id >= len(values) or rule_id >= len(values):
                continue
            scored.append(
                {
                    "label_pred": float(values[label_id]),
                    "rule_pred": float(values[rule_id]),
                    "target": float(targets[label_id]),
                    "opponent": str(row.get("opponent")),
                    "source": str(row.get("source")),
                }
            )
        model_payload: dict[str, Any] = {
            "model": str(model_path),
            "samples": len(scored),
            "thresholds": [],
        }
        print(f"\nMODEL {model_path}")
        for threshold in thresholds:
            selected = [
                row
                for row in scored
                if row["label_pred"] >= threshold and row["label_pred"] >= row["rule_pred"] + threshold
            ]
            values = [float(row["target"]) for row in selected]
            by_opponent: dict[str, list[float]] = {}
            for row in selected:
                by_opponent.setdefault(row["opponent"], []).append(float(row["target"]))
            stats = {
                "threshold": threshold,
                **_stats(values),
                "by_opponent": {
                    opponent: _stats(opponent_values)
                    for opponent, opponent_values in sorted(by_opponent.items())
                },
            }
            model_payload["thresholds"].append(stats)
            if values:
                print(
                    f"  th={threshold:.3f} n={len(values)} mean={stats['mean']:.1f} "
                    f"median={stats['median']:.1f} pos={stats['positive']} "
                    f"neg={stats['negative']} zero={stats['zero']}"
                )
        payload["models"].append(model_payload)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
