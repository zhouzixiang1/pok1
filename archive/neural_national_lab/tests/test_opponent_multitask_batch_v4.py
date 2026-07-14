from __future__ import annotations

import copy
from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import opponent_multitask_batch_v4 as batches  # noqa: E402
import opponent_multitask_model_v4 as models  # noqa: E402


def _value_row() -> dict:
    mask = [0, 0, 1, 1, 0, 1]
    return {
        "encoded_context_schema": "opponent_multitask_inference_context_v3",
        "encoded_row_schema": "opponent_multitask_encoded_row_v3",
        "response_mode": False,
        "state": [0.1] * 81,
        "opponent_profile": [0.2] * 12,
        "history": [[0.3] * 24],
        "cross_hand_sequence": [[0.25] * 15 + [-0.1]],
        "row_weight": 2.0,
        "opponent": "national_v119",
        "rule_action": [0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
        "strategy_context": [0.0] * 66,
        "strategy_context_available": True,
        "legal_action_mask": mask,
        "value_targets": {
            field: [0.0, 0.0, 0.0, 150.0, 0.0, -50.0]
            for field in models.VALUE_FIELDS
        },
        "value_target_masks": {
            field: list(mask) for field in models.VALUE_FIELDS
        },
        "match_outcome_supervision": {
            "schema": "national_70_hand_match_outcome_supervision_v1",
            "estimand": (
                "single_decision_70_hand_positive_outcome_uplift_clustered_v1"
            ),
            "hands": 70,
            "baseline_match_net_chips": -100.0,
            "baseline_match_positive": 0,
            "match_positive_targets": [0, 0, 0, 1, 0, 0],
            "match_positive_uplift_targets": [0, 0, 0, 1, 0, 0],
            "target_mask": list(mask),
        },
    }


def test_value_batch_contains_masked_absolute_outcome_targets() -> None:
    batch = batches.collate_encoded_rows([_value_row()], response=False)
    output = models.model_from_scale("small", dropout=0.0).forward_joint_value(
        **batch["inputs"]
    )

    assert batch["schema"] == "opponent_multitask_tensor_batch_v4"
    assert batch["supervision"]["match_positive_targets"].tolist() == [
        [0.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    ]
    assert batch["supervision"]["match_positive_target_mask"].tolist() == [
        [0.0, 0.0, 1.0, 1.0, 0.0, 1.0]
    ]
    assert output["match_positive_logits"].shape == (1, 6)


def test_v4_batch_rejects_missing_or_mismatched_outcome_supervision() -> None:
    missing = _value_row()
    missing.pop("match_outcome_supervision")
    with pytest.raises(ValueError, match="lacks match outcome"):
        batches.collate_encoded_rows([missing], response=False)

    mismatched = copy.deepcopy(_value_row())
    mismatched["match_outcome_supervision"]["target_mask"][5] = 0
    with pytest.raises(ValueError, match="masks differ"):
        batches.collate_encoded_rows([mismatched], response=False)


def test_v4_inference_does_not_require_outcome_targets() -> None:
    row = _value_row()
    for key in (
        "encoded_row_schema",
        "row_weight",
        "value_targets",
        "value_target_masks",
        "match_outcome_supervision",
    ):
        row.pop(key)

    batch = batches.collate_inference_rows([row], response=False)

    assert batch["schema"] == "opponent_multitask_inference_batch_v4"
    assert "supervision" not in batch
