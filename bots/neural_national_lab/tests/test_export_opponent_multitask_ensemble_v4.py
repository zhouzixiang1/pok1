from __future__ import annotations

import copy
from pathlib import Path
import sys

import pytest
import torch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import export_opponent_multitask_ensemble_v4 as exporter  # noqa: E402
import export_opponent_multitask_v4 as member_exporter  # noqa: E402
import match_outcome_calibration as outcome_calibration  # noqa: E402
import opponent_multitask_ensemble_runtime_v3 as v3_runtime  # noqa: E402
import opponent_multitask_ensemble_runtime_v4 as runtime  # noqa: E402
import opponent_multitask_model_v3 as v3_models  # noqa: E402
import opponent_multitask_model_v4 as models  # noqa: E402
import win_first_policy_v4 as win_first  # noqa: E402


CHECKPOINTS = ["a" * 64, "d" * 64, "f" * 64]
SEEDS = [101, 211, 307]


def _outcome(checkpoint: str, seed: int) -> dict:
    payload = {
        "schema": outcome_calibration.CALIBRATION_SCHEMA,
        "method": outcome_calibration.CALIBRATION_METHOD,
        "scale": 1.0,
        "bias": seed / 1000.0,
        "run_id": "run-1",
        "member_seed": seed,
        "model_format": models.MODEL_FORMAT,
        "checkpoint_sha256": checkpoint,
        "role_manifest_sha256": "b" * 64,
        "calibration_role": "model_calibration",
        "model_calibration_artifact_sha256": "c" * 64,
        "model_calibration_opponents": ["national_v142"],
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


def _calibrated() -> dict:
    outcomes = [
        _outcome(checkpoint, seed)
        for checkpoint, seed in zip(CHECKPOINTS, SEEDS, strict=True)
    ]
    members = [
        {
            "seed": seed,
            "checkpoint_sha256": checkpoint,
            "checkpoint_path": f"/unused/{seed}/checkpoint.pt",
            "source_collection_complete": True,
            "outcome_calibration": outcome,
        }
        for seed, checkpoint, outcome in zip(
            SEEDS, CHECKPOINTS, outcomes, strict=True
        )
    ]
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
                for seed, checkpoint in zip(SEEDS, CHECKPOINTS, strict=True)
            ],
            "lower_quantile": 0.2,
            "uncertainty_std_weight": 1.0,
            "outcome_aggregation": win_first.OUTCOME_AGGREGATION_METHOD,
            "outcome_uncertainty_std_weight": 1.0,
            "outcome_calibration_payload_sha256": [
                outcome["payload_sha256"] for outcome in outcomes
            ],
        },
        "value_lower": {
            "target_preprocessing": "symmetric_clip_before_residual",
            "target_clips": {
                field: 2000.0 for field in v3_models.VALUE_FIELDS
            },
            "fields": {
                field: {"offsets": [0.0] * 6}
                for field in v3_models.VALUE_FIELDS
            },
        },
        "response_temperature": {"temperature": 1.0},
        "source_collection_complete": True,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    original_calibration["payload_sha256"] = (
        v3_runtime._canonical_sha256(original_calibration)
    )
    return {
        "members": members,
        "outcome_calibrations": outcomes,
        "ensemble": {
            "role_manifest_sha256": "b" * 64,
            "source_collection_complete": True,
        },
        "calibration": original_calibration,
        "calibration_payload_sha256": original_calibration[
            "payload_sha256"
        ],
        "calibration_file_sha256": "e" * 64,
        "lower_quantile": 0.2,
        "uncertainty_std_weight": 1.0,
        "clips": {field: 2000.0 for field in v3_models.VALUE_FIELDS},
        "offsets": {
            field: [0.0] * 6 for field in v3_models.VALUE_FIELDS
        },
        "response_temperature": 1.0,
        "outcome_uncertainty_std_weight": 1.0,
    }


def _policy() -> dict:
    return {
        "schema": win_first.POLICY_SCHEMA,
        "selection_priority": win_first.SELECTION_PRIORITY,
        "min_positive_probability_lcb": 0.5,
        "min_probability_uplift_lcb": 0.0,
        "chip_margin": 0.0,
        "hand_weight": 0.25,
        "tail_weight": 0.25,
        "match_weight": 0.5,
        "response_weight": 0.0,
        "min_hand_lcb": 0.0,
        "use_lower": True,
    }


@pytest.fixture(scope="module")
def member_payloads() -> list[dict]:
    calibrated = _calibrated()
    payloads = []
    for index, (checkpoint, seed) in enumerate(
        zip(CHECKPOINTS, SEEDS, strict=True)
    ):
        torch.manual_seed(seed)
        model = models.model_from_scale("small", dropout=0.0).eval()
        payloads.append(member_exporter.build_export_payload(
            model,
                {
                    "schema": member_exporter.CHECKPOINT_SCHEMA,
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
            outcome_calibration=calibrated["outcome_calibrations"][index],
        ))
    return payloads


def test_formal_v4_bundle_binds_seeds_roles_and_selected_policy(
    member_payloads: list[dict],
) -> None:
    calibrated = _calibrated()
    verified = exporter.verify_calibrated_members(calibrated, formal=True)
    assert [member["seed"] for member in verified] == SEEDS
    selected = _policy()
    policy = {
        "selected_policy": selected,
        "selected_policy_sha256": v3_runtime._canonical_sha256(selected),
        "selection_passed": True,
    }
    payload = exporter.build_bundle_payload(
        member_payloads,
        calibrated=calibrated,
        policy=policy,
        source={
            "run_id": "run-1",
            "role_manifest_sha256": "b" * 64,
            "source_collection_complete": True,
            "source_completed_passes": 160,
            "source_requested_passes": 160,
            "candidate_snapshot": {
                "name": "v140_test",
                "sha256": "6" * 64,
            },
            "strategy_context_runtime_mode": (
                runtime.STRATEGY_CONTEXT_RUNTIME_MODE
            ),
            "deployment_policy_value": False,
            "strength_evidence": False,
        },
    )

    loaded = runtime.OpponentMultiTaskEnsembleRuntimeV4(payload)
    assert loaded.member_seeds == SEEDS
    assert loaded.policy == selected
    assert payload["deployment_policy_value"] is False
    assert payload["strength_evidence"] is False


def test_v4_ensemble_export_bytes_are_deterministic(
    member_payloads: list[dict], tmp_path: Path
) -> None:
    calibrated = _calibrated()
    selected = _policy()
    payload = exporter.build_bundle_payload(
        member_payloads,
        calibrated=calibrated,
        policy={
            "selected_policy": selected,
            "selected_policy_sha256": v3_runtime._canonical_sha256(selected),
            "selection_passed": True,
        },
        source={
            "run_id": "run-1",
            "role_manifest_sha256": "b" * 64,
            "source_collection_complete": True,
            "source_completed_passes": 160,
            "source_requested_passes": 160,
            "candidate_snapshot": {
                "name": "v140_test",
                "sha256": "6" * 64,
            },
            "strategy_context_runtime_mode": (
                runtime.STRATEGY_CONTEXT_RUNTIME_MODE
            ),
        },
    )
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    first_artifact = exporter.write_export(first, payload)
    second_artifact = exporter.write_export(second, payload)

    assert first.read_bytes() == second.read_bytes()
    assert first_artifact["sha256"] == second_artifact["sha256"]
    assert runtime.OpponentMultiTaskEnsembleRuntimeV4.load(first) is not None


def test_exact_bundle_rebuild_rejects_weight_with_synchronized_member_hash(
    member_payloads: list[dict], tmp_path: Path
) -> None:
    calibrated = _calibrated()
    selected = _policy()
    expected = exporter.build_bundle_payload(
        member_payloads,
        calibrated=calibrated,
        policy={
            "selected_policy": selected,
            "selected_policy_sha256": v3_runtime._canonical_sha256(selected),
            "selection_passed": True,
        },
        source={
            "run_id": "run-1",
            "role_manifest_sha256": "b" * 64,
            "source_collection_complete": True,
            "source_completed_passes": 160,
            "source_requested_passes": 160,
            "candidate_snapshot": {
                "name": "v140_test",
                "sha256": "6" * 64,
            },
            "strategy_context_runtime_mode": (
                runtime.STRATEGY_CONTEXT_RUNTIME_MODE
            ),
        },
    )
    tampered = copy.deepcopy(expected)
    weight_name = next(iter(tampered["members"][0]["weights"]))
    weight = tampered["members"][0]["weights"][weight_name]
    if isinstance(weight[0], list):
        weight[0][0] += 1.0
    else:
        weight[0] += 1.0
    tampered["member_payload_sha256"][0] = (
        v3_runtime._canonical_sha256(tampered["members"][0])
    )
    path = tmp_path / "tampered.json"
    path.write_bytes(exporter.canonical_bundle_bytes(tampered))

    with pytest.raises(ValueError, match="deterministic exporter output"):
        exporter.verify_exact_bundle(path, expected)


def test_v4_export_rejects_cross_role_or_incomplete_formal_members() -> None:
    calibrated = _calibrated()
    calibrated["members"][0]["outcome_calibration"] = _outcome(
        CHECKPOINTS[0], SEEDS[0]
    )
    calibrated["members"][0]["outcome_calibration"][
        "model_calibration_artifact_sha256"
    ] = "9" * 64
    calibrated["members"][0]["outcome_calibration"]["payload_sha256"] = (
        outcome_calibration.calibration_payload_sha256(
            calibrated["members"][0]["outcome_calibration"]
        )
    )
    calibrated["outcome_calibrations"][0] = calibrated["members"][0][
        "outcome_calibration"
    ]
    with pytest.raises(ValueError, match="share one role"):
        exporter.verify_calibrated_members(calibrated, formal=True)

    calibrated = _calibrated()
    calibrated["members"] = calibrated["members"][:2]
    calibrated["outcome_calibrations"] = calibrated[
        "outcome_calibrations"
    ][:2]
    with pytest.raises(ValueError, match="at least three"):
        exporter.verify_calibrated_members(calibrated, formal=True)

    calibrated = _calibrated()
    calibrated["uncertainty_std_weight"] = 0.5
    with pytest.raises(ValueError, match="must remain 1.0"):
        exporter.verify_calibrated_members(calibrated, formal=True)


def test_incomplete_smoke_always_strips_selected_policy() -> None:
    selected = _policy()
    policy = exporter.policy_for_export({
        "selected_policy": selected,
        "selected_policy_sha256": v3_runtime._canonical_sha256(selected),
        "selection_passed": True,
    }, formal=False)

    assert policy["selected_policy"] is None
    assert policy["selected_policy_sha256"] is None
    assert policy["selection_passed"] is False
