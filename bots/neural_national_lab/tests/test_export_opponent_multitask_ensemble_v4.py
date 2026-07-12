from __future__ import annotations

import copy
import hashlib
import io
import json
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
import select_opponent_multitask_v4_policy as selector  # noqa: E402
import v4_runtime_budget as runtime_budget  # noqa: E402
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


def _calibrated(
    *,
    checkpoints: list[str] | None = None,
    checkpoint_paths: list[Path] | None = None,
) -> dict:
    checkpoint_values = list(
        CHECKPOINTS if checkpoints is None else checkpoints
    )
    if len(checkpoint_values) != len(SEEDS):
        raise ValueError("test calibration requires exactly three checkpoints")
    paths = (
        [Path(f"/unused/{seed}/checkpoint.pt") for seed in SEEDS]
        if checkpoint_paths is None
        else list(checkpoint_paths)
    )
    if len(paths) != len(SEEDS):
        raise ValueError(
            "test calibration requires exactly three checkpoint paths"
        )
    outcomes = [
        _outcome(checkpoint, seed)
        for checkpoint, seed in zip(checkpoint_values, SEEDS, strict=True)
    ]
    members = [
        {
            "seed": seed,
            "checkpoint_sha256": checkpoint,
            "checkpoint_path": str(path),
            "source_collection_complete": True,
            "outcome_calibration": outcome,
        }
        for seed, checkpoint, path, outcome in zip(
            SEEDS, checkpoint_values, paths, outcomes, strict=True
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
                for seed, checkpoint in zip(
                    SEEDS, checkpoint_values, strict=True
                )
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
        "ensemble_manifest_sha256": "1" * 64,
        "artifact_manifest_sha256": "2" * 64,
        "calibration_report_sha256": "3" * 64,
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


def _write_real_checkpoint(path: Path, *, seed: int) -> str:
    torch.manual_seed(seed)
    model = models.model_from_scale("small", dropout=0.0).eval()
    model_source = Path(models.__file__).resolve()
    torch.save({
        "schema": member_exporter.CHECKPOINT_SCHEMA,
        "role_manifest_sha256": "b" * 64,
        "training_artifact_sha256": {
            "train": "7" * 64,
            "early_stop": "8" * 64,
        },
        "source_completed_passes": 160,
        "source_requested_passes": 160,
        "source_collection_complete": True,
        "code_artifacts": {
            "test_model_source": {
                "bytes": model_source.stat().st_size,
                "sha256": hashlib.sha256(model_source.read_bytes()).hexdigest(),
            }
        },
        "model_metadata": model.metadata(),
        "state_dict": model.state_dict(),
    }, path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _real_calibrated_checkpoints(
    tmp_path: Path,
) -> tuple[dict, list[Path], list[str]]:
    paths = [tmp_path / f"member-{seed}.pt" for seed in SEEDS]
    digests = [
        _write_real_checkpoint(path, seed=seed)
        for path, seed in zip(paths, SEEDS, strict=True)
    ]
    return (
        _calibrated(checkpoints=digests, checkpoint_paths=paths),
        paths,
        digests,
    )


class _CompleteDataset:
    manifest_sha256 = "b" * 64
    manifest = {
        "source_collection_complete": True,
        "source_completed_passes": 160,
        "source_requested_passes": 160,
    }

    @staticmethod
    def runtime_context_contract() -> dict:
        return {
            "candidate_snapshot": {
                "name": "v140_test",
                "sha256": "6" * 64,
            },
            "strategy_context_runtime_mode": (
                runtime.STRATEGY_CONTEXT_RUNTIME_MODE
            ),
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

    budget = runtime_budget.measure_bundle_runtime_budget_subprocess(
        first, source_collection_complete=True
    )
    assert runtime_budget.validate_runtime_budget_artifact(
        budget,
        bundle_bytes=first.stat().st_size,
        bundle_sha256=first_artifact["sha256"],
        runtime_identity_sha256=(
            runtime_budget.bundle_runtime_identity_sha256(payload)
        ),
        require_formal=True,
    )["formal_runtime_budget_passed"] is True


def test_preselection_runtime_identity_is_policy_stable_and_bound_to_export(
    member_payloads: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    calibrated = _calibrated()
    dataset = _CompleteDataset()
    monkeypatch.setattr(
        exporter,
        "_export_verified_member_payloads",
        lambda _calibrated, *, formal: copy.deepcopy(member_payloads),
    )
    preselection = exporter.build_preselection_bundle_payload(
        calibrated=calibrated,
        dataset=dataset,
        run_id="run-1",
        formal=True,
    )
    identity_a = runtime_budget.bundle_runtime_identity_sha256(preselection)
    budget_artifact_a = "4" * 64
    selected = _policy()
    policy = {
        "selected_policy": selected,
        "selected_policy_sha256": v3_runtime._canonical_sha256(selected),
        "selection_passed": True,
        "candidate_sha256": "5" * 64,
        "evaluation_sha256": "7" * 64,
        "result_sha256": "8" * 64,
        "artifact_manifest_sha256": "9" * 64,
        "runtime_budget_payload_sha256": budget_artifact_a,
        "runtime_identity_sha256": identity_a,
    }

    final = exporter.build_verified_bundle_payload(
        calibrated=calibrated,
        policy=policy,
        dataset=dataset,
        run_id="run-1",
        formal=True,
    )
    binding = exporter.bundle_artifact_binding(final)

    assert runtime_budget.bundle_runtime_identity_sha256(final) == identity_a
    assert final["source"][
        "preselection_runtime_budget_payload_sha256"
    ] == budget_artifact_a
    assert final["source"]["runtime_identity_sha256"] == identity_a
    assert binding[
        "preselection_runtime_budget_payload_sha256"
    ] == budget_artifact_a
    assert binding["runtime_identity_sha256"] == identity_a

    alternate = copy.deepcopy(final)
    alternate_policy = {**selected, "chip_margin": 125.0}
    alternate["selected_policy"] = alternate_policy
    alternate["source"].update({
        "selected_policy_sha256": v3_runtime._canonical_sha256(
            alternate_policy
        ),
        "policy_candidate_sha256": "a" * 64,
        "policy_evaluation_sha256": "d" * 64,
        "policy_result_sha256": "e" * 64,
        "policy_artifact_manifest_sha256": "f" * 64,
    })
    runtime.OpponentMultiTaskEnsembleRuntimeV4(alternate)
    assert (
        runtime_budget.bundle_runtime_identity_sha256(alternate)
        == identity_a
    )
    assert exporter.bundle_artifact_binding(alternate)[
        "runtime_identity_sha256"
    ] == identity_a

    member_drift, calibration_drift, runtime_module_drift = (
        copy.deepcopy(final),
        copy.deepcopy(final),
        copy.deepcopy(final),
    )
    member_drift["member_payload_sha256"][0] = "0" * 64
    calibration_drift["calibration"][
        "calibration_projection_sha256"
    ] = "0" * 64
    module_name = next(iter(
        runtime_module_drift["export_contract"]["copied_tool_modules"]
    ))
    runtime_module_drift["export_contract"]["copied_tool_modules"][
        module_name
    ]["sha256"] = "0" * 64
    for drifted in (member_drift, calibration_drift, runtime_module_drift):
        with pytest.raises(ValueError, match="runtime identity changed"):
            exporter.bundle_artifact_binding(drifted)


def test_real_checkpoint_preselection_budget_round_trip_binds_final_bundle(
    tmp_path: Path,
) -> None:
    checkpoint_paths = []
    checkpoint_sha256 = []
    model_source = Path(models.__file__).resolve()
    code_artifacts = {
        "test_model_source": {
            "bytes": model_source.stat().st_size,
            "sha256": hashlib.sha256(model_source.read_bytes()).hexdigest(),
        }
    }
    for seed in SEEDS:
        torch.manual_seed(seed)
        model = models.model_from_scale("small", dropout=0.0).eval()
        path = tmp_path / f"member-{seed}.pt"
        torch.save({
            "schema": member_exporter.CHECKPOINT_SCHEMA,
            "role_manifest_sha256": "b" * 64,
            "training_artifact_sha256": {
                "train": "7" * 64,
                "early_stop": "8" * 64,
            },
            "source_completed_passes": 160,
            "source_requested_passes": 160,
            "source_collection_complete": True,
            "code_artifacts": code_artifacts,
            "model_metadata": model.metadata(),
            "state_dict": model.state_dict(),
        }, path)
        loaded, checkpoint = exporter.load_checkpoint(path, device="cpu")
        assert loaded.metadata() == model.metadata()
        assert checkpoint["source_collection_complete"] is True
        checkpoint_paths.append(path)
        checkpoint_sha256.append(hashlib.sha256(path.read_bytes()).hexdigest())

    calibrated = _calibrated(
        checkpoints=checkpoint_sha256,
        checkpoint_paths=checkpoint_paths,
    )
    dataset = _CompleteDataset()
    assessed_a = selector.assess_preselection_runtime_budget(
        calibrated,
        dataset=dataset,
        run_id="run-1",
        formal=True,
    )
    budget_path = tmp_path / selector.RUNTIME_BUDGET_NAME
    budget_path.write_text(
        json.dumps(assessed_a, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    verified_a = selector.verify_preselection_runtime_budget(
        budget_path,
        calibrated=calibrated,
        dataset=dataset,
        run_id="run-1",
        formal=True,
    )

    assert verified_a == assessed_a
    assert verified_a["formal_runtime_budget_passed"] is True
    selected = _policy()
    final = exporter.build_verified_bundle_payload(
        calibrated=calibrated,
        policy={
            "selected_policy": selected,
            "selected_policy_sha256": v3_runtime._canonical_sha256(selected),
            "selection_passed": True,
            "candidate_sha256": "5" * 64,
            "evaluation_sha256": "7" * 64,
            "result_sha256": "8" * 64,
            "artifact_manifest_sha256": "9" * 64,
            "runtime_budget_payload_sha256": verified_a["payload_sha256"],
            "runtime_identity_sha256": verified_a[
                "runtime_identity_sha256"
            ],
        },
        dataset=dataset,
        run_id="run-1",
        formal=True,
    )
    binding = exporter.bundle_artifact_binding(final)

    assert runtime_budget.bundle_runtime_identity_sha256(final) == verified_a[
        "runtime_identity_sha256"
    ]
    assert final["source"][
        "preselection_runtime_budget_payload_sha256"
    ] == verified_a["payload_sha256"]
    assert binding[
        "preselection_runtime_budget_payload_sha256"
    ] == verified_a["payload_sha256"]
    assert binding["runtime_identity_sha256"] == verified_a[
        "runtime_identity_sha256"
    ]


def test_member_export_loads_the_exact_bound_checkpoint_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calibrated, _paths, checkpoint_sha256 = _real_calibrated_checkpoints(
        tmp_path
    )
    loaded_snapshot_sha256 = []
    original_load_checkpoint = exporter.load_checkpoint

    def load_snapshot(source: object, *, device: str) -> tuple[object, dict]:
        assert isinstance(source, io.BytesIO)
        assert source.tell() == 0
        loaded_snapshot_sha256.append(
            hashlib.sha256(source.getvalue()).hexdigest()
        )
        return original_load_checkpoint(source, device=device)

    monkeypatch.setattr(exporter, "load_checkpoint", load_snapshot)

    payloads = exporter._export_verified_member_payloads(
        calibrated, formal=True
    )

    assert loaded_snapshot_sha256 == checkpoint_sha256
    assert [
        payload["source"]["checkpoint_sha256"] for payload in payloads
    ] == checkpoint_sha256


def test_transient_checkpoint_replace_and_revert_cannot_change_exported_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calibrated, checkpoint_paths, checkpoint_sha256 = (
        _real_calibrated_checkpoints(tmp_path)
    )
    first_path = checkpoint_paths[0]
    original_model, original_checkpoint = exporter.load_checkpoint(
        first_path, device="cpu"
    )
    expected_first = member_exporter.build_export_payload(
        original_model,
        original_checkpoint,
        checkpoint_sha256=checkpoint_sha256[0],
        outcome_calibration=calibrated["members"][0]["outcome_calibration"],
    )
    replacement_path = tmp_path / "replacement.pt"
    replacement_sha256 = _write_real_checkpoint(replacement_path, seed=997)
    backup_path = tmp_path / "original-backup.pt"
    original_load_checkpoint = exporter.load_checkpoint
    race_exercised = False

    def load_during_transient_replace(
        source: object, *, device: str
    ) -> tuple[object, dict]:
        nonlocal race_exercised
        if not race_exercised:
            race_exercised = True
            first_path.replace(backup_path)
            replacement_path.replace(first_path)
            try:
                return original_load_checkpoint(source, device=device)
            finally:
                first_path.replace(replacement_path)
                backup_path.replace(first_path)
        return original_load_checkpoint(source, device=device)

    monkeypatch.setattr(
        exporter, "load_checkpoint", load_during_transient_replace
    )

    payloads = exporter._export_verified_member_payloads(
        calibrated, formal=True
    )

    assert race_exercised is True
    assert hashlib.sha256(first_path.read_bytes()).hexdigest() == (
        checkpoint_sha256[0]
    )
    assert hashlib.sha256(replacement_path.read_bytes()).hexdigest() == (
        replacement_sha256
    )
    assert payloads[0] == expected_first


def test_runtime_identity_changes_or_rejects_runtime_relevant_drift(
    member_payloads: list[dict],
) -> None:
    calibrated = _calibrated()
    payload = exporter.build_bundle_payload(
        member_payloads,
        calibrated=calibrated,
        policy={
            "selected_policy": None,
            "selected_policy_sha256": None,
            "selection_passed": False,
        },
        source={
            "run_id": "run-1",
            "role_manifest_sha256": "b" * 64,
            "ensemble_manifest_sha256": "1" * 64,
            "calibration_artifact_manifest_sha256": "2" * 64,
            "calibration_report_sha256": "3" * 64,
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
    original_identity = runtime_budget.bundle_runtime_identity_sha256(payload)

    unsynchronized_member = copy.deepcopy(payload)
    weight_name = next(iter(unsynchronized_member["members"][0]["weights"]))
    weight = unsynchronized_member["members"][0]["weights"][weight_name]
    if isinstance(weight[0], list):
        weight[0][0] += 1.0
    else:
        weight[0] += 1.0
    with pytest.raises(ValueError, match="member payload changed"):
        runtime.OpponentMultiTaskEnsembleRuntimeV4(unsynchronized_member)

    synchronized_member = copy.deepcopy(unsynchronized_member)
    synchronized_member["member_payload_sha256"][0] = (
        v3_runtime._canonical_sha256(synchronized_member["members"][0])
    )
    assert (
        runtime_budget.bundle_runtime_identity_sha256(synchronized_member)
        != original_identity
    )

    calibration_drift = copy.deepcopy(payload)
    calibration_drift["calibration"][
        "calibration_projection_sha256"
    ] = "0" * 64
    assert (
        runtime_budget.bundle_runtime_identity_sha256(calibration_drift)
        != original_identity
    )
    with pytest.raises(ValueError, match="calibration source binding changed"):
        runtime.OpponentMultiTaskEnsembleRuntimeV4(calibration_drift)

    runtime_module_drift = copy.deepcopy(payload)
    module_name = next(iter(
        runtime_module_drift["export_contract"]["copied_tool_modules"]
    ))
    runtime_module_drift["export_contract"]["copied_tool_modules"][
        module_name
    ]["sha256"] = "0" * 64
    assert (
        runtime_budget.bundle_runtime_identity_sha256(runtime_module_drift)
        != original_identity
    )


@pytest.mark.parametrize(
    ("canonical_bytes", "allowed"),
    [
        (exporter.MAX_CANONICAL_BUNDLE_BYTES, True),
        (exporter.MAX_CANONICAL_BUNDLE_BYTES + 1, False),
    ],
)
def test_v4_export_enforces_fixed_canonical_bundle_byte_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_bytes: int,
    allowed: bool,
) -> None:
    monkeypatch.setattr(
        exporter,
        "canonical_bundle_bytes",
        lambda _payload: b"x" * canonical_bytes,
    )
    output = tmp_path / "new-output-directory" / "bundle.json"

    if allowed:
        artifact = exporter.write_export(output, {"payload": "small"})
        assert output.is_file()
        assert artifact["path"] == str(output.resolve())
    else:
        with pytest.raises(ValueError, match="50,000,000-byte limit"):
            exporter.write_export(output, {"payload": "small"})
        assert not output.exists()
        assert not output.parent.exists()


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
