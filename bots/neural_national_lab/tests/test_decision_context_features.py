from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "bots" / "neural_national_lab" / "tools"
sys.path.insert(0, str(TOOLS))

import decision_context_features as context  # noqa: E402
import feature_spec  # noqa: E402


def _index(name: str) -> int:
    return context.DECISION_CONTEXT_FEATURE_INDEX[name]


def _request(**updates: object) -> dict[str, object]:
    request: dict[str, object] = {
        "my_id": 0,
        "my_chips": 15_000,
        "my_cards": [48, 44],
        "public_cards": [0, 5, 10],
        "history": [],
        "pot": 2_000,
        "to_call": 500,
        "remaining_hands": 35,
        "total_win_chips": [0, 0],
    }
    request.update(updates)
    return request


def test_schema_is_versioned_fixed_and_public() -> None:
    metadata = context.decision_context_metadata()

    assert context.DECISION_CONTEXT_SCHEMA == "public_decision_context_v1"
    assert context.DECISION_CONTEXT_DIM == 15
    assert context.DECISION_CONTEXT_DIM == len(
        context.DECISION_CONTEXT_FEATURE_NAMES
    )
    assert len(context.DECISION_CONTEXT_FEATURE_BOUNDS) == (
        context.DECISION_CONTEXT_DIM
    )
    assert context.LEGAL_MASK_SLICE == slice(9, 15)
    assert metadata["dim"] == context.DECISION_CONTEXT_DIM
    assert metadata["dimension"] == context.DECISION_CONTEXT_DIM
    assert metadata["public_only"] is True
    assert metadata["private_feature_indices"] == []
    assert not any(
        "card" in name or "hand_strength" in name
        for name in context.DECISION_CONTEXT_FEATURE_NAMES
    )


def test_public_context_breaks_a_legacy_state_collision() -> None:
    deep_request = _request(opponent_chips=12_000)
    short_request = _request(opponent_chips=3_000)
    deep = {
        "stacks": [15_000, 12_000],
        "to_call": 500,
        "min_raise_action": 1_500,
        "allin_call_amount": 500,
        "pot": 2_000,
    }
    short = {
        "stacks": [15_000, 3_000],
        "to_call": 500,
        "min_raise_action": 3_500,
        "allin_call_amount": 2_500,
        "pot": 2_000,
    }

    assert feature_spec.encode_features(deep_request) == feature_spec.encode_features(
        short_request
    )
    assert context.encode_decision_context(deep_request, deep) != (
        context.encode_decision_context(short_request, short)
    )
    assert context.encode_decision_context(deep_request, deep)[
        _index("opponent_stack_fraction")
    ] == pytest.approx(0.6)
    assert context.encode_decision_context(short_request, short)[
        _index("opponent_stack_fraction")
    ] == pytest.approx(0.15)


def test_pot_preserves_values_above_one_legacy_stack() -> None:
    pot_20k = _request(pot=20_000)
    pot_30k = _request(pot=30_000)

    assert feature_spec.encode_features(pot_20k)[10] == 1.0
    assert feature_spec.encode_features(pot_30k)[10] == 1.0
    first = context.encode_decision_context(pot_20k, {})
    second = context.encode_decision_context(pot_30k, {})

    assert first[_index("pot_fraction")] == pytest.approx(0.5)
    assert second[_index("pot_fraction")] == pytest.approx(0.75)


def test_betting_state_amounts_and_effective_call_ratio() -> None:
    request = _request(my_chips=10_000, opponent_chips=8_000)
    state = {
        "stacks": [10_000, 8_000],
        "to_call": 2_000,
        "min_raise_action": 4_000,
        "allin_call_amount": 3_000,
        "pot": 24_000,
    }

    features = context.encode_decision_context(request, state)

    assert features[_index("opponent_stack_fraction")] == pytest.approx(0.4)
    assert features[_index("effective_stack_fraction")] == pytest.approx(0.4)
    assert features[_index("min_raise_action_fraction")] == pytest.approx(0.2)
    assert features[_index("allin_call_amount_fraction")] == pytest.approx(0.15)
    assert features[_index("to_call_over_effective_stack")] == pytest.approx(0.25)
    assert features[_index("pot_fraction")] == pytest.approx(0.6)


def test_match_score_uses_full_seventy_hand_range() -> None:
    scores = (-1_400_000, -40_000, 0, 40_000, 1_400_000)
    requests = [
        _request(total_win_chips=[score, -score]) for score in scores
    ]
    encoded = [
        context.encode_decision_context(request, {})[
            _index("match_score_fraction")
        ]
        for request in requests
    ]

    assert feature_spec.encode_features(requests[0]) == feature_spec.encode_features(
        requests[1]
    )
    assert feature_spec.encode_features(requests[3]) == feature_spec.encode_features(
        requests[4]
    )
    assert encoded == sorted(encoded)
    assert len(set(encoded)) == len(scores)
    assert encoded[0] == 0.0
    assert 0.0 < encoded[1] < 0.5
    assert encoded[2] == 0.5
    assert 0.5 < encoded[3] < 1.0
    assert encoded[4] == 1.0


def test_score_pressure_is_relative_to_remaining_maximum_swing() -> None:
    early = context.encode_decision_context(
        _request(remaining_hands=70, total_win_chips=[20_000, -20_000]), {}
    )
    late = context.encode_decision_context(
        _request(remaining_hands=1, total_win_chips=[20_000, -20_000]), {}
    )

    pressure = _index("score_over_remaining_swing")
    assert early[pressure] == pytest.approx(0.5 + 0.5 / 70.0)
    assert late[pressure] == 1.0


def test_legal_action_mask_is_the_final_six_dimensions() -> None:
    legal_mask = [1, 1, 0, True, -1, 2]
    features = context.encode_decision_context(_request(), {}, legal_mask)

    assert features[-6:] == [1.0, 1.0, 0.0, 1.0, 0.0, 1.0]
    assert context.DECISION_CONTEXT_FEATURE_NAMES[-6:] == (
        "legal_fold",
        "legal_call",
        "legal_raise_half",
        "legal_raise_pot",
        "legal_raise_2pot",
        "legal_allin",
    )


def test_private_cards_cannot_change_public_context() -> None:
    first = _request(my_cards=[48, 44], opponent_cards=[0, 1])
    second = _request(my_cards=[2, 7], opponent_cards=[50, 51])
    state = {
        "stacks": [15_000, 12_000],
        "to_call": 500,
        "min_raise_action": 1_500,
        "allin_call_amount": 500,
        "pot": 2_000,
    }

    assert context.encode_decision_context(first, state, [1] * 6) == (
        context.encode_decision_context(second, state, [1] * 6)
    )


@pytest.mark.parametrize(
    ("payload", "state", "legal_mask"),
    [
        (None, None, None),
        ({}, {}, []),
        (
            {
                "my_id": "bad",
                "my_chips": -10,
                "opponent_chips": float("inf"),
                "remaining_hands": 999,
                "total_win_chips": [float("nan")],
            },
            {
                "effective_stack": -1,
                "to_call": 1e100,
                "pot": 10**10_000,
                "min_raise_action": -5,
            },
            [float("nan"), float("inf"), -1, 0, 0.5, "bad", 1],
        ),
    ],
)
def test_dimension_and_bounds_survive_missing_or_malformed_fields(
    payload: object, state: object, legal_mask: object
) -> None:
    features = context.encode_decision_context(payload, state, legal_mask)

    assert len(features) == context.DECISION_CONTEXT_DIM
    assert all(math.isfinite(value) for value in features)
    assert all(0.0 <= value <= 1.0 for value in features)


def test_missing_fields_use_stable_neutral_defaults() -> None:
    features = context.encode_decision_context({}, {})

    assert features[_index("opponent_stack_fraction")] == 0.0
    assert features[_index("effective_stack_fraction")] == 0.0
    assert features[_index("match_score_fraction")] == 0.5
    assert features[_index("score_over_remaining_swing")] == 0.5
    assert features[-6:] == [0.0] * 6
