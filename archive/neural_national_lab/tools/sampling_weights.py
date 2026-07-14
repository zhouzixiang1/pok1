"""Training weights for uniformly sampled counterfactual decisions."""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any


WEIGHTING_SCHEMES = (
    "uniform",
    "opponent_balanced",
    "sampling_ipw",
    "opponent_balanced_sampling_ipw",
)
MODALITIES = ("value", "behavior")


def _finite_float(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _positive_integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    number = _finite_float(value, field=field)
    integer = int(number)
    if integer < 1 or number != integer:
        raise ValueError(f"{field} must be a positive integer")
    return integer


def opponent_label(row: dict[str, Any]) -> str:
    name = str(row.get("_opponent_label") or row.get("opponent") or "").strip()
    if not name:
        raise ValueError("training row is missing an opponent label")
    return name


def match_cluster_key(row: dict[str, Any]) -> tuple[str, int, int]:
    opponent = opponent_label(row)
    try:
        deck_seed = int(row["deck_seed_base"])
        bot_seed = int(row["bot_seed_base"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("training row is missing a match-cluster seed") from exc
    return opponent, deck_seed, bot_seed


def decision_sampling_weight(row: dict[str, Any]) -> float:
    if row.get("decision_sampling") != "uniform":
        raise ValueError("value row must declare uniform decision sampling")
    eligible = _positive_integer(
        row.get("eligible_decisions"), field="eligible_decisions"
    )
    selected = _positive_integer(
        row.get("selected_decisions"), field="selected_decisions"
    )
    if selected > eligible:
        raise ValueError("selected_decisions exceeds eligible_decisions")
    probability = _finite_float(
        row.get("decision_inclusion_probability"),
        field="decision_inclusion_probability",
    )
    inverse = _finite_float(
        row.get("decision_inverse_probability_weight"),
        field="decision_inverse_probability_weight",
    )
    expected = selected / eligible
    if not 0.0 < probability <= 1.0:
        raise ValueError("decision inclusion probability must be in (0, 1]")
    if inverse < 1.0:
        raise ValueError("decision inverse-probability weight must be >= 1")
    if not math.isclose(probability, expected, rel_tol=1e-9, abs_tol=1e-12):
        raise ValueError("decision inclusion probability disagrees with row counts")
    if not math.isclose(inverse, 1.0 / probability, rel_tol=1e-9):
        raise ValueError("decision inverse-probability weight is inconsistent")
    return inverse


def _effective_sample_size(weights: list[float]) -> float:
    if not weights:
        return 0.0
    total = sum(weights)
    squared = sum(weight * weight for weight in weights)
    return total * total / squared if squared > 0.0 else 0.0


def attach_training_row_weights(
    rows: list[dict[str, Any]],
    *,
    scheme: str,
    modality: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if scheme not in WEIGHTING_SCHEMES:
        raise ValueError(f"unknown training row weighting scheme: {scheme}")
    if modality not in MODALITIES:
        raise ValueError(f"unknown training modality: {modality}")
    copied = [dict(row) for row in rows]
    uses_ipw = scheme in {
        "sampling_ipw", "opponent_balanced_sampling_ipw"
    }
    balances_opponents = scheme in {
        "opponent_balanced", "opponent_balanced_sampling_ipw"
    }
    if not copied:
        return copied, {
            "scheme": scheme,
            "modality": modality,
            "rows": 0,
            "opponents": 0,
            "clusters": 0,
            "sampling_ipw_applicable": modality == "value",
            "sampling_ipw_used": uses_ipw and modality == "value",
            "min_row_weight": None,
            "max_row_weight": None,
            "mean_row_weight": None,
            "effective_sample_size": 0.0,
        }

    groups: dict[str, list[int]] = defaultdict(list)
    clusters: dict[str, set[tuple[str, int, int]]] = defaultdict(set)
    sampling_weights = []
    for index, row in enumerate(copied):
        key = match_cluster_key(row)
        groups[key[0]].append(index)
        clusters[key[0]].add(key)
        sampling_weights.append(
            decision_sampling_weight(row)
            if uses_ipw and modality == "value"
            else 1.0
        )

    raw_weights = list(sampling_weights)
    if balances_opponents:
        for indices in groups.values():
            total = sum(sampling_weights[index] for index in indices)
            if total <= 0.0:
                raise ValueError("opponent has non-positive total sampling weight")
            for index in indices:
                raw_weights[index] = sampling_weights[index] / total
    total = sum(raw_weights)
    if total <= 0.0:
        raise ValueError("training rows have non-positive total weight")
    scale = len(raw_weights) / total
    weights = [weight * scale for weight in raw_weights]
    for row, weight in zip(copied, weights):
        row["_training_loss_weight"] = float(weight)

    per_opponent = {}
    for opponent, indices in sorted(groups.items()):
        opponent_weights = [weights[index] for index in indices]
        per_opponent[opponent] = {
            "rows": len(indices),
            "clusters": len(clusters[opponent]),
            "sampling_weight_sum": float(
                sum(sampling_weights[index] for index in indices)
            ),
            "total_weight": float(sum(opponent_weights)),
            "effective_sample_size": float(
                _effective_sample_size(opponent_weights)
            ),
        }
    return copied, {
        "scheme": scheme,
        "modality": modality,
        "rows": len(copied),
        "opponents": len(groups),
        "clusters": sum(len(values) for values in clusters.values()),
        "sampling_ipw_applicable": modality == "value",
        "sampling_ipw_used": uses_ipw and modality == "value",
        "opponent_balanced": balances_opponents,
        "min_sampling_weight": float(min(sampling_weights)),
        "max_sampling_weight": float(max(sampling_weights)),
        "min_row_weight": float(min(weights)),
        "max_row_weight": float(max(weights)),
        "mean_row_weight": float(sum(weights) / len(weights)),
        "effective_sample_size": float(_effective_sample_size(weights)),
        "per_opponent": per_opponent,
    }
