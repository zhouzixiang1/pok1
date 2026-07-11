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


def _outcome_calibration(
    checkpoint: str, *, bias: float, member_seed: int
) -> dict:
    payload = {
        "schema": outcome_calibration.CALIBRATION_SCHEMA,
        "method": outcome_calibration.CALIBRATION_METHOD,
        "scale": 1.0,
        "bias": bias,
        "run_id": "run-1",
        "member_seed": member_seed,
        "model_format": models.MODEL_FORMAT,
        "checkpoint_sha256": checkpoint,
        "role_manifest_sha256": "b" * 64,
        "model_calibration_artifact_sha256": "c" * 64,
        "model_calibration_opponents": ["national_v142"],
        "calibration_role": "model_calibration",
        "policy_evidence_used": False,
        "source_collection_complete": True,
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
    checkpoints = ["a" * 64, "d" * 64, "f" * 64]
    member_seeds = [101, 211, 307]
    members = []
    for index, (member_seed, checkpoint) in enumerate(
        zip(member_seeds, checkpoints, strict=True), 1
    ):
        torch.manual_seed(index)
        model = models.model_from_scale("small", dropout=0.0).eval()
        members.append(exporter.build_export_payload(
            model,
            {
                "schema": exporter.CHECKPOINT_SCHEMA,
                "role_manifest_sha256": "b" * 64,
                "training_artifact_sha256": {
                    "train": "7" * 64,
                    "early_stop": "8" * 64,
                },
                "source_collection_complete": True,
                "code_artifacts": {
                    "trainer": {"bytes": 1, "sha256": "9" * 64}
                },
            },
            checkpoint_sha256=checkpoint,
            outcome_calibration=(
                _outcome_calibration(
                    checkpoint, bias=index * 0.1, member_seed=member_seed
                )
                if calibrated else None
            ),
        ))
    policy = _selected_policy() if selected else None
    calibration = {
        "member_seed": member_seeds,
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
            or "0" * 64
            for member in members
        ],
        "run_id": "run-1",
        "role_manifest_sha256": "b" * 64,
        "model_calibration_artifact_sha256": "c" * 64,
        "model_calibration_opponents": ["national_v142"],
        "source_collection_complete": True,
    }
    original_calibration = {
        "schema": runtime.ORIGINAL_CALIBRATION_SCHEMA,
        "run_id": "run-1",
        "role_manifest_sha256": "b" * 64,
        "calibration_role": "model_calibration",
        "calibration_artifact_sha256": "c" * 64,
        "opponents": ["national_v142"],
        "policy_evidence_used": False,
        "ensemble": {
            "members": [
                {"seed": seed, "checkpoint_sha256": checkpoint}
                for seed, checkpoint in zip(
                    member_seeds, checkpoints, strict=True
                )
            ],
            "lower_quantile": calibration["lower_quantile"],
            "uncertainty_std_weight": calibration[
                "uncertainty_std_weight"
            ],
            "outcome_aggregation": calibration["outcome_aggregation"],
            "outcome_uncertainty_std_weight": calibration[
                "outcome_uncertainty_std_weight"
            ],
            "outcome_calibration_payload_sha256": list(
                calibration["outcome_calibration_payload_sha256"]
            ),
        },
        "value_lower": {
            "target_preprocessing": "symmetric_clip_before_residual",
            "target_clips": dict(calibration["clips"]),
            "fields": {
                field: {"offsets": list(calibration["offsets"][field])}
                for field in v3_models.VALUE_FIELDS
            },
        },
        "response_temperature": {
            "temperature": calibration["response_temperature"]
        },
        "source_collection_complete": True,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    original_calibration["payload_sha256"] = (
        v3_ensemble._canonical_sha256(original_calibration)
    )
    projection = runtime.calibration_projection_from_artifact(
        original_calibration
    )
    projection_sha256 = runtime.calibration_projection_sha256(projection)
    calibration.update({
        "payload_sha256": original_calibration["payload_sha256"],
        "original_calibration_artifact": original_calibration,
        "original_calibration_file_sha256": "e" * 64,
        "calibration_projection_sha256": projection_sha256,
    })
    return {
        "schema": runtime.BUNDLE_SCHEMA,
        "format": runtime.ENSEMBLE_FORMAT,
        "members": members,
        "member_payload_sha256": [
            v3_ensemble._canonical_sha256(member) for member in members
        ],
        "calibration": calibration,
        "selected_policy": policy,
        "source": {
            "run_id": "run-1",
            "role_manifest_sha256": "b" * 64,
            "source_collection_complete": True,
            "source_completed_passes": 160,
            "source_requested_passes": 160,
            "calibration_payload_sha256": original_calibration[
                "payload_sha256"
            ],
            "calibration_file_sha256": "e" * 64,
            "calibration_projection_sha256": projection_sha256,
            "candidate_snapshot": {
                "name": "v140_test",
                "sha256": "6" * 64,
            },
            "strategy_context_runtime_mode": (
                runtime.STRATEGY_CONTEXT_RUNTIME_MODE
            ),
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
    with pytest.raises(ValueError, match="differs from original"):
        runtime.OpponentMultiTaskEnsembleRuntimeV4(malformed)

    malformed = copy.deepcopy(payload)
    malformed["selected_policy"]["min_positive_probability_lcb"] = 0.49
    malformed["source"]["selected_policy_sha256"] = (
        v3_ensemble._canonical_sha256(malformed["selected_policy"])
    )
    with pytest.raises(ValueError, match="cannot be below 0.5"):
        runtime.OpponentMultiTaskEnsembleRuntimeV4(malformed)

    malformed = copy.deepcopy(payload)
    malformed["calibration"]["model_calibration_artifact_sha256"] = "a" * 64
    with pytest.raises(ValueError, match="differs from original"):
        runtime.OpponentMultiTaskEnsembleRuntimeV4(malformed)

    malformed = copy.deepcopy(payload)
    malformed["calibration"]["member_seed"] = [101, 101, 307]
    with pytest.raises(ValueError, match="member projection is invalid"):
        runtime.OpponentMultiTaskEnsembleRuntimeV4(malformed)

    malformed = copy.deepcopy(payload)
    malformed["source"]["source_collection_complete"] = False
    with pytest.raises(ValueError, match="complete formal calibration boundary"):
        runtime.OpponentMultiTaskEnsembleRuntimeV4(malformed)


@pytest.mark.parametrize(
    ("field", "mutate"),
    [
        (
            "uncertainty",
            lambda calibration: calibration.__setitem__(
                "uncertainty_std_weight", 0.75
            ),
        ),
        (
            "offset",
            lambda calibration: calibration["offsets"][
                "delta_vs_rule"
            ].__setitem__(0, 1.0),
        ),
        (
            "response_temperature",
            lambda calibration: calibration.__setitem__(
                "response_temperature", 1.25
            ),
        ),
    ],
)
def test_v4_ensemble_rejects_flattened_calibration_tampering(
    field: str, mutate,
) -> None:
    malformed = copy.deepcopy(_payload())
    mutate(malformed["calibration"])
    # Even updating the convenience projection hash cannot detach the runtime
    # fields from the gate-bound original calibration payload.
    projection = runtime.calibration_projection_from_bundle(
        malformed["calibration"]
    )
    digest = runtime.calibration_projection_sha256(projection)
    malformed["calibration"]["calibration_projection_sha256"] = digest
    malformed["source"]["calibration_projection_sha256"] = digest
    with pytest.raises(ValueError, match="differs from original"):
        runtime.OpponentMultiTaskEnsembleRuntimeV4(malformed)


def test_v4_selected_bundle_requires_exact_boundary_and_uncertainty() -> None:
    malformed = _payload()
    malformed["source"]["source_completed_passes"] = 159
    with pytest.raises(ValueError, match="complete formal calibration boundary"):
        runtime.OpponentMultiTaskEnsembleRuntimeV4(malformed)

    malformed = _payload()
    original = malformed["calibration"]["original_calibration_artifact"]
    original["ensemble"]["uncertainty_std_weight"] = 0.75
    malformed["calibration"]["uncertainty_std_weight"] = 0.75
    original.pop("payload_sha256")
    original["payload_sha256"] = v3_ensemble._canonical_sha256(original)
    projection = runtime.calibration_projection_from_artifact(original)
    projection_sha256 = runtime.calibration_projection_sha256(projection)
    malformed["calibration"]["payload_sha256"] = original[
        "payload_sha256"
    ]
    malformed["calibration"][
        "calibration_projection_sha256"
    ] = projection_sha256
    malformed["source"]["calibration_payload_sha256"] = original[
        "payload_sha256"
    ]
    malformed["source"][
        "calibration_projection_sha256"
    ] = projection_sha256
    with pytest.raises(ValueError, match="complete formal calibration boundary"):
        runtime.OpponentMultiTaskEnsembleRuntimeV4(malformed)


def test_v4_ensemble_rejects_original_calibration_artifact_tampering() -> None:
    malformed = _payload()
    original = malformed["calibration"]["original_calibration_artifact"]
    original["value_lower"]["fields"]["match_delta_vs_rule"][
        "offsets"
    ][0] = 10.0
    with pytest.raises(ValueError, match="original calibration payload changed"):
        runtime.OpponentMultiTaskEnsembleRuntimeV4(malformed)
