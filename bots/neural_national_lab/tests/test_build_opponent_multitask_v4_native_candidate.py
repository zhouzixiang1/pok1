from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess
import textwrap
from types import SimpleNamespace
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import build_opponent_multitask_v4_native_candidate as builder  # noqa: E402
import export_opponent_multitask_ensemble_v4 as bundle_exporter  # noqa: E402
import freeze_opponent_role_dataset as freeze  # noqa: E402
import match_outcome_calibration as calibration  # noqa: E402
from match_outcome_schema import match_outcome_metadata  # noqa: E402
import opponent_multitask_ensemble_runtime_v4 as runtime  # noqa: E402
import opponent_multitask_runtime_v3 as v3_runtime  # noqa: E402
import opponent_exposure_ledger as exposure_ledger  # noqa: E402
from opponent_response_schema import response_schema_metadata  # noqa: E402
import v4_native_policy as native_policy  # noqa: E402
import win_first_policy_v4 as win_first  # noqa: E402
from bots.neural_national_lab.tests.role_provenance_fixture import (  # noqa: E402
    add_formal_role_provenance,
)


def _write(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _policy() -> dict:
    return {
        "schema": win_first.POLICY_SCHEMA,
        "selection_priority": win_first.SELECTION_PRIORITY,
        "min_positive_probability_lcb": 0.5,
        "min_probability_uplift_lcb": 0.0,
        "chip_margin": 25.0,
        "hand_weight": 0.25,
        "tail_weight": 0.25,
        "match_weight": 0.5,
        "response_weight": 0.0,
        "min_hand_lcb": 0.0,
        "use_lower": True,
    }


def _outcome(
    checkpoint: str,
    *,
    seed: int = 101,
    artifact: str = "c" * 64,
    role_manifest: str = "d" * 64,
) -> dict:
    payload = {
        "schema": calibration.CALIBRATION_SCHEMA,
        "method": calibration.CALIBRATION_METHOD,
        "scale": 1.0,
        "bias": 0.0,
        "run_id": "test-run",
        "member_seed": seed,
        "model_format": builder.MODEL_FORMAT,
        "checkpoint_sha256": checkpoint,
        "role_manifest_sha256": role_manifest,
        "model_calibration_artifact_sha256": artifact,
        "model_calibration_opponents": ["national_v142"],
        "calibration_role": "model_calibration",
        "policy_evidence_used": False,
        "source_collection_complete": True,
        "metrics": {},
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    payload["payload_sha256"] = calibration.calibration_payload_sha256(payload)
    return payload


def _artifacts(
    tmp_path: Path, *, passed: bool = True
) -> tuple[Path, Path, dict, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    role_root = tmp_path / "roles"
    role_root.mkdir()
    roles = {
        "train": ["national_v901"],
        "early_stop": ["national_v902"],
        "model_calibration": ["national_v142"],
        "policy_selection": ["national_v903"],
        "policy_gate": ["national_v1", "national_v2"],
    }
    outputs = {}
    for prefix in freeze.PREFIXES:
        for role, opponents in roles.items():
            name = f"{prefix}_{role}.jsonl"
            raw = b"x"
            outputs[name] = {
                "rows": 1,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "opponents": opponents,
                **(
                    {"row_schema": "national_opponent_response_v2"}
                    if prefix == "opponent_actions" else {}
                ),
            }
    role_manifest = {
        "schema": freeze.SCHEMA,
        "source_collection_complete": True,
        "source_completed_passes": 160,
        "source_requested_passes": 160,
        "candidate_snapshot": {
            "path": str(builder.DEFAULT_STRATEGY_DONOR.resolve()),
            "name": builder.EXPECTED_STRATEGY_DONOR_NAME,
            "sha256": builder.EXPECTED_STRATEGY_DONOR_SHA256,
        },
        "strategy_context_runtime_mode": (
            runtime.STRATEGY_CONTEXT_RUNTIME_MODE
        ),
        "roles": roles,
        "outputs": outputs,
        "behavior_supervision": response_schema_metadata(),
        "match_outcome_supervision": match_outcome_metadata(),
        "invariants": {
            "opponent_disjoint": True,
            "match_cluster_disjoint": True,
            "deck_blocks_non_overlapping": True,
            "uniform_decision_ipw_validated": True,
            "national_response_v2_validated": True,
            "national_70_hand_outcome_validated": True,
            "artifact_snapshots_verified": True,
            "final_blind_in_dataset": False,
        },
    }
    add_formal_role_provenance(role_root, role_manifest)
    role_manifest_path = role_root / "role_manifest.json"
    _write(role_manifest_path, role_manifest)
    ledger_path = tmp_path / "ledger.json"
    dataset = builder.RoleDatasetAccess(
        role_manifest_path,
        ledger_path=ledger_path,
        run_id="test-run",
        require_complete=True,
    )

    gate = tmp_path / "gate"
    gate.mkdir(parents=True)
    selected = _policy()
    selected_sha = builder._canonical_sha256(selected)
    candidate_sha = "a" * 64
    selection_sha = "b" * 64
    role_manifest_sha = dataset.manifest_sha256
    policy_gate_artifact_sha = hashlib.sha256(
        (
            dataset._role_artifact_sha256("policy_gate")
            + ":"
            + selection_sha
        ).encode()
    ).hexdigest()
    exposure_ledger.open_exposure(
        ledger_path,
        role="policy_gate",
        opponents=roles["policy_gate"],
        run_id="test-run",
        candidate_sha256=candidate_sha,
        artifact_sha256=policy_gate_artifact_sha,
    )
    checkpoints = [character * 64 for character in ("1", "2", "3")]
    seeds = [101, 211, 307]
    training_artifacts = {"train": "7" * 64, "early_stop": "8" * 64}
    code_artifacts = {
        "trainer": {"bytes": 1, "sha256": "9" * 64}
    }
    member_export_contract = {
        "export_tool_sha256": builder._sha256(
            builder.TOOLS / "export_opponent_multitask_v4.py"
        ),
        "runtime_tool_sha256": builder._sha256(
            builder.TOOLS / "opponent_multitask_runtime_v4.py"
        ),
    }
    members = [
        {
            "source": {
                "checkpoint_sha256": checkpoint,
                "checkpoint_schema": builder.CHECKPOINT_SCHEMA,
                "role_manifest_sha256": role_manifest_sha,
                "training_artifact_sha256": training_artifacts,
                "code_artifacts": code_artifacts,
                "source_collection_complete": True,
            },
            "outcome_calibration": _outcome(
                checkpoint, seed=seed, role_manifest=role_manifest_sha
            ),
            "export_contract": member_export_contract,
        }
        for seed, checkpoint in zip(seeds, checkpoints, strict=True)
    ]
    clips = {field: 2000.0 for field in v3_runtime.VALUE_FIELDS}
    offsets = {
        field: [0.0] * len(v3_runtime.LABELS)
        for field in v3_runtime.VALUE_FIELDS
    }
    original_calibration = {
        "schema": runtime.ORIGINAL_CALIBRATION_SCHEMA,
        "run_id": "test-run",
        "role_manifest_sha256": role_manifest_sha,
        "calibration_role": "model_calibration",
        "calibration_artifact_sha256": "c" * 64,
        "opponents": ["national_v142"],
        "policy_evidence_used": False,
        "ensemble": {
            "members": [
                {"seed": seed, "checkpoint_sha256": checkpoint}
                for seed, checkpoint in zip(seeds, checkpoints, strict=True)
            ],
            "lower_quantile": 0.2,
            "uncertainty_std_weight": 1.0,
            "outcome_aggregation": win_first.OUTCOME_AGGREGATION_METHOD,
            "outcome_uncertainty_std_weight": 1.0,
            "outcome_calibration_payload_sha256": [
                member["outcome_calibration"]["payload_sha256"]
                for member in members
            ],
        },
        "value_lower": {
            "target_preprocessing": "symmetric_clip_before_residual",
            "target_clips": dict(clips),
            "fields": {
                field: {"offsets": list(values)}
                for field, values in offsets.items()
            },
        },
        "response_temperature": {"temperature": 1.0},
        "source_collection_complete": True,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    original_calibration["payload_sha256"] = builder._canonical_sha256(
        original_calibration
    )
    calibration_payload_sha = original_calibration["payload_sha256"]
    projection = runtime.calibration_projection_from_artifact(
        original_calibration
    )
    projection_sha = runtime.calibration_projection_sha256(projection)
    runtime_modules = {
        name: {
            "bytes": (builder.TOOLS / name).stat().st_size,
            "sha256": builder._sha256(builder.TOOLS / name),
        }
        for name in builder.COPIED_TOOL_MODULES
    }
    runtime_context = dataset.runtime_context_contract()
    bundle = {
        "schema": builder.BUNDLE_SCHEMA,
        "format": builder.ENSEMBLE_FORMAT,
        "members": members,
        "member_payload_sha256": [
            builder._canonical_sha256(member) for member in members
        ],
        "calibration": {
            "payload_sha256": calibration_payload_sha,
            "member_seed": seeds,
            "run_id": "test-run",
            "role_manifest_sha256": role_manifest_sha,
            "model_calibration_artifact_sha256": "c" * 64,
            "model_calibration_opponents": ["national_v142"],
            "source_collection_complete": True,
            "member_checkpoint_sha256": checkpoints,
            "lower_quantile": 0.2,
            "uncertainty_std_weight": 1.0,
            "clips": clips,
            "offsets": offsets,
            "response_temperature": 1.0,
            "outcome_aggregation": win_first.OUTCOME_AGGREGATION_METHOD,
            "outcome_uncertainty_std_weight": 1.0,
            "outcome_calibration_payload_sha256": [
                member["outcome_calibration"]["payload_sha256"]
                for member in members
            ],
            "original_calibration_artifact": original_calibration,
            "original_calibration_file_sha256": "e" * 64,
            "calibration_projection_sha256": projection_sha,
        },
        "selected_policy": selected,
        "source": {
            "run_id": "test-run",
            "role_manifest_sha256": role_manifest_sha,
            "ensemble_manifest_sha256": "6" * 64,
            "selected_policy_sha256": selected_sha,
            "policy_selection_passed": True,
            "source_collection_complete": True,
            "source_completed_passes": 160,
            "source_requested_passes": 160,
            "policy_candidate_sha256": candidate_sha,
            "policy_result_sha256": selection_sha,
            "calibration_payload_sha256": calibration_payload_sha,
            "calibration_file_sha256": "e" * 64,
            "calibration_projection_sha256": projection_sha,
            **runtime_context,
            "deployment_policy_value": False,
            "strength_evidence": False,
        },
        "export_contract": {
            "bundle_tool_sha256": builder._sha256(
                Path(bundle_exporter.__file__).resolve()
            ),
            "runtime_tool_sha256": builder._sha256(
                builder.TOOLS / "opponent_multitask_ensemble_runtime_v4.py"
            ),
            "copied_tool_modules": runtime_modules,
        },
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    bundle["source"]["preselection_runtime_budget_payload_sha256"] = "a" * 64
    bundle["source"]["runtime_identity_sha256"] = (
        builder.runtime_budget.bundle_runtime_identity_sha256(bundle)
    )
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_bytes(bundle_exporter.canonical_bundle_bytes(bundle))
    bundle_binding = bundle_exporter.bundle_artifact_binding(bundle)
    bootstrap_contract = {
        "schema": builder.BOOTSTRAP_CONTRACT_SCHEMA,
        "samples": 2000,
        "seed": 20260714,
        "observed_70_hand_match_clusters": True,
        "ordinary": True,
        "opponent_stratified": True,
    }
    gate_code_artifacts = builder.gate_code_artifacts()
    native_build_contract = builder.current_native_build_contract()
    evaluation = {
        "schema": builder.GATE_EVALUATION_SCHEMA,
        "config": selected,
        "selected_policy": selected,
        "source_collection_complete": True,
        "policy_search_performed": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "offline_estimand": builder.POLICY_OFFLINE_ESTIMAND_V4,
        "match_outcome_estimand": builder.MATCH_OUTCOME_ESTIMAND,
        "bootstrap_contract": bootstrap_contract,
        "inference_contract": {"device": "cpu", "batch_size": 128},
        "code_artifacts": gate_code_artifacts,
        "native_build_contract": native_build_contract,
        "policy_gate_artifact_sha256": policy_gate_artifact_sha,
        "policy_gate_opponents": roles["policy_gate"],
        **bundle_binding,
        **runtime_context,
        "overrides": 16,
        "override_clusters": 8,
        "match_outcome_row_coverage": 1.0,
        "match_outcome_cluster_coverage": 1.0,
        "match_positive_rate": 0.7,
        "rule_match_positive_rate": 0.6,
        "match_positive_uplift_mean": 0.1,
        "match_cluster_bootstrap_mean_ci": {
            "lower": 1.0, "mean": 2.0, "upper": 3.0,
        },
        "match_opponent_stratified_cluster_ci": {
            "lower": 1.0, "mean": 2.0, "upper": 3.0,
        },
        "match_positive_rate_cluster_bootstrap_ci": {
            "lower": 0.6, "mean": 0.7, "upper": 0.8,
        },
        "match_positive_rate_opponent_stratified_cluster_ci": {
            "lower": 0.6, "mean": 0.7, "upper": 0.8,
        },
        "rule_match_positive_rate_cluster_bootstrap_ci": {
            "lower": 0.5, "mean": 0.6, "upper": 0.7,
        },
        "rule_match_positive_rate_opponent_stratified_cluster_ci": {
            "lower": 0.5, "mean": 0.6, "upper": 0.7,
        },
        "match_positive_uplift_cluster_bootstrap_ci": {
            "lower": 0.0, "mean": 0.1, "upper": 0.2,
        },
        "match_positive_uplift_opponent_stratified_cluster_ci": {
            "lower": 0.0, "mean": 0.1, "upper": 0.2,
        },
        "by_opponent": {
            "national_v1": {
                "overrides": 8,
                "mean": 10.0,
                "match_outcome_clusters": 4,
                "match_positive_rate": 0.7,
                "rule_match_positive_rate": 0.6,
                "match_positive_uplift_mean": 0.1,
            },
            "national_v2": {
                "overrides": 8,
                "mean": 10.0,
                "match_outcome_clusters": 4,
                "match_positive_rate": 0.7,
                "rule_match_positive_rate": 0.6,
                "match_positive_uplift_mean": 0.1,
            },
        },
    }
    thresholds = {
        "min_overrides": 12,
        "min_override_clusters": 8,
        "min_overrides_per_opponent": 4,
        "min_cluster_ci_lower": 0.0,
        "min_opponent_stratified_ci_lower": 0.0,
        "min_match_outcome_coverage": 1.0,
        "min_match_positive_rate_ci_lower": 0.5,
        "min_match_positive_uplift_ci_lower": 0.0,
        "min_opponent_match_positive_rate": 0.5,
    }
    result = {
        "schema": builder.POLICY_GATE_RESULT_SCHEMA_V4,
        "run_id": "test-run",
        "passed": passed,
        "errors": [] if passed else ["negative_ci"],
        "native_candidate_build_authorized": passed,
        "candidate_sha256": candidate_sha,
        "selection_result_sha256": selection_sha,
        "role_manifest_sha256": role_manifest_sha,
        "selected_policy_sha256": selected_sha,
        "evaluation_report_sha256": builder._canonical_sha256(evaluation),
        "calibration_payload_sha256": calibration_payload_sha,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "offline_estimand": builder.POLICY_OFFLINE_ESTIMAND_V4,
        "match_outcome_estimand": builder.MATCH_OUTCOME_ESTIMAND,
        "thresholds": thresholds,
        "summary": {"bootstrap_contract": bootstrap_contract},
        "policy_gate_artifact_sha256": policy_gate_artifact_sha,
        "native_build_contract": native_build_contract,
        **bundle_binding,
        **runtime_context,
    }
    _write(gate / "policy_gate_evaluation.json", evaluation)
    _write(gate / "policy_gate_result.json", result)
    report = {
        "schema": builder.GATE_REPORT_SCHEMA,
        "run_id": "test-run",
        "role_manifest_sha256": role_manifest_sha,
        "candidate_sha256": candidate_sha,
        "selection_result_sha256": selection_sha,
        "selected_policy_sha256": selected_sha,
        "gate_passed": passed,
        "gate_errors": [] if passed else ["negative_ci"],
        "native_candidate_build_authorized": passed,
        "gate_result_sha256": _sha(gate / "policy_gate_result.json"),
        "source_collection_complete": True,
        "collection_boundary": {
            "schema": "complete_atomic_collection_boundary_v1",
            "source_completed_passes": 160,
            "source_requested_passes": 160,
            "source_collection_complete": True,
        },
        "calibration_payload_sha256": calibration_payload_sha,
        "code_artifacts": gate_code_artifacts,
        "native_build_contract": native_build_contract,
        "policy_gate_artifact_sha256": policy_gate_artifact_sha,
        **bundle_binding,
        **runtime_context,
        "policy_gate_opponents": ["national_v1", "national_v2"],
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    _write(gate / "policy_gate_report.json", report)
    names = (
        "policy_gate_evaluation.json",
        "policy_gate_result.json",
        "policy_gate_report.json",
    )
    _write(gate / "artifact_manifest.json", {
        "schema": builder.GATE_ARTIFACT_SCHEMA,
        "run_id": "test-run",
        "candidate_sha256": candidate_sha,
        "native_candidate_build_authorized": passed,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "code_artifacts": gate_code_artifacts,
        "native_build_contract": native_build_contract,
        "policy_gate_artifact_sha256": policy_gate_artifact_sha,
        **bundle_binding,
        **runtime_context,
        "files": {
            name: {
                "bytes": (gate / name).stat().st_size,
                "sha256": _sha(gate / name),
            }
            for name in names
        },
    })

    return gate, bundle_path, selected, role_manifest_path, ledger_path


def _mock_runtime(monkeypatch, selected: dict) -> None:
    monkeypatch.setattr(
        builder.OpponentMultiTaskEnsembleRuntimeV4,
        "load",
        lambda path: SimpleNamespace(policy=selected),
    )
    monkeypatch.setattr(
        builder,
        "_verify_semantic_authorization",
        lambda **kwargs: {
            "calibration_payload_sha256": "c" * 64,
            "policy_selection_recomputed_sha256": "d" * 64,
            "policy_gate_recomputed_sha256": "e" * 64,
        },
    )

    def assess(target: Path, authorization: dict) -> dict:
        artifact = builder.runtime_budget._artifact(
            bundle_bytes=authorization["bundle_bytes"],
            bundle_sha256=authorization["bundle_sha256"],
            runtime_identity_sha256=authorization["runtime_identity_sha256"],
            preselection_runtime_budget_payload_sha256=authorization[
                "preselection_runtime_budget_payload_sha256"
            ],
            source_collection_complete=True,
            cpu_ns=[1] * builder.runtime_budget.MEASURED_REPEATS,
            wall_ns=[2] * builder.runtime_budget.MEASURED_REPEATS,
            warmups_completed=builder.runtime_budget.WARMUP_ROUNDS,
            errors=[],
        )
        path = target / builder.RUNTIME_BUDGET_FILENAME
        path.write_text(json.dumps(artifact), encoding="utf-8")
        authorization.update({
            "final_runtime_budget_payload_sha256": artifact["payload_sha256"],
            "final_runtime_budget_file_sha256": builder._sha256(path),
        })
        return artifact

    monkeypatch.setattr(builder, "_assess_final_runtime_budget", assess)


def _replay_args(tmp_path: Path) -> dict:
    calibration_dir = tmp_path / "calibration"
    policy_dir = tmp_path / "policy"
    calibration_dir.mkdir(exist_ok=True)
    policy_dir.mkdir(exist_ok=True)
    return {
        "calibration_dir": calibration_dir,
        "policy_dir": policy_dir,
        "device": "cpu",
        "batch_size": 128,
    }


@pytest.mark.parametrize("unsafe_kind", ["oversized", "symlink"])
def test_unsafe_bundle_is_rejected_before_protected_json_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe_kind: str,
) -> None:
    bundle = tmp_path / f"{unsafe_kind}-bundle.json"
    if unsafe_kind == "oversized":
        with bundle.open("wb") as handle:
            handle.truncate(builder.runtime_budget.MAX_BUNDLE_BYTES + 1)
    else:
        target = tmp_path / "bundle-target.json"
        target.write_text("{}\n", encoding="utf-8")
        bundle.symlink_to(target.name)
    protected_reads = []

    def forbidden_read(*args, **kwargs):
        protected_reads.append((args, kwargs))
        raise AssertionError("protected JSON must not be read")

    monkeypatch.setattr(builder, "_read_json_snapshot", forbidden_read)
    with pytest.raises(ValueError, match="immutable native size budget"):
        builder.build_candidate(
            bundle_path=bundle,
            gate_dir=tmp_path / "protected-gate",
            calibration_dir=tmp_path / "protected-calibration",
            policy_dir=tmp_path / "protected-policy",
            role_manifest_path=tmp_path / "protected-role-manifest.json",
            ledger_path=tmp_path / "protected-ledger.json",
            device="cpu",
            batch_size=128,
            output=tmp_path / "v999_unsafe_v4_native_tcp",
        )
    assert protected_reads == []
    assert not list(tmp_path.glob(".v999_unsafe_v4_native_tcp.*"))


def test_failed_v4_gate_cannot_authorize_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, bundle, selected, role_manifest, ledger = _artifacts(
        tmp_path, passed=False
    )
    _mock_runtime(monkeypatch, selected)

    with pytest.raises(ValueError, match="does not authorize"):
        builder.verify_build_authorization(
            gate,
            bundle,
            role_manifest_path=role_manifest,
            ledger_path=ledger,
            **_replay_args(tmp_path),
        )


def test_builder_rechecks_ordinary_and_stratified_evidence(
    tmp_path: Path,
) -> None:
    gate, _bundle, _selected, _role_manifest, _ledger = _artifacts(tmp_path)
    evaluation = json.loads(
        (gate / "policy_gate_evaluation.json").read_text(encoding="utf-8")
    )
    result = json.loads(
        (gate / "policy_gate_result.json").read_text(encoding="utf-8")
    )
    evaluation["match_opponent_stratified_cluster_ci"]["lower"] = 0.0

    with pytest.raises(ValueError, match="chip evidence"):
        builder._verify_observed_evidence(evaluation, result)


def test_builder_rejects_member_calibration_role_drift(tmp_path: Path) -> None:
    _gate, bundle_path, _selected, _role_manifest, _ledger = _artifacts(tmp_path)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["members"][0]["outcome_calibration"] = _outcome(
        "1" * 64,
        artifact="e" * 64,
        role_manifest=bundle["members"][0]["source"][
            "role_manifest_sha256"
        ],
    )
    bundle["member_payload_sha256"][0] = builder._canonical_sha256(
        bundle["members"][0]
    )

    with pytest.raises(ValueError, match="share one unique-seed role"):
        builder._verify_bundle_members(bundle)

    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["calibration"]["member_seed"] = [101, 101, 307]
    with pytest.raises(ValueError, match="unique member seeds"):
        builder._verify_bundle_members(bundle)


def test_builder_rejects_nonformal_bootstrap_and_coverage(tmp_path: Path) -> None:
    gate, _bundle, _selected, _role_manifest, _ledger = _artifacts(tmp_path)
    evaluation = json.loads(
        (gate / "policy_gate_evaluation.json").read_text(encoding="utf-8")
    )
    result = json.loads(
        (gate / "policy_gate_result.json").read_text(encoding="utf-8")
    )
    evaluation["bootstrap_contract"]["samples"] = 1999
    result["summary"]["bootstrap_contract"]["samples"] = 1999
    with pytest.raises(ValueError, match="not formal"):
        builder._verify_observed_evidence(evaluation, result)

    evaluation["bootstrap_contract"]["samples"] = 2000
    result["summary"]["bootstrap_contract"]["samples"] = 2000
    result["thresholds"]["min_overrides"] = 11
    with pytest.raises(ValueError, match="coverage thresholds"):
        builder._verify_observed_evidence(evaluation, result)


def test_builder_rejects_gate_and_runtime_code_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, bundle_path, selected, role_manifest, ledger = _artifacts(tmp_path)
    artifact_path = gate / "artifact_manifest.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    first_gate_module = next(iter(artifact["code_artifacts"]))
    artifact["code_artifacts"][first_gate_module]["sha256"] = "0" * 64
    _write(artifact_path, artifact)
    _mock_runtime(monkeypatch, selected)
    with pytest.raises(ValueError, match="does not authorize"):
        builder.verify_build_authorization(
            gate,
            bundle_path,
            role_manifest_path=role_manifest,
            ledger_path=ledger,
            **_replay_args(tmp_path),
        )

    gate, bundle_path, _selected, role_manifest, ledger = _artifacts(
        tmp_path / "second"
    )
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    first_runtime_module = builder.COPIED_TOOL_MODULES[0]
    bundle["export_contract"]["copied_tool_modules"][first_runtime_module][
        "sha256"
    ] = "0" * 64
    bundle_path.write_bytes(bundle_exporter.canonical_bundle_bytes(bundle))
    with pytest.raises(ValueError, match="runtime module changed"):
        builder._verify_runtime_module_artifacts(bundle, builder.TOOLS)


def test_builder_binds_original_calibration_payload_to_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, bundle_path, selected, role_manifest, ledger = _artifacts(tmp_path)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    original = bundle["calibration"]["original_calibration_artifact"]
    original["response_temperature"]["temperature"] = 1.25
    bundle["calibration"]["response_temperature"] = 1.25
    original.pop("payload_sha256")
    original["payload_sha256"] = builder._canonical_sha256(original)
    projection = runtime.calibration_projection_from_artifact(original)
    projection_sha = runtime.calibration_projection_sha256(projection)
    bundle["calibration"]["payload_sha256"] = original["payload_sha256"]
    bundle["calibration"]["calibration_projection_sha256"] = projection_sha
    bundle["source"]["calibration_payload_sha256"] = original[
        "payload_sha256"
    ]
    bundle["source"]["calibration_projection_sha256"] = projection_sha
    bundle_path.write_bytes(bundle_exporter.canonical_bundle_bytes(bundle))
    _mock_runtime(monkeypatch, selected)

    with pytest.raises(ValueError, match="authorize|bound|runtime identity"):
        builder.verify_build_authorization(
            gate,
            bundle_path,
            role_manifest_path=role_manifest,
            ledger_path=ledger,
            **_replay_args(tmp_path),
        )


def test_builder_rechecks_runtime_modules_after_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, bundle, selected, role_manifest, ledger = _artifacts(tmp_path)
    _mock_runtime(monkeypatch, selected)
    original_copy = builder._copy_runtime_modules

    def copy_then_tamper(target: Path, native_sources: dict[str, bytes]) -> None:
        original_copy(target, native_sources)
        path = target / builder.COPIED_TOOL_MODULES[0]
        path.write_text(path.read_text(encoding="utf-8") + "\n# drift\n")

    monkeypatch.setattr(builder, "_copy_runtime_modules", copy_then_tamper)
    output = tmp_path / "v998_tampered_v4_native_tcp"
    with pytest.raises(ValueError, match="runtime module changed"):
        builder.build_candidate(
            bundle_path=bundle,
            gate_dir=gate,
            role_manifest_path=role_manifest,
            ledger_path=ledger,
            output=output,
            **_replay_args(tmp_path),
        )
    assert not output.exists()


def test_passing_v4_gate_builds_native_candidate_with_false_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, bundle, selected, role_manifest, ledger = _artifacts(tmp_path)
    _mock_runtime(monkeypatch, selected)
    output = tmp_path / "v999_test_v4_native_tcp"
    strategy_before = builder._directory_sha256(builder.DEFAULT_STRATEGY_DONOR)
    transport_before = builder._directory_sha256(builder.DEFAULT_TRANSPORT_DONOR)

    manifest = builder.build_candidate(
        bundle_path=bundle,
        gate_dir=gate,
        role_manifest_path=role_manifest,
        ledger_path=ledger,
        output=output,
        **_replay_args(tmp_path),
    )

    assert manifest["deployment_policy_value"] is False
    assert manifest["strength_evidence"] is False
    assert manifest["runtime_contract"]["ablation_env"] == [
        "POK_V4_DISABLE",
        "POK_V4_DISABLE_CROSS_HAND",
        "POK_V4_DISABLE_OUTCOME_UNCERTAINTY_MATCH",
    ]
    assert (output / "v4_ensemble_bundle.json").is_file()
    assert (output / "V4_BUILD_MANIFEST.json").is_file()
    assert native_policy._authorized_bundle_payload(output) == json.loads(
        (output / "v4_ensemble_bundle.json").read_text(encoding="utf-8")
    )
    native = (output / "national_bot.py").read_text(encoding="utf-8")
    assert "self.v4_policy" in native
    assert "v4_decision" in native
    assert "self.v3_policy" not in native
    smoke = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
                from national_bot import NativeNationalBot

                request = {
                    "my_chips": 19950, "opponent_chips": 19900,
                    "my_stage_bet": 50, "opponent_stage_bet": 100,
                    "pot": 150, "to_call": 50,
                }
                state = {
                    "round": 0, "round_bet": 100, "round_raise": 100,
                    "min_raise_action": 151, "my_round_bet": 50,
                    "to_call": 50, "pot": 150, "opponent_allin": False,
                }
                bot = NativeNationalBot("Smoke", "upper")
                assert bot.v4_policy is None
                bot._request = lambda: dict(request)
                bot.get_action = lambda req, requests: 151
                bot.consume_strategy_context = lambda: {}
                bot.reconstruct_state = lambda req: dict(state)
                bot.apply_neural_advice = lambda req, state, action: action
                bot.sanitize_action = lambda action, state, chips: 201

                class BrokenPolicy:
                    last_decision = None
                    def advise(self, *args):
                        raise RuntimeError("broken v4 policy")

                bot.v4_policy = BrokenPolicy()
                assert bot._strategy_action(0) == 201
            """),
        ],
        cwd=output,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert smoke.returncode == 0, smoke.stderr
    assert builder._directory_sha256(builder.DEFAULT_STRATEGY_DONOR) == strategy_before
    assert builder._directory_sha256(builder.DEFAULT_TRANSPORT_DONOR) == transport_before


def test_final_runtime_budget_failure_cleans_temp_and_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, bundle, selected, role_manifest, ledger = _artifacts(tmp_path / "case")
    actual_assess = builder._assess_final_runtime_budget
    _mock_runtime(monkeypatch, selected)
    monkeypatch.setattr(builder, "_assess_final_runtime_budget", actual_assess)
    benchmark_calls = []

    def failed_benchmark(bundle_path: Path, **kwargs) -> dict:
        benchmark_calls.append((Path(bundle_path), dict(kwargs)))
        payload = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
        return builder.runtime_budget._artifact(
            bundle_bytes=Path(bundle_path).stat().st_size,
            bundle_sha256=builder._sha256(Path(bundle_path)),
            runtime_identity_sha256=(
                builder.runtime_budget.bundle_runtime_identity_sha256(payload)
            ),
            preselection_runtime_budget_payload_sha256=kwargs[
                "preselection_runtime_budget_payload_sha256"
            ],
            source_collection_complete=True,
            cpu_ns=[],
            wall_ns=[],
            warmups_completed=0,
            errors=["synthetic final runtime benchmark failure"],
        )

    monkeypatch.setattr(
        builder.runtime_budget,
        "measure_bundle_runtime_budget_subprocess",
        failed_benchmark,
    )
    output = tmp_path / "v997_runtime_budget_failure_v4_native_tcp"
    with pytest.raises(ValueError, match="not formal-eligible"):
        builder.build_candidate(
            bundle_path=bundle,
            gate_dir=gate,
            role_manifest_path=role_manifest,
            ledger_path=ledger,
            output=output,
            **_replay_args(tmp_path / "case"),
        )

    assert len(benchmark_calls) == 1
    assert not output.exists()
    assert not list(tmp_path.glob(f".{output.name}.*"))


def test_v4_cli_output_must_be_new_formal_version(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="formal output"):
        builder._formal_output(tmp_path / "candidate")


@pytest.mark.parametrize("tamper", ["manifest", "ledger", "snapshot_path"])
def test_builder_requires_external_manifest_ledger_and_exact_snapshot(
    tmp_path: Path, tamper: str,
) -> None:
    gate, bundle, _selected, role_manifest, ledger = _artifacts(tmp_path)
    if tamper == "ledger":
        ledger.unlink()
    else:
        payload = json.loads(role_manifest.read_text(encoding="utf-8"))
        if tamper == "manifest":
            payload["candidate_snapshot"]["sha256"] = "0" * 64
        else:
            payload["candidate_snapshot"]["path"] = str(tmp_path / "wrong")
        _write(role_manifest, payload)

    with pytest.raises((FileNotFoundError, ValueError), match="snapshot|ledger|role"):
        builder.verify_build_authorization(
            gate,
            bundle,
            role_manifest_path=role_manifest,
            ledger_path=ledger,
            **_replay_args(tmp_path),
        )


def test_builder_uses_verified_input_snapshot_after_source_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, bundle, selected, role_manifest, ledger = _artifacts(tmp_path)
    _mock_runtime(monkeypatch, selected)
    original_bundle = bundle.read_bytes()
    original_result = (gate / "policy_gate_result.json").read_bytes()

    def mutate_sources_after_copy(target: Path) -> None:
        bundle.write_bytes(b"{}\n")
        (gate / "policy_gate_result.json").write_bytes(b"{}\n")

    monkeypatch.setattr(builder, "_smoke_candidate", mutate_sources_after_copy)
    output = tmp_path / "v997_snapshot_v4_native_tcp"
    builder.build_candidate(
        bundle_path=bundle,
        gate_dir=gate,
        role_manifest_path=role_manifest,
        ledger_path=ledger,
        output=output,
        **_replay_args(tmp_path),
    )

    assert (output / "v4_ensemble_bundle.json").read_bytes() == original_bundle
    assert (
        output / "evidence" / "offline_policy_gate" /
        "policy_gate_result.json"
    ).read_bytes() == original_result


@pytest.mark.parametrize("dependency", ["requests", "scipy"])
def test_candidate_stdlib_allowlist_rejects_third_party(
    tmp_path: Path, dependency: str,
) -> None:
    (tmp_path / "entry.py").write_text(f"import {dependency}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="non-stdlib/nonlocal"):
        builder._verify_stdlib_candidate(tmp_path)


def test_builder_rejects_gate_probability_domain_forgery(tmp_path: Path) -> None:
    gate, _bundle, _selected, _role_manifest, _ledger = _artifacts(tmp_path)
    evaluation = json.loads(
        (gate / "policy_gate_evaluation.json").read_text(encoding="utf-8")
    )
    result = json.loads(
        (gate / "policy_gate_result.json").read_text(encoding="utf-8")
    )
    evaluation["match_positive_rate"] = 999.0
    evaluation["match_positive_rate_cluster_bootstrap_ci"] = {
        "lower": 999.0, "mean": 999.0, "upper": 999.0,
    }
    with pytest.raises(ValueError, match="outside its domain"):
        builder._verify_observed_evidence(evaluation, result)


def test_builder_semantically_replays_gate_and_rejects_coherent_forgery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation = {
        "inference_contract": {"device": "cpu", "batch_size": 128},
        "bootstrap_contract": {"samples": 2000, "seed": 7},
        "observed_score": 4.0,
    }
    result = {"thresholds": {"minimum": 1}, "passed": True}
    calibrated = {"calibration_payload_sha256": "c" * 64}
    policy = {
        "root": tmp_path,
        "candidate_sha256": "a" * 64,
        "selected_policy": _policy(),
        "recomputed_evaluation_sha256": "d" * 64,
    }
    binding = {"bundle_sha256": "e" * 64}
    replay_phase = {"selection_result_sha256": "f" * 64}
    monkeypatch.setattr(
        builder, "load_calibrated_ensemble", lambda *args, **kwargs: calibrated
    )
    monkeypatch.setattr(
        builder, "_validated_calibrated_ensemble",
        lambda payload, **kwargs: payload,
    )
    monkeypatch.setattr(
        builder, "recompute_and_verify_formal_policy_selection",
        lambda *args, **kwargs: policy,
    )
    monkeypatch.setattr(
        builder.bundle_exporter, "build_verified_bundle_payload",
        lambda **kwargs: {"rebuilt": True},
    )
    monkeypatch.setattr(
        builder.bundle_exporter, "verify_exact_bundle",
        lambda *args: ({}, b"", binding),
    )
    monkeypatch.setattr(
        builder.v4_gate, "recompute_bound_fixed_gate",
        lambda **kwargs: (replay_phase, dict(evaluation)),
    )
    monkeypatch.setattr(
        builder.v4_gate, "build_bound_gate_result",
        lambda **kwargs: dict(result),
    )
    kwargs = {
        "dataset": SimpleNamespace(),
        "calibration_dir": tmp_path / "calibration",
        "policy_dir": tmp_path / "policy",
        "bundle_path": tmp_path / "bundle.json",
        "bundle_binding": binding,
        "result": result,
        "run_id": "test-run",
        "device": "cpu",
        "batch_size": 128,
    }
    authorization = builder._verify_semantic_authorization(
        evaluation=evaluation, **kwargs
    )
    assert authorization["policy_gate_recomputed_sha256"] == (
        builder._canonical_sha256(evaluation)
    )

    forged = dict(evaluation, observed_score=999.0)
    with pytest.raises(ValueError, match="protected replay"):
        builder._verify_semantic_authorization(evaluation=forged, **kwargs)


def test_builder_handles_readonly_donor_and_cleans_failed_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = builder.DEFAULT_STRATEGY_DONOR
    readonly = tmp_path / "snapshots" / source.name
    readonly.parent.mkdir()
    shutil.copytree(source, readonly)
    items = list(readonly.rglob("*"))
    for path in items:
        if path.is_file():
            path.chmod(0o400)
    for path in reversed(items):
        if path.is_dir():
            path.chmod(0o500)
    readonly.chmod(0o500)
    monkeypatch.setattr(builder, "DEFAULT_STRATEGY_DONOR", readonly)
    gate, bundle, selected, role_manifest, ledger = _artifacts(tmp_path / "case")
    _mock_runtime(monkeypatch, selected)
    output = tmp_path / "v995_readonly_v4_native_tcp"
    manifest = builder.build_candidate(
        bundle_path=bundle,
        gate_dir=gate,
        role_manifest_path=role_manifest,
        ledger_path=ledger,
        output=output,
        **_replay_args(tmp_path / "case"),
    )
    assert manifest["candidate"] == output.name
    assert (output / "strategy.py").stat().st_mode & stat.S_IWUSR

    monkeypatch.setattr(
        builder,
        "_smoke_candidate",
        lambda target: (_ for _ in ()).throw(RuntimeError("smoke failed")),
    )
    failed = tmp_path / "v994_readonly_failure_v4_native_tcp"
    with pytest.raises(RuntimeError, match="smoke failed"):
        builder.build_candidate(
            bundle_path=bundle,
            gate_dir=gate,
            role_manifest_path=role_manifest,
            ledger_path=ledger,
            output=failed,
            **_replay_args(tmp_path / "case"),
        )
    assert not list(tmp_path.glob(f".{failed.name}.*"))
    builder._grant_owner_build_access(readonly)


def test_builder_rejects_native_input_drift_after_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, bundle, selected, role_manifest, ledger = _artifacts(tmp_path)
    _mock_runtime(monkeypatch, selected)
    drifted = builder.current_native_build_contract()
    drifted = json.loads(json.dumps(drifted))
    drifted["artifacts"]["national_validator_source"]["sha256"] = "0" * 64
    monkeypatch.setattr(builder, "current_native_build_contract", lambda: drifted)

    with pytest.raises(ValueError, match="does not authorize"):
        builder.verify_build_authorization(
            gate,
            bundle,
            role_manifest_path=role_manifest,
            ledger_path=ledger,
            **_replay_args(tmp_path),
        )
