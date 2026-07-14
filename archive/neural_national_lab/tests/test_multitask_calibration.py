from __future__ import annotations

from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import multitask_calibration as calibration  # noqa: E402


def test_weighted_quantile_respects_evaluation_weights() -> None:
    assert calibration.weighted_quantile([0.0, 10.0], [9.0, 1.0], 0.5) == 0.0
    assert calibration.weighted_quantile([0.0, 10.0], [1.0, 9.0], 0.5) == 10.0


def test_value_offsets_use_ess_gated_per_action_or_global_fallback() -> None:
    observations = []
    for index in range(12):
        observations.append({
            "field": "match_delta_vs_rule",
            "action_id": 0,
            "residual": -10.0 + index,
            "weight": 1.0,
            "opponent": "national_v1",
        })
    observations.append({
        "field": "match_delta_vs_rule",
        "action_id": 1,
        "residual": 100.0,
        "weight": 1.0,
        "opponent": "national_v2",
    })

    report = calibration.calibrate_value_lower_offsets(
        observations,
        value_fields=["match_delta_vs_rule"],
        num_actions=2,
        quantile=0.2,
        min_rows_per_action=10,
        min_ess_per_action=8.0,
    )
    field = report["fields"]["match_delta_vs_rule"]

    assert field["per_action"][0]["source"] == "per_action"
    assert field["per_action"][1]["source"] == "global_fallback"
    assert field["offsets"][1] == field["global_offset"]
    assert field["opponents"] == 2


def test_value_offsets_reject_nonpositive_weights() -> None:
    with pytest.raises(ValueError, match="positive"):
        calibration.calibrate_value_lower_offsets(
            [{
                "field": "hand",
                "action_id": 0,
                "residual": 1.0,
                "weight": 0.0,
                "opponent": "national_v1",
            }],
            value_fields=["hand"],
            num_actions=1,
        )


def _response_row(
    logits: list[float], target: int, legal: list[int], *, opponent: str = "v1"
) -> dict:
    return {
        "logits": logits,
        "target": target,
        "legal_action_mask": legal,
        "weight": 1.0,
        "opponent": opponent,
    }


def test_response_temperature_masks_illegal_logits() -> None:
    rows = [
        _response_row([0.0, 100.0, 5.0, 0.0, 0.0], 2, [1, 0, 1, 0, 0]),
        _response_row([0.0, 100.0, 4.0, 0.0, 0.0], 2, [1, 0, 1, 0, 0]),
    ]

    report = calibration.calibrate_response_temperature(rows)

    assert report["nll_after"] <= report["nll_before"]
    assert report["legal_action_counts"] == {"call": 2, "fold": 2}
    assert report["temperature"] < 1.0


def test_response_temperature_rejects_illegal_target() -> None:
    row = _response_row([0.0] * 5, 1, [1, 0, 1, 1, 1])

    with pytest.raises(ValueError, match="illegal"):
        calibration.calibrate_response_temperature([row])


def test_response_temperature_reports_each_opponent() -> None:
    rows = [
        _response_row([2.0, 0.0, 0.0, 0.0, 0.0], 0, [1] * 5, opponent="v1"),
        _response_row([0.0, 0.0, 2.0, 0.0, 0.0], 2, [1] * 5, opponent="v2"),
    ]

    report = calibration.calibrate_response_temperature(rows)

    assert set(report["by_opponent"]) == {"v1", "v2"}
    assert all(item["rows"] == 1 for item in report["by_opponent"].values())


def _calibration_phase() -> dict:
    return {
        "schema": "multitask_role_training_data_v1",
        "phase": "model_calibration",
        "run_id": "run-1",
        "role_manifest_sha256": "a" * 64,
        "checkpoint_sha256": "b" * 64,
        "opened_roles": ["model_calibration"],
        "roles": {
            "model_calibration": {
                "opponents": ["national_v142"],
                "value": [{"row": 1}],
                "behavior": [{"row": 2}],
                "weighting": {"value": {}, "behavior": {}},
                "provenance": {"artifact_sha256": "c" * 64},
            }
        },
    }


def test_calibration_artifact_binds_checkpoint_and_uses_no_policy_data() -> None:
    value = calibration.calibrate_value_lower_offsets(
        [], value_fields=["match"], num_actions=2
    )
    response = calibration.calibrate_response_temperature([])

    artifact = calibration.build_calibration_artifact(
        _calibration_phase(),
        value_lower=value,
        response_temperature=response,
    )

    assert artifact["checkpoint_sha256"] == "b" * 64
    assert artifact["calibration_artifact_sha256"] == "c" * 64
    assert artifact["policy_evidence_used"] is False
    assert len(artifact["payload_sha256"]) == 64


def test_calibration_artifact_rejects_wrong_phase() -> None:
    phase = _calibration_phase()
    phase["phase"] = "training"
    value = calibration.calibrate_value_lower_offsets(
        [], value_fields=["match"], num_actions=2
    )
    response = calibration.calibrate_response_temperature([])

    with pytest.raises(ValueError, match="invalid"):
        calibration.build_calibration_artifact(
            phase, value_lower=value, response_temperature=response
        )
