from __future__ import annotations

from pathlib import Path
import sys

import pytest
import torch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import calibrate_match_outcome_v4 as calibrator  # noqa: E402
from multitask_training_data import MODEL_TRAINING_ROLES  # noqa: E402


def test_fit_probability_calibration_keeps_identity_as_safe_candidate() -> None:
    logits = torch.tensor([-4.0, -2.0, -0.5, 0.5, 2.0, 4.0])
    targets = torch.tensor([0.0, 0.0, 1.0, 0.0, 1.0, 1.0])
    weights = torch.tensor([1.0, 2.0, 1.0, 1.0, 2.0, 1.0])
    before = calibrator.probability_metrics(
        logits, targets, weights, scale=1.0, bias=0.0
    )

    fitted = calibrator.fit_probability_calibration(
        logits,
        targets,
        weights,
        steps=300,
        learning_rate=0.03,
        l2=1.0e-4,
    )
    after = calibrator.probability_metrics(
        logits,
        targets,
        weights,
        scale=fitted["scale"],
        bias=fitted["bias"],
    )

    assert fitted["scale"] > 0.0
    assert after["nll"] <= before["nll"] + 1.0e-7


def test_probability_metrics_reports_each_class_accuracy_correctly() -> None:
    report = calibrator.probability_metrics(
        torch.tensor([-2.0, 2.0]),
        torch.tensor([0.0, 1.0]),
        torch.ones(2),
        scale=1.0,
        bias=0.0,
    )

    assert report["class_accuracy"] == {
        "nonpositive": 1.0,
        "positive": 1.0,
    }
    assert report["balanced_accuracy"] == 1.0


def test_fit_probability_calibration_requires_both_classes() -> None:
    with pytest.raises(ValueError, match="both outcome classes"):
        calibrator.fit_probability_calibration(
            torch.tensor([-1.0, 1.0]),
            torch.tensor([1.0, 1.0]),
            torch.ones(2),
            steps=2,
            learning_rate=0.1,
            l2=0.0,
        )


class _Dataset:
    run_id = "run-1"
    manifest_sha256 = "f" * 64

    def _role_artifact_sha256(self, role: str) -> str:
        return {
            "train": "a" * 64,
            "early_stop": "b" * 64,
        }[role]


def test_training_phase_reconstruction_rejects_artifact_drift() -> None:
    checkpoint = {
        "training_artifact_sha256": {
            "train": "a" * 64,
            "early_stop": "b" * 64,
        }
    }

    phase = calibrator._training_phase_from_checkpoint(_Dataset(), checkpoint)

    assert phase["opened_roles"] == list(MODEL_TRAINING_ROLES)
    checkpoint["training_artifact_sha256"]["train"] = "c" * 64
    with pytest.raises(ValueError, match="does not match role data"):
        calibrator._training_phase_from_checkpoint(_Dataset(), checkpoint)
