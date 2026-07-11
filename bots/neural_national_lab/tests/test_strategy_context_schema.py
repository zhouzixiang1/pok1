from __future__ import annotations

from pathlib import Path
import sys


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import strategy_context_schema as schema  # noqa: E402


def test_strategy_context_preserves_exact_rule_signals() -> None:
    context = {
        "preflop_strength": 0.85,
        "weighted_win_rate": 0.63,
        "simulations": 900,
        "range_weights": [1.0, 2.0, 4.0, 8.0],
        "opponent_model": {
            "confidence": 0.7,
            "pfr": 0.31,
            "fold_to_raise": 0.44,
        },
        "spot_info": {
            "has_position": True,
            "facing_raise": True,
            "preflop_spot": "bb_vs_raise",
        },
    }

    values = schema.encode_strategy_context(context)
    by_name = dict(zip(schema.STRATEGY_CONTEXT_FIELDS, values))

    assert by_name["preflop_strength"] == 0.85
    assert by_name["weighted_win_rate"] == 0.63
    assert by_name["simulations_norm"] == 0.45
    assert by_name["opp_pfr"] == 0.31
    assert by_name["spot_preflop_bb_vs_raise"] == 1.0


def test_range_summary_distinguishes_uniform_and_concentrated_beliefs() -> None:
    uniform = schema.summarize_range_weights([1.0] * 100)
    concentrated = schema.summarize_range_weights([100.0] + [1.0] * 99)

    assert uniform[1] > concentrated[1]
    assert uniform[2] > concentrated[2]
    assert uniform[3] < concentrated[3]
    assert uniform[4] < concentrated[4]


def test_missing_context_has_fixed_bounded_dimension() -> None:
    values = schema.encode_strategy_context(None)

    assert len(values) == schema.STRATEGY_CONTEXT_DIM
    assert values[0] == 0.0
    assert all(0.0 <= value <= 1.0 for value in values)


def test_postflop_profiles_and_signed_features_are_bounded() -> None:
    values = schema.encode_strategy_context({
        "weighted_win_rate": 2.0,
        "made_strength": -1.0,
        "draw_profile": {
            "quality": 9.0,
            "flush_draw": True,
            "straight_draw": "open_ended",
            "size_bonus": 99.0,
        },
        "value_profile": {
            "tier": "nut",
            "is_value": True,
            "size_bonus": -99.0,
        },
        "opponent_model": {"betsize_polarity": -5.0},
        "value_plan": {"size_delta": 5.0, "protect": True},
    })

    assert len(values) == schema.STRATEGY_CONTEXT_DIM
    assert all(0.0 <= value <= 1.0 for value in values)


def test_oversized_numbers_are_bounded_instead_of_raising() -> None:
    values = schema.encode_strategy_context({
        "simulations": 10**10_000,
        "preflop_strength": 10**10_000,
        "range_weights": [10**10_000, 1],
    })

    assert len(values) == schema.STRATEGY_CONTEXT_DIM
    assert all(0.0 <= value <= 1.0 for value in values)


def test_strategy_context_is_for_value_head_only() -> None:
    metadata = schema.strategy_context_metadata()

    assert metadata["schema"] == "v140_strategy_context_v1"
    assert metadata["dim"] == schema.STRATEGY_CONTEXT_DIM
    assert metadata["value_head_only"] is True
    assert metadata["response_head_allowed"] is False
