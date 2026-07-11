from __future__ import annotations

import copy
from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import multitask_training_data as data  # noqa: E402


def _profile() -> dict:
    return {
        "confidence": 0.25,
        "actions_total_norm": 0.125,
        "fold_rate": 0.10,
        "call_rate": 0.30,
        "check_rate": 0.40,
        "raise_rate": 0.15,
        "allin_rate": 0.05,
        "aggression": 0.20,
        "preflop_actions_norm": 0.10,
        "preflop_raise_rate": 0.15,
        "postflop_actions_norm": 0.15,
        "postflop_raise_rate": 0.05,
    }


def _cross_hand() -> list[float]:
    return [0.25, 0.1, 0.3, 0.4, 0.15, 0.05, 0.2, 0.2, 0.2,
            0.1, 0.2, 1.0, 0.0, 0.0, 1.0, -0.1]


def _request() -> dict:
    return {
        "my_id": 0,
        "dealer_id": 0,
        "my_chips": 19_950,
        "opponent_chips": 19_900,
        "my_stage_bet": 50,
        "opponent_stage_bet": 100,
        "pot": 150,
        "to_call": 50,
        "history": [],
        "my_cards": [0, 4],
        "public_cards": [],
        "remaining_hands": 70,
        "total_win_chips": [0, 0],
        "opponent_profile": _profile(),
        "cross_hand_sequence": [_cross_hand()],
        "cross_hand_sequence_schema": "public_opponent_hand_v1",
    }


def _value_row(opponent: str, seed: int, *, eligible: int = 24) -> dict:
    selected = 12
    probability = selected / eligible
    mask = [0, 0, 1, 1, 0, 1]
    hand = [None, None, 0.0, float(seed), None, -float(seed)]
    return {
        "opponent": opponent,
        "deck_seed_base": seed,
        "bot_seed_base": seed + 10_000,
        "decision_sampling": "uniform",
        "eligible_decisions": eligible,
        "selected_decisions": selected,
        "decision_inclusion_probability": probability,
        "decision_inverse_probability_weight": 1.0 / probability,
        "state_features": [0.5] * 48,
        "legal_mask": mask,
        "rule_label_id": 2,
        "delta_vs_rule": hand,
        "tail_delta_vs_rule": hand,
        "match_delta_vs_rule": hand,
        "target_masks": {
            field: mask
            for field in (
                "delta_vs_rule", "tail_delta_vs_rule", "match_delta_vs_rule"
            )
        },
        "opponent_profile_features": list(_profile().values()),
        "cross_hand_sequence_schema": "public_opponent_hand_v1",
        "cross_hand_sequence": [_cross_hand()],
        "request": _request(),
        "state": {"round": 0, "pot": 150, "to_call": 50},
    }


def _behavior_row(opponent: str, seed: int) -> dict:
    return {
        "opponent": opponent,
        "deck_seed_base": seed,
        "bot_seed_base": seed + 10_000,
        "stage": "preflop",
        "hero_action": 200,
        "hero_action_label_id": 2,
        "opponent_action": "call",
        "opponent_action_label_id": 2,
        "opponent_action_amount": 100,
        "opponent_action_amount_norm": 100 / 20_000,
        "opponent_action_pot_ratio": 100 / 150,
        "state_features": [0.5] * 48,
        "opponent_profile_features": list(_profile().values()),
        "cross_hand_sequence_schema": "public_opponent_hand_v1",
        "cross_hand_sequence": [_cross_hand()],
        "request": _request(),
        "state": {"round": 0, "pot": 150, "to_call": 50},
    }


class _Dataset:
    run_id = "model-run"
    manifest_sha256 = "f" * 64

    def __init__(self) -> None:
        self.opened: list[str] = []
        self.payloads = {
            "train": {
                "opponents": ["national_v1", "national_v2"],
                "value": [
                    _value_row("national_v1", 1, eligible=24),
                    _value_row("national_v1", 2, eligible=48),
                    _value_row("national_v2", 3, eligible=12),
                ],
                "behavior": [
                    _behavior_row("national_v1", 1),
                    _behavior_row("national_v1", 2),
                    _behavior_row("national_v2", 3),
                ],
            },
            "early_stop": {
                "opponents": ["national_v3"],
                "value": [_value_row("national_v3", 4)],
                "behavior": [_behavior_row("national_v3", 4)],
            },
            "model_calibration": {
                "opponents": ["national_v4"],
                "value": [_value_row("national_v4", 5)],
                "behavior": [_behavior_row("national_v4", 5)],
            },
        }

    def open_role(self, role: str) -> dict:
        self.opened.append(role)
        payload = copy.deepcopy(self.payloads[role])
        return {
            "role": role,
            "artifact_sha256": role[0] * 64,
            "manifest_sha256": self.manifest_sha256,
            "candidate_sha256": None,
            **payload,
        }


def _authorization(dataset: _Dataset, training: dict) -> dict:
    return {
        "schema": "frozen_model_checkpoint_v1",
        "frozen": True,
        "early_stop_complete": True,
        "run_id": dataset.run_id,
        "role_manifest_sha256": dataset.manifest_sha256,
        "training_roles": ["train", "early_stop"],
        "training_artifact_sha256": {
            role: training["roles"][role]["provenance"]["artifact_sha256"]
            for role in ("train", "early_stop")
        },
        "checkpoint_sha256": "a" * 64,
    }


def _prepared(dataset: _Dataset) -> dict:
    training = data.prepare_training_phase(dataset)
    authorization = _authorization(dataset, training)
    calibration = data.prepare_model_calibration(
        dataset, training, authorization
    )
    return data.combine_model_development(training, calibration)


def test_training_phase_does_not_open_model_calibration() -> None:
    dataset = _Dataset()

    prepared = data.prepare_training_phase(dataset)

    assert dataset.opened == ["train", "early_stop"]
    assert prepared["opened_roles"] == dataset.opened
    assert "model_calibration" not in prepared["roles"]


def test_model_development_requires_frozen_checkpoint_before_calibration() -> None:
    dataset = _Dataset()

    prepared = _prepared(dataset)

    assert dataset.opened == ["train", "early_stop", "model_calibration"]
    assert prepared["opened_roles"] == dataset.opened
    assert prepared["policy_roles_opened"] is False
    assert prepared["checkpoint_sha256"] == "a" * 64


def test_training_uses_ipw_and_balances_both_modalities() -> None:
    dataset = _Dataset()
    prepared = _prepared(dataset)
    train = prepared["roles"]["train"]

    value_report = train["weighting"]["value"]
    behavior_report = train["weighting"]["behavior"]
    assert value_report["sampling_ipw_used"] is True
    assert value_report["opponent_balanced"] is True
    assert behavior_report["sampling_ipw_used"] is False
    assert behavior_report["opponent_balanced"] is True
    assert value_report["per_opponent"]["national_v1"]["total_weight"] == pytest.approx(
        value_report["per_opponent"]["national_v2"]["total_weight"]
    )
    assert behavior_report["per_opponent"]["national_v1"]["total_weight"] == pytest.approx(
        behavior_report["per_opponent"]["national_v2"]["total_weight"]
    )
    assert all(data.TRAIN_WEIGHT_FIELD in row for row in train["value"])


def test_early_stop_and_calibration_weights_cannot_update_gradients() -> None:
    dataset = _Dataset()
    prepared = _prepared(dataset)

    for role in ("early_stop", "model_calibration"):
        payload = prepared["roles"][role]
        for modality in ("value", "behavior"):
            assert payload["weighting"][modality]["used_for_gradient_updates"] is False
            assert all(
                data.EVALUATION_WEIGHT_FIELD in row
                and data.TRAIN_WEIGHT_FIELD not in row
                for row in payload[modality]
            )


def test_behavior_rows_are_upgraded_and_targets_are_legal() -> None:
    dataset = _Dataset()
    train = _prepared(dataset)["roles"]["train"]

    for row in train["behavior"]:
        assert row["response_schema"] == "national_opponent_response_v2"
        target = row["opponent_action_label_id"]
        assert row["response_target_mask"] == 1
        assert row["response_legal_action_mask"][target] == 1
        assert row["response_aggressive_increment"] == 0


def test_invalid_checkpoint_cannot_open_calibration_data() -> None:
    dataset = _Dataset()
    training = data.prepare_training_phase(dataset)
    authorization = _authorization(dataset, training)
    authorization["early_stop_complete"] = False

    with pytest.raises(ValueError, match="not bound"):
        data.prepare_model_calibration(dataset, training, authorization)

    assert dataset.opened == ["train", "early_stop"]


def test_checkpoint_authorization_is_bound_to_opened_training_artifacts() -> None:
    dataset = _Dataset()
    training = data.prepare_training_phase(dataset)
    authorization = _authorization(dataset, training)
    authorization["training_artifact_sha256"]["train"] = "b" * 64

    with pytest.raises(ValueError, match="not bound"):
        data.prepare_model_calibration(dataset, training, authorization)

    assert dataset.opened == ["train", "early_stop"]


def test_encoded_response_has_public_state_mask_and_legal_target() -> None:
    dataset = _Dataset()
    prepared = _prepared(dataset)
    row = prepared["roles"]["train"]["behavior"][0]

    encoded = data.encode_prepared_row(row, response=True)

    assert len(encoded["state"]) == 81
    assert encoded["response_mode"] is True
    assert encoded["response_target"] == 2
    assert encoded["response_legal_action_mask"] == [1, 0, 1, 1, 1]
    assert len(encoded["hero_action_features"]) == 10
    assert encoded["opponent_profile"] == list(_profile().values())
    assert encoded["cross_hand_sequence"] == [_cross_hand()]
    assert encoded["cross_hand_sequence_schema"] == "public_opponent_hand_v1"
    assert encoded["response_size_targets"] == [0.0, 0.0]
    assert encoded["response_size_target_mask"] == [0, 0]
    assert all(
        encoded["state"][index] == 0.0
        for index in encoded["response_private_state_masked"]
    )
    assert encoded["strategy_context"] == []


def test_encoded_value_accepts_only_versioned_strategy_context() -> None:
    dataset = _Dataset()
    prepared = _prepared(dataset)
    row = prepared["roles"]["train"]["value"][0]
    row["strategy_context_schema"] = "v140_strategy_context_v1"
    row["strategy_context_features"] = [0.25] * 66

    encoded = data.encode_prepared_row(row, response=False)

    assert len(encoded["state"]) == 81
    assert encoded["response_mode"] is False
    assert len(encoded["strategy_context"]) == 66
    assert encoded["strategy_context_available"] is True
    assert encoded["rule_action"] == [0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
    assert encoded["value_target_masks"]["delta_vs_rule"] == [0, 0, 1, 1, 0, 1]
    assert encoded["value_targets"]["delta_vs_rule"] == [
        0.0, 0.0, 0.0, 1.0, 0.0, -1.0
    ]

    row["strategy_context_schema"] = "unknown"
    with pytest.raises(ValueError, match="unsupported strategy"):
        data.encode_prepared_row(row, response=False)


def test_value_inference_context_requires_no_target_or_role_weight() -> None:
    row = _value_row("national_v119", 11)
    for field in data.VALUE_FIELDS:
        row.pop(field)
    row.pop("target_masks")
    row["strategy_context_schema"] = "v140_strategy_context_v1"
    row["strategy_context_features"] = [0.25] * 66

    encoded = data.encode_value_inference_row(row)

    assert encoded["encoded_context_schema"] == (
        "opponent_multitask_inference_context_v3"
    )
    assert encoded["response_mode"] is False
    assert len(encoded["state"]) == 81
    assert len(encoded["opponent_profile"]) == 12
    assert len(encoded["history"]) == 0
    assert encoded["cross_hand_sequence"] == [_cross_hand()]
    assert encoded["rule_action"] == [0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
    assert encoded["legal_action_mask"] == [0, 0, 1, 1, 0, 1]
    assert encoded["strategy_context"] == [0.25] * 66
    assert "row_weight" not in encoded
    assert "value_targets" not in encoded


def test_hypothetical_response_context_uses_national_legality() -> None:
    row = _value_row("national_v119", 12)
    row["hero_action"] = 200
    row["hero_action_label_id"] = 2

    encoded = data.encode_response_inference_row(row)

    assert encoded is not None
    assert encoded["encoded_context_schema"] == (
        "opponent_multitask_inference_context_v3"
    )
    assert encoded["response_mode"] is True
    assert encoded["response_legal_action_mask"] == [1, 0, 1, 1, 1]
    assert encoded["response_context"]["minimum_raise_to_total"] == 401
    assert len(encoded["hero_action_features"]) == 10
    assert all(
        encoded["state"][index] == 0.0
        for index in encoded["response_private_state_masked"]
    )
    assert "response_target" not in encoded
    assert "row_weight" not in encoded


def test_response_inference_omits_actions_that_settle_or_close() -> None:
    folded = _value_row("national_v119", 13)
    folded["hero_action"] = -1
    folded["hero_action_label_id"] = 0
    assert data.encode_response_inference_row(folded) is None

    called = _value_row("national_v119", 14)
    called["hero_action"] = 0
    called["hero_action_label_id"] = 1
    called["request"].update({
        "my_stage_bet": 100,
        "opponent_stage_bet": 200,
        "to_call": 100,
        "history": [{
            "round": 1,
            "player_id": 1,
            "action_type": "raise",
            "stage_bet": 200,
            "chips_after": 19_800,
        }],
    })
    called["state"].update({"round": 1, "to_call": 100, "pot": 300})
    assert data.encode_response_inference_row(called) is None


def test_duplicate_opponent_across_model_roles_is_rejected() -> None:
    dataset = _Dataset()
    dataset.payloads["early_stop"]["opponents"] = ["national_v1"]
    dataset.payloads["early_stop"]["value"] = [_value_row("national_v1", 4)]
    dataset.payloads["early_stop"]["behavior"] = [_behavior_row("national_v1", 4)]

    with pytest.raises(RuntimeError, match="multiple model roles"):
        _prepared(dataset)


def test_profile_vector_must_match_request_profile() -> None:
    dataset = _Dataset()
    row = _prepared(dataset)["roles"]["train"]["value"][0]
    row["opponent_profile_features"][3] = 0.99

    with pytest.raises(ValueError, match="disagrees"):
        data.encode_prepared_row(row, response=False)


def test_cross_hand_sequence_requires_versioned_strict_rows() -> None:
    dataset = _Dataset()
    row = _prepared(dataset)["roles"]["train"]["value"][0]
    row["cross_hand_sequence_schema"] = "legacy"

    with pytest.raises(ValueError, match="unsupported cross-hand"):
        data.encode_prepared_row(row, response=False)

    row["cross_hand_sequence_schema"] = "public_opponent_hand_v1"
    row["cross_hand_sequence"] = [[0.0] * 15]
    with pytest.raises(ValueError, match="wrong dimension"):
        data.encode_prepared_row(row, response=False)

    row["cross_hand_sequence"] = [_cross_hand()]
    row["cross_hand_sequence"][0][0] = 0.75
    with pytest.raises(ValueError, match="row and request"):
        data.encode_prepared_row(row, response=False)


def test_missing_strategy_context_is_fixed_zero_vector() -> None:
    dataset = _Dataset()
    row = _prepared(dataset)["roles"]["train"]["value"][0]

    encoded = data.encode_prepared_row(row, response=False)

    assert encoded["strategy_context"] == [0.0] * 66
    assert encoded["strategy_context_available"] is False


def test_metadata_freezes_role_and_feature_contracts() -> None:
    metadata = data.training_data_metadata()

    assert metadata["model_development_roles"] == [
        "train", "early_stop", "model_calibration"
    ]
    assert metadata["policy_roles_forbidden"] == ["policy_selection", "policy_gate"]
    assert metadata["frozen_checkpoint_schema"] == "frozen_model_checkpoint_v1"
    assert metadata["encoded_context_schema"] == (
        "opponent_multitask_inference_context_v3"
    )
    assert metadata["model_input"]["state_dim"] == 81
    assert metadata["model_input"]["history_feature_dim"] == 24
    assert metadata["max_current_hand_history"] == 16
    assert metadata["opponent_profile"]["dim"] == 12
    assert metadata["cross_hand_sequence"]["dim"] == 16
    assert metadata["cross_hand_sequence"]["max_hands"] == 32
    assert metadata["hero_response_action"]["dim"] == 10
