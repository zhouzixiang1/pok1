"""Shared win-first scoring contract for v4 offline and native policy paths."""
from __future__ import annotations

import math
from typing import Any

from feature_spec import LABELS


POLICY_SCHEMA = "opponent_multitask_win_first_policy_v4"
SELECTION_PRIORITY = (
    "positive_probability_lcb_then_uplift_lcb_then_chip_lcb_v1"
)
OUTCOME_AGGREGATION_SCHEMA = "opponent_multitask_outcome_ensemble_v1"
OUTCOME_AGGREGATION_METHOD = (
    "mean_calibrated_probability_plusminus_population_std_v1"
)


def _finite(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def normalize_policy(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("v4 selected policy must be an object")
    expected = {
        "schema",
        "selection_priority",
        "min_positive_probability_lcb",
        "min_probability_uplift_lcb",
        "chip_margin",
        "hand_weight",
        "tail_weight",
        "match_weight",
        "response_weight",
        "min_hand_lcb",
        "use_lower",
    }
    if set(raw) != expected:
        raise ValueError("v4 selected policy has unknown or missing fields")
    if (
        raw.get("schema") != POLICY_SCHEMA
        or raw.get("selection_priority") != SELECTION_PRIORITY
        or raw.get("use_lower") is not True
    ):
        raise ValueError("v4 selected policy contract changed")
    result = {
        "schema": POLICY_SCHEMA,
        "selection_priority": SELECTION_PRIORITY,
        "min_positive_probability_lcb": _finite(
            raw["min_positive_probability_lcb"],
            field="policy.min_positive_probability_lcb",
        ),
        "min_probability_uplift_lcb": _finite(
            raw["min_probability_uplift_lcb"],
            field="policy.min_probability_uplift_lcb",
        ),
        "chip_margin": _finite(raw["chip_margin"], field="policy.chip_margin"),
        "hand_weight": _finite(
            raw["hand_weight"], field="policy.hand_weight"
        ),
        "tail_weight": _finite(
            raw["tail_weight"], field="policy.tail_weight"
        ),
        "match_weight": _finite(
            raw["match_weight"], field="policy.match_weight"
        ),
        "response_weight": _finite(
            raw["response_weight"], field="policy.response_weight"
        ),
        "min_hand_lcb": _finite(
            raw["min_hand_lcb"], field="policy.min_hand_lcb"
        ),
        "use_lower": True,
    }
    if not 0.5 <= result["min_positive_probability_lcb"] <= 1.0:
        raise ValueError("positive probability floor cannot be below 0.5")
    if not 0.0 <= result["min_probability_uplift_lcb"] <= 1.0:
        raise ValueError("probability uplift floor must be in [0, 1]")
    if min(
        result["chip_margin"],
        result["hand_weight"],
        result["tail_weight"],
        result["match_weight"],
        result["response_weight"],
        result["min_hand_lcb"],
    ) < 0.0:
        raise ValueError("v4 selected policy thresholds must be nonnegative")
    if abs(
        result["hand_weight"]
        + result["tail_weight"]
        + result["match_weight"]
        - 1.0
    ) > 1.0e-8:
        raise ValueError("v4 value-policy weights must sum to one")
    return result


def aggregate_member_probabilities(
    member_probabilities: list[list[float]],
    *,
    uncertainty_std_weight: float,
) -> dict[str, Any]:
    uncertainty_std_weight = _finite(
        uncertainty_std_weight, field="outcome uncertainty std weight"
    )
    if uncertainty_std_weight < 0.0:
        raise ValueError("outcome uncertainty std weight must be nonnegative")
    if not member_probabilities:
        raise ValueError("outcome ensemble has no members")
    normalized = []
    for member_index, raw in enumerate(member_probabilities):
        if not isinstance(raw, list) or len(raw) != len(LABELS):
            raise ValueError("outcome member has the wrong action dimension")
        row = [
            _finite(value, field=f"member[{member_index}].probability")
            for value in raw
        ]
        if any(not 0.0 <= value <= 1.0 for value in row):
            raise ValueError("outcome member probability is outside [0, 1]")
        normalized.append(row)
    means = []
    standard_deviations = []
    lowers = []
    uppers = []
    for action in range(len(LABELS)):
        values = [member[action] for member in normalized]
        mean = sum(values) / len(values)
        std = math.sqrt(
            sum((value - mean) ** 2 for value in values) / len(values)
        )
        radius = uncertainty_std_weight * std
        means.append(mean)
        standard_deviations.append(std)
        lowers.append(max(0.0, mean - radius))
        uppers.append(min(1.0, mean + radius))
    return {
        "schema": OUTCOME_AGGREGATION_SCHEMA,
        "method": OUTCOME_AGGREGATION_METHOD,
        "members": len(normalized),
        "uncertainty_std_weight": uncertainty_std_weight,
        "mean": means,
        "lower": lowers,
        "upper": uppers,
        "member_probability_std": standard_deviations,
    }


def _action_vector(
    payload: dict[str, Any], key: str, *, field: str
) -> list[float]:
    raw = payload.get(key)
    if not isinstance(raw, list) or len(raw) != len(LABELS):
        raise ValueError(f"{field} has the wrong action dimension")
    return [_finite(value, field=field) for value in raw]


def validate_outcome_aggregation(payload: Any) -> dict[str, Any]:
    expected = {
        "schema",
        "method",
        "members",
        "uncertainty_std_weight",
        "mean",
        "lower",
        "upper",
        "member_probability_std",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("outcome aggregation has unknown or missing fields")
    if (
        payload.get("schema") != OUTCOME_AGGREGATION_SCHEMA
        or payload.get("method") != OUTCOME_AGGREGATION_METHOD
    ):
        raise ValueError("outcome aggregation contract changed")
    members = int(payload.get("members", 0))
    uncertainty = _finite(
        payload.get("uncertainty_std_weight"),
        field="outcome uncertainty std weight",
    )
    if members < 1 or uncertainty < 0.0:
        raise ValueError("outcome aggregation metadata is invalid")
    mean = _action_vector(payload, "mean", field="outcome mean")
    lower = _action_vector(payload, "lower", field="outcome lower")
    upper = _action_vector(payload, "upper", field="outcome upper")
    std = _action_vector(
        payload, "member_probability_std", field="outcome member std"
    )
    for index in range(len(LABELS)):
        if (
            not 0.0 <= lower[index] <= mean[index] <= upper[index] <= 1.0
            or std[index] < 0.0
        ):
            raise ValueError("outcome aggregation bounds are invalid")
    return dict(payload)


def score_candidate(
    policy: dict[str, Any],
    outcomes: dict[str, Any],
    values: dict[str, dict[str, list[float]]],
    *,
    label_id: int,
    rule_label_id: int,
    response_signal: float = 0.0,
) -> dict[str, float] | None:
    policy = normalize_policy(policy)
    if policy is None:
        return None
    label_id = int(label_id)
    rule_label_id = int(rule_label_id)
    if (
        not 0 <= label_id < len(LABELS)
        or not 0 <= rule_label_id < len(LABELS)
        or label_id == rule_label_id
    ):
        raise ValueError("candidate and rule labels are invalid")
    outcomes = validate_outcome_aggregation(outcomes)
    outcome_lower = _action_vector(outcomes, "lower", field="outcome lower")
    outcome_upper = _action_vector(outcomes, "upper", field="outcome upper")
    outcome_mean = _action_vector(outcomes, "mean", field="outcome mean")
    candidate_probability_lcb = outcome_lower[label_id]
    rule_probability_ucb = outcome_upper[rule_label_id]
    probability_uplift_lcb = candidate_probability_lcb - rule_probability_ucb
    if (
        candidate_probability_lcb
        < policy["min_positive_probability_lcb"]
        or probability_uplift_lcb
        <= policy["min_probability_uplift_lcb"]
    ):
        return None
    try:
        hand = _finite(
            values["delta_vs_rule"]["lower"][label_id], field="hand lower"
        )
        tail = _finite(
            values["tail_delta_vs_rule"]["lower"][label_id],
            field="tail lower",
        )
        match = _finite(
            values["match_delta_vs_rule"]["lower"][label_id],
            field="match lower",
        )
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("v4 candidate value prediction is malformed") from exc
    if hand < policy["min_hand_lcb"]:
        return None
    response_signal = _finite(response_signal, field="response signal")
    chip_score = (
        policy["hand_weight"] * hand
        + policy["tail_weight"] * tail
        + policy["match_weight"] * match
        + policy["response_weight"] * response_signal
    )
    if chip_score <= policy["chip_margin"]:
        return None
    return {
        "candidate_probability_mean": outcome_mean[label_id],
        "candidate_probability_lcb": candidate_probability_lcb,
        "rule_probability_ucb": rule_probability_ucb,
        "probability_uplift_lcb": probability_uplift_lcb,
        "chip_score": chip_score,
        "hand": hand,
        "tail": tail,
        "match": match,
        "response_signal": response_signal,
    }


def select_candidate(
    policy: dict[str, Any] | None,
    outcomes: dict[str, Any],
    values: dict[str, dict[str, list[float]]],
    candidates: list[dict[str, Any]],
    *,
    rule_label_id: int,
) -> dict[str, Any] | None:
    normalized = normalize_policy(policy)
    if normalized is None:
        return None
    best = None
    for candidate in candidates:
        label_id = int(candidate.get("label_id", -1))
        scored = score_candidate(
            normalized,
            outcomes,
            values,
            label_id=label_id,
            rule_label_id=rule_label_id,
            response_signal=candidate.get("response_signal", 0.0),
        )
        if scored is None:
            continue
        key = (
            scored["candidate_probability_lcb"],
            scored["probability_uplift_lcb"],
            scored["chip_score"],
            -label_id,
        )
        if best is None or key > best[0]:
            best = (key, dict(candidate), scored)
    if best is None:
        return None
    return {**best[1], "prediction": best[2]}
