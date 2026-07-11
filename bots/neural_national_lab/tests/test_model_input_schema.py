from __future__ import annotations

from pathlib import Path
import sys


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import decision_context_features  # noqa: E402
import feature_spec  # noqa: E402
import hand_context_features  # noqa: E402
import history_feature_schema  # noqa: E402
import model_input_schema  # noqa: E402


def _row() -> dict:
    request = {
        "my_id": 0,
        "dealer_id": 0,
        "my_cards": [48, 45],
        "public_cards": [44, 40, 3],
        "my_chips": 12_000,
        "opponent_chips": 8_000,
        "remaining_hands": 9,
        "total_win_chips": [30_000, -30_000],
        "pot": 4_000,
        "to_call": 1_000,
        "history": [
            {
                "round": 1,
                "player_id": 0,
                "action_type": "raise",
                "action": 1_000,
                "pot_after": 2_500,
                "chips_after": 12_000,
            },
            {
                "round": 1,
                "player_id": 1,
                "action_type": "call",
                "committed": 1_000,
                "pot_after": 4_000,
                "chips_after": 8_000,
            },
        ],
    }
    return {
        "request": request,
        "state": {
            "stacks": [12_000, 8_000],
            "pot": 4_000,
            "to_call": 1_000,
            "min_raise_action": 3_000,
            "allin_call_amount": 7_000,
        },
        "legal_mask": [1, 1, 1, 1, 0, 1],
    }


def _base(row: dict) -> list[float]:
    request = dict(row["request"])
    request.update({
        key: row["state"][key] for key in ("pot", "to_call")
    })
    return feature_spec.encode_features(request)


def test_value_input_composes_all_reconstructable_features() -> None:
    row = _row()
    encoded = model_input_schema.encode_model_input(row, _base(row))

    assert encoded["schema"] == "opponent_multitask_input_v2"
    assert len(encoded["state"]) == (
        48
        + hand_context_features.HAND_CONTEXT_DIM
        + decision_context_features.DECISION_CONTEXT_DIM
    )
    assert len(encoded["history"]) == 2
    assert len(encoded["history"][0]) == (
        history_feature_schema.ACTOR_AWARE_HISTORY_FEATURE_DIM
    )
    first_actor = encoded["history"][0][
        history_feature_schema.ACTOR_FEATURE_SLICE
    ]
    second_actor = encoded["history"][1][
        history_feature_schema.ACTOR_FEATURE_SLICE
    ]
    assert first_actor == [1.0, 0.0, 0.0]
    assert second_actor == [0.0, 1.0, 0.0]
    assert encoded["state"][-6:] == [1.0, 1.0, 1.0, 1.0, 0.0, 1.0]


def test_response_masks_private_features_but_keeps_public_context() -> None:
    row = _row()
    base = _base(row)
    value = model_input_schema.encode_model_input(row, base)
    response = model_input_schema.encode_model_input(row, base, response=True)
    private = set(response["response_private_state_masked"])

    assert private
    for index, (value_feature, response_feature) in enumerate(zip(
        value["state"], response["state"]
    )):
        if index in private:
            assert response_feature == 0.0
        else:
            assert response_feature == value_feature
    assert response["state"][-decision_context_features.DECISION_CONTEXT_DIM:] == (
        value["state"][-decision_context_features.DECISION_CONTEXT_DIM:]
    )


def test_metadata_matches_encoded_shapes_and_declares_missing_strategy() -> None:
    metadata = model_input_schema.model_input_metadata(base_state_dim=48)

    assert metadata["state_dim"] == 81
    assert metadata["history_feature_dim"] == 24
    assert metadata["strategy_context_captured"] is False
    assert metadata["strategy_context_schema"] is None


def test_history_is_bounded_to_latest_configured_events() -> None:
    row = _row()
    row["request"]["history"] *= 10

    encoded = model_input_schema.encode_model_input(
        row, _base(row), max_hist=3
    )

    assert len(encoded["history"]) == 3
