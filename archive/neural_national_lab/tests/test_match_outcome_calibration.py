from __future__ import annotations

import copy
from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import match_outcome_calibration as calibration  # noqa: E402


def _artifact() -> dict:
    payload = {
        "schema": calibration.CALIBRATION_SCHEMA,
        "method": calibration.CALIBRATION_METHOD,
        "scale": 1.5,
        "bias": -0.25,
        "run_id": "run-1",
        "model_format": "opponent_multitask_distributional_outcome_v4",
        "checkpoint_sha256": "a" * 64,
        "role_manifest_sha256": "b" * 64,
        "model_calibration_artifact_sha256": "c" * 64,
        "model_calibration_opponents": ["national_v142"],
        "source_collection_complete": False,
        "metrics": {},
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    payload["payload_sha256"] = calibration.calibration_payload_sha256(payload)
    return payload


def test_apply_calibration_uses_positive_scale_and_bias() -> None:
    result = calibration.apply_calibration(
        [-1.0, 0.0, 1.0],
        {
            "schema": calibration.CALIBRATION_SCHEMA,
            "method": calibration.CALIBRATION_METHOD,
            "scale": 2.0,
            "bias": 0.5,
        },
    )

    assert result["logits"] == [-1.5, 0.5, 2.5]
    assert result["probabilities"] == pytest.approx(
        [0.1824255, 0.6224593, 0.9241418]
    )


def test_calibration_artifact_binds_hash_checkpoint_and_model() -> None:
    payload = _artifact()

    validated = calibration.validate_calibration_artifact(
        payload,
        checkpoint_sha256="a" * 64,
        model_format="opponent_multitask_distributional_outcome_v4",
    )

    assert validated == payload
    changed = copy.deepcopy(payload)
    changed["bias"] = 1.0
    with pytest.raises(ValueError, match="payload hash changed"):
        calibration.validate_calibration_artifact(changed)
    with pytest.raises(ValueError, match="checkpoint does not match"):
        calibration.validate_calibration_artifact(
            payload, checkpoint_sha256="d" * 64
        )


@pytest.mark.parametrize("scale", [0.0, -1.0, float("inf")])
def test_calibration_rejects_nonpositive_or_nonfinite_scale(scale: float) -> None:
    with pytest.raises(ValueError):
        calibration.calibration_parameters({
            "schema": calibration.CALIBRATION_SCHEMA,
            "method": calibration.CALIBRATION_METHOD,
            "scale": scale,
            "bias": 0.0,
        })
