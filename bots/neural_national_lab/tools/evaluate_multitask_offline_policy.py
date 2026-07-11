#!/usr/bin/env python3
"""Screen neural overrides using single-decision counterfactual action uplift."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import random
import sys
from typing import Any


TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from feature_spec import LABELS  # noqa: E402
from match_outcome_schema import (  # noqa: E402
    MATCH_OUTCOME_ESTIMAND,
    MATCH_OUTCOME_SCHEMA,
    candidate_outcome,
    derive_match_outcome_supervision,
    policy_outcome_context,
)
from opp_multitask_ensemble_runtime import OpponentMultiTaskEnsemble  # noqa: E402
from sampling_weights import decision_sampling_weight  # noqa: E402
from train_opponent_multitask_net import (  # noqa: E402
    _hero_action_features,
    build_value_sample,
)


OFFLINE_ESTIMAND = "single_decision_action_uplift_ipw_v3_win_first_70_hand"


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


def _weighted_mean(values: list[float], weights: list[float]) -> float:
    if len(values) != len(weights):
        raise ValueError("value/weight lengths differ")
    total_weight = sum(weights)
    if not values or total_weight <= 0.0:
        return 0.0
    return sum(value * weight for value, weight in zip(values, weights)) / total_weight


def _effective_sample_size(weights: list[float]) -> float:
    total = sum(weights)
    squared = sum(weight * weight for weight in weights)
    return total * total / squared if squared > 0.0 else 0.0


def _weighted_pairs(values: list[Any]) -> list[tuple[float, float]]:
    pairs = []
    for item in values:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            value, weight = float(item[0]), float(item[1])
        else:
            value, weight = float(item), 1.0
        if not math.isfinite(value) or not math.isfinite(weight) or weight <= 0.0:
            raise ValueError("bootstrap values and weights must be finite and positive")
        pairs.append((value, weight))
    return pairs


def _bootstrap_mean_ci(
    values: list[float],
    *,
    weights: list[float] | None = None,
    samples: int,
    seed: int,
) -> dict[str, float]:
    if not values:
        return {"lower": 0.0, "mean": 0.0, "upper": 0.0}
    weights = list(weights) if weights is not None else [1.0] * len(values)
    pairs = _weighted_pairs(list(zip(values, weights)))
    rng = random.Random(seed)
    n = len(pairs)
    means = []
    for _ in range(max(1, samples)):
        selected = [pairs[rng.randrange(n)] for _ in range(n)]
        means.append(_weighted_mean(
            [value for value, _ in selected],
            [weight for _, weight in selected],
        ))
    return {
        "lower": _percentile(means, 0.025),
        "mean": _weighted_mean(values, weights),
        "upper": _percentile(means, 0.975),
    }


def _cluster_bootstrap_mean_ci(
    clusters: dict[str, list[Any]],
    *,
    samples: int,
    seed: int,
) -> dict[str, float]:
    nonempty = [_weighted_pairs(values) for values in clusters.values() if values]
    if not nonempty:
        return {"lower": 0.0, "mean": 0.0, "upper": 0.0}
    observed = [pair for values in nonempty for pair in values]
    rng = random.Random(seed)
    cluster_count = len(nonempty)
    means = []
    for _ in range(max(1, samples)):
        sampled = [
            nonempty[rng.randrange(cluster_count)] for _ in range(cluster_count)
        ]
        pairs = [pair for cluster in sampled for pair in cluster]
        means.append(_weighted_mean(
            [value for value, _ in pairs],
            [weight for _, weight in pairs],
        ))
    return {
        "lower": _percentile(means, 0.025),
        "mean": _weighted_mean(
            [value for value, _ in observed],
            [weight for _, weight in observed],
        ),
        "upper": _percentile(means, 0.975),
    }


def _opponent_stratified_cluster_ci(
    clusters: dict[str, dict[str, list[Any]]],
    *,
    samples: int,
    seed: int,
) -> dict[str, float]:
    usable = {
        opponent: [
            _weighted_pairs(values)
            for values in opponent_clusters.values()
            if values
        ]
        for opponent, opponent_clusters in clusters.items()
    }
    usable = {opponent: values for opponent, values in usable.items() if values}
    if not usable:
        return {"lower": 0.0, "mean": 0.0, "upper": 0.0}
    observed = [
        pair
        for opponent_clusters in usable.values()
        for cluster in opponent_clusters
        for pair in cluster
    ]
    rng = random.Random(seed)
    means = []
    for _ in range(max(1, samples)):
        pairs = []
        for opponent_clusters in usable.values():
            count = len(opponent_clusters)
            for _ in range(count):
                pairs.extend(opponent_clusters[rng.randrange(count)])
        means.append(_weighted_mean(
            [value for value, _ in pairs],
            [weight for _, weight in pairs],
        ))
    return {
        "lower": _percentile(means, 0.025),
        "mean": _weighted_mean(
            [value for value, _ in observed],
            [weight for _, weight in observed],
        ),
        "upper": _percentile(means, 0.975),
    }


def _cluster_point_values(
    clusters: dict[str, list[Any]],
) -> list[float]:
    points = []
    for values in clusters.values():
        pairs = _weighted_pairs(values)
        if pairs:
            points.append(_weighted_mean(
                [value for value, _ in pairs],
                [weight for _, weight in pairs],
            ))
    return points


def _equal_cluster_bootstrap_mean_ci(
    clusters: dict[str, list[Any]],
    *,
    samples: int,
    seed: int,
) -> dict[str, float]:
    """Bootstrap one point per 70-hand match, not one point per decision."""
    points = _cluster_point_values(clusters)
    if not points:
        return {"lower": 0.0, "mean": 0.0, "upper": 0.0}
    rng = random.Random(seed)
    count = len(points)
    means = []
    for _ in range(max(1, samples)):
        means.append(sum(points[rng.randrange(count)] for _ in range(count)) / count)
    return {
        "lower": _percentile(means, 0.025),
        "mean": sum(points) / count,
        "upper": _percentile(means, 0.975),
    }


def _opponent_stratified_equal_cluster_ci(
    clusters: dict[str, dict[str, list[Any]]],
    *,
    samples: int,
    seed: int,
) -> dict[str, float]:
    """Resample 70-hand matches within opponent strata with equal match weight."""
    usable = {
        opponent: _cluster_point_values(opponent_clusters)
        for opponent, opponent_clusters in clusters.items()
    }
    usable = {opponent: values for opponent, values in usable.items() if values}
    if not usable:
        return {"lower": 0.0, "mean": 0.0, "upper": 0.0}
    observed = [value for values in usable.values() for value in values]
    rng = random.Random(seed)
    means = []
    for _ in range(max(1, samples)):
        sampled = []
        for values in usable.values():
            sampled.extend(values[rng.randrange(len(values))] for _ in values)
        means.append(sum(sampled) / len(sampled))
    return {
        "lower": _percentile(means, 0.025),
        "mean": sum(observed) / len(observed),
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
    raw_rows: list[dict[str, Any]],
    ensemble: OpponentMultiTaskEnsemble,
    *,
    require_ipw: bool = True,
    require_match_outcome: bool = False,
) -> list[dict[str, Any]]:
    prepared = []
    for source_row_index, raw in enumerate(raw_rows):
        match_outcome_supervision = derive_match_outcome_supervision(
            raw, required=require_match_outcome
        )
        match_outcome = (
            policy_outcome_context(match_outcome_supervision)
            if match_outcome_supervision is not None else None
        )
        sample = build_value_sample(raw, max_hist=ensemble.max_hist)
        rule_id = int(sample.get("rule_id", 1) or 0)
        values = ensemble.predict_values(
            sample["state"],
            sample["profile"],
            sample["history"],
            sample["cross_hand"],
            rule_id,
            sample["cross_hand_sequence"],
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
                    sample["cross_hand_sequence"],
                )
                if response:
                    response_signal = _response_signal(response, raw, action)
            candidate = {
                "label_id": label_id,
                "label": label_name,
                "action": action,
                "response_signal": response_signal,
                "hand_delta": float(probe["delta_vs_rule"]),
                "tail_delta": float(probe["tail_delta_vs_rule"]),
                "match_delta": float(probe["match_delta_vs_rule"]),
            }
            if match_outcome_supervision is not None:
                candidate.update(candidate_outcome(
                    match_outcome_supervision, label_id
                ))
            candidates.append(candidate)
        if candidates:
            opponent = str(raw.get("_opponent_label") or raw.get("opponent"))
            has_sampling_evidence = any(
                key in raw for key in (
                    "decision_inclusion_probability",
                    "decision_inverse_probability_weight",
                    "eligible_decisions",
                    "selected_decisions",
                )
            )
            if require_ipw or has_sampling_evidence:
                sampling_weight = decision_sampling_weight(raw)
            else:
                sampling_weight = 1.0
            prepared.append({
                "source_row_index": source_row_index,
                "opponent": opponent,
                "cluster": "|".join((
                    opponent,
                    str(raw.get("deck_seed_base")),
                    str(raw.get("bot_seed_base")),
                )),
                "rule_id": rule_id,
                "sampling_weight": sampling_weight,
                "match_outcome": match_outcome,
                "decision": {
                    key: raw.get(key)
                    for key in (
                        "deck_seed_base",
                        "bot_seed_base",
                        "hand",
                        "stage",
                        "hand_decision_index",
                        "decision_serial",
                        "rule_label",
                        "rule_final",
                        "rule_value",
                    )
                },
                "values": values,
                "candidates": candidates,
            })
    return prepared


def _match_outcome_observation(
    row: dict[str, Any], candidate: dict[str, Any] | None
) -> dict[str, float | int] | None:
    raw = row.get("match_outcome")
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get("schema") != MATCH_OUTCOME_SCHEMA:
        raise ValueError("policy row has invalid match outcome context")
    if raw.get("estimand") != MATCH_OUTCOME_ESTIMAND or int(
        raw.get("hands", 0) or 0
    ) != 70:
        raise ValueError("policy row does not describe a 70-hand outcome")
    baseline_net = float(raw.get("baseline_match_net_chips"))
    baseline_positive = int(raw.get("baseline_match_positive"))
    if (
        not math.isfinite(baseline_net)
        or baseline_positive not in (0, 1)
        or baseline_positive != int(baseline_net > 0.0)
    ):
        raise ValueError("policy row has invalid baseline match outcome")
    if candidate is None:
        candidate_net = baseline_net
        candidate_positive = baseline_positive
        uplift = 0
    else:
        if candidate.get("match_outcome_schema") != MATCH_OUTCOME_SCHEMA:
            raise ValueError("selected candidate lacks 70-hand outcome evidence")
        candidate_net = float(candidate.get("forced_match_net_chips"))
        candidate_positive = int(candidate.get("forced_match_positive"))
        uplift = int(candidate.get("match_positive_uplift"))
        if (
            not math.isfinite(candidate_net)
            or candidate_positive not in (0, 1)
            or candidate_positive != int(candidate_net > 0.0)
            or uplift != candidate_positive - baseline_positive
        ):
            raise ValueError("selected candidate has invalid 70-hand outcome evidence")
    return {
        "baseline_match_net_chips": baseline_net,
        "forced_match_net_chips": candidate_net,
        "baseline_match_positive": baseline_positive,
        "forced_match_positive": candidate_positive,
        "match_positive_uplift": uplift,
    }


def _evaluate_config(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    bootstrap_samples: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    deltas = []
    sampling_weights = []
    selected = []
    selected_weights = []
    override_trace = []
    by_opponent: dict[str, list[tuple[float, float]]] = {}
    by_cluster: dict[str, list[tuple[float, float]]] = {}
    by_opponent_cluster: dict[str, dict[str, list[tuple[float, float]]]] = {}
    override_clusters: set[str] = set()
    override_opponents: set[str] = set()
    override_by_action = Counter()
    opponent_override_counts = Counter()
    opponent_negative_overrides = Counter()
    opponent_override_clusters: dict[str, set[str]] = {}
    outcome_rows = 0
    outcome_weights = []
    outcome_clusters: set[str] = set()
    outcome_baseline_by_cluster: dict[str, float] = {}
    positive_by_cluster: dict[str, list[tuple[float, float]]] = {}
    rule_positive_by_cluster: dict[str, list[tuple[float, float]]] = {}
    positive_uplift_by_cluster: dict[str, list[tuple[float, float]]] = {}
    positive_by_opponent_cluster: dict[
        str, dict[str, list[tuple[float, float]]]
    ] = {}
    rule_positive_by_opponent_cluster: dict[
        str, dict[str, list[tuple[float, float]]]
    ] = {}
    positive_uplift_by_opponent_cluster: dict[
        str, dict[str, list[tuple[float, float]]]
    ] = {}
    outcome_flip_counts = Counter()
    hand_weight = float(config["hand_weight"])
    tail_weight = float(config.get("tail_weight", 0.0))
    match_weight = float(
        config.get("match_weight", 1.0 - hand_weight - tail_weight)
    )
    if min(hand_weight, tail_weight, match_weight) < -1e-9:
        raise ValueError("policy value weights must be non-negative")
    response_weight = float(config["response_weight"])
    margin = float(config["margin"])
    min_hand_lcb = config.get("min_hand_lcb")
    if min_hand_lcb is not None:
        min_hand_lcb = float(min_hand_lcb)
    use_lower = bool(config.get("use_lower", True))
    value_key = "lower" if use_lower else "mean"
    for row_index, row in enumerate(rows):
        opponent = row["opponent"]
        cluster = str(row.get("cluster") or f"row:{row_index}")
        sampling_weight = float(row.get("sampling_weight", 1.0))
        if not math.isfinite(sampling_weight) or sampling_weight <= 0.0:
            raise ValueError("sampling_weight must be finite and positive")
        best = None
        for candidate in row["candidates"]:
            label_id = candidate["label_id"]
            hand = row["values"]["delta_vs_rule"][value_key][label_id]
            tail = row["values"]["tail_delta_vs_rule"][value_key][label_id]
            match = row["values"]["match_delta_vs_rule"][value_key][label_id]
            if min_hand_lcb is not None and float(hand) < min_hand_lcb:
                continue
            score = (
                hand_weight * float(hand)
                + tail_weight * float(tail)
                + match_weight * float(match)
                + response_weight * float(candidate["response_signal"])
            )
            if best is None or score > best[0]:
                best = (
                    score, candidate, float(hand), float(tail), float(match)
                )
        chosen = None
        if best is not None and best[0] > margin:
            candidate = best[1]
            delta = float(candidate["match_delta"])
            selected.append(candidate)
            selected_weights.append(sampling_weight)
            chosen = candidate
        else:
            delta = 0.0
        outcome = _match_outcome_observation(row, chosen)
        if chosen is not None:
            observed = {
                "hand_delta": chosen["hand_delta"],
                "tail_delta": chosen["tail_delta"],
                "match_delta": chosen["match_delta"],
            }
            if outcome is not None:
                observed.update(outcome)
            override_trace.append({
                "source_row_index": row.get("source_row_index", row_index),
                "opponent": opponent,
                "cluster": cluster,
                "sampling_weight": sampling_weight,
                "decision": row.get("decision") or {},
                "rule_id": row["rule_id"],
                "candidate": {
                    "label_id": chosen["label_id"],
                    "label": chosen.get("label"),
                    "action": chosen.get("action"),
                },
                "prediction": {
                    "value_key": value_key,
                    "hand": best[2],
                    "tail": best[3],
                    "match": best[4],
                    "hand_weight": hand_weight,
                    "tail_weight": tail_weight,
                    "match_weight": match_weight,
                    "response_signal": chosen["response_signal"],
                    "policy_score": best[0],
                },
                "observed": observed,
            })
        deltas.append(delta)
        sampling_weights.append(sampling_weight)
        pair = (delta, sampling_weight)
        by_opponent.setdefault(opponent, []).append(pair)
        by_cluster.setdefault(cluster, []).append(pair)
        by_opponent_cluster.setdefault(opponent, {}).setdefault(
            cluster, []
        ).append(pair)
        if outcome is not None:
            baseline_net = float(outcome["baseline_match_net_chips"])
            previous_baseline = outcome_baseline_by_cluster.setdefault(
                cluster, baseline_net
            )
            if not math.isclose(
                previous_baseline, baseline_net, rel_tol=0.0, abs_tol=1e-6
            ):
                raise ValueError("one match cluster has inconsistent baseline chips")
            baseline_positive = float(outcome["baseline_match_positive"])
            candidate_positive = float(outcome["forced_match_positive"])
            positive_uplift = float(outcome["match_positive_uplift"])
            outcome_rows += 1
            outcome_weights.append(sampling_weight)
            outcome_clusters.add(cluster)
            positive_pair = (candidate_positive, sampling_weight)
            rule_pair = (baseline_positive, sampling_weight)
            uplift_pair = (positive_uplift, sampling_weight)
            positive_by_cluster.setdefault(cluster, []).append(positive_pair)
            rule_positive_by_cluster.setdefault(cluster, []).append(rule_pair)
            positive_uplift_by_cluster.setdefault(cluster, []).append(uplift_pair)
            positive_by_opponent_cluster.setdefault(opponent, {}).setdefault(
                cluster, []
            ).append(positive_pair)
            rule_positive_by_opponent_cluster.setdefault(
                opponent, {}
            ).setdefault(cluster, []).append(rule_pair)
            positive_uplift_by_opponent_cluster.setdefault(
                opponent, {}
            ).setdefault(cluster, []).append(uplift_pair)
            if baseline_positive == 0.0 and candidate_positive == 1.0:
                outcome_flip_counts["negative_to_positive"] += 1
            elif baseline_positive == 1.0 and candidate_positive == 0.0:
                outcome_flip_counts["positive_to_negative"] += 1
            elif candidate_positive == 1.0:
                outcome_flip_counts["unchanged_positive"] += 1
            else:
                outcome_flip_counts["unchanged_nonpositive"] += 1
        if chosen is not None:
            override_clusters.add(cluster)
            override_opponents.add(opponent)
            opponent_override_counts[opponent] += 1
            opponent_override_clusters.setdefault(opponent, set()).add(cluster)
            if delta < 0:
                opponent_negative_overrides[opponent] += 1
            label = str(chosen.get("label") or LABELS[chosen["label_id"]])
            override_by_action[label] += 1
    override_match = [candidate["match_delta"] for candidate in selected]
    override_hand = [candidate["hand_delta"] for candidate in selected]
    override_tail = [candidate["tail_delta"] for candidate in selected]
    ci = _bootstrap_mean_ci(
        deltas,
        weights=sampling_weights,
        samples=bootstrap_samples,
        seed=bootstrap_seed,
    )
    cluster_ci = _cluster_bootstrap_mean_ci(
        by_cluster, samples=bootstrap_samples, seed=bootstrap_seed + 1
    )
    stratified_cluster_ci = _opponent_stratified_cluster_ci(
        by_opponent_cluster,
        samples=bootstrap_samples,
        seed=bootstrap_seed + 2,
    )
    positive_cluster_ci = _equal_cluster_bootstrap_mean_ci(
        positive_by_cluster,
        samples=bootstrap_samples,
        seed=bootstrap_seed + 3,
    )
    positive_stratified_ci = _opponent_stratified_equal_cluster_ci(
        positive_by_opponent_cluster,
        samples=bootstrap_samples,
        seed=bootstrap_seed + 4,
    )
    rule_positive_cluster_ci = _equal_cluster_bootstrap_mean_ci(
        rule_positive_by_cluster,
        samples=bootstrap_samples,
        seed=bootstrap_seed + 5,
    )
    rule_positive_stratified_ci = _opponent_stratified_equal_cluster_ci(
        rule_positive_by_opponent_cluster,
        samples=bootstrap_samples,
        seed=bootstrap_seed + 6,
    )
    positive_uplift_cluster_ci = _equal_cluster_bootstrap_mean_ci(
        positive_uplift_by_cluster,
        samples=bootstrap_samples,
        seed=bootstrap_seed + 7,
    )
    positive_uplift_stratified_ci = _opponent_stratified_equal_cluster_ci(
        positive_uplift_by_opponent_cluster,
        samples=bootstrap_samples,
        seed=bootstrap_seed + 8,
    )
    outcome_by_opponent = {}
    for opponent in sorted(by_opponent):
        candidate_points = _cluster_point_values(
            positive_by_opponent_cluster.get(opponent, {})
        )
        rule_points = _cluster_point_values(
            rule_positive_by_opponent_cluster.get(opponent, {})
        )
        uplift_points = _cluster_point_values(
            positive_uplift_by_opponent_cluster.get(opponent, {})
        )
        outcome_by_opponent[opponent] = {
            "match_outcome_clusters": len(candidate_points),
            "match_positive_rate": (
                sum(candidate_points) / len(candidate_points)
                if candidate_points else 0.0
            ),
            "rule_match_positive_rate": (
                sum(rule_points) / len(rule_points) if rule_points else 0.0
            ),
            "match_positive_uplift_mean": (
                sum(uplift_points) / len(uplift_points)
                if uplift_points else 0.0
            ),
        }
    return {
        "estimand": OFFLINE_ESTIMAND,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "config": config,
        "rows": len(rows),
        "overrides": len(selected),
        "override_rate": len(selected) / len(rows) if rows else 0.0,
        "weighted_override_rate": (
            sum(selected_weights) / sum(sampling_weights)
            if sampling_weights else 0.0
        ),
        "match_total": sum(
            delta * weight for delta, weight in zip(deltas, sampling_weights)
        ),
        "sample_match_total": sum(deltas),
        "match_mean_per_opportunity": _weighted_mean(deltas, sampling_weights),
        "estimated_opportunities": sum(sampling_weights),
        "sampling_effective_sample_size": _effective_sample_size(
            sampling_weights
        ),
        "sampling_weight_contract": "uniform_decision_inverse_probability_v1",
        "match_bootstrap_mean_ci": ci,
        "match_cluster_bootstrap_mean_ci": cluster_ci,
        "match_opponent_stratified_cluster_ci": stratified_cluster_ci,
        "match_outcome_estimand": MATCH_OUTCOME_ESTIMAND,
        "match_outcome_rows": outcome_rows,
        "match_outcome_clusters": len(outcome_clusters),
        "match_outcome_row_coverage": (
            outcome_rows / len(rows) if rows else 0.0
        ),
        "match_outcome_cluster_coverage": (
            len(outcome_clusters) / len(by_cluster) if by_cluster else 0.0
        ),
        "match_outcome_estimated_opportunities": sum(outcome_weights),
        "match_positive_rate": positive_cluster_ci["mean"],
        "rule_match_positive_rate": rule_positive_cluster_ci["mean"],
        "match_positive_uplift_mean": positive_uplift_cluster_ci["mean"],
        "match_positive_rate_cluster_bootstrap_ci": positive_cluster_ci,
        "match_positive_rate_opponent_stratified_cluster_ci": (
            positive_stratified_ci
        ),
        "rule_match_positive_rate_cluster_bootstrap_ci": (
            rule_positive_cluster_ci
        ),
        "rule_match_positive_rate_opponent_stratified_cluster_ci": (
            rule_positive_stratified_ci
        ),
        "match_positive_uplift_cluster_bootstrap_ci": (
            positive_uplift_cluster_ci
        ),
        "match_positive_uplift_opponent_stratified_cluster_ci": (
            positive_uplift_stratified_ci
        ),
        "match_positive_flip_counts": {
            key: outcome_flip_counts[key]
            for key in (
                "negative_to_positive",
                "positive_to_negative",
                "unchanged_positive",
                "unchanged_nonpositive",
            )
        },
        "match_clusters": len(by_cluster),
        "override_clusters": len(override_clusters),
        "override_opponents": len(override_opponents),
        "override_opponent_names": sorted(override_opponents),
        "overrides_by_action": dict(sorted(override_by_action.items())),
        "override_trace": override_trace,
        "override_match_mean": _weighted_mean(override_match, selected_weights),
        "override_hand_mean": _weighted_mean(override_hand, selected_weights),
        "override_tail_mean": _weighted_mean(override_tail, selected_weights),
        "negative_override_rate": (
            sum(
                weight for value, weight in zip(override_match, selected_weights)
                if value < 0
            ) / sum(selected_weights)
            if override_match else 0.0
        ),
        "worst_override_match": min(override_match) if override_match else 0.0,
        "by_opponent": {
            opponent: {
                "rows": len(values),
                "clusters": len(by_opponent_cluster.get(opponent, {})),
                "overrides": opponent_override_counts[opponent],
                "override_clusters": len(
                    opponent_override_clusters.get(opponent, set())
                ),
                "negative_overrides": opponent_negative_overrides[opponent],
                "estimated_opportunities": sum(weight for _, weight in values),
                "total": sum(value * weight for value, weight in values),
                "mean": _weighted_mean(
                    [value for value, _ in values],
                    [weight for _, weight in values],
                ),
                **outcome_by_opponent[opponent],
            }
            for opponent, values in sorted(by_opponent.items())
        },
    }


def _win_first_errors(
    result: dict[str, Any],
    *,
    min_match_positive_rate_ci_lower: float,
    min_match_positive_uplift_ci_lower: float,
    min_opponent_match_positive_rate: float,
) -> list[str]:
    errors = []
    if result.get("match_outcome_row_coverage", 0.0) < 1.0:
        errors.append("match_outcome_row_coverage<1.0")
    if result.get("match_outcome_cluster_coverage", 0.0) < 1.0:
        errors.append("match_outcome_cluster_coverage<1.0")
    for field in (
        "match_positive_rate_cluster_bootstrap_ci",
        "match_positive_rate_opponent_stratified_cluster_ci",
    ):
        lower = float((result.get(field) or {}).get("lower", 0.0))
        if lower <= min_match_positive_rate_ci_lower:
            errors.append(
                f"{field}_lower<={min_match_positive_rate_ci_lower}"
            )
    for field in (
        "match_positive_uplift_cluster_bootstrap_ci",
        "match_positive_uplift_opponent_stratified_cluster_ci",
    ):
        lower = float((result.get(field) or {}).get("lower", 0.0))
        if lower < min_match_positive_uplift_ci_lower:
            errors.append(
                f"{field}_lower<{min_match_positive_uplift_ci_lower}"
            )
    for opponent, row in result.get("by_opponent", {}).items():
        if row.get("match_outcome_clusters", 0) < 1:
            errors.append(f"{opponent}:match_outcome_clusters<1")
        if row.get("match_positive_rate", 0.0) < min_opponent_match_positive_rate:
            errors.append(
                f"{opponent}:match_positive_rate<"
                f"{min_opponent_match_positive_rate}"
            )
        if row.get("match_positive_uplift_mean", 0.0) < 0.0:
            errors.append(f"{opponent}:match_positive_uplift_mean<0")
    return errors


def _selection_eligibility(
    result: dict[str, Any],
    *,
    min_overrides: int,
    min_selection_clusters: int,
    min_override_clusters: int,
    min_overrides_per_opponent: int,
    min_override_hand_mean: float,
    require_nonnegative_opponent_mean: bool,
    min_cluster_ci_lower: float = 0.0,
    min_opponent_stratified_ci_lower: float = 0.0,
    require_win_first: bool = False,
    min_match_positive_rate_ci_lower: float = 0.5,
    min_match_positive_uplift_ci_lower: float = 0.0,
    min_opponent_match_positive_rate: float = 0.5,
) -> list[str]:
    errors = []
    if result["overrides"] < min_overrides:
        errors.append(f"overrides<{min_overrides}")
    if result["match_clusters"] < min_selection_clusters:
        errors.append(f"selection_clusters<{min_selection_clusters}")
    if result["override_clusters"] < min_override_clusters:
        errors.append(f"override_clusters<{min_override_clusters}")
    if result["override_hand_mean"] < min_override_hand_mean:
        errors.append(f"override_hand_mean<{min_override_hand_mean}")
    cluster_lower = result["match_cluster_bootstrap_mean_ci"]["lower"]
    if cluster_lower <= min_cluster_ci_lower:
        errors.append(f"cluster_ci_lower<={min_cluster_ci_lower}")
    stratified_lower = result[
        "match_opponent_stratified_cluster_ci"
    ]["lower"]
    if stratified_lower <= min_opponent_stratified_ci_lower:
        errors.append(
            "opponent_stratified_cluster_ci_lower"
            f"<={min_opponent_stratified_ci_lower}"
        )
    for opponent, row in result["by_opponent"].items():
        if row["overrides"] < min_overrides_per_opponent:
            errors.append(
                f"{opponent}:overrides<{min_overrides_per_opponent}"
            )
        if require_nonnegative_opponent_mean and row["mean"] < 0:
            errors.append(f"{opponent}:mean<0")
    if require_win_first:
        errors[:0] = _win_first_errors(
            result,
            min_match_positive_rate_ci_lower=(
                min_match_positive_rate_ci_lower
            ),
            min_match_positive_uplift_ci_lower=(
                min_match_positive_uplift_ci_lower
            ),
            min_opponent_match_positive_rate=(
                min_opponent_match_positive_rate
            ),
        )
    return errors


def _calibration_gate(
    result: dict[str, Any] | None,
    *,
    min_overrides: int,
    min_override_clusters: int,
    require_nonnegative_opponent_mean: bool,
    min_cluster_ci_lower: float = 0.0,
    min_opponent_stratified_ci_lower: float = 0.0,
    require_win_first: bool = False,
    min_match_positive_rate_ci_lower: float = 0.5,
    min_match_positive_uplift_ci_lower: float = 0.0,
    min_opponent_match_positive_rate: float = 0.5,
) -> dict[str, Any] | None:
    if result is None:
        return None
    errors = []
    if result["overrides"] < min_overrides:
        errors.append(f"overrides<{min_overrides}")
    if result["override_clusters"] < min_override_clusters:
        errors.append(f"override_clusters<{min_override_clusters}")
    cluster_lower = result["match_cluster_bootstrap_mean_ci"]["lower"]
    if cluster_lower <= min_cluster_ci_lower:
        errors.append(f"cluster_ci_lower<={min_cluster_ci_lower}")
    stratified_lower = result[
        "match_opponent_stratified_cluster_ci"
    ]["lower"]
    if stratified_lower <= min_opponent_stratified_ci_lower:
        errors.append(
            "opponent_stratified_cluster_ci_lower"
            f"<={min_opponent_stratified_ci_lower}"
        )
    if require_nonnegative_opponent_mean:
        for opponent, row in result["by_opponent"].items():
            if row["mean"] < 0:
                errors.append(f"{opponent}:mean<0")
    if require_win_first:
        errors[:0] = _win_first_errors(
            result,
            min_match_positive_rate_ci_lower=(
                min_match_positive_rate_ci_lower
            ),
            min_match_positive_uplift_ci_lower=(
                min_match_positive_uplift_ci_lower
            ),
            min_opponent_match_positive_rate=(
                min_opponent_match_positive_rate
            ),
        )
    return {"passed": not errors, "errors": errors}


def _file_manifest(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    return {
        "path": str(path.resolve()),
        "opened": True,
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _unopened_file_manifest(
    path: Path | None, *, role: str
) -> dict[str, Any] | None:
    if path is None:
        return None
    return {
        "path": str(path.resolve()),
        "role": role,
        "opened": False,
        "bytes": None,
        "sha256": None,
    }


def _may_open_policy_gate(
    path: Path | None, calibration_gate: dict[str, Any] | None
) -> bool:
    return bool(
        path is not None
        and calibration_gate is not None
        and calibration_gate.get("passed")
    )


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )


def select_offline_policy(
    rows: list[dict[str, Any]],
    *,
    margins: list[float],
    hand_weights: list[float],
    response_weights: list[float],
    min_overrides: int,
    min_selection_clusters: int,
    min_override_clusters: int,
    min_overrides_per_opponent: int,
    min_override_hand_mean: float,
    require_nonnegative_opponent_mean: bool,
    bootstrap_samples: int,
    bootstrap_seed: int,
    min_cluster_ci_lower: float = 0.0,
    min_opponent_stratified_ci_lower: float = 0.0,
    tail_weights: list[float] | None = None,
    min_match_weight: float = 0.0,
    min_hand_lcb: float | None = None,
    require_win_first: bool = False,
    min_match_positive_rate_ci_lower: float = 0.5,
    min_match_positive_uplift_ci_lower: float = 0.0,
    min_opponent_match_positive_rate: float = 0.5,
) -> dict[str, Any]:
    grid = []
    tail_weights = list(tail_weights or [0.0])
    for margin in margins:
        for hand_weight in hand_weights:
            for tail_weight in tail_weights:
                match_weight = 1.0 - hand_weight - tail_weight
                if match_weight + 1e-9 < min_match_weight:
                    continue
                for response_weight in response_weights:
                    config = {
                        "margin": float(margin),
                        "hand_weight": float(hand_weight),
                        "tail_weight": float(tail_weight),
                        "match_weight": float(match_weight),
                        "response_weight": float(response_weight),
                        "use_lower": True,
                    }
                    if min_hand_lcb is not None:
                        config["min_hand_lcb"] = float(min_hand_lcb)
                    result = _evaluate_config(
                        rows,
                        config,
                        bootstrap_samples=bootstrap_samples,
                        bootstrap_seed=bootstrap_seed,
                    )
                    result["eligibility_errors"] = _selection_eligibility(
                        result,
                        min_overrides=min_overrides,
                        min_selection_clusters=min_selection_clusters,
                        min_override_clusters=min_override_clusters,
                        min_overrides_per_opponent=min_overrides_per_opponent,
                        min_override_hand_mean=min_override_hand_mean,
                        require_nonnegative_opponent_mean=(
                            require_nonnegative_opponent_mean
                        ),
                        min_cluster_ci_lower=min_cluster_ci_lower,
                        min_opponent_stratified_ci_lower=(
                            min_opponent_stratified_ci_lower
                        ),
                        require_win_first=require_win_first,
                        min_match_positive_rate_ci_lower=(
                            min_match_positive_rate_ci_lower
                        ),
                        min_match_positive_uplift_ci_lower=(
                            min_match_positive_uplift_ci_lower
                        ),
                        min_opponent_match_positive_rate=(
                            min_opponent_match_positive_rate
                        ),
                    )
                    result["eligible"] = not result["eligibility_errors"]
                    grid.append(result)
    eligible = [result for result in grid if result["eligible"]]
    selected = max(
        eligible,
        key=lambda result: (
            result[
                "match_positive_rate_opponent_stratified_cluster_ci"
            ]["lower"],
            result["match_positive_rate_cluster_bootstrap_ci"]["lower"],
            result[
                "match_positive_uplift_opponent_stratified_cluster_ci"
            ]["lower"],
            result["match_positive_uplift_cluster_bootstrap_ci"]["lower"],
            result["match_positive_rate"],
            result["match_opponent_stratified_cluster_ci"]["lower"],
            result["match_cluster_bootstrap_mean_ci"]["lower"],
            result["match_mean_per_opportunity"],
            result["override_clusters"],
            -result["negative_override_rate"],
            -result["config"]["margin"],
        ),
    ) if eligible else None
    return {
        "estimand": OFFLINE_ESTIMAND,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "rows": len(rows),
        "grid": grid,
        "selected": selected,
        "selection_failure": (
            None if selected is not None else
            "no offline policy config met override coverage/safety constraints"
        ),
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
    parser.add_argument("--tail-weight-grid", default="0,0.25")
    parser.add_argument("--min-match-weight", type=float, default=0.25)
    parser.add_argument("--min-hand-lcb", type=float, default=0.0)
    parser.add_argument("--response-weight-grid", default="0,0.05,0.1")
    parser.add_argument("--min-overrides", type=int, default=10)
    parser.add_argument("--min-selection-clusters", type=int, default=8)
    parser.add_argument("--min-override-clusters", type=int, default=8)
    parser.add_argument("--min-overrides-per-opponent", type=int, default=2)
    parser.add_argument("--min-override-hand-mean", type=float, default=0.0)
    parser.add_argument("--min-selection-ci-lower", type=float, default=0.0)
    parser.add_argument(
        "--min-match-positive-rate-ci-lower", type=float, default=0.5
    )
    parser.add_argument(
        "--min-match-positive-uplift-ci-lower", type=float, default=0.0
    )
    parser.add_argument(
        "--min-opponent-match-positive-rate", type=float, default=0.5
    )
    parser.add_argument(
        "--allow-missing-match-outcome",
        action="store_true",
        help="Legacy diagnostic only; disable the 70-hand win-first evidence gate",
    )
    parser.add_argument("--allow-negative-selection-opponent", action="store_true")
    parser.add_argument("--min-calibration-overrides", type=int, default=5)
    parser.add_argument(
        "--min-calibration-override-clusters", type=int, default=3
    )
    parser.add_argument("--min-calibration-ci-lower", type=float, default=0.0)
    parser.add_argument("--allow-negative-calibration-opponent", action="store_true")
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260710)
    parser.add_argument(
        "--allow-missing-ipw",
        action="store_true",
        help="Legacy diagnostic only; treat rows without sampling evidence as weight 1",
    )
    args = parser.parse_args()
    if not 0.0 <= args.min_match_weight <= 1.0:
        raise SystemExit("min-match-weight must be in [0, 1]")
    if not math.isfinite(args.min_hand_lcb):
        raise SystemExit("min-hand-lcb must be finite")
    win_thresholds = (
        args.min_match_positive_rate_ci_lower,
        args.min_match_positive_uplift_ci_lower,
        args.min_opponent_match_positive_rate,
    )
    if not all(math.isfinite(value) for value in win_thresholds):
        raise SystemExit("match outcome thresholds must be finite")
    if not args.allow_missing_match_outcome and (
        not 0.5 <= args.min_match_positive_rate_ci_lower <= 1.0
        or not 0.0 <= args.min_match_positive_uplift_ci_lower <= 1.0
        or not 0.5 <= args.min_opponent_match_positive_rate <= 1.0
    ):
        raise SystemExit("win-first thresholds cannot be weakened")

    model_paths = [Path(path).resolve() for path in args.model]
    ensemble = OpponentMultiTaskEnsemble.load(model_paths)
    if ensemble is None:
        raise SystemExit("failed to load multi-task model ensemble")
    selection_rows = _prepare_rows(
        _read(args.selection_data),
        ensemble,
        require_ipw=not args.allow_missing_ipw,
        require_match_outcome=not args.allow_missing_match_outcome,
    )
    policy_selection = select_offline_policy(
        selection_rows,
        margins=[
            float(value) for value in args.margin_grid.split(",") if value.strip()
        ],
        hand_weights=[
            float(value)
            for value in args.hand_weight_grid.split(",")
            if value.strip()
        ],
        tail_weights=[
            float(value)
            for value in args.tail_weight_grid.split(",")
            if value.strip()
        ],
        min_match_weight=args.min_match_weight,
        min_hand_lcb=args.min_hand_lcb,
        response_weights=[
            float(value)
            for value in args.response_weight_grid.split(",")
            if value.strip()
        ],
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
        require_win_first=not args.allow_missing_match_outcome,
        min_match_positive_rate_ci_lower=(
            args.min_match_positive_rate_ci_lower
        ),
        min_match_positive_uplift_ci_lower=(
            args.min_match_positive_uplift_ci_lower
        ),
        min_opponent_match_positive_rate=(
            args.min_opponent_match_positive_rate
        ),
    )
    grid = policy_selection["grid"]
    model_manifests = [_file_manifest(path) for path in model_paths]
    data_manifests = {
        "selection": _file_manifest(args.selection_data),
        "calibration": _unopened_file_manifest(
            args.calibration_data, role="policy_calibration"
        ),
        "held_out": _unopened_file_manifest(
            args.held_out_data, role="policy_gate_not_final_blind"
        ),
    }
    selection_criteria = {
        "min_overrides": args.min_overrides,
        "min_selection_clusters": args.min_selection_clusters,
        "min_override_clusters": args.min_override_clusters,
        "min_overrides_per_opponent": args.min_overrides_per_opponent,
        "min_override_hand_mean": args.min_override_hand_mean,
        "min_match_weight": args.min_match_weight,
        "min_hand_lcb": args.min_hand_lcb,
        "require_nonnegative_opponent_mean": (
            not args.allow_negative_selection_opponent
        ),
        "min_cluster_ci_lower": args.min_selection_ci_lower,
        "min_opponent_stratified_ci_lower": args.min_selection_ci_lower,
        "require_win_first": not args.allow_missing_match_outcome,
        "min_match_positive_rate_ci_lower": (
            args.min_match_positive_rate_ci_lower
        ),
        "min_match_positive_uplift_ci_lower": (
            args.min_match_positive_uplift_ci_lower
        ),
        "min_opponent_match_positive_rate": (
            args.min_opponent_match_positive_rate
        ),
    }
    if policy_selection["selected"] is None:
        payload = {
            "schema_version": 7,
            "estimand": OFFLINE_ESTIMAND,
            "deployment_policy_value": False,
            "strength_evidence": False,
            "selection_used_held_out": False,
            "models": model_manifests,
            "selection_data": str(args.selection_data.resolve()),
            "data_manifests": data_manifests,
            "selection_criteria": selection_criteria,
            "grid": grid,
            "selected": None,
            "selection_failure": policy_selection["selection_failure"],
            "calibration": None,
            "calibration_gate": None,
            "held_out": None,
            "ablations": {},
        }
        _write_payload(args.output, payload)
        print(json.dumps({"selection_failure": payload["selection_failure"]}, indent=2))
        return 1
    selected = policy_selection["selected"]

    def evaluate_optional(path: Path | None, config: dict[str, Any]):
        if path is None:
            return None
        rows = _prepare_rows(
            _read(path),
            ensemble,
            require_ipw=not args.allow_missing_ipw,
            require_match_outcome=not args.allow_missing_match_outcome,
        )
        return _evaluate_config(
            rows,
            config,
            bootstrap_samples=args.bootstrap_samples,
            bootstrap_seed=args.bootstrap_seed,
        )

    if args.calibration_data is not None:
        data_manifests["calibration"] = _file_manifest(args.calibration_data)
    calibration = evaluate_optional(args.calibration_data, selected["config"])
    calibration_gate = _calibration_gate(
        calibration,
        min_overrides=args.min_calibration_overrides,
        min_override_clusters=args.min_calibration_override_clusters,
        require_nonnegative_opponent_mean=(
            not args.allow_negative_calibration_opponent
        ),
        min_cluster_ci_lower=args.min_calibration_ci_lower,
        min_opponent_stratified_ci_lower=args.min_calibration_ci_lower,
        require_win_first=not args.allow_missing_match_outcome,
        min_match_positive_rate_ci_lower=(
            args.min_match_positive_rate_ci_lower
        ),
        min_match_positive_uplift_ci_lower=(
            args.min_match_positive_uplift_ci_lower
        ),
        min_opponent_match_positive_rate=(
            args.min_opponent_match_positive_rate
        ),
    )
    if args.held_out_data is not None and calibration_gate is None:
        calibration_gate = {
            "passed": False,
            "errors": ["calibration_data_required_before_policy_gate"],
        }
    held_out_opened = _may_open_policy_gate(
        args.held_out_data, calibration_gate
    )
    if held_out_opened:
        data_manifests["held_out"] = _file_manifest(args.held_out_data)
        held_out = evaluate_optional(args.held_out_data, selected["config"])
    else:
        held_out = None
    policy_gate = _calibration_gate(
        held_out,
        min_overrides=args.min_calibration_overrides,
        min_override_clusters=args.min_calibration_override_clusters,
        require_nonnegative_opponent_mean=(
            not args.allow_negative_calibration_opponent
        ),
        min_cluster_ci_lower=args.min_calibration_ci_lower,
        min_opponent_stratified_ci_lower=args.min_calibration_ci_lower,
        require_win_first=not args.allow_missing_match_outcome,
        min_match_positive_rate_ci_lower=(
            args.min_match_positive_rate_ci_lower
        ),
        min_match_positive_uplift_ci_lower=(
            args.min_match_positive_uplift_ci_lower
        ),
        min_opponent_match_positive_rate=(
            args.min_opponent_match_positive_rate
        ),
    )
    no_response_config = dict(selected["config"], response_weight=0.0)
    mean_only_config = dict(selected["config"], use_lower=False)
    payload = {
        "schema_version": 7,
        "estimand": OFFLINE_ESTIMAND,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "selection_used_held_out": False,
        "models": model_manifests,
        "selection_data": str(args.selection_data.resolve()),
        "data_manifests": data_manifests,
        "selection_criteria": selection_criteria,
        "grid": grid,
        "selected": selected,
        "calibration": calibration,
        "calibration_gate": calibration_gate,
        "held_out_opened": held_out_opened,
        "held_out": held_out,
        "policy_gate": policy_gate,
        "ablations": {
            "no_response_held_out": (
                evaluate_optional(args.held_out_data, no_response_config)
                if held_out_opened else None
            ),
            "mean_only_held_out": (
                evaluate_optional(args.held_out_data, mean_only_config)
                if held_out_opened else None
            ),
        },
    }
    _write_payload(args.output, payload)
    print(json.dumps({
        "selected": selected,
        "calibration": calibration,
        "calibration_gate": calibration_gate,
        "held_out_opened": held_out_opened,
        "held_out": held_out,
        "policy_gate": policy_gate,
    }, indent=2, sort_keys=True))
    gates = [gate for gate in (calibration_gate, policy_gate) if gate is not None]
    return 0 if all(gate["passed"] for gate in gates) else 1


if __name__ == "__main__":
    raise SystemExit(main())
