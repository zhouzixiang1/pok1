#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import importlib
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
from feature_spec import LABELS  # noqa: E402


CONTEXT_FEATURE_SET = "opponent_context_v1"
OPPONENT_CONTEXT_DEFAULTS = {
    "confidence": 0.0,
    "vpip": 0.58,
    "pfr": 0.28,
    "allin_rate": 0.05,
    "postflop_aggr": 0.36,
    "postflop_check_rate": 0.42,
    "fold_to_raise": 0.44,
    "fold_to_open_preflop": 0.42,
    "threebet_vs_open": 0.16,
    "aggression": 0.30,
    "avg_raise_bb": 2.60,
    "flop_aggr": 0.36,
    "turn_aggr": 0.32,
    "river_aggr": 0.28,
    "barrel_freq": 0.45,
    "turn_to_river_barrel": 0.35,
    "barrel_abandon_turn": 0.55,
    "barrel_abandon_river": 0.65,
    "sizing_aggr": 0.35,
    "fold_to_bet_flop": 0.44,
    "fold_to_bet_turn": 0.44,
    "fold_to_bet_river": 0.44,
    "call_down_flop_turn": 0.35,
    "call_down_turn_river": 0.35,
    "passivity_score": 0.50,
    "value_maximizer_index": 0.40,
    "river_call_size_ratio": 0.50,
    "turn_call_size_ratio": 0.50,
    "flop_call_size_ratio": 0.50,
    "large_bet_ratio": 0.32,
    "per_street_mean_bet_ratio.1": 0.50,
    "per_street_mean_bet_ratio.2": 0.50,
    "per_street_mean_bet_ratio.3": 0.50,
}


def _load_opponent_module(version_dir: Path):
    if "opponent" in sys.modules:
        return sys.modules["opponent"]
    sys.path.insert(0, str(version_dir))
    try:
        return importlib.import_module("opponent")
    except Exception:
        return None
    finally:
        try:
            sys.path.remove(str(version_dir))
        except ValueError:
            pass


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


def _stage(req: dict[str, Any]) -> str:
    n_public = len(req.get("public_cards") or [])
    if n_public >= 5:
        return "river"
    if n_public == 4:
        return "turn"
    if n_public >= 3:
        return "flop"
    return "preflop"


def _clipped(value: float, limit: float | None) -> float:
    if limit is None:
        return float(value)
    bound = abs(float(limit))
    return max(-bound, min(bound, float(value)))


def _clip01(value: Any, default: float) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        x = float(default)
    if x != x:
        x = float(default)
    return max(0.0, min(1.0, x))


def _opponent_value(model: dict[str, Any], key: str, default: float) -> Any:
    if "." not in key:
        return model.get(key, default)
    root, subkey = key.split(".", 1)
    nested = model.get(root)
    if isinstance(nested, dict):
        return nested.get(subkey, nested.get(int(subkey), default))
    return default


def _full_requests(divergence: dict[str, Any]) -> list[dict[str, Any]]:
    raw = divergence.get("candidate_full_requests") or divergence.get("baseline_full_requests") or []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _opponent_context_features(
    opponent_mod,
    divergence: dict[str, Any],
    req: dict[str, Any],
) -> list[float]:
    requests = _full_requests(divergence)
    model: dict[str, Any] = dict(OPPONENT_CONTEXT_DEFAULTS)
    builder = getattr(opponent_mod, "build_opponent_model", None) if opponent_mod is not None else None
    if builder is not None and requests:
        try:
            my_id = int(req.get("my_id", requests[-1].get("my_id", 0)) or 0)
            built = builder(requests, my_id)
            if isinstance(built, dict):
                model.update(built)
        except Exception:
            pass
    features: list[float] = []
    for key, default in OPPONENT_CONTEXT_DEFAULTS.items():
        raw = _opponent_value(model, key, default)
        if key == "avg_raise_bb":
            try:
                avg_raise_bb = float(raw)
            except (TypeError, ValueError):
                avg_raise_bb = float(default)
            features.append(_clip01(avg_raise_bb / 10.0, float(default) / 10.0))
        else:
            features.append(_clip01(raw, float(default)))
    return features


def _raise_delta_target(pair_delta: float, baseline_action: int, candidate_action: int) -> float | None:
    if baseline_action == 0 and candidate_action > 0:
        return float(pair_delta)
    if baseline_action > 0 and candidate_action == 0:
        return -float(pair_delta)
    return None


def _iter_divergences(pair: dict[str, Any], side: str, all_divergences: bool) -> list[dict[str, Any]]:
    compare = pair.get(side, {}).get("action_compare", {})
    if all_divergences:
        return [div for div in compare.get("divergences", []) if div]
    first = compare.get("first_divergence")
    return [first] if first else []


def _rows_from_payload(
    payload: dict[str, Any],
    source: str,
    state_mod,
    neural_mod,
    label_id: int,
    all_divergences: bool,
    clip_target: float | None,
    max_candidate: int,
    require_stage: str | None,
    min_abs_target: float,
    opponent_mod,
    opponent_context_features: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    label_name = LABELS[label_id]
    for pair in payload.get("pairs", []):
        pair_delta = float(pair.get("delta_net_chips", 0.0))
        for side in ("normal", "mirror"):
            for divergence in _iter_divergences(pair, side, all_divergences):
                baseline_action = int(divergence.get("baseline_action", 0))
                candidate_action = int(divergence.get("candidate_action", 0))
                target = _raise_delta_target(pair_delta, baseline_action, candidate_action)
                if target is None:
                    continue
                raised_action = candidate_action if candidate_action > 0 else baseline_action
                if raised_action <= 0 or raised_action > int(max_candidate):
                    continue
                req = _request(divergence)
                stage = _stage(req)
                if require_stage is not None and stage != require_stage:
                    continue
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
                probs, _, _ = _neural_probs(neural_mod, req, state)
                if probs is None or len(probs) <= label_id:
                    continue
                label_conf = float(probs[label_id])
                # The low-interaction runtime head only scores raise-vs-call
                # spots, so use the default call/check action as the rule
                # baseline even when the compared older bot was the side that
                # raised.
                rule_action = 0
                features = _advantage_features(req, state, rule_action, label_id, label_conf, probs)
                if features is None:
                    continue
                feature_set = "advantage"
                if opponent_context_features:
                    features = features + _opponent_context_features(opponent_mod, divergence, req)
                    feature_set = f"advantage+{CONTEXT_FEATURE_SET}"
                clipped = _clipped(target, clip_target)
                if abs(clipped) < float(min_abs_target):
                    continue
                targets = [0.0] * len(LABELS)
                target_mask = [0] * len(LABELS)
                legal_mask = [0] * len(LABELS)
                targets[label_id] = clipped
                target_mask[label_id] = 1
                legal_mask[label_id] = 1
                rows.append({
                    "features": features,
                    "targets": targets,
                    "raw_targets": list(targets[:label_id]) + [float(target)] + list(targets[label_id + 1 :]),
                    "target_mask": target_mask,
                    "legal_mask": legal_mask,
                    "weight": max(0.05, min(5.0, abs(clipped) / 1000.0)),
                    "source": source,
                    "feature_set": feature_set,
                    "opponent_context_features": bool(opponent_context_features),
                    "target": "outlier_delta_vs_rule",
                    "labels": list(LABELS),
                    "row_index": len(rows),
                    "side": side,
                    "stage": stage,
                    "hand": req.get("hand"),
                    "opponent": pair.get("opponent_label"),
                    "pair_idx": pair.get("idx"),
                    "seed": pair.get("seed"),
                    "raw_pair_delta_candidate_minus_baseline": pair_delta,
                    "baseline_action": baseline_action,
                    "candidate_action": candidate_action,
                    "raised_action": raised_action,
                    "rule_final": rule_action,
                    "rule_value": 0.0,
                    "rule_final_in_menu": True,
                    "best_label_id": label_id if clipped > 0 else 1,
                    "best_label": label_name if clipped > 0 else "call",
                    "same_request": divergence.get("same_request"),
                    "label_conf": label_conf,
                })
    return rows


def _summary(rows: list[dict[str, Any]], inputs: list[str], version: str, label_id: int) -> dict[str, Any]:
    values = [float(row["targets"][label_id]) for row in rows]
    by_opponent: dict[str, list[float]] = {}
    templates = Counter()
    for row in rows:
        by_opponent.setdefault(str(row.get("opponent")), []).append(float(row["targets"][label_id]))
        templates[f"{row.get('baseline_action')}->{row.get('candidate_action')}|{row.get('side')}"] += 1
    return {
        "inputs": inputs,
        "version": version,
        "rows": len(rows),
        "input_dim": len(rows[0]["features"]) if rows else 0,
        "feature_set": rows[0].get("feature_set") if rows else None,
        "label": LABELS[label_id],
        "positive": sum(1 for value in values if value > 0),
        "zero": sum(1 for value in values if value == 0),
        "negative": sum(1 for value in values if value < 0),
        "mean_target": statistics.mean(values) if values else 0.0,
        "median_target": statistics.median(values) if values else 0.0,
        "min_target": min(values) if values else None,
        "max_target": max(values) if values else None,
        "action_templates": dict(sorted(templates.items())),
        "by_opponent": {
            opponent: {
                "rows": len(opponent_values),
                "positive": sum(1 for value in opponent_values if value > 0),
                "negative": sum(1 for value in opponent_values if value < 0),
                "mean_target": statistics.mean(opponent_values) if opponent_values else 0.0,
                "min_target": min(opponent_values) if opponent_values else None,
                "max_target": max(opponent_values) if opponent_values else None,
            }
            for opponent, opponent_values in sorted(by_opponent.items())
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--summary-output", type=Path)
    parser.add_argument("--label", choices=list(LABELS), default="raise_half")
    parser.add_argument("--all-divergences", action="store_true")
    parser.add_argument("--clip-target", type=float, default=1000.0)
    parser.add_argument("--max-candidate", type=int, default=125)
    parser.add_argument("--stage", default="flop")
    parser.add_argument("--min-abs-target", type=float, default=1.0)
    parser.add_argument("--opponent-context-features", action="store_true")
    args = parser.parse_args()

    version_path = _resolve(args.version)
    version_dir = version_path if version_path.is_dir() else version_path.parent
    _, state_mod, _, neural_mod, _ = _load_bot(version_dir)
    if neural_mod is None:
        raise SystemExit(f"version has no neural_policy: {version_dir}")
    opponent_mod = _load_opponent_module(version_dir) if args.opponent_context_features else None
    label_id = int(LABELS.index(args.label))

    rows: list[dict[str, Any]] = []
    input_labels: list[str] = []
    for input_arg in args.input:
        input_path = _resolve(input_arg)
        label = str(input_path.relative_to(ROOT)) if input_path.is_relative_to(ROOT) else str(input_path)
        input_labels.append(label)
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        rows.extend(
            _rows_from_payload(
                payload,
                label,
                state_mod,
                neural_mod,
                label_id,
                args.all_divergences,
                args.clip_target,
                args.max_candidate,
                args.stage if args.stage else None,
                args.min_abs_target,
                opponent_mod,
                bool(args.opponent_context_features),
            )
        )
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
    summary = _summary(rows, input_labels, str(version_path), label_id)
    if args.summary_output:
        summary_out = _resolve(args.summary_output)
        summary_out.parent.mkdir(parents=True, exist_ok=True)
        summary_out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
