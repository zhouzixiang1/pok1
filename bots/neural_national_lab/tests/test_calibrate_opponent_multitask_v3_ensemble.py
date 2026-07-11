from __future__ import annotations

from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import calibrate_opponent_multitask_v3_ensemble as ensemble  # noqa: E402
import opponent_multitask_model_v3 as models  # noqa: E402


def _summary(*, formal: bool) -> dict:
    selected = {
        "scale": "small",
        "encoder": "deep_set",
        "requested_seeds": [101, 211, 307] if formal else [101],
    }
    seeds = selected["requested_seeds"]
    return {
        "schema": "opponent_multitask_v3_scaling_summary_v1",
        "model_calibration_opened": False,
        "policy_roles_opened": False,
        "strength_evidence": False,
        "selection_eligible": formal,
        "selected_configuration": selected if formal else None,
        "provisional_best_configuration": selected,
        "runs": [
            {
                "completed": True,
                "scale": "small",
                "encoder": "deep_set",
                "seed": seed,
                "checkpoint_sha256": f"{seed:064x}",
                "output_dir": f"/tmp/seed-{seed}",
            }
            for seed in seeds
        ],
    }


def _common(*, response: bool) -> dict:
    return {
        "encoded_context_schema": "opponent_multitask_inference_context_v3",
        "encoded_row_schema": "opponent_multitask_encoded_row_v3",
        "response_mode": response,
        "state": [0.1] * 81,
        "opponent_profile": [0.2] * 12,
        "history": [[0.3] * 24],
        "cross_hand_sequence": [[0.25] * 15 + [-0.1]],
        "row_weight": 1.0,
        "opponent": "national_v142",
    }


def _value_row() -> dict:
    row = _common(response=False)
    mask = [0, 0, 1, 1, 0, 1]
    row.update({
        "rule_label_id": 2,
        "rule_action": [0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
        "strategy_context": [0.0] * 66,
        "strategy_context_available": False,
        "legal_action_mask": mask,
        "value_targets": {
            field: [0.0, 0.0, 0.0, 200.0, 0.0, -150.0]
            for field in models.VALUE_FIELDS
        },
        "value_target_masks": {
            field: list(mask) for field in models.VALUE_FIELDS
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


def test_formal_calibration_requires_formally_selected_three_seed_grid() -> None:
    selected, rows = ensemble.selected_scaling_runs(
        _summary(formal=True), allow_incomplete_smoke=False
    )

    assert selected["encoder"] == "deep_set"
    assert [row["seed"] for row in rows] == [101, 211, 307]

    with pytest.raises(ValueError, match="not eligible"):
        ensemble.selected_scaling_runs(
            _summary(formal=False), allow_incomplete_smoke=False
        )


def test_incomplete_smoke_uses_only_provisional_configuration() -> None:
    selected, rows = ensemble.selected_scaling_runs(
        _summary(formal=False), allow_incomplete_smoke=True
    )

    assert selected["requested_seeds"] == [101]
    assert len(rows) == 1


def test_ensemble_prediction_has_zero_epistemic_std_for_identical_members() -> None:
    first = models.model_from_scale("small", dropout=0.0)
    second = models.model_from_scale("small", dropout=0.0)
    second.load_state_dict(first.state_dict())
    role = {"value": [_value_row()], "behavior": [_response_row()]}

    values, responses, diagnostics = ensemble.ensemble_calibration_predictions(
        [first, second],
        role,
        clips={field: 2000.0 for field in models.VALUE_FIELDS},
        batch_size=1,
        device="cpu",
        lower_quantile=0.20,
        uncertainty_std_weight=1.0,
    )

    assert len(values) == 6
    assert len(responses) == 1
    assert diagnostics["response_legal_logit_epistemic_std"] == 0.0
    assert all(
        value == 0.0
        for value in diagnostics["value_mean_epistemic_std"].values()
    )


def test_ensemble_calibration_payload_binds_every_member() -> None:
    base = {
        "schema": "multitask_model_calibration_v1",
        "payload_sha256": "a" * 64,
        "checkpoint_sha256": "b" * 64,
    }
    members = [
        {"seed": 101, "checkpoint_sha256": "c" * 64},
        {"seed": 211, "checkpoint_sha256": "d" * 64},
    ]

    artifact = ensemble._ensemble_calibration_artifact(
        base,
        ensemble_manifest_sha256="e" * 64,
        members=members,
        lower_quantile=0.20,
        uncertainty_std_weight=1.0,
        diagnostics={},
    )

    assert artifact["schema"] == ensemble.ENSEMBLE_CALIBRATION_SCHEMA
    assert artifact["ensemble"]["members"] == members
    assert artifact["deployment_policy_value"] is False
    assert artifact["strength_evidence"] is False
    assert len(artifact["payload_sha256"]) == 64
