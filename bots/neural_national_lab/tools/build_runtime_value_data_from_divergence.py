#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = Path(__file__).resolve().parent
for path in (ROOT, TOOL_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from analyze_advice import _load_bot, _neural_probs  # noqa: E402
from counterfactual_rollout_probe import _advantage_features  # noqa: E402


def _resolve(path: Path | str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def _request(divergence: dict[str, Any]) -> dict[str, Any]:
    req = dict(divergence.get("baseline_request") or divergence.get("candidate_request") or {})
    if "history" not in req and "last_history" in req:
        req["history"] = list(req.get("last_history") or [])
    req.setdefault("my_chips", 20000)
    req.setdefault("pot", 150)
    req.setdefault("to_call", 0)
    req.setdefault("my_stage_bet", 0)
    req.setdefault("opponent_stage_bet", 0)
    req.setdefault("opponent_allin", False)
    return req


def _runtime_delta(pair_delta: float, mode: str) -> float:
    if mode == "baseline_minus_candidate":
        return -float(pair_delta)
    if mode == "candidate_minus_baseline":
        return float(pair_delta)
    raise ValueError(f"unknown target mode: {mode}")


def _iter_rows(
    payload: dict[str, Any],
    source: str,
    state_mod,
    neural_mod,
    all_divergences: bool,
    target_mode: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pair in payload.get("pairs", []):
        pair_delta = float(pair.get("delta_net_chips", 0.0))
        delta = _runtime_delta(pair_delta, target_mode)
        for side in ("normal", "mirror"):
            compare = pair.get(side, {}).get("action_compare", {})
            divergences = list(compare.get("divergences") or [])
            if not all_divergences:
                first = compare.get("first_divergence")
                divergences = [first] if first else []
            for divergence in divergences:
                if not divergence:
                    continue
                req = _request(divergence)
                try:
                    state = state_mod.reconstruct_state(req)
                except Exception:
                    state = {
                        "pot": req.get("pot", 150),
                        "to_call": req.get("to_call", 0),
                        "my_stage_bet": req.get("my_stage_bet", 0),
                        "opponent_stage_bet": req.get("opponent_stage_bet", 0),
                        "opponent_allin": req.get("opponent_allin", False),
                    }
                rule_action = int(divergence.get("candidate_action", 0))
                probs, top_label, top_conf = _neural_probs(neural_mod, req, state)
                features = _advantage_features(req, state, rule_action, top_label, top_conf, probs)
                if features is None:
                    continue
                rows.append({
                    "source": source,
                    "opponent": pair.get("opponent_label"),
                    "pair_idx": pair.get("idx"),
                    "variant_id": pair.get("variant_id"),
                    "seed": pair.get("seed"),
                    "side": side,
                    "delta": delta,
                    "raw_pair_delta_candidate_minus_baseline": pair_delta,
                    "target": 1.0 if delta > 0 else 0.0,
                    "weight": max(0.05, min(5.0, abs(delta) / 1000.0)),
                    "baseline_action": divergence.get("baseline_action"),
                    "candidate_action": divergence.get("candidate_action"),
                    "same_request": divergence.get("same_request"),
                    "top_label": int(top_label) if top_label is not None else None,
                    "top_conf": float(top_conf),
                    "probs": probs,
                    "request": req,
                    "features": features,
                })
    return rows


def _summary(rows: list[dict[str, Any]], inputs: list[str], version: str, target_mode: str) -> dict[str, Any]:
    deltas = [float(row["delta"]) for row in rows]
    templates = Counter(f"{row['baseline_action']}->{row['candidate_action']}|{row['side']}" for row in rows)
    top_labels = Counter(str(row.get("top_label")) for row in rows)
    dim = len(rows[0]["features"]) if rows else 0
    return {
        "inputs": inputs,
        "version": version,
        "target_mode": target_mode,
        "rows": len(rows),
        "input_dim": dim,
        "positive": sum(1 for value in deltas if value > 0),
        "zero": sum(1 for value in deltas if value == 0),
        "negative": sum(1 for value in deltas if value < 0),
        "mean_delta": statistics.mean(deltas) if deltas else 0.0,
        "median_delta": statistics.median(deltas) if deltas else 0.0,
        "action_templates": dict(sorted(templates.items())),
        "top_labels": dict(sorted(top_labels.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--summary-output", type=Path)
    parser.add_argument("--all-divergences", action="store_true")
    parser.add_argument(
        "--target-mode",
        choices=["baseline_minus_candidate", "candidate_minus_baseline"],
        default="baseline_minus_candidate",
    )
    args = parser.parse_args()

    version_path = _resolve(args.version)
    version_dir = version_path if version_path.is_dir() else version_path.parent
    _, state_mod, _, neural_mod, _ = _load_bot(version_dir)

    rows: list[dict[str, Any]] = []
    input_labels: list[str] = []
    for input_arg in args.input:
        input_path = _resolve(input_arg)
        try:
            label = str(input_path.relative_to(ROOT))
        except ValueError:
            label = str(input_path)
        input_labels.append(label)
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        rows.extend(_iter_rows(payload, label, state_mod, neural_mod, args.all_divergences, args.target_mode))
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
    summary = _summary(rows, input_labels, str(version_path), args.target_mode)
    if args.summary_output:
        summary_out = _resolve(args.summary_output)
        summary_out.parent.mkdir(parents=True, exist_ok=True)
        summary_out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
