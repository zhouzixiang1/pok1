#!/usr/bin/env python3
"""Select a value policy with an independent catastrophe-risk UCB gate."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from evaluate_multitask_offline_policy import (  # noqa: E402
    _calibration_gate,
    _evaluate_config,
    _prepare_rows,
    _read,
    select_offline_policy,
)
from opp_catastrophe_ensemble_runtime import (  # noqa: E402
    OpponentCatastropheEnsemble,
)
from opp_multitask_ensemble_runtime import OpponentMultiTaskEnsemble  # noqa: E402
from train_opponent_multitask_net import build_value_sample  # noqa: E402


def _float_grid(value: str, *, name: str) -> list[float]:
    try:
        values = sorted({float(item.strip()) for item in value.split(",") if item.strip()})
    except ValueError as exc:
        raise SystemExit(f"invalid {name}") from exc
    if not values or any(item < 0 for item in values):
        raise SystemExit(f"{name} must contain non-negative values")
    return values


def _file_manifest(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    return {
        "path": str(source.resolve()),
        "bytes": source.stat().st_size,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }


def _attach_risk(
    prepared: list[dict[str, Any]],
    raw_rows: list[dict[str, Any]],
    risk_ensemble: OpponentCatastropheEnsemble,
    *,
    max_hist: int,
) -> list[dict[str, Any]]:
    by_source = {int(row["source_row_index"]): row for row in prepared}
    for source_row_index, raw in enumerate(raw_rows):
        prepared_row = by_source.get(source_row_index)
        if prepared_row is None:
            continue
        sample = build_value_sample(raw, max_hist=max_hist)
        risk = risk_ensemble.predict(
            sample["state"],
            sample["profile"],
            sample["history"],
            sample["cross_hand"],
            sample["rule_id"],
            sample["cross_hand_sequence"],
        )
        if not risk:
            raise RuntimeError(
                f"catastrophe inference failed for source row {source_row_index}"
            )
        prepared_row["catastrophe"] = risk
        for candidate in prepared_row["candidates"]:
            action_id = int(candidate["label_id"])
            candidate["catastrophe_probability"] = float(
                risk["probability"][action_id]
            )
            candidate["catastrophe_probability_upper"] = float(
                risk["probability_upper"][action_id]
            )
            candidate["catastrophe_severity"] = float(
                risk["severity"][action_id]
            )
            candidate["catastrophe_expected_loss_upper"] = float(
                risk["expected_loss_upper"][action_id]
            )
    return prepared


def _filter_risk(
    rows: list[dict[str, Any]], *, max_probability: float
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    filtered = []
    removed = 0
    removed_by_action: dict[str, int] = {}
    for row in rows:
        retained = []
        for candidate in row["candidates"]:
            if candidate["catastrophe_probability_upper"] > max_probability:
                removed += 1
                label = str(candidate.get("label") or candidate["label_id"])
                removed_by_action[label] = removed_by_action.get(label, 0) + 1
            else:
                retained.append(dict(candidate))
        item = dict(row)
        item["candidates"] = retained
        filtered.append(item)
    return filtered, {
        "max_catastrophe_probability_upper": float(max_probability),
        "input_candidates": sum(len(row["candidates"]) for row in rows),
        "retained_candidates": sum(len(row["candidates"]) for row in filtered),
        "removed_candidates": removed,
        "removed_by_action": dict(sorted(removed_by_action.items())),
    }


def _annotate_selected_risk(
    result: dict[str, Any] | None,
    rows: list[dict[str, Any]],
    *,
    catastrophe_threshold: float,
) -> dict[str, Any] | None:
    if result is None:
        return None
    lookup = {}
    for row in rows:
        for candidate in row["candidates"]:
            lookup[(
                int(row["source_row_index"]), int(candidate["label_id"])
            )] = candidate
    probabilities = []
    expected_losses = []
    observed_catastrophes = 0
    for trace in result.get("override_trace") or []:
        key = (
            int(trace["source_row_index"]),
            int((trace.get("candidate") or {})["label_id"]),
        )
        candidate = lookup.get(key)
        if candidate is None:
            continue
        probability = float(candidate["catastrophe_probability_upper"])
        expected_loss = float(candidate["catastrophe_expected_loss_upper"])
        probabilities.append(probability)
        expected_losses.append(expected_loss)
        hand_delta = float((trace.get("observed") or {}).get("hand_delta", 0.0))
        observed_catastrophes += int(hand_delta <= -catastrophe_threshold)
        trace["prediction"]["catastrophe_probability_upper"] = probability
        trace["prediction"]["catastrophe_expected_loss_upper"] = expected_loss
    result["catastrophe_risk"] = {
        "selected_predictions": len(probabilities),
        "observed_catastrophes": observed_catastrophes,
        "max_probability_upper": max(probabilities) if probabilities else 0.0,
        "mean_probability_upper": (
            sum(probabilities) / len(probabilities) if probabilities else 0.0
        ),
        "max_expected_loss_upper": max(expected_losses) if expected_losses else 0.0,
    }
    return result


def _select_for_threshold(
    rows: list[dict[str, Any]],
    *,
    max_probability: float,
    margins: list[float],
    hand_weights: list[float],
    tail_weights: list[float],
    response_weights: list[float],
    args: argparse.Namespace,
    catastrophe_threshold: float,
) -> dict[str, Any]:
    filtered, filter_report = _filter_risk(
        rows, max_probability=max_probability
    )
    selection = select_offline_policy(
        filtered,
        margins=margins,
        hand_weights=hand_weights,
        tail_weights=tail_weights,
        response_weights=response_weights,
        min_match_weight=args.min_match_weight,
        min_hand_lcb=args.min_hand_lcb,
        min_overrides=args.min_overrides,
        min_selection_clusters=args.min_selection_clusters,
        min_override_clusters=args.min_override_clusters,
        min_overrides_per_opponent=args.min_overrides_per_opponent,
        min_override_hand_mean=args.min_override_hand_mean,
        require_nonnegative_opponent_mean=(
            not args.allow_negative_selection_opponent
        ),
        bootstrap_samples=args.bootstrap_samples,
        bootstrap_seed=args.bootstrap_seed,
        min_cluster_ci_lower=args.min_selection_ci_lower,
        min_opponent_stratified_ci_lower=args.min_selection_ci_lower,
    )
    for grid_row in selection["grid"]:
        _annotate_selected_risk(
            grid_row,
            filtered,
            catastrophe_threshold=catastrophe_threshold,
        )
    selection["selected"] = _annotate_selected_risk(
        selection["selected"],
        filtered,
        catastrophe_threshold=catastrophe_threshold,
    )
    return {
        "max_catastrophe_probability_upper": float(max_probability),
        "filter": filter_report,
        "selection": selection,
    }


def _selection_key(row: dict[str, Any]) -> tuple[float, ...]:
    selected = row["selection"]["selected"]
    if selected is None:
        return (float("-inf"),)
    return (
        float(selected["match_opponent_stratified_cluster_ci"]["lower"]),
        float(selected["match_cluster_bootstrap_mean_ci"]["lower"]),
        float(selected["match_mean_per_opportunity"]),
        float(selected["override_clusters"]),
        -float(selected["negative_override_rate"]),
    )


def _evaluate_frozen(
    raw_rows: list[dict[str, Any]],
    base_ensemble: OpponentMultiTaskEnsemble,
    risk_ensemble: OpponentCatastropheEnsemble,
    *,
    max_probability: float,
    policy_config: dict[str, Any],
    args: argparse.Namespace,
    catastrophe_threshold: float,
) -> dict[str, Any]:
    prepared = _prepare_rows(raw_rows, base_ensemble)
    prepared = _attach_risk(
        prepared,
        raw_rows,
        risk_ensemble,
        max_hist=base_ensemble.max_hist,
    )
    filtered, filter_report = _filter_risk(
        prepared, max_probability=max_probability
    )
    result = _evaluate_config(
        filtered,
        copy.deepcopy(policy_config),
        bootstrap_samples=args.bootstrap_samples,
        bootstrap_seed=args.bootstrap_seed,
    )
    _annotate_selected_risk(
        result,
        filtered,
        catastrophe_threshold=catastrophe_threshold,
    )
    return {"filter": filter_report, "result": result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-model", action="append", required=True)
    parser.add_argument("--risk-head", action="append", required=True)
    parser.add_argument("--selection-data", required=True, type=Path)
    parser.add_argument("--calibration-data", type=Path)
    parser.add_argument("--held-out-data", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--max-catastrophe-probability-grid", default="0.05,0.1,0.2,0.3"
    )
    parser.add_argument("--margin-grid", default="0,25,50,100,200,400")
    parser.add_argument("--hand-weight-grid", default="0.25,0.5,0.75,1")
    parser.add_argument("--tail-weight-grid", default="0,0.25")
    parser.add_argument("--response-weight-grid", default="0,0.05,0.1")
    parser.add_argument("--min-match-weight", type=float, default=0.25)
    parser.add_argument("--min-hand-lcb", type=float, default=0.0)
    parser.add_argument("--min-overrides", type=int, default=10)
    parser.add_argument("--min-selection-clusters", type=int, default=8)
    parser.add_argument("--min-override-clusters", type=int, default=8)
    parser.add_argument("--min-overrides-per-opponent", type=int, default=2)
    parser.add_argument("--min-override-hand-mean", type=float, default=0.0)
    parser.add_argument("--min-selection-ci-lower", type=float, default=0.0)
    parser.add_argument("--allow-negative-selection-opponent", action="store_true")
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260710)
    parser.add_argument("--min-calibration-overrides", type=int, default=5)
    parser.add_argument("--min-calibration-override-clusters", type=int, default=3)
    parser.add_argument("--min-calibration-ci-lower", type=float, default=0.0)
    parser.add_argument("--min-held-out-overrides", type=int, default=10)
    parser.add_argument("--min-held-out-override-clusters", type=int, default=8)
    parser.add_argument("--min-held-out-ci-lower", type=float, default=0.0)
    args = parser.parse_args(argv)
    if len(args.base_model) != len(args.risk_head):
        raise SystemExit("base model and risk head counts differ")
    if args.bootstrap_samples <= 0:
        raise SystemExit("bootstrap samples must be positive")

    base_ensemble = OpponentMultiTaskEnsemble.load(args.base_model)
    risk_ensemble = OpponentCatastropheEnsemble.from_paths(
        args.base_model, args.risk_head
    )
    if base_ensemble is None or risk_ensemble is None:
        raise SystemExit("failed to load a hash-aligned model/risk ensemble")
    thresholds = _float_grid(
        args.max_catastrophe_probability_grid,
        name="catastrophe probability grid",
    )
    if any(threshold > 1 for threshold in thresholds):
        raise SystemExit("catastrophe probabilities must be in [0, 1]")
    margins = _float_grid(args.margin_grid, name="margin grid")
    hand_weights = _float_grid(args.hand_weight_grid, name="hand weight grid")
    tail_weights = _float_grid(args.tail_weight_grid, name="tail weight grid")
    response_weights = _float_grid(
        args.response_weight_grid, name="response weight grid"
    )
    catastrophe_thresholds = {
        float(member[1].catastrophe_threshold)
        for member in risk_ensemble.members
    }
    if len(catastrophe_thresholds) != 1:
        raise SystemExit("risk heads use different catastrophe thresholds")
    catastrophe_threshold = catastrophe_thresholds.pop()

    raw_selection = _read(args.selection_data)
    prepared = _prepare_rows(raw_selection, base_ensemble)
    prepared = _attach_risk(
        prepared,
        raw_selection,
        risk_ensemble,
        max_hist=base_ensemble.max_hist,
    )
    threshold_rows = [
        _select_for_threshold(
            prepared,
            max_probability=threshold,
            margins=margins,
            hand_weights=hand_weights,
            tail_weights=tail_weights,
            response_weights=response_weights,
            args=args,
            catastrophe_threshold=catastrophe_threshold,
        )
        for threshold in thresholds
    ]
    eligible = [
        row for row in threshold_rows if row["selection"]["selected"] is not None
    ]
    selected_threshold = max(eligible, key=_selection_key) if eligible else None
    post_selection = None
    if selected_threshold is not None:
        max_probability = selected_threshold[
            "max_catastrophe_probability_upper"
        ]
        policy_config = copy.deepcopy(
            selected_threshold["selection"]["selected"]["config"]
        )
        calibration = None
        calibration_gate = None
        if args.calibration_data is not None:
            calibration = _evaluate_frozen(
                _read(args.calibration_data),
                base_ensemble,
                risk_ensemble,
                max_probability=max_probability,
                policy_config=policy_config,
                args=args,
                catastrophe_threshold=catastrophe_threshold,
            )
            calibration_gate = _calibration_gate(
                calibration["result"],
                min_overrides=args.min_calibration_overrides,
                min_override_clusters=args.min_calibration_override_clusters,
                require_nonnegative_opponent_mean=True,
                min_cluster_ci_lower=args.min_calibration_ci_lower,
                min_opponent_stratified_ci_lower=args.min_calibration_ci_lower,
            )
        held_out = None
        held_out_gate = None
        if args.held_out_data is not None:
            held_out = _evaluate_frozen(
                _read(args.held_out_data),
                base_ensemble,
                risk_ensemble,
                max_probability=max_probability,
                policy_config=policy_config,
                args=args,
                catastrophe_threshold=catastrophe_threshold,
            )
            held_out_gate = _calibration_gate(
                held_out["result"],
                min_overrides=args.min_held_out_overrides,
                min_override_clusters=args.min_held_out_override_clusters,
                require_nonnegative_opponent_mean=True,
                min_cluster_ci_lower=args.min_held_out_ci_lower,
                min_opponent_stratified_ci_lower=args.min_held_out_ci_lower,
            )
        gates = [gate for gate in (calibration_gate, held_out_gate) if gate]
        post_selection = {
            "max_catastrophe_probability_upper": max_probability,
            "policy_config_frozen_before_post_selection": policy_config,
            "calibration": calibration,
            "calibration_gate": calibration_gate,
            "held_out": held_out,
            "held_out_gate": held_out_gate,
            "passed": (
                calibration_gate is not None
                and held_out_gate is not None
                and all(gate["passed"] for gate in gates)
            ),
        }

    payload = {
        "format": "catastrophe_policy_ab_v1",
        "catastrophe_threshold": catastrophe_threshold,
        "model_manifests": {
            "base": [_file_manifest(path) for path in args.base_model],
            "risk": [_file_manifest(path) for path in args.risk_head],
        },
        "data_manifests": {
            "selection": _file_manifest(args.selection_data),
            "calibration": (
                _file_manifest(args.calibration_data)
                if args.calibration_data is not None else None
            ),
            "held_out": (
                _file_manifest(args.held_out_data)
                if args.held_out_data is not None else None
            ),
        },
        "selection_requirements": {
            "min_overrides": args.min_overrides,
            "min_selection_clusters": args.min_selection_clusters,
            "min_override_clusters": args.min_override_clusters,
            "min_overrides_per_opponent": args.min_overrides_per_opponent,
            "min_override_hand_mean": args.min_override_hand_mean,
            "min_selection_ci_lower": args.min_selection_ci_lower,
            "min_hand_lcb": args.min_hand_lcb,
            "min_match_weight": args.min_match_weight,
            "bootstrap_samples": args.bootstrap_samples,
        },
        "threshold_grid": threshold_rows,
        "selected": selected_threshold,
        "post_selection": post_selection,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps({
        "output": str(args.output),
        "selected_probability": (
            selected_threshold["max_catastrophe_probability_upper"]
            if selected_threshold else None
        ),
        "post_selection_passed": (
            post_selection["passed"] if post_selection else None
        ),
    }, sort_keys=True))
    return 0 if selected_threshold is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
