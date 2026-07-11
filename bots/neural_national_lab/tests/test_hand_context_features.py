from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "bots" / "neural_national_lab" / "tools"
sys.path.insert(0, str(TOOLS))

import feature_spec  # noqa: E402
import hand_context_features as hand_context  # noqa: E402
import state_feature_schema  # noqa: E402


def card(rank: int, suit: int) -> int:
    return (rank - 2) * 4 + suit


def test_hand_context_distinguishes_flush_relation_lost_by_legacy_state() -> None:
    board = [card(12, 0), card(7, 0), card(2, 1)]
    flush_draw = {
        "my_cards": [card(14, 0), card(13, 0)],
        "public_cards": board,
    }
    backdoor_only = {
        "my_cards": [card(14, 1), card(13, 1)],
        "public_cards": board,
    }

    assert feature_spec.encode_features(flush_draw) == feature_spec.encode_features(
        backdoor_only
    )
    draw_features = hand_context.encode_hand_context(flush_draw)
    backdoor_features = hand_context.encode_hand_context(backdoor_only)
    assert draw_features != backdoor_features
    assert draw_features[11] == 1.0
    assert backdoor_features[11] == 0.0
    assert backdoor_features[12] == 1.0


def test_hand_context_recognizes_board_only_straight_flush() -> None:
    request = {
        "my_cards": [card(2, 1), card(3, 2)],
        "public_cards": [
            card(10, 0), card(11, 0), card(12, 0), card(13, 0), card(14, 0)
        ],
    }

    features = hand_context.encode_hand_context(request)

    assert features[1] == 1.0
    assert features[2] == 1.0
    assert features[3] == 0.0


def test_hand_context_is_invariant_to_suit_permutation() -> None:
    request = {
        "my_cards": [card(14, 0), card(13, 0)],
        "public_cards": [card(12, 0), card(11, 1), card(10, 2), card(2, 3)],
    }

    def rotate(value: int) -> int:
        return (value // 4) * 4 + ((value % 4 + 1) % 4)

    rotated = {
        "my_cards": [rotate(value) for value in request["my_cards"]],
        "public_cards": [rotate(value) for value in request["public_cards"]],
    }

    assert hand_context.encode_hand_context(request) == hand_context.encode_hand_context(
        rotated
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"my_cards": [card(14, 0)], "public_cards": []},
        {
            "my_cards": [card(14, 0), card(14, 1)],
            "public_cards": [card(14, 2), card(7, 0), card(2, 1)],
        },
    ],
)
def test_hand_context_has_stable_bounded_dimension(payload) -> None:
    features = hand_context.encode_hand_context(payload)

    assert len(features) == hand_context.HAND_CONTEXT_DIM
    assert all(0.0 <= value <= 1.0 for value in features)


def test_hand_context_ignores_non_finite_cards() -> None:
    features = hand_context.encode_hand_context({
        "my_cards": [float("inf"), float("nan")],
        "public_cards": [0, 1, 2],
    })

    assert features == [0.0] * hand_context.HAND_CONTEXT_DIM


def test_versioned_state_schema_extends_dimension_and_private_mask() -> None:
    request = {
        "my_cards": [card(14, 0), card(13, 0)],
        "public_cards": [card(12, 0), card(7, 0), card(2, 1)],
    }
    base = feature_spec.encode_features(request)

    extended = state_feature_schema.extend_state_features(
        base,
        request,
        schema=state_feature_schema.HERO_HAND_STATE_SCHEMA,
    )
    metadata = state_feature_schema.feature_schema_metadata(
        schema=state_feature_schema.HERO_HAND_STATE_SCHEMA,
        base_dim=len(base),
    )

    assert len(extended) == len(base) + hand_context.HAND_CONTEXT_DIM
    assert metadata["state_dim"] == len(extended)
    assert metadata["response_private_state_masked"][:5] == list(range(5, 10))
    assert metadata["response_private_state_masked"][5:] == list(
        range(len(base), len(extended))
    )


def test_versioned_state_schema_rejects_unknown_contract() -> None:
    with pytest.raises(ValueError, match="unsupported state feature schema"):
        state_feature_schema.extend_state_features([], {}, schema="unknown")
