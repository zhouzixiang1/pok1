#!/usr/bin/env python3
"""Freeze an uncertainty-driven neural override policy on selection data only."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import sys
from typing import Any


TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from feature_spec import LABELS  # noqa: E402
from opp_multitask_ensemble_runtime import OpponentMultiTaskEnsemble  # noqa: E402
from train_opponent_multitask_net import (  # noqa: E402
    _hero_action_features,
    build_value_sample,
)


def _read(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _bootstrap_mean_ci(
    values: list[float], *, samples: int, seed: int
) -> dict[str, float]:
    if not values:
        return {"lower": 0.0, "mean": 0.0, "upper": 0.0}
    rng = random.Random(seed)
    n = len(values)
    means = [
        sum(values[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(max(1, samples))
    ]
    return {
        "lower": _percentile(means, 0.025),
        "mean": statistics.fmean(values),
        "upper": _percentile(means, 0.975),
    }


def _response_signal(
    response: dict[str, Any], row: dict[str, Any], action: int
) -> float:
    probabilities = response.get("probabilities") or {}
    state = row.get("state") or {}
    request = row.get("request") or {}
    pot = max(1.0, float(state.get("pot", request.get("pot", 150)) or 150))
    my_bet = max(0.0, float(state.get("my_round_bet", request.get("my_stage_bet", 0)) or 0))
    stack = max(0.0, float(request.get("my_chips", 20000) or 20000))
    committed = stack if action == -2 else max(0.0, float(action) - my_bet)
    fold_gain = float(probabilities.get("fold", 0.0)) * pot
    aggression = float(probabilities.get("raise", 0.0)) + float(
        probabilities.get("allin", 0.0)
    )
    aggression_risk = aggression * min(stack, max(pot, committed))
    entropy_penalty = 0.25 * float(response.get("normalized_entropy", 0.0)) * pot
    return fold_gain - aggression_risk - entropy_penalty


def _prepare_rows(
    raw_rows: list[dict[str, Any]], ensemble: OpponentMultiTaskEnsemble
) -> list[dict[str, Any]]:
    prepared = []
    for raw in raw_rows:
        sample = build_value_sample(raw, max_hist=ensemble.max_hist)
        rule_id = int(sample.get("rule_id", 1) or 0)
        values = ensemble.predict_values(
            sample["state"],
            sample["profile"],
            sample["history"],
            sample["cross_hand"],
            rule_id,
        )
        if not values:
            continue
        candidates = []
        for probe in raw.get("probes") or []:
            if probe.get("status") != "ok" or probe.get("force_confirmed") is not True:
                continue
            label_name = str(probe.get("forced_label", ""))
            if label_name not in LABELS:
                continue
            label_id = LABELS.index(label_name)
            if label_id == rule_id:
                continue
            action = int(probe.get("forced_action", 0) or 0)
            response_signal = 0.0
            response = None
            if label_name.startswith("raise") or label_name == "allin":
                response_row = dict(raw)
                response_row["hero_action"] = action
                response_row["hero_action_label_id"] = label_id
                response = ensemble.predict_response(
                    sample["state"],
                    sample["profile"],
                    sample["history"],
                    sample["cross_hand"],
                    _hero_action_features(response_row),
                )
                if response:
                    response_signal = _response_signal(response, raw, action)
            candidates.append({
                "label_id": label_id,
                "label": label_name,
                "action": action,
                "response_signal": response_signal,
                "hand_delta": float(probe["delta_vs_rule"]),
                "tail_delta": float(probe["tail_delta_vs_rule"]),
                "match_delta": float(probe["match_delta_vs_rule"]),
            })
        if candidates:
            prepared.append({
                "opponent": str(raw.get("_opponent_label") or raw.get("opponent")),
                "rule_id": rule_id,
                "values": values,
                "candidates": candidates,
            })
    return prepared


def _evaluate_config(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    bootstrap_samples: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    deltas = []
    selected = []
    by_opponent: dict[str, list[float]] = {}
    hand_weight = float(config["hand_weight"])
    response_weight = float(config["response_weight"])
    margin = float(config["margin"])
    use_lower = bool(config.get("use_lower", True))
    value_key = "lower" if use_lower else "mean"
    for row in rows:
        best = None
        for candidate in row["candidates"]:
            label_id = candidate["label_id"]
            hand = row["values"]["delta_vs_rule"][value_key][label_id]
            match = row["values"]["match_delta_vs_rule"][value_key][label_id]
            score = (
                hand_weight * float(hand)
                + (1.0 - hand_weight) * float(match)
                + response_weight * float(candidate["response_signal"])
            )
            if best is None or score > best[0]:
                best = (score, candidate)
        if best is not None and best[0] > margin:
            candidate = best[1]
            delta = float(candidate["match_delta"])
            selected.append(candidate)
        else:
            delta = 0.0
        deltas.append(delta)
        by_opponent.setdefault(row["opponent"], []).append(delta)
    override_match = [candidate["match_delta"] for candidate in selected]
    override_hand = [candidate["hand_delta"] for candidate in selected]
    override_tail = [candidate["tail_delta"] for candidate in selected]
    ci = _bootstrap_mean_ci(
        deltas, samples=bootstrap_samples, seed=bootstrap_seed
    )
    return {
        "config": config,
        "rows": len(rows),
        "overrides": len(selected),
        "override_rate": len(selected) / len(rows) if rows else 0.0,
        "match_total": sum(deltas),
        "match_mean_per_opportunity": statistics.fmean(deltas) if deltas else 0.0,
        "match_bootstrap_mean_ci": ci,
        "override_match_mean": statistics.fmean(override_match) if override_match else 0.0,
        "override_hand_mean": statistics.fmean(override_hand) if override_hand else 0.0,
        "override_tail_mean": statistics.fmean(override_tail) if override_tail else 0.0,
        "negative_override_rate": (
            sum(value < 0 for value in override_match) / len(override_match)
            if override_match else 0.0
        ),
        "worst_override_match": min(override_match) if override_match else 0.0,
        "by_opponent": {
            opponent: {
                "rows": len(values),
                "total": sum(values),
                "mean": statistics.fmean(values),
            }
            for opponent, values in sorted(by_opponent.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", required=True)
    parser.add_argument("--selection-data", required=True, type=Path)
    parser.add_argument("--calibration-data", type=Path)
    parser.add_argument("--held-out-data", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--margin-grid", default="0,25,50,100,200,400")
    parser.add_argument("--hand-weight-grid", default="0.25,0.5,0.75,1.0")
    parser.add_argument("--response-weight-grid", default="0,0.05,0.1")
    parser.add_argument("--min-overrides", type=int, default=10)
    parser.add_argument("--min-override-hand-mean", type=float, default=0.0)
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260710)
    args = parser.parse_args()

    model_paths = [Path(path).resolve() for path in args.model]
    ensemble = OpponentMultiTaskEnsemble.load(model_paths)
    if ensemble is None:
        raise SystemExit("failed to load multi-task model ensemble")
    selection_rows = _prepare_rows(_read(args.selection_data), ensemble)
    grid = []
    for margin in (float(value) for value in args.margin_grid.split(",") if value.strip()):
        for hand_weight in (
            float(value) for value in args.hand_weight_grid.split(",") if value.strip()
        ):
            for response_weight in (
                float(value) for value in args.response_weight_grid.split(",") if value.strip()
            ):
                config = {
                    "margin": margin,
                    "hand_weight": hand_weight,
                    "response_weight": response_weight,
                    "use_lower": True,
                }
                result = _evaluate_config(
                    selection_rows,
                    config,
                    bootstrap_samples=args.bootstrap_samples,
                    bootstrap_seed=args.bootstrap_seed,
                )
                result["eligible"] = (
                    result["overrides"] >= args.min_overrides
                    and result["override_hand_mean"] >= args.min_override_hand_mean
                )
                grid.append(result)
    eligible = [result for result in grid if result["eligible"]]
    if not eligible:
        raise SystemExit("no offline policy config met minimum override/safety constraints")
    selected = max(
        eligible,
        key=lambda result: (
            result["match_bootstrap_mean_ci"]["lower"],
            result["match_mean_per_opportunity"],
            -result["negative_override_rate"],
            -result["config"]["margin"],
        ),
    )

    def evaluate_optional(path: Path | None, config: dict[str, Any]):
        if path is None:
            return None
        rows = _prepare_rows(_read(path), ensemble)
        return _evaluate_config(
            rows,
            config,
            bootstrap_samples=args.bootstrap_samples,
            bootstrap_seed=args.bootstrap_seed,
        )

    calibration = evaluate_optional(args.calibration_data, selected["config"])
    held_out = evaluate_optional(args.held_out_data, selected["config"])
    no_response_config = dict(selected["config"], response_weight=0.0)
    mean_only_config = dict(selected["config"], use_lower=False)
    payload = {
        "schema_version": 1,
        "selection_used_held_out": False,
        "models": [
            {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in model_paths
        ],
        "selection_data": str(args.selection_data.resolve()),
        "grid": grid,
        "selected": selected,
        "calibration": calibration,
        "held_out": held_out,
        "ablations": {
            "no_response_held_out": evaluate_optional(
                args.held_out_data, no_response_config
            ),
            "mean_only_held_out": evaluate_optional(
                args.held_out_data, mean_only_config
            ),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps({
        "selected": selected,
        "calibration": calibration,
        "held_out": held_out,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
