#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from blueprint_contract import CONTRACT_VERSION  # noqa: E402
from feature_spec import LABELS, encode_features  # noqa: E402


RULE_LABEL_IDS = {
    "fold": 0,
    "call": 1,
    "check": 1,
    "raise": 3,
    "raise_half": 2,
    "raise_pot": 3,
    "raise_2pot": 4,
    "allin": 5,
}


def _label_id(name: Any) -> int:
    if isinstance(name, str):
        if name in LABELS:
            return LABELS.index(name)
        return RULE_LABEL_IDS.get(name, 1)
    return 1


def _stage(row: dict[str, Any]) -> int:
    public_cards = list(row.get("public_cards") or [])
    if len(public_cards) >= 5:
        return 3
    if len(public_cards) == 4:
        return 2
    if len(public_cards) >= 3:
        return 1
    return 0


def _row_features(row: dict[str, Any]) -> list[float]:
    to_call = float(row.get("to_call") or 0.0)
    pot = float(row.get("pot") or 150.0)
    my_chips = float(row.get("my_chips") or 20000.0)
    req = {
        "my_cards": list(row.get("my_cards") or []),
        "public_cards": list(row.get("public_cards") or []),
        "pot": pot,
        "to_call": to_call,
        "my_chips": my_chips,
        "my_stage_bet": 0,
        "opponent_stage_bet": to_call,
        "history": [],
    }
    top_label = _label_id(row.get("top_label"))
    rule_label = _label_id(row.get("rule_label") or _final_label(row.get("base_final")))
    top_onehot = [1.0 if i == top_label else 0.0 for i in range(len(LABELS))]
    rule_onehot = [1.0 if i == rule_label else 0.0 for i in range(len(LABELS))]
    extras = [
        float(row.get("top_conf") or 0.0),
        float(row.get("call_conf") or 0.0),
        min(1.0, max(0.0, to_call / 20000.0)),
        min(1.0, max(0.0, pot / 20000.0)),
        1.0 if to_call <= 0 else 0.0,
        min(1.0, max(0.0, my_chips / 20000.0)),
    ]
    stage_id = _stage(row)
    extras.extend(1.0 if i == stage_id else 0.0 for i in range(4))
    return encode_features(req, {}) + top_onehot + rule_onehot + extras


def _final_label(action: Any) -> str:
    try:
        value = int(action)
    except (TypeError, ValueError):
        return "call"
    if value == -1:
        return "fold"
    if value == -2:
        return "allin"
    if value == 0:
        return "call"
    return "raise"


def _iter_trace_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: dict[tuple[int, str, int, str], dict[str, Any]] = {}
    for pair in payload.get("pairs") or []:
        pair_idx = int(pair.get("idx", 0))
        for side in ("normal", "mirror"):
            side_payload = pair.get(side) or {}
            for source_name in ("candidates", "changes"):
                for raw in side_payload.get(source_name) or []:
                    if not isinstance(raw, dict):
                        continue
                    key = (
                        pair_idx,
                        side,
                        int(raw.get("decision_index", -1)),
                        str(raw.get("top_label")),
                    )
                    row = dict(raw)
                    row["source"] = "actual_change" if source_name == "changes" else "candidate"
                    row["trace"] = str(path)
                    row["pair"] = pair_idx
                    row["side"] = side
                    if key not in rows or row["source"] == "actual_change":
                        rows[key] = row
    return list(rows.values())


def _weight(delta: float, scale: float, max_weight: float) -> float:
    return max(0.25, min(max_weight, 1.0 + abs(delta) / max(1.0, scale)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--positive-threshold", type=float, default=0.0)
    parser.add_argument("--weight-scale", type=float, default=500.0)
    parser.add_argument("--max-weight", type=float, default=6.0)
    args = parser.parse_args()

    samples: list[dict[str, Any]] = []
    for trace in args.trace:
        trace_path = trace if trace.is_absolute() else ROOT / trace
        for row in _iter_trace_rows(trace_path):
            try:
                delta = float(row.get("hand_delta") or 0.0)
            except (TypeError, ValueError):
                delta = 0.0
            target = 1 if delta > args.positive_threshold else 0
            samples.append(
                {
                    "features": _row_features(row),
                    "target": target,
                    "weight": _weight(delta, args.weight_scale, args.max_weight),
                    "meta": {
                        "hand_delta": delta,
                        "top_label": row.get("top_label"),
                        "rule_label": row.get("rule_label") or _final_label(row.get("base_final")),
                        "stage": row.get("stage"),
                        "to_call": row.get("to_call"),
                        "pot": row.get("pot"),
                        "source": row.get("source"),
                        "trace": row.get("trace"),
                        "pair": row.get("pair"),
                        "side": row.get("side"),
                        "contract": CONTRACT_VERSION,
                        "target": "advisor_candidate_positive_delta",
                    },
                }
            )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as fh:
        for sample in samples:
            fh.write(json.dumps(sample, separators=(",", ":")) + "\n")

    positives = sum(int(row["target"]) for row in samples)
    summary = {
        "samples": len(samples),
        "positive": positives,
        "negative": len(samples) - positives,
        "positive_rate": positives / max(1, len(samples)),
        "feature_dim": len(samples[0]["features"]) if samples else 0,
        "contract": CONTRACT_VERSION,
        "target": "advisor_candidate_positive_delta",
        "traces": [str(path) for path in args.trace],
    }
    args.output.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
