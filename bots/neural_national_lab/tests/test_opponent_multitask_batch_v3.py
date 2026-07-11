from __future__ import annotations

import copy
from pathlib import Path
import sys

import pytest
import torch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import opponent_multitask_batch_v3 as batches  # noqa: E402
import opponent_multitask_model_v3 as models  # noqa: E402


def _common(*, response: bool) -> dict:
    return {
        "encoded_row_schema": "opponent_multitask_encoded_row_v3",
        "response_mode": response,
        "state": [0.1] * 81,
        "opponent_profile": [0.2] * 12,
        "history": [[0.3] * 24, [0.4] * 24],
        "cross_hand_sequence": [[0.25] * 15 + [-0.1]],
        "row_weight": 1.5,
        "opponent": "national_v119",
    }


def _value_row() -> dict:
    row = _common(response=False)
    mask = [0, 0, 1, 1, 0, 1]
    row.update({
        "rule_action": [0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
        "strategy_context": [0.0] * 66,
        "strategy_context_available": False,
        "legal_action_mask": mask,
        "value_targets": {
            field: [0.0, 0.0, 0.0, 100.0, 0.0, -50.0]
            for field in models.VALUE_FIELDS
        },
        "value_target_masks": {
            field: mask for field in models.VALUE_FIELDS
        },
    })
    return row


def _response_row() -> dict:
    row = _common(response=True)
    row.update({
        "hero_action_features": [0.0, 0.0, 1.0, 0.0, 0.0, 0.0,
                                 0.01, 0.20, 0.01, 0.99],
        "response_legal_action_mask": [1, 0, 1, 1, 1],
        "response_target": 2,
        "response_size_targets": [0.0, 0.0],
        "response_size_target_mask": [0, 0],
    })
    return row


def test_value_collator_pads_sequences_and_feeds_model() -> None:
    first = _value_row()
    second = copy.deepcopy(first)
    second["history"] = []
    second["cross_hand_sequence"] = []
    second["opponent"] = "national_v72"

    batch = batches.collate_encoded_rows([first, second], response=False)

    assert batch["schema"] == "opponent_multitask_tensor_batch_v3"
    assert batch["inputs"]["state"].shape == (2, 81)
    assert batch["inputs"]["history"].shape == (2, 2, 24)
    assert batch["inputs"]["history_lengths"].tolist() == [2, 0]
    assert batch["inputs"]["cross_sequence"].shape == (2, 1, 16)
    assert batch["inputs"]["cross_lengths"].tolist() == [1, 0]
    assert batch["supervision"]["targets"]["match_delta_vs_rule"].shape == (
        2, 6
    )

    model = models.model_from_scale("small", dropout=0.0)
    output = model.forward_value(**batch["inputs"])
    assert output["match_delta_vs_rule"]["quantiles"].shape == (2, 6, 4)


def test_response_collator_masks_legality_and_feeds_model() -> None:
    batch = batches.collate_encoded_rows([_response_row()], response=True)

    assert batch["inputs"]["hero_action"].shape == (1, 10)
    assert batch["supervision"]["target"].tolist() == [2]
    assert batch["supervision"]["size_target_mask"].tolist() == [[0.0, 0.0]]

    model = models.model_from_scale("small", dropout=0.0)
    output = model.forward_response(**batch["inputs"])
    assert output["logits"].shape == (1, 5)
    assert output["logits"][0, 1] < -1.0e8


def test_collator_rejects_schema_mode_and_dimension_drift() -> None:
    row = _value_row()
    row["encoded_row_schema"] = "old"
    with pytest.raises(ValueError, match="wrong encoded schema"):
        batches.collate_encoded_rows([row], response=False)

    row = _value_row()
    row["response_mode"] = True
    with pytest.raises(ValueError, match="wrong task mode"):
        batches.collate_encoded_rows([row], response=False)

    row = _value_row()
    row["opponent_profile"] = [0.0] * 11
    with pytest.raises(ValueError, match="wrong dimension"):
        batches.collate_encoded_rows([row], response=False)


def test_collator_rejects_illegal_response_target() -> None:
    row = _response_row()
    row["response_target"] = 1

    with pytest.raises(ValueError, match="response target is illegal"):
        batches.collate_encoded_rows([row], response=True)


def test_model_and_batch_dimensions_share_the_data_contract() -> None:
    model = models.model_from_scale("medium", dropout=0.0)
    metadata = model.metadata()

    assert metadata["state_dim"] == batches.STATE_DIM == 81
    assert metadata["history_dim"] == batches.HISTORY_DIM == 24
    assert metadata["profile_dim"] == 12
    assert metadata["opponent_profile_schema"] == "opponent_profile_features_v1"
    assert len(metadata["opponent_profile_fields"]) == 12
