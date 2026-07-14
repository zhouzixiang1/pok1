"""Versioned value-head features captured from the exact classic strategy path."""
from __future__ import annotations

import math
import statistics
from typing import Any


STRATEGY_CONTEXT_SCHEMA = "v140_strategy_context_v1"
STRATEGY_CONTEXT_FIELDS = (
    "context_available",
    "preflop_available",
    "postflop_available",
    "equity_available",
    "range_available",
    "preflop_strength",
    "weighted_win_rate",
    "simulations_norm",
    "made_strength",
    "range_combo_count_norm",
    "range_entropy_norm",
    "range_effective_support_norm",
    "range_top_decile_mass",
    "range_concentration",
    "range_weight_cv_mapped",
    "draw_quality",
    "draw_flush",
    "draw_nut_flush",
    "draw_near_nut_flush",
    "draw_high_flush",
    "draw_straight_none",
    "draw_straight_gutshot",
    "draw_straight_open_ended",
    "draw_straight_double_gutshot",
    "draw_combo",
    "draw_overcards_norm",
    "draw_semi_bluff",
    "draw_size_bonus_mapped",
    "value_tier_none",
    "value_tier_thin",
    "value_tier_strong",
    "value_tier_nut",
    "value_is_value",
    "value_size_bonus_mapped",
    "opp_confidence",
    "opp_vpip",
    "opp_pfr",
    "opp_allin_rate",
    "opp_postflop_aggr",
    "opp_fold_to_raise",
    "opp_aggression",
    "opp_avg_raise_bb_norm",
    "opp_barrel_freq",
    "opp_river_overcall_freq",
    "opp_fold_to_jam_rate",
    "opp_betsize_polarity_mapped",
    "opp_shove_rate",
    "spot_has_position",
    "spot_facing_raise",
    "spot_facing_allin",
    "spot_last_raise_pot_ratio_norm",
    "spot_preflop_other",
    "spot_preflop_sb_open",
    "spot_preflop_bb_vs_limp",
    "spot_preflop_bb_vs_raise",
    "spot_preflop_sb_vs_reraise",
    "board_wetness",
    "board_flush_pressure",
    "board_straight_pressure",
    "board_paired",
    "board_dynamic",
    "nutted_risk",
    "value_plan_size_delta_mapped",
    "value_plan_induce",
    "value_plan_protect",
    "value_plan_thin_control",
)
STRATEGY_CONTEXT_DIM = len(STRATEGY_CONTEXT_FIELDS)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return result if math.isfinite(result) else float(default)


def _clip(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    number = _float(value, low)
    return low if number < low else high if number > high else number


def _mapped(value: Any, low: float, high: float) -> float:
    if high <= low:
        raise ValueError("invalid mapped feature range")
    return _clip((_float(value, low) - low) / (high - low))


def _one_hot(value: Any, labels: tuple[str, ...]) -> list[float]:
    normalized = str(value or labels[0])
    if normalized not in labels:
        normalized = labels[0]
    return [1.0 if normalized == label else 0.0 for label in labels]


def summarize_range_weights(weights: Any) -> list[float]:
    if not isinstance(weights, (list, tuple)):
        return [0.0] * 6
    positive = [max(0.0, _float(value)) for value in weights]
    positive = [value for value in positive if value > 0.0]
    if not positive:
        return [0.0] * 6
    count = len(positive)
    total = sum(positive)
    probabilities = [value / total for value in positive]
    if count > 1:
        entropy = -sum(
            probability * math.log(probability)
            for probability in probabilities
        ) / math.log(count)
        hhi_floor = 1.0 / count
        hhi = sum(probability * probability for probability in probabilities)
        concentration = (hhi - hhi_floor) / (1.0 - hhi_floor)
    else:
        entropy = 0.0
        concentration = 1.0
    effective_support = 1.0 / sum(
        probability * probability for probability in probabilities
    )
    top_count = max(1, math.ceil(0.10 * count))
    top_mass = sum(sorted(probabilities, reverse=True)[:top_count])
    mean = statistics.fmean(positive)
    cv = (
        statistics.pstdev(positive) / mean
        if count > 1 and mean > 0.0
        else 0.0
    )
    return [
        _clip(count / 1225.0),
        _clip(entropy),
        _clip(effective_support / count),
        _clip(top_mass),
        _clip(concentration),
        _clip(cv / (1.0 + cv)),
    ]


def encode_strategy_context(context: dict[str, Any] | None) -> list[float]:
    context = context if isinstance(context, dict) else {}
    draw = context.get("draw_profile") or {}
    value = context.get("value_profile") or {}
    opponent = context.get("opponent_model") or {}
    spot = context.get("spot_info") or {}
    board = context.get("board_texture") or {}
    risk = context.get("nutted_risk") or {}
    plan = context.get("value_plan") or {}
    weights = context.get("range_weights")
    range_summary = summarize_range_weights(weights)
    preflop = context.get("preflop_strength")
    win_rate = context.get("weighted_win_rate", context.get("win_rate"))
    made = context.get("made_strength")

    features = [
        1.0 if context else 0.0,
        1.0 if preflop is not None else 0.0,
        1.0 if made is not None or draw or value else 0.0,
        1.0 if win_rate is not None else 0.0,
        1.0 if any(range_summary) else 0.0,
        _clip(preflop),
        _clip(win_rate),
        _clip(_float(context.get("simulations")) / 2000.0),
        _clip(made),
    ]
    features.extend(range_summary)
    features.extend([
        _clip(draw.get("quality")),
        float(bool(draw.get("flush_draw"))),
        float(bool(draw.get("nut_flush_draw"))),
        float(bool(draw.get("near_nut_flush_draw"))),
        float(bool(draw.get("high_flush_draw"))),
    ])
    features.extend(_one_hot(
        draw.get("straight_draw"),
        ("none", "gutshot", "open_ended", "double_gutshot"),
    ))
    features.extend([
        float(bool(draw.get("combo_draw"))),
        _clip(_float(draw.get("overcards")) / 2.0),
        float(bool(draw.get("semi_bluff"))),
        _mapped(draw.get("size_bonus"), -0.04, 0.08),
    ])
    features.extend(_one_hot(
        value.get("tier"), ("none", "thin", "strong", "nut")
    ))
    features.extend([
        float(bool(value.get("is_value"))),
        _mapped(value.get("size_bonus"), -0.04, 0.24),
    ])
    features.extend([
        _clip(opponent.get("confidence")),
        _clip(opponent.get("vpip")),
        _clip(opponent.get("pfr")),
        _clip(opponent.get("allin_rate")),
        _clip(opponent.get("postflop_aggr")),
        _clip(opponent.get("fold_to_raise")),
        _clip(opponent.get("aggression")),
        _clip(_float(opponent.get("avg_raise_bb")) / 20.0),
        _clip(opponent.get("barrel_freq")),
        _clip(opponent.get("river_overcall_freq")),
        _clip(opponent.get("fold_to_jam_rate")),
        _mapped(opponent.get("betsize_polarity"), -1.0, 1.0),
        _clip(opponent.get("shove_rate")),
        float(bool(spot.get("has_position"))),
        float(bool(spot.get("facing_raise"))),
        float(bool(spot.get("facing_allin"))),
        _clip(_float(spot.get("last_raise_pot_ratio")) / 4.0),
    ])
    features.extend(_one_hot(
        spot.get("preflop_spot"),
        ("other", "sb_open", "bb_vs_limp", "bb_vs_raise", "sb_vs_reraise"),
    ))
    features.extend([
        _clip(board.get("wetness")),
        _clip(board.get("flush_pressure")),
        _clip(board.get("straight_pressure")),
        float(bool(board.get("paired"))),
        float(bool(board.get("dynamic"))),
        _clip(risk.get("risk")),
        _mapped(plan.get("size_delta"), -0.30, 0.30),
        float(bool(plan.get("induce"))),
        float(bool(plan.get("protect"))),
        float(bool(plan.get("thin_control"))),
    ])
    if len(features) != STRATEGY_CONTEXT_DIM:
        raise RuntimeError(
            f"strategy context dimension mismatch: "
            f"{len(features)} != {STRATEGY_CONTEXT_DIM}"
        )
    return features


def strategy_context_metadata() -> dict[str, Any]:
    return {
        "schema": STRATEGY_CONTEXT_SCHEMA,
        "dim": STRATEGY_CONTEXT_DIM,
        "fields": list(STRATEGY_CONTEXT_FIELDS),
        "value_head_only": True,
        "response_head_allowed": False,
    }
