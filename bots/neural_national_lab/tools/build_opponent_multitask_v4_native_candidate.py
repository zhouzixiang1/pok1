#!/usr/bin/env python3
"""Build a native v4 candidate only from a passing protected policy gate."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any

import build_opponent_multitask_v3_native_candidate as v3_builder
import evaluate_opponent_multitask_v4_policy_gate as v4_gate
import export_opponent_multitask_ensemble_v4 as bundle_exporter
from evaluate_opponent_multitask_v4_policy_gate import (
    GATE_ARTIFACT_SCHEMA,
    GATE_EVALUATION_SCHEMA,
    GATE_REPORT_SCHEMA,
    gate_code_artifacts,
)
from match_outcome_calibration import validate_calibration_artifact
from match_outcome_schema import MATCH_OUTCOME_ESTIMAND
from opponent_exposure_ledger import status as ledger_status
from opponent_multitask_ensemble_runtime_v4 import (
    BUNDLE_SCHEMA,
    CHECKPOINT_SCHEMA,
    ENSEMBLE_FORMAT,
    FORMAL_COLLECTION_PASSES,
    FORMAL_UNCERTAINTY_STD_WEIGHT,
    OpponentMultiTaskEnsembleRuntimeV4,
    RUNTIME_MODULE_FILENAMES,
    calibration_projection_sha256,
    validate_calibration_binding,
)
from opponent_multitask_model_v4 import MODEL_FORMAT
from policy_role_evidence import (
    BOOTSTRAP_CONTRACT_SCHEMA,
    POLICY_GATE_RESULT_SCHEMA_V4,
)
from role_dataset_access import POLICY_OFFLINE_ESTIMAND_V4, RoleDatasetAccess
from select_opponent_multitask_v4_policy import (
    _validated_calibrated_ensemble,
    load_calibrated_ensemble,
    recompute_and_verify_formal_policy_selection,
)
from v4_native_build_contract import (
    current_native_build_contract,
    snapshot_native_build_inputs,
)
import v4_runtime_budget as runtime_budget
from win_first_policy_v4 import normalize_policy


ROOT = v3_builder.ROOT
TOOLS = v3_builder.TOOLS
VERSIONS = v3_builder.VERSIONS
BUILD_SCHEMA = "opponent_multitask_v4_native_candidate_build_v1"
RUNTIME_BUDGET_FILENAME = "V4_RUNTIME_BUDGET.json"
VERSION_RE = re.compile(r"^v\d+_[a-z0-9_]+$")
COPIED_TOOL_MODULES = RUNTIME_MODULE_FILENAMES
FORMAL_MIN_BOOTSTRAP_SAMPLES = 2000
FORMAL_MIN_OVERRIDES = 12
FORMAL_MIN_OVERRIDE_CLUSTERS = 8
FORMAL_MIN_OVERRIDES_PER_OPPONENT = 4
EXPECTED_STRATEGY_DONOR_NAME = (
    "v140_national_v123_overlay_no_large_commit_veto_tcp"
)
DEFAULT_STRATEGY_DONOR = VERSIONS / EXPECTED_STRATEGY_DONOR_NAME
DEFAULT_TRANSPORT_DONOR = (
    VERSIONS / "v151_national_v150_temporal_multitask_shadow_tcp"
)
EXPECTED_STRATEGY_DONOR_SHA256 = (
    "a8dadfefca945832df00a4bc438551834361f5464a8463dda20d146d02aa045d"
)
EXPECTED_STRATEGY_FILES = {
    "national_bot.py": "60636a2fd03e4e570f716b56b6518bdeb7d9ceef44e2a8ebff6958c93dbf5be3",
    "strategy.py": "28a36e11f42aecd93dd931af01bc98241eea31fff64b262a36b120ca18bbcf7a",
    "neural_policy.py": "342cf69633ca87ec146f76d0523ec565e75be4d81251ed45bb1500da114e8a5c",
}
EXPECTED_TRANSPORT_DONOR_SHA256 = (
    "0e7d3f42e2cc82417cef96b2c902a58a646373ce15e80aceb4d1b441c554749c"
)
EXPECTED_TRANSPORT_FILES = {
    "national_bot.py": "8bf7003cb8bd38b3cd8d1bdc3f5c2d169bb94ebe61f02e0adf21ccdda7a7ebd2",
}


_canonical_sha256 = v3_builder._canonical_sha256
_sha256 = v3_builder._sha256
_directory_sha256 = v3_builder._directory_sha256
_load_json = v3_builder._load_json
_digest = v3_builder._digest


def _finite(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _bounded_bundle_path(path: Path) -> Path:
    """Reject an unsafe/oversized bundle before protected evidence is read."""
    source = Path(path)
    if source.is_symlink():
        raise ValueError("v4 bundle exceeds the immutable native size budget")
    resolved = source.resolve()
    if (
        not resolved.is_file()
        or resolved.stat().st_size > runtime_budget.MAX_BUNDLE_BYTES
    ):
        raise ValueError("v4 bundle exceeds the immutable native size budget")
    return resolved


def _read_json_snapshot(path: Path, *, field: str) -> tuple[bytes, dict[str, Any]]:
    raw = path.resolve().read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {field}: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{field} must be a JSON object")
    return raw, payload


def _verify_donor(
    path: Path,
    *,
    expected_sha256: str,
    critical_files: dict[str, str],
    field: str,
) -> dict[str, Any]:
    root = path.resolve()
    if not root.is_dir() or _directory_sha256(root) != expected_sha256:
        raise ValueError(f"{field} directory snapshot changed")
    files = {}
    for name, expected in critical_files.items():
        item = root / name
        if not item.is_file() or _sha256(item) != expected:
            raise ValueError(f"{field} critical file changed: {name}")
        files[name] = {
            "bytes": item.stat().st_size,
            "sha256": expected,
        }
    return {
        "path": str(root),
        "sha256": expected_sha256,
        "critical_files": files,
    }


def _verify_external_authority(
    *,
    role_manifest_path: Path,
    ledger_path: Path,
    run_id: str,
    candidate_sha256: str,
    selection_result_sha256: str,
    policy_gate_artifact_sha256: str,
    gate_opponents: list[str],
) -> tuple[RoleDatasetAccess, Path, dict[str, Any]]:
    dataset = RoleDatasetAccess(
        role_manifest_path,
        ledger_path=ledger_path,
        run_id=run_id,
        require_complete=True,
    )
    dataset.require_collection_boundary(FORMAL_COLLECTION_PASSES)
    snapshot = dataset.manifest.get("candidate_snapshot")
    snapshot_path = Path(str((snapshot or {}).get("path") or "")).resolve()
    if (
        dataset.candidate_snapshot
        != {
            "name": EXPECTED_STRATEGY_DONOR_NAME,
            "sha256": EXPECTED_STRATEGY_DONOR_SHA256,
        }
        or snapshot_path.name != EXPECTED_STRATEGY_DONOR_NAME
    ):
        raise ValueError("role manifest candidate snapshot is not exact v140")
    strategy_contract = _verify_donor(
        snapshot_path,
        expected_sha256=EXPECTED_STRATEGY_DONOR_SHA256,
        critical_files=EXPECTED_STRATEGY_FILES,
        field="strategy donor",
    )
    expected_opponents = list(dataset.roles["policy_gate"])
    if gate_opponents != expected_opponents:
        raise ValueError("policy gate opponents differ from role manifest")
    base_artifact = dataset._role_artifact_sha256("policy_gate")
    expected_gate_artifact = _sha256_bytes(
        f"{base_artifact}:{selection_result_sha256}".encode()
    )
    if policy_gate_artifact_sha256 != expected_gate_artifact:
        raise ValueError("policy gate artifact does not match role manifest")
    ledger = ledger_status(ledger_path.resolve())
    for opponent in expected_opponents:
        exposures = (
            ledger.get("opponents", {})
            .get(opponent, {})
            .get("exposures", [])
        )
        if not any(
            row.get("role") == "policy_gate"
            and row.get("run_id") == run_id
            and row.get("candidate_sha256") == candidate_sha256
            and row.get("artifact_sha256") == expected_gate_artifact
            for row in exposures
        ):
            raise ValueError(
                f"policy gate ledger exposure is missing: {opponent}"
            )
    return dataset, snapshot_path, {
        "role_manifest_path": str(role_manifest_path.resolve()),
        "role_manifest_sha256": dataset.manifest_sha256,
        "ledger_path": str(ledger_path.resolve()),
        "candidate_snapshot": dict(dataset.candidate_snapshot),
        "strategy_context_runtime_mode": (
            dataset.strategy_context_runtime_mode
        ),
        "strategy_donor": strategy_contract,
        "policy_gate_artifact_sha256": expected_gate_artifact,
    }


def _verify_collection_boundary(raw: Any) -> None:
    expected = {
        "schema": "complete_atomic_collection_boundary_v1",
        "source_completed_passes": FORMAL_COLLECTION_PASSES,
        "source_requested_passes": FORMAL_COLLECTION_PASSES,
        "source_collection_complete": True,
    }
    if raw != expected:
        raise ValueError("v4 candidate requires the complete 160-pass boundary")


def _verify_bootstrap_contract(raw: Any) -> dict[str, Any]:
    expected_fields = {
        "schema",
        "samples",
        "seed",
        "observed_70_hand_match_clusters",
        "ordinary",
        "opponent_stratified",
    }
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise ValueError("v4 gate bootstrap contract is missing")
    samples = raw.get("samples")
    seed = raw.get("seed")
    if (
        raw.get("schema") != BOOTSTRAP_CONTRACT_SCHEMA
        or isinstance(samples, bool)
        or not isinstance(samples, int)
        or samples < FORMAL_MIN_BOOTSTRAP_SAMPLES
        or isinstance(seed, bool)
        or not isinstance(seed, int)
        or raw.get("observed_70_hand_match_clusters") is not True
        or raw.get("ordinary") is not True
        or raw.get("opponent_stratified") is not True
    ):
        raise ValueError("v4 gate bootstrap contract is not formal")
    return dict(raw)


def _verify_runtime_module_artifacts(
    bundle: dict[str, Any], root: Path
) -> dict[str, dict[str, Any]]:
    export_contract = bundle.get("export_contract")
    artifacts = (
        export_contract.get("copied_tool_modules")
        if isinstance(export_contract, dict) else None
    )
    if not isinstance(artifacts, dict) or set(artifacts) != set(COPIED_TOOL_MODULES):
        raise ValueError("v4 bundle runtime module contract is incomplete")
    normalized = {}
    for name in COPIED_TOOL_MODULES:
        contract = artifacts.get(name)
        path = root / name
        if (
            not isinstance(contract, dict)
            or set(contract) != {"bytes", "sha256"}
            or not path.is_file()
            or path.stat().st_size != int(contract.get("bytes", -1))
            or _sha256(path)
            != _digest(contract.get("sha256"), field=f"{name} sha256")
        ):
            raise ValueError(f"v4 runtime module changed: {name}")
        normalized[name] = dict(contract)
    return normalized


def _verify_observed_evidence(
    evaluation: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    """Independently enforce both ordinary and opponent-stratified gates."""
    v4_gate.validate_gate_probability_domain(evaluation)
    selected = evaluation.get("selected_policy")
    normalized = normalize_policy(selected)
    if normalized is None or normalized != selected:
        raise ValueError("policy gate selected policy is not protected v4")
    bootstrap_contract = _verify_bootstrap_contract(
        evaluation.get("bootstrap_contract")
    )
    summary = result.get("summary")
    if (
        not isinstance(summary, dict)
        or summary.get("bootstrap_contract") != bootstrap_contract
    ):
        raise ValueError("v4 gate result bootstrap binding changed")
    thresholds = result.get("thresholds")
    if not isinstance(thresholds, dict):
        raise ValueError("policy gate evidence thresholds are missing")
    required = {
        "min_overrides",
        "min_override_clusters",
        "min_overrides_per_opponent",
        "min_cluster_ci_lower",
        "min_opponent_stratified_ci_lower",
        "min_match_outcome_coverage",
        "min_match_positive_rate_ci_lower",
        "min_match_positive_uplift_ci_lower",
        "min_opponent_match_positive_rate",
    }
    if set(thresholds) != required:
        raise ValueError("policy gate evidence threshold contract changed")
    coverage = _finite(
        thresholds["min_match_outcome_coverage"], field="outcome coverage"
    )
    rate_floor = _finite(
        thresholds["min_match_positive_rate_ci_lower"], field="rate floor"
    )
    uplift_floor = _finite(
        thresholds["min_match_positive_uplift_ci_lower"],
        field="uplift floor",
    )
    opponent_rate_floor = _finite(
        thresholds["min_opponent_match_positive_rate"],
        field="opponent rate floor",
    )
    ordinary_floor = _finite(
        thresholds["min_cluster_ci_lower"], field="ordinary CI floor"
    )
    stratified_floor = _finite(
        thresholds["min_opponent_stratified_ci_lower"],
        field="stratified CI floor",
    )
    if (
        coverage != 1.0
        or not 0.5 <= rate_floor <= 1.0
        or not 0.0 <= uplift_floor <= 1.0
        or not 0.5 <= opponent_rate_floor <= 1.0
        or ordinary_floor < 0.0
        or stratified_floor < 0.0
        or evaluation.get("match_outcome_estimand") != MATCH_OUTCOME_ESTIMAND
        or result.get("match_outcome_estimand") != MATCH_OUTCOME_ESTIMAND
        or _finite(
            evaluation.get("match_outcome_row_coverage", 0.0),
            field="row outcome coverage",
        ) < coverage
        or _finite(
            evaluation.get("match_outcome_cluster_coverage", 0.0),
            field="cluster outcome coverage",
        ) < coverage
    ):
        raise ValueError("policy gate does not satisfy the v4 evidence contract")
    for field in (
        "match_positive_rate_cluster_bootstrap_ci",
        "match_positive_rate_opponent_stratified_cluster_ci",
    ):
        if _finite(
            (evaluation.get(field) or {}).get("lower", 0.0),
            field=f"{field}.lower",
        ) <= rate_floor:
            raise ValueError("policy gate positive-rate evidence is insufficient")
    for field in (
        "match_positive_uplift_cluster_bootstrap_ci",
        "match_positive_uplift_opponent_stratified_cluster_ci",
    ):
        if _finite(
            (evaluation.get(field) or {}).get("lower", -1.0),
            field=f"{field}.lower",
        ) < uplift_floor:
            raise ValueError("policy gate positive-uplift evidence is insufficient")
    chip_intervals = (
        ("match_cluster_bootstrap_mean_ci", ordinary_floor),
        ("match_opponent_stratified_cluster_ci", stratified_floor),
    )
    for field, floor in chip_intervals:
        if _finite(
            (evaluation.get(field) or {}).get("lower", 0.0),
            field=f"{field}.lower",
        ) <= floor:
            raise ValueError("policy gate chip evidence is insufficient")

    raw_coverage = (
        thresholds["min_overrides"],
        thresholds["min_override_clusters"],
        thresholds["min_overrides_per_opponent"],
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in raw_coverage):
        raise ValueError("policy gate coverage thresholds must be integers")
    minimum_overrides, minimum_clusters, minimum_per_opponent = raw_coverage
    if (
        minimum_overrides < FORMAL_MIN_OVERRIDES
        or minimum_clusters < FORMAL_MIN_OVERRIDE_CLUSTERS
        or minimum_per_opponent < FORMAL_MIN_OVERRIDES_PER_OPPONENT
    ):
        raise ValueError("policy gate coverage thresholds are invalid")
    if int(evaluation.get("overrides", 0) or 0) < minimum_overrides:
        raise ValueError("policy gate override coverage is insufficient")
    if int(evaluation.get("override_clusters", 0) or 0) < minimum_clusters:
        raise ValueError("policy gate cluster coverage is insufficient")
    by_opponent = evaluation.get("by_opponent")
    if not isinstance(by_opponent, dict) or not by_opponent:
        raise ValueError("policy gate has no per-opponent evidence")
    for opponent, raw in sorted(by_opponent.items()):
        row = raw if isinstance(raw, dict) else {}
        if (
            int(row.get("overrides", 0) or 0) < minimum_per_opponent
            or int(row.get("match_outcome_clusters", 0) or 0) < 1
            or _finite(row.get("mean", -1.0), field=f"{opponent}.mean") < 0.0
            or _finite(
                row.get("match_positive_rate", 0.0),
                field=f"{opponent}.positive_rate",
            ) < opponent_rate_floor
            or _finite(
                row.get("match_positive_uplift_mean", -1.0),
                field=f"{opponent}.uplift",
            ) < 0.0
        ):
            raise ValueError(f"policy gate failed for opponent {opponent}")
    return normalized


def _verify_bundle_members(bundle: dict[str, Any]) -> None:
    members = bundle.get("members")
    member_hashes = bundle.get("member_payload_sha256")
    calibration = bundle.get("calibration")
    if (
        not isinstance(members, list)
        or len(members) < 3
        or not isinstance(member_hashes, list)
        or len(member_hashes) != len(members)
        or not isinstance(calibration, dict)
    ):
        raise ValueError("formal v4 bundle requires three calibrated members")
    member_seeds = calibration.get("member_seed")
    if (
        not isinstance(member_seeds, list)
        or len(member_seeds) != len(members)
        or len(member_seeds) < 3
        or any(
            isinstance(seed, bool) or not isinstance(seed, int)
            for seed in member_seeds
        )
        or len(set(member_seeds)) != len(member_seeds)
    ):
        raise ValueError("formal v4 bundle requires three unique member seeds")
    checkpoints = []
    signatures = set()
    outcome_hashes = []
    for index, (member, member_hash) in enumerate(zip(members, member_hashes)):
        if not isinstance(member, dict) or _canonical_sha256(member) != _digest(
            member_hash, field=f"member_payload_sha256[{index}]"
        ):
            raise ValueError("v4 bundle member payload changed")
        source = member.get("source")
        if not isinstance(source, dict):
            raise ValueError("v4 bundle member source is missing")
        checkpoint = _digest(
            source.get("checkpoint_sha256"), field="member checkpoint"
        )
        if source.get("source_collection_complete") is not True:
            raise ValueError("formal v4 bundle member used incomplete data")
        outcome = validate_calibration_artifact(
            member.get("outcome_calibration"),
            checkpoint_sha256=checkpoint,
            model_format=MODEL_FORMAT,
        )
        if outcome.get("source_collection_complete") is not True:
            raise ValueError("formal v4 outcome calibration used incomplete data")
        if (
            source.get("checkpoint_schema") != CHECKPOINT_SCHEMA
            or source.get("role_manifest_sha256")
            != outcome.get("role_manifest_sha256")
            or outcome.get("calibration_role") != "model_calibration"
            or outcome.get("policy_evidence_used") is not False
            or outcome.get("member_seed") != member_seeds[index]
        ):
            raise ValueError("formal v4 outcome calibration used policy evidence")
        training = source.get("training_artifact_sha256")
        code = source.get("code_artifacts")
        if (
            not isinstance(training, dict)
            or set(training) != {"train", "early_stop"}
            or not isinstance(code, dict)
            or not code
        ):
            raise ValueError("formal v4 member provenance is incomplete")
        for role, digest in training.items():
            _digest(digest, field=f"{role} training artifact")
        for name, contract in code.items():
            if (
                not isinstance(contract, dict)
                or set(contract) != {"bytes", "sha256"}
                or isinstance(contract.get("bytes"), bool)
                or not isinstance(contract.get("bytes"), int)
                or contract["bytes"] < 1
            ):
                raise ValueError("formal v4 member code contract is invalid")
            _digest(contract.get("sha256"), field=f"{name} code artifact")
        export_contract = member.get("export_contract")
        member_exporter_path = Path(
            sys.modules["export_opponent_multitask_v4"].__file__
        ).resolve()
        member_runtime_path = TOOLS / "opponent_multitask_runtime_v4.py"
        if (
            not isinstance(export_contract, dict)
            or export_contract.get("export_tool_sha256")
            != _sha256(member_exporter_path)
            or export_contract.get("runtime_tool_sha256")
            != _sha256(member_runtime_path)
        ):
            raise ValueError("formal v4 member exporter contract changed")
        checkpoints.append(checkpoint)
        outcome_hashes.append(outcome["payload_sha256"])
        signatures.add((
            outcome.get("run_id"),
            outcome["role_manifest_sha256"],
            outcome["model_calibration_artifact_sha256"],
            tuple(outcome["model_calibration_opponents"]),
            outcome["source_collection_complete"],
        ))
    if len(set(checkpoints)) != len(checkpoints) or len(signatures) != 1:
        raise ValueError("v4 bundle members do not share one unique-seed role")
    signature = next(iter(signatures))
    expected = (
        calibration.get("run_id"),
        calibration.get("role_manifest_sha256"),
        calibration.get("model_calibration_artifact_sha256"),
        tuple(calibration.get("model_calibration_opponents") or ()),
        calibration.get("source_collection_complete"),
    )
    if (
        signature != expected
        or calibration.get("member_checkpoint_sha256") != checkpoints
        or calibration.get("outcome_calibration_payload_sha256")
        != outcome_hashes
    ):
        raise ValueError("v4 bundle calibration member binding changed")


def _verify_semantic_authorization(
    *,
    dataset: RoleDatasetAccess,
    calibration_dir: Path,
    policy_dir: Path,
    bundle_path: Path,
    bundle_binding: dict[str, Any],
    evaluation: dict[str, Any],
    result: dict[str, Any],
    run_id: str,
    device: str,
    batch_size: int,
) -> dict[str, Any]:
    if evaluation.get("inference_contract") != {
        "device": str(device),
        "batch_size": batch_size,
    }:
        raise ValueError("v4 gate inference contract changed")
    calibrated = _validated_calibrated_ensemble(
        load_calibrated_ensemble(
            calibration_dir,
            dataset=dataset,
            run_id=run_id,
            device=device,
            formal=True,
        ),
        dataset=dataset,
        run_id=run_id,
        formal=True,
    )
    policy = recompute_and_verify_formal_policy_selection(
        policy_dir,
        calibrated=calibrated,
        dataset=dataset,
        run_id=run_id,
        device=device,
        batch_size=batch_size,
    )
    expected_bundle = bundle_exporter.build_verified_bundle_payload(
        calibrated=calibrated,
        policy=policy,
        dataset=dataset,
        run_id=run_id,
        formal=True,
    )
    _actual, _raw, rebuilt_binding = bundle_exporter.verify_exact_bundle(
        bundle_path, expected_bundle
    )
    if rebuilt_binding != bundle_binding:
        raise ValueError("v4 rebuilt bundle binding changed")
    bootstrap = evaluation.get("bootstrap_contract")
    if not isinstance(bootstrap, dict):
        raise ValueError("v4 gate bootstrap contract is missing")
    phase, replayed_evaluation = v4_gate.recompute_bound_fixed_gate(
        dataset=dataset,
        calibrated=calibrated,
        selected_policy=policy["selected_policy"],
        candidate_sha256=policy["candidate_sha256"],
        selection_result_path=(
            policy["root"] / "policy_selection_result.json"
        ),
        bundle_binding=bundle_binding,
        batch_size=batch_size,
        device=device,
        bootstrap_samples=bootstrap.get("samples"),
        bootstrap_seed=bootstrap.get("seed"),
    )
    if replayed_evaluation != evaluation:
        raise ValueError("v4 gate evaluation differs from protected replay")
    replayed_result = v4_gate.build_bound_gate_result(
        phase=phase,
        evaluation=replayed_evaluation,
        thresholds=result.get("thresholds"),
        bundle_binding=bundle_binding,
        dataset=dataset,
    )
    if replayed_result != result:
        raise ValueError("v4 gate result differs from protected replay")
    return {
        "calibration_payload_sha256": calibrated[
            "calibration_payload_sha256"
        ],
        "policy_selection_recomputed_sha256": policy[
            "recomputed_evaluation_sha256"
        ],
        "policy_gate_recomputed_sha256": _canonical_sha256(
            replayed_evaluation
        ),
    }


def verify_build_authorization(
    gate_dir: Path,
    bundle_path: Path,
    *,
    calibration_dir: Path,
    policy_dir: Path,
    role_manifest_path: Path,
    ledger_path: Path,
    device: str,
    batch_size: int,
    expected_native_build_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("v4 candidate replay batch size must be a positive integer")
    gate_root = gate_dir.resolve()
    bundle_path = _bounded_bundle_path(bundle_path)
    artifact_raw, artifact = _read_json_snapshot(
        gate_root / "artifact_manifest.json", field="gate manifest"
    )
    evaluation_path = gate_root / "policy_gate_evaluation.json"
    result_path = gate_root / "policy_gate_result.json"
    report_path = gate_root / "policy_gate_report.json"
    evaluation_raw, evaluation = _read_json_snapshot(
        evaluation_path, field="gate evaluation"
    )
    result_raw, result = _read_json_snapshot(result_path, field="gate result")
    report_raw, report = _read_json_snapshot(report_path, field="gate report")
    bundle_raw, bundle = _read_json_snapshot(
        bundle_path, field="v4 ensemble bundle"
    )
    snapshots = {
        "policy_gate_evaluation.json": evaluation_raw,
        "policy_gate_result.json": result_raw,
        "policy_gate_report.json": report_raw,
    }
    files = artifact.get("files")
    if not isinstance(files, dict) or set(files) != set(snapshots):
        raise ValueError("policy gate artifact file set is invalid")
    for name, raw in snapshots.items():
        contract = files.get(name)
        if (
            not isinstance(contract, dict)
            or set(contract) != {"bytes", "sha256"}
            or contract.get("bytes") != len(raw)
            or contract.get("sha256") != _sha256_bytes(raw)
        ):
            raise ValueError(f"policy gate artifact changed: {name}")
    bundle_binding = bundle_exporter.bundle_artifact_binding(
        bundle, raw=bundle_raw
    )
    if (
        bundle_binding.get("runtime_identity_sha256")
        != runtime_budget.bundle_runtime_identity_sha256(bundle)
    ):
        raise ValueError("v4 bundle runtime identity is not bound")
    selected = _verify_observed_evidence(evaluation, result)
    current_gate_code = gate_code_artifacts()
    selected_sha256 = _canonical_sha256(selected)
    result_sha256 = _sha256_bytes(result_raw)
    run_id = result.get("run_id")
    role_manifest_sha256 = result.get("role_manifest_sha256")
    gate_opponents = report.get("policy_gate_opponents")
    if (
        not isinstance(gate_opponents, list)
        or not gate_opponents
        or any(not isinstance(name, str) or not name for name in gate_opponents)
    ):
        raise ValueError("policy gate opponents are invalid")
    policy_gate_artifact_sha256 = _digest(
        result.get("policy_gate_artifact_sha256"),
        field="policy_gate_artifact_sha256",
    )
    dataset, strategy_donor, external_authority = _verify_external_authority(
        role_manifest_path=role_manifest_path,
        ledger_path=ledger_path,
        run_id=str(run_id or ""),
        candidate_sha256=_digest(
            result.get("candidate_sha256"), field="candidate_sha256"
        ),
        selection_result_sha256=_digest(
            result.get("selection_result_sha256"),
            field="selection_result_sha256",
        ),
        policy_gate_artifact_sha256=policy_gate_artifact_sha256,
        gate_opponents=gate_opponents,
    )
    runtime_context = dataset.runtime_context_contract()
    native_build_contract = current_native_build_contract()
    if (
        expected_native_build_contract is not None
        and expected_native_build_contract != native_build_contract
    ):
        raise ValueError("native build inputs changed during candidate build")
    binding_changed = any(
        document.get(field) != value
        for document in (evaluation, result, report, artifact)
        for field, value in bundle_binding.items()
    )
    if (
        binding_changed
        or any(
            document.get("native_build_contract") != native_build_contract
            for document in (evaluation, result, report, artifact)
        )
        or artifact.get("schema") != GATE_ARTIFACT_SCHEMA
        or artifact.get("run_id") != run_id
        or artifact.get("candidate_sha256") != result.get("candidate_sha256")
        or artifact.get("native_candidate_build_authorized") is not True
        or artifact.get("deployment_policy_value") is not False
        or artifact.get("strength_evidence") is not False
        or artifact.get("policy_gate_artifact_sha256")
        != policy_gate_artifact_sha256
        or artifact.get("candidate_snapshot")
        != runtime_context["candidate_snapshot"]
        or artifact.get("strategy_context_runtime_mode")
        != runtime_context["strategy_context_runtime_mode"]
        or artifact.get("code_artifacts") != current_gate_code
        or result.get("schema") != POLICY_GATE_RESULT_SCHEMA_V4
        or result.get("passed") is not True
        or result.get("errors") != []
        or result.get("native_candidate_build_authorized") is not True
        or result.get("selected_policy_sha256") != selected_sha256
        or result.get("evaluation_report_sha256")
        != _canonical_sha256(evaluation)
        or result.get("offline_estimand") != POLICY_OFFLINE_ESTIMAND_V4
        or result.get("deployment_policy_value") is not False
        or result.get("strength_evidence") is not False
        or result.get("policy_gate_artifact_sha256")
        != policy_gate_artifact_sha256
        or result.get("candidate_snapshot")
        != runtime_context["candidate_snapshot"]
        or result.get("strategy_context_runtime_mode")
        != runtime_context["strategy_context_runtime_mode"]
        or evaluation.get("schema") != GATE_EVALUATION_SCHEMA
        or evaluation.get("config") != selected
        or evaluation.get("offline_estimand") != POLICY_OFFLINE_ESTIMAND_V4
        or evaluation.get("source_collection_complete") is not True
        or evaluation.get("policy_search_performed") is not False
        or evaluation.get("deployment_policy_value") is not False
        or evaluation.get("strength_evidence") is not False
        or evaluation.get("code_artifacts") != current_gate_code
        or evaluation.get("policy_gate_artifact_sha256")
        != policy_gate_artifact_sha256
        or evaluation.get("policy_gate_opponents") != gate_opponents
        or set(evaluation.get("by_opponent") or {}) != set(gate_opponents)
        or evaluation.get("candidate_snapshot")
        != runtime_context["candidate_snapshot"]
        or evaluation.get("strategy_context_runtime_mode")
        != runtime_context["strategy_context_runtime_mode"]
        or report.get("schema") != GATE_REPORT_SCHEMA
        or report.get("run_id") != run_id
        or report.get("role_manifest_sha256") != role_manifest_sha256
        or report.get("candidate_sha256") != result.get("candidate_sha256")
        or report.get("selection_result_sha256")
        != result.get("selection_result_sha256")
        or report.get("selected_policy_sha256") != selected_sha256
        or report.get("gate_passed") is not True
        or report.get("gate_errors") != []
        or report.get("native_candidate_build_authorized") is not True
        or report.get("gate_result_sha256") != result_sha256
        or report.get("source_collection_complete") is not True
        or report.get("code_artifacts") != current_gate_code
        or report.get("deployment_policy_value") is not False
        or report.get("strength_evidence") is not False
        or report.get("policy_gate_artifact_sha256")
        != policy_gate_artifact_sha256
        or report.get("candidate_snapshot")
        != runtime_context["candidate_snapshot"]
        or report.get("strategy_context_runtime_mode")
        != runtime_context["strategy_context_runtime_mode"]
        or role_manifest_sha256 != dataset.manifest_sha256
    ):
        raise ValueError("v4 policy gate does not authorize candidate build")
    _verify_collection_boundary(report.get("collection_boundary"))

    source = bundle.get("source")
    if (
        bundle.get("schema") != BUNDLE_SCHEMA
        or bundle.get("format") != ENSEMBLE_FORMAT
        or not isinstance(source, dict)
        or source.get("run_id") != run_id
        or source.get("role_manifest_sha256") != role_manifest_sha256
        or source.get("source_collection_complete") is not True
        or source.get("source_completed_passes") != FORMAL_COLLECTION_PASSES
        or source.get("source_requested_passes") != FORMAL_COLLECTION_PASSES
        or source.get("candidate_snapshot")
        != runtime_context["candidate_snapshot"]
        or source.get("strategy_context_runtime_mode")
        != runtime_context["strategy_context_runtime_mode"]
        or source.get("policy_candidate_sha256")
        != result.get("candidate_sha256")
        or source.get("policy_result_sha256")
        != result.get("selection_result_sha256")
        or source.get("calibration_payload_sha256")
        != result.get("calibration_payload_sha256")
        or report.get("calibration_payload_sha256")
        != result.get("calibration_payload_sha256")
        or source.get("selected_policy_sha256") != selected_sha256
        or source.get("policy_selection_passed") is not True
        or source.get("deployment_policy_value") is not False
        or source.get("strength_evidence") is not False
        or bundle.get("selected_policy") != selected
        or bundle.get("deployment_policy_value") is not False
        or bundle.get("strength_evidence") is not False
    ):
        raise ValueError("v4 bundle is not bound to the passing policy gate")
    export_contract = bundle.get("export_contract")
    if (
        not isinstance(export_contract, dict)
        or export_contract.get("bundle_tool_sha256")
        != _sha256(Path(bundle_exporter.__file__).resolve())
        or export_contract.get("runtime_tool_sha256")
        != _sha256(TOOLS / "opponent_multitask_ensemble_runtime_v4.py")
    ):
        raise ValueError("v4 bundle exporter contract changed")
    projection = validate_calibration_binding(bundle.get("calibration"), source)
    if (
        projection["uncertainty_std_weight"]
        != FORMAL_UNCERTAINTY_STD_WEIGHT
        or projection["outcome_uncertainty_std_weight"]
        != FORMAL_UNCERTAINTY_STD_WEIGHT
    ):
        raise ValueError("formal v4 bundle uncertainty weights must remain 1.0")
    runtime_modules = _verify_runtime_module_artifacts(bundle, TOOLS)
    _verify_bundle_members(bundle)
    runtime = OpponentMultiTaskEnsembleRuntimeV4.load(bundle_path)
    if runtime is None or runtime.policy is None or runtime.policy != selected:
        raise ValueError("v4 bundle failed strict selected-policy loading")
    semantic_authorization = _verify_semantic_authorization(
        dataset=dataset,
        calibration_dir=calibration_dir.resolve(),
        policy_dir=policy_dir.resolve(),
        bundle_path=bundle_path,
        bundle_binding=bundle_binding,
        evaluation=evaluation,
        result=result,
        run_id=str(run_id),
        device=str(device),
        batch_size=batch_size,
    )
    # Selection and gate replay may append protected-role exposures.  Freeze the
    # ledger only after both replays so the candidate binds their final state.
    external_authority["ledger_sha256"] = _sha256(ledger_path.resolve())
    return {
        "gate_dir": str(gate_root),
        "gate_artifact_manifest_sha256": _sha256_bytes(artifact_raw),
        "gate_evaluation_sha256": _sha256_bytes(evaluation_raw),
        "gate_result_sha256": result_sha256,
        "gate_report_sha256": _sha256_bytes(report_raw),
        "candidate_sha256": _digest(
            result.get("candidate_sha256"), field="candidate_sha256"
        ),
        "selection_result_sha256": _digest(
            result.get("selection_result_sha256"),
            field="selection_result_sha256",
        ),
        "selected_policy_sha256": _digest(
            selected_sha256, field="selected_policy_sha256"
        ),
        "bundle_path": str(bundle_path),
        **bundle_binding,
        "run_id": run_id,
        "role_manifest_sha256": _digest(
            role_manifest_sha256, field="role_manifest_sha256"
        ),
        "policy_gate_opponents": gate_opponents,
        "policy_gate_artifact_sha256": policy_gate_artifact_sha256,
        "calibration_payload_sha256": result.get(
            "calibration_payload_sha256"
        ),
        "calibration_projection_sha256": calibration_projection_sha256(
            projection
        ),
        "runtime_module_artifacts": runtime_modules,
        "native_build_contract": native_build_contract,
        "semantic_authorization": semantic_authorization,
        "external_authority": external_authority,
        "strategy_donor_path": str(strategy_donor),
        "deployment_policy_value": False,
        "strength_evidence": False,
    }


def _strategy_action_v4() -> str:
    return '''    def _strategy_action(self, decision_index: int) -> int:
        req = self._request()
        self._requests.append(req)
        try:
            rule_action = int(self.get_action(req, list(self._requests)))
            state = self.reconstruct_state(req)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return 0

        advised_action = rule_action
        if self.apply_neural_advice is not None:
            try:
                advised_action = int(
                    self.apply_neural_advice(req, state, rule_action)
                )
            except Exception:
                traceback.print_exc(file=sys.stderr)
                advised_action = rule_action
        try:
            # This is the exact observed-final baseline used by the collector:
            # v140 strategy/legacy overlay followed by one sanitize_action call.
            baseline_final = int(
                self.sanitize_action(advised_action, state, req["my_chips"])
            )
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return 0

        final_action = baseline_final
        if self.v4_policy is not None:
            try:
                final_action = int(self.v4_policy.advise(
                    req, state, baseline_final, None
                ))
            except Exception:
                traceback.print_exc(file=sys.stderr)
                final_action = baseline_final
        forced = False
        if self._should_force(decision_index):
            # Preserve the donor's original force semantics without a second
            # sanitizer or raise-total reinterpretation.
            final_action = int(self._force_action)
            forced = True
        v4_decision = (
            dict(self.v4_policy.last_decision)
            if self.v4_policy is not None
            and isinstance(self.v4_policy.last_decision, dict)
            else None
        )
        try:
            self._trace_decision(
                req=req,
                state=state,
                decision_index=decision_index,
                rule_action=rule_action,
                advised_action=advised_action,
                sanitized_action=baseline_final,
                final_action=final_action,
                forced=forced,
                strategy_context=None,
                v4_decision=v4_decision,
            )
        except Exception:
            traceback.print_exc(file=sys.stderr)
        self._decision_serial += 1
        return int(final_action)

'''


def _patch_national_bot(source_text: str) -> str:
    text = v3_builder._patch_national_bot_text(source_text)
    replacements = (
        ("v3_native_policy", "v4_native_policy"),
        ("load_native_v3_policy", "load_native_v4_policy"),
        ("self.v3_policy", "self.v4_policy"),
        ("v3_decision", "v4_decision"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    text = text.replace(
        "        from strategy_trace import consume_strategy_context\n", ""
    )
    text = text.replace(
        "        self.consume_strategy_context = consume_strategy_context\n", ""
    )
    text = text.replace(
        "        from v4_native_policy import (\n"
        "            load_native_v4_policy, sanitize_stage_total\n"
        "        )\n",
        "        from v4_native_policy import load_native_v4_policy\n",
    )
    text = text.replace(
        "        self.sanitize_stage_total = sanitize_stage_total\n", ""
    )
    start = text.index("    def _strategy_action(self, decision_index: int) -> int:\n")
    end = text.index("    def _current_round_has_allin", start)
    text = text[:start] + _strategy_action_v4() + text[end:]
    if (
        "self.v3_policy" in text
        or "v3_decision" in text
        or "strategy_trace" in text
        or "consume_strategy_context" in text
        or "self.sanitize_stage_total" in text
    ):
        raise ValueError("transport donor retained a v3 policy reference")
    return text


def _copy_runtime_modules(
    target: Path, native_sources: dict[str, bytes]
) -> None:
    for name in COPIED_TOOL_MODULES:
        shutil.copy2(TOOLS / name, target / name)
    (target / "national_validator.py").write_bytes(
        native_sources["national_validator"]
    )
    (target / "opponent_response_schema.py").write_text(
        v3_builder._patched_response_schema_text(
            native_sources["opponent_response_schema"].decode("utf-8")
        ),
        encoding="utf-8",
    )


def _donor_derived_contracts(source: Path) -> dict[str, dict[str, Any]]:
    replaced = {
        "national_bot.py",
        "TRACE_VERSION.md",
        "trace_manifest.json",
        "VERSION_NOTES.md",
        *COPIED_TOOL_MODULES,
        "national_validator.py",
        "opponent_response_schema.py",
        "v4_ensemble_bundle.json",
        RUNTIME_BUDGET_FILENAME,
        "V4_BUILD_MANIFEST.json",
    }
    result = {}
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if (
            not path.is_file()
            or "__pycache__" in relative.parts
            or path.suffix == ".pyc"
            or path.name == ".completed"
            or str(relative) in replaced
        ):
            continue
        result[str(relative)] = {
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
    return result


def _verify_file_contracts(
    root: Path, contracts: dict[str, dict[str, Any]], *, field: str
) -> None:
    for name, contract in contracts.items():
        path = root / name
        if (
            not path.is_file()
            or path.stat().st_size != contract.get("bytes")
            or _sha256(path) != contract.get("sha256")
        ):
            raise ValueError(f"{field} changed: {name}")


def _candidate_artifacts(target: Path) -> dict[str, dict[str, Any]]:
    names = {
        "national_bot.py",
        "national_validator.py",
        "opponent_response_schema.py",
        "v4_ensemble_bundle.json",
        RUNTIME_BUDGET_FILENAME,
        *COPIED_TOOL_MODULES,
    }
    return {
        name: {
            "bytes": (target / name).stat().st_size,
            "sha256": _sha256(target / name),
        }
        for name in sorted(names)
    }


def _verify_stdlib_candidate(target: Path) -> None:
    local_modules = {
        path.stem for path in target.rglob("*.py") if path.is_file()
    }
    allowed = set(sys.stdlib_module_names) | local_modules | {"__future__"}
    for path in sorted(target.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = []
            if isinstance(node, ast.Import):
                imported = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported = [node.module.split(".", 1)[0]]
            unknown = sorted(set(imported) - allowed)
            if unknown:
                raise ValueError(
                    f"candidate imports non-stdlib/nonlocal modules in "
                    f"{path.relative_to(target)}: {unknown}"
                )


def _compile_candidate(target: Path) -> None:
    for path in sorted(target.rglob("*.py")):
        v3_builder.py_compile.compile(str(path), doraise=True)
    for cache in sorted(target.rglob("__pycache__"), reverse=True):
        shutil.rmtree(cache, ignore_errors=True)


def _smoke_candidate(target: Path) -> None:
    script = r'''
from national_bot import NativeNationalBot

bot = NativeNationalBot("Smoke", "upper")
assert bot.apply_neural_advice is not None
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
bot._request = lambda: dict(request)
bot.get_action = lambda req, requests: 101
bot.apply_neural_advice = lambda req, state, action: 151
bot.reconstruct_state = lambda req: dict(state)
sanitize_calls = []
def observed_sanitize(action, state, chips):
    sanitize_calls.append(action)
    return 201
bot.sanitize_action = observed_sanitize
bot._should_force = lambda index: False

class BrokenPolicy:
    last_decision = None
    def advise(self, request, state, baseline, context):
        assert baseline == 201
        assert context is None
        raise RuntimeError("expected fail-closed smoke")

bot.v4_policy = BrokenPolicy()
assert bot._strategy_action(0) == 201
assert sanitize_calls == [151]
'''
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=target,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(
            "candidate subprocess smoke failed: "
            f"{completed.stderr[-2000:]}"
        )


def _assess_final_runtime_budget(
    target: Path, authorization: dict[str, Any]
) -> dict[str, Any]:
    bundle_path = target / "v4_ensemble_bundle.json"
    preselection_sha256 = _digest(
        authorization.get("preselection_runtime_budget_payload_sha256"),
        field="preselection_runtime_budget_payload_sha256",
    )
    artifact = runtime_budget.measure_bundle_runtime_budget_subprocess(
        bundle_path,
        source_collection_complete=True,
        preselection_runtime_budget_payload_sha256=preselection_sha256,
        worker_script=target / "v4_runtime_budget.py",
    )
    validated = runtime_budget.validate_runtime_budget_artifact(
        artifact,
        bundle_bytes=authorization["bundle_bytes"],
        bundle_sha256=authorization["bundle_sha256"],
        runtime_identity_sha256=authorization["runtime_identity_sha256"],
        preselection_runtime_budget_payload_sha256=preselection_sha256,
        require_formal=True,
    )
    path = target / RUNTIME_BUDGET_FILENAME
    path.write_text(
        json.dumps(validated, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    authorization.update({
        "final_runtime_budget_payload_sha256": validated["payload_sha256"],
        "final_runtime_budget_file_sha256": _sha256(path),
    })
    return validated


def _verify_copied_authorization_inputs(
    target: Path, authorization: dict[str, Any]
) -> None:
    bundle = target / "v4_ensemble_bundle.json"
    if (
        not bundle.is_file()
        or bundle.stat().st_size != authorization.get("bundle_bytes")
        or _sha256(bundle) != authorization.get("bundle_sha256")
    ):
        raise ValueError("copied v4 bundle changed during candidate build")
    runtime_budget_path = target / RUNTIME_BUDGET_FILENAME
    runtime_budget_artifact = _load_json(
        runtime_budget_path, field="v4 final runtime budget"
    )
    runtime_budget.validate_runtime_budget_artifact(
        runtime_budget_artifact,
        bundle_bytes=authorization["bundle_bytes"],
        bundle_sha256=authorization["bundle_sha256"],
        runtime_identity_sha256=authorization["runtime_identity_sha256"],
        preselection_runtime_budget_payload_sha256=authorization[
            "preselection_runtime_budget_payload_sha256"
        ],
        require_formal=True,
    )
    if (
        runtime_budget_artifact["payload_sha256"]
        != authorization.get("final_runtime_budget_payload_sha256")
        or _sha256(runtime_budget_path)
        != authorization.get("final_runtime_budget_file_sha256")
    ):
        raise ValueError("copied v4 runtime budget authorization changed")
    evidence = target / "evidence" / "offline_policy_gate"
    expected = {
        "artifact_manifest.json": "gate_artifact_manifest_sha256",
        "policy_gate_evaluation.json": "gate_evaluation_sha256",
        "policy_gate_result.json": "gate_result_sha256",
        "policy_gate_report.json": "gate_report_sha256",
    }
    for name, authorization_field in expected.items():
        path = evidence / name
        if (
            not path.is_file()
            or _sha256(path) != authorization.get(authorization_field)
        ):
            raise ValueError(
                f"copied v4 authorization evidence changed: {name}"
            )


def _grant_owner_build_access(root: Path) -> None:
    """Make only a copied candidate tree writable/searchable by its owner."""
    root.chmod(stat.S_IMODE(root.stat().st_mode) | stat.S_IRWXU)
    for current, directories, filenames in os.walk(root):
        base = Path(current)
        for name in directories:
            path = base / name
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IRWXU)
        for name in filenames:
            path = base / name
            if not path.is_symlink():
                path.chmod(
                    stat.S_IMODE(path.stat().st_mode)
                    | stat.S_IRUSR
                    | stat.S_IWUSR
                )


def _remove_candidate_tree(root: Path) -> None:
    if not root.exists():
        return
    _grant_owner_build_access(root)
    shutil.rmtree(root)


def build_candidate(
    *,
    bundle_path: Path,
    gate_dir: Path,
    calibration_dir: Path,
    policy_dir: Path,
    role_manifest_path: Path,
    ledger_path: Path,
    device: str,
    batch_size: int,
    output: Path,
) -> dict[str, Any]:
    output = output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    bundle_path = _bounded_bundle_path(bundle_path)
    _manifest_raw, manifest_preview = _read_json_snapshot(
        role_manifest_path, field="role manifest"
    )
    strategy_donor = Path(str(
        (manifest_preview.get("candidate_snapshot") or {}).get("path") or ""
    )).resolve()
    strategy_contract = _verify_donor(
        strategy_donor,
        expected_sha256=EXPECTED_STRATEGY_DONOR_SHA256,
        critical_files=EXPECTED_STRATEGY_FILES,
        field="strategy donor",
    )
    native_build_contract, native_sources = snapshot_native_build_inputs()
    transport_donor = DEFAULT_TRANSPORT_DONOR.resolve()
    transport_snapshot = native_build_contract["transport"]
    if (
        transport_donor.name != transport_snapshot.get("name")
        or transport_snapshot.get("sha256")
        != EXPECTED_TRANSPORT_DONOR_SHA256
        or transport_snapshot.get("national_bot.py")
        != {
            "bytes": len(native_sources["transport_national_bot"]),
            "sha256": EXPECTED_TRANSPORT_FILES["national_bot.py"],
        }
    ):
        raise ValueError("fixed transport donor contract changed")
    transport_contract = {
        "path": str(transport_donor),
        "sha256": transport_snapshot["sha256"],
        "critical_files": {
            "national_bot.py": transport_snapshot["national_bot.py"]
        },
    }
    donor_derived = _donor_derived_contracts(strategy_donor)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    _remove_candidate_tree(temporary)
    try:
        v3_builder._copy_tree(strategy_donor, temporary)
        _grant_owner_build_access(temporary)
        for stale in ("TRACE_VERSION.md", "trace_manifest.json", "VERSION_NOTES.md"):
            (temporary / stale).unlink(missing_ok=True)
        (temporary / "national_bot.py").write_text(
            _patch_national_bot(
                native_sources["transport_national_bot"].decode("utf-8")
            ),
            encoding="utf-8",
        )
        _copy_runtime_modules(temporary, native_sources)
        bundle_raw = bundle_path.resolve().read_bytes()
        (temporary / "v4_ensemble_bundle.json").write_bytes(bundle_raw)
        evidence = temporary / "evidence" / "offline_policy_gate"
        evidence.mkdir(parents=True)
        for name in (
            "artifact_manifest.json",
            "policy_gate_evaluation.json",
            "policy_gate_result.json",
            "policy_gate_report.json",
        ):
            (evidence / name).write_bytes(
                (gate_dir.resolve() / name).read_bytes()
            )
        authorization = verify_build_authorization(
            evidence,
            temporary / "v4_ensemble_bundle.json",
            calibration_dir=calibration_dir,
            policy_dir=policy_dir,
            role_manifest_path=role_manifest_path,
            ledger_path=ledger_path,
            device=device,
            batch_size=batch_size,
            expected_native_build_contract=native_build_contract,
        )
        if Path(authorization["strategy_donor_path"]) != strategy_donor:
            raise ValueError("role authority changed the strategy donor")
        bundle = _load_json(
            temporary / "v4_ensemble_bundle.json", field="v4 ensemble bundle"
        )
        _verify_runtime_module_artifacts(bundle, temporary)
        _verify_file_contracts(
            temporary, donor_derived, field="strategy donor derived file"
        )
        _verify_stdlib_candidate(temporary)
        _compile_candidate(temporary)
        runtime = OpponentMultiTaskEnsembleRuntimeV4.load(
            temporary / "v4_ensemble_bundle.json"
        )
        if runtime is None or runtime.policy is None:
            raise ValueError("copied v4 candidate bundle failed strict loading")
        _assess_final_runtime_budget(temporary, authorization)
        _smoke_candidate(temporary)
        _verify_copied_authorization_inputs(temporary, authorization)
        candidate_artifacts = _candidate_artifacts(temporary)
        manifest = {
            "schema": BUILD_SCHEMA,
            "candidate": output.name,
            "strategy_donor": strategy_contract,
            "transport_donor": transport_contract,
            "strategy_donor_derived_files": donor_derived,
            "candidate_artifacts": candidate_artifacts,
            "native_build_contract": authorization["native_build_contract"],
            "authorization": authorization,
            "runtime_contract": {
                "entry": "national_bot.py",
                "native_tcp": True,
                "adapter": False,
                "official_action_delay_default_sec": 0.30,
                "stream_numeric_coalescing": True,
                "sanitized_rule_fallback": True,
                "observed_final_baseline": True,
                "baseline_sanitize_calls": 1,
                "strategy_context_runtime_mode": (
                    "zero_vector_training_aligned_v1"
                ),
                "win_first_shared_selector": True,
                "stdlib_only": True,
                "ablation_env": [
                    "POK_V4_DISABLE",
                    "POK_V4_DISABLE_CROSS_HAND",
                ],
            },
            "deployment_policy_value": False,
            "strength_evidence": False,
            "native_strength_evidence": False,
            "official_exe_accepted": False,
            "deployment_eligible": False,
        }
        (temporary / "V4_BUILD_MANIFEST.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (temporary / "VERSION_NOTES.md").write_text(
            "# Native opponent-aware v4 candidate\n\n"
            "This development candidate passed protected offline selection and "
            "policy-gate checks, but those checks are not deployed-policy value or "
            "strength evidence. Fresh native TCP and official-platform gates remain "
            "required. See `V4_BUILD_MANIFEST.json`.\n",
            encoding="utf-8",
        )
        if (
            _sha256(role_manifest_path.resolve())
            != authorization["external_authority"]["role_manifest_sha256"]
            or _sha256(ledger_path.resolve())
            != authorization["external_authority"]["ledger_sha256"]
        ):
            raise ValueError("external authorization changed during build")
        temporary.replace(output)
    except Exception:
        _remove_candidate_tree(temporary)
        raise
    return manifest


def _formal_output(path: Path) -> Path:
    output = path.resolve()
    if output.parent != VERSIONS.resolve() or not VERSION_RE.fullmatch(output.name):
        raise ValueError(
            f"formal output must be a new vNNN_<description> directory under {VERSIONS}"
        )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--policy-gate-dir", type=Path, required=True)
    parser.add_argument("--calibration-dir", type=Path, required=True)
    parser.add_argument("--policy-dir", type=Path, required=True)
    parser.add_argument("--role-manifest", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = build_candidate(
            bundle_path=args.bundle,
            gate_dir=args.policy_gate_dir,
            calibration_dir=args.calibration_dir,
            policy_dir=args.policy_dir,
            role_manifest_path=args.role_manifest,
            ledger_path=args.ledger,
            device=str(args.device),
            batch_size=args.batch_size,
            output=_formal_output(args.output),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
