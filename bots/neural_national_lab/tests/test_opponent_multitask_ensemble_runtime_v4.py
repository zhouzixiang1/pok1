from __future__ import annotations

import copy
from pathlib import Path
import sys

import pytest
import torch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import export_opponent_multitask_v4 as exporter  # noqa: E402
import match_outcome_calibration as outcome_calibration  # noqa: E402
import opponent_multitask_ensemble_runtime_v3 as v3_ensemble  # noqa: E402
import opponent_multitask_ensemble_runtime_v4 as runtime  # noqa: E402
import opponent_multitask_model_v3 as v3_models  # noqa: E402
import opponent_multitask_model_v4 as models  # noqa: E402
import win_first_policy_v4 as win_first  # noqa: E402


def _inputs() -> dict[str, torch.Tensor]:
    generator = torch.Generator().manual_seed(20260711)
    return {
        "state": torch.rand(1, 81, generator=generator),
        "profile": torch.rand(1, 12, generator=generator),
        "history": torch.rand(1, 2, 24, generator=generator),
        "history_lengths": torch.tensor([2]),
        "cross_sequence": torch.rand(1, 2, 16, generator=generator),
        "cross_lengths": torch.tensor([2]),
        "rule_action": torch.tensor([[0.0, 0.0, 1.0, 0.0, 0.0, 0.0]]),
        "strategy_context": torch.rand(1, 66, generator=generator),
    }


def _stdlib_inputs(inputs: dict[str, torch.Tensor]) -> dict:
    return {
        "state": inputs["state"][0].tolist(),
        "profile": inputs["profile"][0].tolist(),
        "history": inputs["history"][0].tolist(),
        "cross_sequence": inputs["cross_sequence"][0].tolist(),
        "rule_action": inputs["rule_action"][0].tolist(),
        "strategy_context": inputs["strategy_context"][0].tolist(),
    }


def _outcome_calibration(checkpoint: str, *, bias: float) -> dict:
    payload = {
        "schema": outcome_calibration.CALIBRATION_SCHEMA,
        "method": outcome_calibration.CALIBRATION_METHOD,
        "scale": 1.0,
        "bias": bias,
        "run_id": "run-1",
        "model_format": models.MODEL_FORMAT,
        "checkpoint_sha256": checkpoint,
        "role_manifest_sha256": "b" * 64,
        "model_calibration_artifact_sha256": "c" * 64,
        "model_calibration_opponents": ["national_v142"],
        "source_collection_complete": False,
        "metrics": {},
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    payload["payload_sha256"] = (
        outcome_calibration.calibration_payload_sha256(payload)
    )
    return payload


def _selected_policy() -> dict:
    return {
        "schema": win_first.POLICY_SCHEMA,
        "selection_priority": win_first.SELECTION_PRIORITY,
        "min_positive_probability_lcb": 0.5,
        "min_probability_uplift_lcb": 0.0,
        "chip_margin": 0.0,
        "hand_weight": 0.25,
        "tail_weight": 0.25,
        "match_weight": 0.50,
        "response_weight": 0.0,
        "min_hand_lcb": 0.0,
        "use_lower": True,
    }


def _payload(*, selected: bool = True, calibrated: bool = True) -> dict:
    checkpoints = ["a" * 64, "d" * 64]
    members = []
    for seed, checkpoint in enumerate(checkpoints, 1):
        torch.manual_seed(seed)
        model = models.model_from_scale("small", dropout=0.0).eval()
        members.append(exporter.build_export_payload(
            model,
            {"schema": "test_v4_checkpoint", "code_artifacts": {}},
            checkpoint_sha256=checkpoint,
            outcome_calibration=(
                _outcome_calibration(checkpoint, bias=seed * 0.1)
                if calibrated else None
            ),
        ))
    policy = _selected_policy() if selected else None
    calibration = {
        "payload_sha256": "e" * 64,
        "member_checkpoint_sha256": checkpoints,
        "lower_quantile": 0.2,
        "uncertainty_std_weight": 1.0,
        "clips": {field: 2000.0 for field in v3_models.VALUE_FIELDS},
        "offsets": {
            field: [0.0] * 6 for field in v3_models.VALUE_FIELDS
        },
        "response_temperature": 1.0,
        "outcome_aggregation": win_first.OUTCOME_AGGREGATION_METHOD,
        "outcome_uncertainty_std_weight": 1.0,
        "outcome_calibration_payload_sha256": [
            member.get("outcome_calibration", {}).get("payload_sha256")
            for member in members
        ],
    }
    return {
        "format": runtime.ENSEMBLE_FORMAT,
        "members": members,
        "member_payload_sha256": [
            v3_ensemble._canonical_sha256(member) for member in members
        ],
        "calibration": calibration,
        "selected_policy": policy,
        "source": {
            "selected_policy_sha256": (
                v3_ensemble._canonical_sha256(policy)
                if policy is not None else None
            ),
            "policy_selection_passed": policy is not None,
            "deployment_policy_value": False,
            "strength_evidence": False,
        },
        "deployment_policy_value": False,
        "strength_evidence": False,
    }


def test_v4_ensemble_aggregates_calibrated_member_probabilities() -> None:
    payload = _payload()
    ensemble = runtime.OpponentMultiTaskEnsembleRuntimeV4(payload)
    inputs = _stdlib_inputs(_inputs())

    expected_members = [
        member.predict_match_outcome(**inputs)["probabilities"]
        for member in ensemble.members
    ]
    actual = ensemble.predict_match_outcomes(**inputs)

    expected = win_first.aggregate_member_probabilities(
        expected_members, uncertainty_std_weight=1.0
    )
    assert actual["mean"] == pytest.approx(expected["mean"], abs=1.0e-12)
    assert actual["lower"] == pytest.approx(expected["lower"], abs=1.0e-12)
    assert set(ensemble.predict_values(**inputs)) == set(v3_models.VALUE_FIELDS)


def test_v4_ensemble_selection_is_disabled_without_bound_policy() -> None:
    ensemble = runtime.OpponentMultiTaskEnsembleRuntimeV4(
        _payload(selected=False)
    )
    outcomes = win_first.aggregate_member_probabilities(
        [[0.1, 0.1, 0.2, 0.8, 0.1, 0.1]],
        uncertainty_std_weight=0.0,
    )
    values = {
        field: {"lower": [0.0, 0.0, 0.0, 10.0, 0.0, 0.0]}
        for field in v3_models.VALUE_FIELDS
    }

    assert ensemble.select_candidate(
        values, outcomes, [{"label_id": 3}], rule_label_id=2
    ) is None


def test_v4_ensemble_rejects_member_or_calibration_drift() -> None:
    with pytest.raises(ValueError, match="uncalibrated"):
        runtime.OpponentMultiTaskEnsembleRuntimeV4(
            _payload(calibrated=False)
        )

    payload = _payload()
    malformed = copy.deepcopy(payload)
    malformed["calibration"]["outcome_calibration_payload_sha256"][0] = (
        "f" * 64
    )
    with pytest.raises(ValueError, match="member binding changed"):
        runtime.OpponentMultiTaskEnsembleRuntimeV4(malformed)

    malformed = copy.deepcopy(payload)
    malformed["selected_policy"]["min_positive_probability_lcb"] = 0.49
    malformed["source"]["selected_policy_sha256"] = (
        v3_ensemble._canonical_sha256(malformed["selected_policy"])
    )
    with pytest.raises(ValueError, match="cannot be below 0.5"):
        runtime.OpponentMultiTaskEnsembleRuntimeV4(malformed)
