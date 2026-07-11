#!/usr/bin/env python3
"""Calibrate one protected multi-seed v4 ensemble on model_calibration."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import sys
from typing import Any

import torch

import calibrate_match_outcome_v4 as outcome_fit
import calibrate_opponent_multitask_v3_ensemble as v3_calibration
from feature_spec import LABELS
from match_outcome_calibration import (
    calibration_payload_sha256,
    validate_calibration_artifact,
)
from multitask_calibration import (
    build_calibration_artifact,
    calibrate_response_temperature,
    calibrate_value_lower_offsets,
)
from multitask_training_data import (
    FROZEN_CHECKPOINT_SCHEMA,
    MODEL_CALIBRATION_ROLE,
    MODEL_TRAINING_ROLES,
    VALUE_FIELDS,
    prepare_model_calibration,
    prepare_training_phase,
)
from opponent_multitask_model_v3 import QUANTILE_LEVELS
from opponent_multitask_model_v4 import MODEL_FORMAT
from role_dataset_access import RoleDatasetAccess
from run_opponent_multitask_v4_scaling import (
    SELECTION_METHOD,
    SELECTION_KEY_ORDER,
    SUMMARY_SCHEMA,
    _finite_selection_key,
    _is_cuda_device,
    summarize_runs,
)
from train_opponent_multitask_v3 import checkpoint_authorization
from train_opponent_multitask_v4 import (
    ARTIFACT_MANIFEST_SCHEMA as TRAINING_ARTIFACT_SCHEMA,
    CHECKPOINT_SCHEMA as TRAINING_CHECKPOINT_SCHEMA,
    REPORT_SCHEMA as TRAINING_REPORT_SCHEMA,
    _code_artifacts as current_training_code_artifacts,
    load_checkpoint,
)
from win_first_policy_v4 import OUTCOME_AGGREGATION_METHOD


ENSEMBLE_MANIFEST_SCHEMA = "opponent_multitask_v4_ensemble_checkpoint_v1"
ENSEMBLE_CALIBRATION_SCHEMA = "opponent_multitask_v4_ensemble_calibration_v1"
CALIBRATION_REPORT_SCHEMA = "opponent_multitask_v4_ensemble_calibration_report_v1"
ARTIFACT_MANIFEST_SCHEMA = "opponent_multitask_v4_ensemble_artifacts_v1"
FORMAL_GRID_VERIFICATION_SCHEMA = "opponent_multitask_v4_formal_grid_verification_v1"
EXPECTED_TRAINING_FILES = {
    "checkpoint.pt",
    "checkpoint_authorization.json",
    "training_report.json",
}
EXPECTED_CALIBRATION_FILES = {
    "ensemble_checkpoint_manifest.json",
    "checkpoint_authorization.json",
    "calibration.json",
    "calibration_report.json",
}
FORMAL_COLLECTION_PASSES = 160


def _scaling_tool_path() -> Path:
    return Path(sys.modules["run_opponent_multitask_v4_scaling"].__file__).resolve()


def _current_training_code_artifacts() -> dict[str, dict[str, Any]]:
    return current_training_code_artifacts()

def _verify_current_code_artifacts(report: dict[str, Any], checkpoint: dict[str, Any]) -> dict[str, dict[str, Any]]:
    current = _current_training_code_artifacts()
    if (
        report.get("code_artifacts") != current
        or checkpoint.get("code_artifacts") != current
    ):
        raise ValueError("v4 member training code artifacts changed")
    return current


def require_formal_collection_boundary(
    dataset: RoleDatasetAccess, *, formal: bool
) -> None:
    if formal:
        dataset.require_collection_boundary(
            expected_passes=FORMAL_COLLECTION_PASSES
        )


def require_formal_uncertainty_contract(
    uncertainty_std_weight: float,
    outcome_uncertainty_std_weight: float,
    *,
    formal: bool,
) -> None:
    if formal and (
        uncertainty_std_weight != 1.0
        or outcome_uncertainty_std_weight != 1.0
    ):
        raise ValueError(
            "formal v4 ensemble requires both uncertainty std weights to be 1.0"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _digest(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return normalized


def _finite(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _positive(value: Any, *, field: str) -> float:
    number = _finite(value, field=field)
    if number <= 0.0:
        raise ValueError(f"{field} must be positive")
    return number


def _load_json(path: Path, *, field: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {field}: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{field} must be a JSON object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _verify_file_contracts(
    root: Path,
    artifact: dict[str, Any],
    *,
    expected: set[str],
    field: str,
) -> None:
    files = artifact.get("files")
    if not isinstance(files, dict) or set(files) != expected:
        raise ValueError(f"{field} has an incomplete file contract")
    for name, contract in files.items():
        path = root / name
        if (
            not isinstance(contract, dict)
            or not path.is_file()
            or path.stat().st_size != int(contract.get("bytes", -1))
            or _sha256(path) != contract.get("sha256")
        ):
            raise ValueError(f"{field} changed: {path}")


def selected_scaling_runs(
    summary: dict[str, Any], *, allow_incomplete_smoke: bool
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if (
        summary.get("schema") != SUMMARY_SCHEMA
        or summary.get("model_calibration_opened") is not False
        or summary.get("policy_roles_opened") is not False
        or summary.get("deployment_policy_value") is not False
        or summary.get("strength_evidence") is not False
        or summary.get("native_tcp_evaluated") is not False
        or summary.get("selection_key_order") != list(SELECTION_KEY_ORDER)
        or summary.get("selection_method") != SELECTION_METHOD
        or summary.get("scaling_tool_sha256") != _sha256(_scaling_tool_path())
    ):
        raise ValueError("invalid v4 scaling summary")
    if allow_incomplete_smoke:
        selected = summary.get("provisional_best_configuration")
    else:
        if summary.get("selection_eligible") is not True:
            raise ValueError("v4 scaling summary is not eligible for formal calibration")
        selected = summary.get("selected_configuration")
    if not isinstance(selected, dict):
        raise ValueError("v4 scaling summary has no selected configuration")
    scale = str(selected.get("scale", ""))
    encoder = str(selected.get("encoder", ""))
    expected_seeds = sorted(int(seed) for seed in selected.get("requested_seeds", []))
    runs = [
        dict(row)
        for row in summary.get("runs", [])
        if isinstance(row, dict)
        and row.get("completed") is True
        and row.get("scale") == scale
        and row.get("encoder") == encoder
    ]
    observed_seeds = sorted(int(row["seed"]) for row in runs)
    if not expected_seeds or observed_seeds != expected_seeds:
        raise ValueError("selected v4 runs do not cover every requested seed")
    if len(set(observed_seeds)) != len(observed_seeds):
        raise ValueError("selected v4 runs reuse a seed")
    checkpoints = [str(row.get("checkpoint_sha256", "")) for row in runs]
    if len(set(checkpoints)) != len(checkpoints):
        raise ValueError("selected v4 runs reuse a checkpoint")
    if not allow_incomplete_smoke:
        if len(runs) < 3:
            raise ValueError("formal v4 ensemble calibration requires at least three seeds")
        requested = summary.get("requested") or {}
        configurations = summary.get("configurations")
        requested_scales = list(requested.get("scales") or [])
        requested_encoders = list(requested.get("encoders") or [])
        requested_seeds = sorted(int(seed) for seed in requested.get("seeds") or [])
        expected_pairs = {
            (scale, encoder)
            for scale in requested_scales
            for encoder in requested_encoders
        }
        observed_pairs = {
            (str(row.get("scale")), str(row.get("encoder")))
            for row in configurations or []
            if isinstance(row, dict)
        }
        all_runs = summary.get("runs")
        expected_jobs = {
            (scale, encoder, seed)
            for scale, encoder in expected_pairs
            for seed in requested_seeds
        }
        observed_jobs = {
            (
                str(row.get("scale")),
                str(row.get("encoder")),
                int(row.get("seed", -1)),
            )
            for row in all_runs or []
            if isinstance(row, dict)
        }
        if (
            len(set(requested_scales)) < 2
            or len(requested_scales) != len(set(requested_scales))
            or len(set(requested_encoders)) < 2
            or len(requested_encoders) != len(set(requested_encoders))
            or len(set(requested_seeds)) < 3
            or len(requested_seeds) != len(requested.get("seeds") or [])
            or not _is_cuda_device(requested.get("device"))
            or int(requested.get("configurations", 0) or 0)
            != len(expected_pairs)
            or not isinstance(configurations, list)
            or observed_pairs != expected_pairs
            or any(row.get("all_seeds_completed") is not True for row in configurations)
            or any(
                sorted(int(seed) for seed in row.get("requested_seeds", []))
                != requested_seeds
                for row in configurations
            )
            or not any(row == selected for row in configurations)
            or not isinstance(all_runs, list)
            or observed_jobs != expected_jobs
            or len(all_runs) != len(expected_jobs)
            or any(
                row.get("completed") is not True
                or row.get("source_collection_complete") is not True
                or row.get("source_completed_passes") != FORMAL_COLLECTION_PASSES
                or row.get("source_requested_passes") != FORMAL_COLLECTION_PASSES
                or not _is_cuda_device(row.get("training_device"))
                for row in all_runs
            )
        ):
            raise ValueError(
                "formal v4 calibration requires complete CUDA runs across "
                "two scales, two encoders, and three seeds"
            )
        if summary.get("source_collection_complete") is not True:
            raise ValueError("formal v4 scaling summary used incomplete source data")
    return selected, sorted(runs, key=lambda row: int(row["seed"]))


def _verified_member(
    row: dict[str, Any],
    *,
    role_manifest_sha256: str,
    training_artifact_sha256: dict[str, str],
    device: torch.device | str,
    retain_model: bool = True,
) -> dict[str, Any]:
    root = Path(str(row.get("output_dir", ""))).resolve()
    artifact = _load_json(root / "artifact_manifest.json", field="training artifact")
    if artifact.get("schema") != TRAINING_ARTIFACT_SCHEMA:
        raise ValueError("v4 member has the wrong training artifact schema")
    _verify_file_contracts(
        root,
        artifact,
        expected=EXPECTED_TRAINING_FILES,
        field="v4 member artifact",
    )
    report = _load_json(root / "training_report.json", field="training report")
    authorization = _load_json(
        root / "checkpoint_authorization.json", field="checkpoint authorization"
    )
    checkpoint_path = root / "checkpoint.pt"
    checkpoint_sha256 = _sha256(checkpoint_path)
    config = report.get("config") or {}
    metadata = report.get("model") or {}
    early = report.get("early_stop") or {}
    environment = report.get("environment") or {}
    selection_key = _finite_selection_key(early.get("selection_key"))
    try:
        parameters = int(metadata.get("parameters", -1))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("v4 member has an invalid parameter count") from exc
    if (
        report.get("schema") != TRAINING_REPORT_SCHEMA
        or row.get("completed") is not True
        or artifact.get("run_id") != report.get("run_id")
        or row.get("run_id") != report.get("run_id")
        or report.get("opened_roles") != list(MODEL_TRAINING_ROLES)
        or report.get("model_calibration_opened") is not False
        or report.get("policy_roles_opened") is not False
        or report.get("deployment_policy_value") is not False
        or report.get("strength_evidence") is not False
        or report.get("native_tcp_evaluated") is not False
        or report.get("role_manifest_sha256") != role_manifest_sha256
        or report.get("checkpoint_sha256") != checkpoint_sha256
        or row.get("checkpoint_sha256") != checkpoint_sha256
        or row.get("role_manifest_sha256") != role_manifest_sha256
        or metadata.get("format") != MODEL_FORMAT
        or metadata.get("scale") != row.get("scale")
        or metadata.get("cross_encoder") != row.get("encoder")
        or config.get("scale") != row.get("scale")
        or config.get("cross_encoder") != row.get("encoder")
        or int(config.get("seed", -1)) != int(row["seed"])
        or parameters < 1
        or parameters != row.get("parameters")
        or environment.get("device") != row.get("training_device")
        or row.get("selection_key_order") != list(SELECTION_KEY_ORDER)
        or early.get("selection_key_order") != list(SELECTION_KEY_ORDER)
        or early.get("selection_key_is_lexicographic") is not True
        or early.get("selection_score_is_strength_evidence") is not False
        or selection_key != row.get("selection_key")
        or report.get("source_completed_passes")
        != row.get("source_completed_passes")
        or report.get("source_requested_passes")
        != row.get("source_requested_passes")
        or report.get("source_collection_complete")
        is not row.get("source_collection_complete")
        or report.get("incomplete_smoke") is not row.get("incomplete_smoke")
        or report.get("incomplete_smoke")
        is report.get("source_collection_complete")
        or artifact.get("source_collection_complete")
        is not report.get("source_collection_complete")
        or artifact.get("deployment_policy_value") is not False
        or artifact.get("strength_evidence") is not False
    ):
        raise ValueError("v4 member training report is not selection-safe")
    if (
        authorization.get("schema") != FROZEN_CHECKPOINT_SCHEMA
        or authorization.get("frozen") is not True
        or authorization.get("early_stop_complete") is not True
        or authorization.get("run_id") != report.get("run_id")
        or authorization.get("role_manifest_sha256") != role_manifest_sha256
        or authorization.get("training_roles") != list(MODEL_TRAINING_ROLES)
        or authorization.get("training_artifact_sha256")
        != training_artifact_sha256
        or authorization.get("checkpoint_sha256") != checkpoint_sha256
    ):
        raise ValueError("v4 member checkpoint authorization does not match")
    model, checkpoint = load_checkpoint(checkpoint_path, device=device)
    if (
        checkpoint.get("schema") != TRAINING_CHECKPOINT_SCHEMA
        or checkpoint.get("role_manifest_sha256") != role_manifest_sha256
        or checkpoint.get("training_artifact_sha256")
        != training_artifact_sha256
        or checkpoint.get("model_metadata") != metadata
        or checkpoint.get("training_config") != config
        or checkpoint.get("code_artifacts") != report.get("code_artifacts")
        or checkpoint.get("source_completed_passes")
        != report.get("source_completed_passes")
        or checkpoint.get("source_requested_passes")
        != report.get("source_requested_passes")
        or checkpoint.get("source_collection_complete")
        is not report.get("source_collection_complete")
    ):
        raise ValueError("v4 member checkpoint metadata changed")
    current_code_artifacts = _verify_current_code_artifacts(report, checkpoint)
    verified = {
        "seed": int(row["seed"]),
        "run_id": str(report["run_id"]),
        "output_dir": str(root),
        "checkpoint_path": str(checkpoint_path),
        "checkpoint_sha256": checkpoint_sha256,
        "checkpoint_authorization_sha256": _sha256(
            root / "checkpoint_authorization.json"
        ),
        "training_report_sha256": _sha256(root / "training_report.json"),
        "training_artifact_manifest_sha256": _sha256(
            root / "artifact_manifest.json"
        ),
        "scale": str(metadata["scale"]),
        "encoder": str(metadata["cross_encoder"]),
        "parameters": parameters,
        "role_manifest_sha256": role_manifest_sha256,
        "selection_key_order": list(SELECTION_KEY_ORDER),
        "selection_key": selection_key,
        "early_stop_selection_key": selection_key,
        "model_metadata": metadata,
        "training_config": config,
        "training_artifact_sha256": checkpoint["training_artifact_sha256"],
        "code_artifacts": current_code_artifacts,
        "source_completed_passes": report.get("source_completed_passes"),
        "source_requested_passes": report.get("source_requested_passes"),
        "source_collection_complete": report["source_collection_complete"],
        "incomplete_smoke": report["incomplete_smoke"],
        "training_device": str(environment.get("device")),
    }
    if retain_model:
        verified["model"] = model
    else:
        del model
    return verified


def verify_members(
    rows: list[dict[str, Any]],
    *,
    role_manifest_sha256: str,
    training_artifact_sha256: dict[str, str],
    device: torch.device | str,
    formal: bool,
    retain_models: bool = True,
) -> list[dict[str, Any]]:
    if not rows:
        raise ValueError("v4 ensemble has no members")
    members = [
        _verified_member(
            row,
            role_manifest_sha256=role_manifest_sha256,
            training_artifact_sha256=training_artifact_sha256,
            device=device,
            retain_model=retain_models,
        )
        for row in rows
    ]
    if len({member["seed"] for member in members}) != len(members):
        raise ValueError("v4 ensemble reuses a seed")
    if len({member["checkpoint_sha256"] for member in members}) != len(members):
        raise ValueError("v4 ensemble reuses a checkpoint")
    first = members[0]
    reference_config = {
        key: value
        for key, value in first["training_config"].items()
        if key != "seed"
    }
    for member in members[1:]:
        config = {
            key: value
            for key, value in member["training_config"].items()
            if key != "seed"
        }
        if (
            config != reference_config
            or member["training_artifact_sha256"]
            != first["training_artifact_sha256"]
            or member["code_artifacts"] != first["code_artifacts"]
            or member["model_metadata"] != first["model_metadata"]
        ):
            raise ValueError("v4 ensemble members do not share one training contract")
    if formal:
        if len(members) < 3:
            raise ValueError("formal v4 ensemble requires at least three members")
        if any(not _is_cuda_device(member["training_device"]) for member in members):
            raise ValueError("formal v4 ensemble members must be trained on CUDA")
        if any(
            member["source_collection_complete"] is not True
            for member in members
        ):
            raise ValueError("formal v4 ensemble member used incomplete source data")
    return members


def _formal_requested_matrix(
    summary: dict[str, Any],
) -> tuple[list[str], list[str], list[int], str]:
    requested = summary.get("requested")
    if not isinstance(requested, dict):
        raise ValueError("formal v4 scaling summary has no requested matrix")
    scales = requested.get("scales")
    encoders = requested.get("encoders")
    raw_seeds = requested.get("seeds")
    if (
        not isinstance(scales, list)
        or not all(isinstance(value, str) and value for value in scales)
        or not isinstance(encoders, list)
        or not all(isinstance(value, str) and value for value in encoders)
        or not isinstance(raw_seeds, list)
    ):
        raise ValueError("formal v4 scaling requested matrix is invalid")
    try:
        seeds = [int(seed) for seed in raw_seeds]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("formal v4 scaling requested seeds are invalid") from exc
    device = str(requested.get("device", ""))
    if (
        len(scales) < 2
        or len(scales) != len(set(scales))
        or len(encoders) < 2
        or len(encoders) != len(set(encoders))
        or len(seeds) < 3
        or len(seeds) != len(set(seeds))
        or any(seed < 0 for seed in seeds)
        or not _is_cuda_device(device)
        or requested.get("configurations") != len(scales) * len(encoders)
    ):
        raise ValueError("formal v4 scaling requested matrix is incomplete")
    return list(scales), list(encoders), sorted(seeds), device


def _formal_verified_row(member: dict[str, Any]) -> dict[str, Any]:
    return {
        "scale": member["scale"],
        "encoder": member["encoder"],
        "seed": member["seed"],
        "run_id": member["run_id"],
        "output_dir": member["output_dir"],
        "completed": True,
        "selection_key": member["early_stop_selection_key"],
        "selection_key_order": list(SELECTION_KEY_ORDER),
        "parameters": member["parameters"],
        "checkpoint_sha256": member["checkpoint_sha256"],
        "role_manifest_sha256": member["role_manifest_sha256"],
        "source_collection_complete": member["source_collection_complete"],
        "source_completed_passes": member["source_completed_passes"],
        "source_requested_passes": member["source_requested_passes"],
        "incomplete_smoke": member["incomplete_smoke"],
        "training_device": member["training_device"],
    }


def verify_formal_scaling_artifacts(
    summary: dict[str, Any],
    *,
    role_manifest_sha256: str,
    training_artifact_sha256: dict[str, str],
) -> dict[str, Any]:
    """Verify every real sweep artifact on CPU and recompute the formal winner."""
    scales, encoders, seeds, requested_device = _formal_requested_matrix(summary)
    raw_rows = summary.get("runs")
    if not isinstance(raw_rows, list):
        raise ValueError("formal v4 scaling summary has no runs")
    expected_jobs = {
        (scale, encoder, seed)
        for scale in scales
        for encoder in encoders
        for seed in seeds
    }
    observed_jobs: set[tuple[str, str, int]] = set()
    verified_members: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise ValueError("formal v4 scaling run is not an object")
        try:
            job = (str(raw["scale"]), str(raw["encoder"]), int(raw["seed"]))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("formal v4 scaling run identity is invalid") from exc
        if job not in expected_jobs or job in observed_jobs:
            raise ValueError("formal v4 scaling jobs do not match the requested matrix")
        observed_jobs.add(job)
        verified_members.append(_verified_member(
            raw,
            role_manifest_sha256=role_manifest_sha256,
            training_artifact_sha256=training_artifact_sha256,
            device="cpu",
            retain_model=False,
        ))
    if observed_jobs != expected_jobs or len(raw_rows) != len(expected_jobs):
        raise ValueError("formal v4 scaling jobs do not cover the requested matrix")
    if (
        len({member["checkpoint_sha256"] for member in verified_members})
        != len(verified_members)
        or len({member["run_id"] for member in verified_members})
        != len(verified_members)
        or len({member["output_dir"] for member in verified_members})
        != len(verified_members)
    ):
        raise ValueError("formal v4 scaling reuses a training artifact")
    reference_base_config: dict[str, Any] | None = None
    group_contracts: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    for member in verified_members:
        if (
            member["training_device"] != requested_device
            or not _is_cuda_device(member["training_device"])
            or member["source_collection_complete"] is not True
            or member["source_completed_passes"] != FORMAL_COLLECTION_PASSES
            or member["source_requested_passes"] != FORMAL_COLLECTION_PASSES
            or member["incomplete_smoke"] is not False
        ):
            raise ValueError("formal v4 scaling artifact is not a complete CUDA run")
        config = dict(member["training_config"])
        base_config = {
            key: value
            for key, value in config.items()
            if key not in {"seed", "scale", "cross_encoder"}
        }
        if reference_base_config is None:
            reference_base_config = base_config
        elif base_config != reference_base_config:
            raise ValueError("formal v4 scaling jobs use different training contracts")
        group = (member["scale"], member["encoder"])
        group_contract = (member["model_metadata"], {
            key: value for key, value in config.items() if key != "seed"
        })
        previous = group_contracts.setdefault(group, group_contract)
        if previous != group_contract:
            raise ValueError("formal v4 scaling seeds do not reproduce one architecture")
    verified_rows = sorted(
        (_formal_verified_row(member) for member in verified_members),
        key=lambda row: (row["scale"], row["encoder"], row["seed"]),
    )
    recomputed_configurations, recomputed_selected = summarize_runs(
        verified_rows, required_seeds=seeds
    )
    if (
        recomputed_selected is None
        or summary.get("configurations") != recomputed_configurations
        or summary.get("selected_configuration") != recomputed_selected
        or summary.get("provisional_best_configuration") != recomputed_selected
    ):
        raise ValueError("formal v4 scaling summary does not match verified artifacts")
    unsigned = {
        "schema": FORMAL_GRID_VERIFICATION_SCHEMA,
        "role_manifest_sha256": role_manifest_sha256,
        "training_artifact_sha256": training_artifact_sha256,
        "training_code_artifacts": _current_training_code_artifacts(),
        "requested": summary["requested"],
        "selection_key_order": list(SELECTION_KEY_ORDER),
        "selection_method": SELECTION_METHOD,
        "scaling_tool_sha256": _sha256(_scaling_tool_path()),
        "verified_runs": verified_rows,
        "configurations": recomputed_configurations,
        "selected_configuration": recomputed_selected,
        "all_models_loaded_on_cpu_without_retention": True,
        "source_collection_complete": True,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    return {**unsigned, "payload_sha256": _canonical_sha256(unsigned)}


def validate_formal_grid_verification(
    raw: Any,
    *,
    role_manifest_sha256: str,
    training_artifact_sha256: dict[str, str],
    selected_configuration: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("formal v4 ensemble has no grid verification")
    proof = dict(raw)
    payload_sha256 = str(proof.pop("payload_sha256", ""))
    scales, encoders, seeds, requested_device = _formal_requested_matrix(proof)
    rows = proof.get("verified_runs")
    expected_jobs = {
        (scale, encoder, seed)
        for scale in scales
        for encoder in encoders
        for seed in seeds
    }
    if not isinstance(rows, list):
        raise ValueError("formal v4 grid verification has no verified runs")
    try:
        observed_jobs = {
            (str(row["scale"]), str(row["encoder"]), int(row["seed"]))
            for row in rows
        }
        checkpoints = [str(row["checkpoint_sha256"]) for row in rows]
        normalized_keys = [_finite_selection_key(row["selection_key"]) for row in rows]
        recomputed_configurations, recomputed_selected = summarize_runs(
            rows, required_seeds=seeds
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("formal v4 grid verification run is invalid") from exc
    if (
        proof.get("schema") != FORMAL_GRID_VERIFICATION_SCHEMA
        or payload_sha256 != _canonical_sha256(proof)
        or proof.get("role_manifest_sha256") != role_manifest_sha256
        or proof.get("training_artifact_sha256") != training_artifact_sha256
        or proof.get("training_code_artifacts")
        != _current_training_code_artifacts()
        or proof.get("selection_key_order") != list(SELECTION_KEY_ORDER)
        or proof.get("selection_method") != SELECTION_METHOD
        or proof.get("scaling_tool_sha256") != _sha256(_scaling_tool_path())
        or proof.get("all_models_loaded_on_cpu_without_retention") is not True
        or proof.get("source_collection_complete") is not True
        or proof.get("deployment_policy_value") is not False
        or proof.get("strength_evidence") is not False
        or len(rows) != len(expected_jobs)
        or observed_jobs != expected_jobs
        or len(checkpoints) != len(set(checkpoints))
        or len(normalized_keys) != len(rows)
        or any(
            row.get("completed") is not True
            or row.get("selection_key_order") != list(SELECTION_KEY_ORDER)
            or row.get("role_manifest_sha256") != role_manifest_sha256
            or row.get("training_device") != requested_device
            or not _is_cuda_device(row.get("training_device"))
            or row.get("source_collection_complete") is not True
            or row.get("source_completed_passes") != FORMAL_COLLECTION_PASSES
            or row.get("source_requested_passes") != FORMAL_COLLECTION_PASSES
            or row.get("incomplete_smoke") is not False
            or not isinstance(row.get("parameters"), int)
            or isinstance(row.get("parameters"), bool)
            or int(row["parameters"]) < 1
            for row in rows
        )
        or proof.get("configurations") != recomputed_configurations
        or proof.get("selected_configuration") != recomputed_selected
        or selected_configuration != recomputed_selected
    ):
        raise ValueError("formal v4 grid verification binding changed")
    return raw


def prepare_ensemble_calibration_phase(
    dataset: RoleDatasetAccess,
    members: list[dict[str, Any]],
    *,
    ensemble_manifest_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Open train/early and exactly one model_calibration role for this chain."""
    expected = {
        role: dataset._role_artifact_sha256(role)
        for role in MODEL_TRAINING_ROLES
    }
    if not members or any(
        member.get("training_artifact_sha256") != expected for member in members
    ):
        raise ValueError("v4 members are not bound to this training role manifest")
    training_phase = prepare_training_phase(dataset)
    authorization = checkpoint_authorization(
        dataset,
        training_phase,
        checkpoint_sha256=_digest(
            ensemble_manifest_sha256, field="ensemble_manifest_sha256"
        ),
    )
    calibration_phase = prepare_model_calibration(
        dataset, training_phase, authorization
    )
    if calibration_phase.get("opened_roles") != [MODEL_CALIBRATION_ROLE]:
        raise RuntimeError("v4 ensemble opened an unexpected calibration role")
    return training_phase, authorization, calibration_phase


def _member_outcome_calibration(
    member: dict[str, Any],
    observations: dict[str, Any],
    role: dict[str, Any],
    *,
    run_id: str,
    role_manifest_sha256: str,
    source_collection_complete: bool,
    steps: int,
    learning_rate: float,
    l2: float,
) -> dict[str, Any]:
    fitted = outcome_fit.fit_probability_calibration(
        observations["logits"],
        observations["targets"],
        observations["weights"],
        steps=steps,
        learning_rate=learning_rate,
        l2=l2,
    )
    before = outcome_fit.probability_metrics(
        observations["logits"],
        observations["targets"],
        observations["weights"],
        scale=1.0,
        bias=0.0,
    )
    after = outcome_fit.probability_metrics(
        observations["logits"],
        observations["targets"],
        observations["weights"],
        scale=float(fitted["scale"]),
        bias=float(fitted["bias"]),
    )
    action_counts = {
        label: int((observations["action_ids"] == index).sum().item())
        for index, label in enumerate(LABELS)
    }
    fitted.update({
        "run_id": run_id,
        "member_seed": int(member["seed"]),
        "model_format": MODEL_FORMAT,
        "checkpoint_sha256": member["checkpoint_sha256"],
        "role_manifest_sha256": role_manifest_sha256,
        "calibration_role": MODEL_CALIBRATION_ROLE,
        "model_calibration_artifact_sha256": role["provenance"][
            "artifact_sha256"
        ],
        "model_calibration_opponents": list(role["opponents"]),
        "source_collection_complete": bool(source_collection_complete),
        "metrics": {"before": before, "after": after},
        "action_observations": action_counts,
        "policy_evidence_used": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
    })
    fitted["payload_sha256"] = calibration_payload_sha256(fitted)
    return fitted


def _ensemble_calibration_artifact(
    base: dict[str, Any],
    *,
    ensemble_manifest_sha256: str,
    members: list[dict[str, Any]],
    outcome_calibrations: list[dict[str, Any]],
    lower_quantile: float,
    uncertainty_std_weight: float,
    outcome_uncertainty_std_weight: float,
    diagnostics: dict[str, Any],
    source_collection_complete: bool,
) -> dict[str, Any]:
    payload = dict(base)
    payload.pop("payload_sha256", None)
    payload.update({
        "schema": ENSEMBLE_CALIBRATION_SCHEMA,
        "ensemble": {
            "manifest_sha256": ensemble_manifest_sha256,
            "members": [
                {
                    "seed": member["seed"],
                    "checkpoint_sha256": member["checkpoint_sha256"],
                }
                for member in members
            ],
            "value_lower_aggregation": (
                "mean_member_quantile_minus_mean_value_std"
            ),
            "lower_quantile": lower_quantile,
            "uncertainty_std_weight": uncertainty_std_weight,
            "response_aggregation": "mean_member_logits_then_temperature",
            "outcome_aggregation": OUTCOME_AGGREGATION_METHOD,
            "outcome_uncertainty_std_weight": outcome_uncertainty_std_weight,
            "outcome_calibration_payload_sha256": [
                calibration["payload_sha256"]
                for calibration in outcome_calibrations
            ],
            "diagnostics": diagnostics,
        },
        "outcome_calibrations": outcome_calibrations,
        "source_collection_complete": bool(source_collection_complete),
        "deployment_policy_value": False,
        "strength_evidence": False,
    })
    return {**payload, "payload_sha256": _canonical_sha256(payload)}


def _parse_value_response_calibration(
    calibration: dict[str, Any],
) -> tuple[dict[str, float], dict[str, list[float]], float, float, float]:
    ensemble = calibration.get("ensemble")
    if not isinstance(ensemble, dict):
        raise ValueError("v4 ensemble calibration contract is missing")
    lower_quantile = _finite(
        ensemble.get("lower_quantile"), field="lower quantile"
    )
    if lower_quantile not in QUANTILE_LEVELS:
        raise ValueError("v4 calibrated lower quantile is unsupported")
    uncertainty = _finite(
        ensemble.get("uncertainty_std_weight"), field="uncertainty_std_weight"
    )
    outcome_uncertainty = _finite(
        ensemble.get("outcome_uncertainty_std_weight"),
        field="outcome_uncertainty_std_weight",
    )
    if min(uncertainty, outcome_uncertainty) < 0.0:
        raise ValueError("v4 ensemble uncertainty weights must be nonnegative")
    value_lower = calibration.get("value_lower")
    fields = value_lower.get("fields") if isinstance(value_lower, dict) else None
    clips = value_lower.get("target_clips") if isinstance(value_lower, dict) else None
    if (
        not isinstance(fields, dict)
        or set(fields) != set(VALUE_FIELDS)
        or not isinstance(clips, dict)
        or set(clips) != set(VALUE_FIELDS)
        or value_lower.get("target_preprocessing")
        != "symmetric_clip_before_residual"
    ):
        raise ValueError("invalid v4 value calibration contract")
    normalized_clips = {
        field: _positive(clips[field], field=f"{field} clip")
        for field in VALUE_FIELDS
    }
    offsets = {}
    for field in VALUE_FIELDS:
        raw = fields[field].get("offsets") if isinstance(fields[field], dict) else None
        if not isinstance(raw, list) or len(raw) != len(LABELS):
            raise ValueError(f"{field} calibration offsets have wrong dimension")
        offsets[field] = [
            _finite(value, field=f"{field} offset") for value in raw
        ]
    response = calibration.get("response_temperature")
    if not isinstance(response, dict):
        raise ValueError("v4 response temperature calibration is missing")
    temperature = _positive(
        response.get("temperature"), field="response temperature"
    )
    return normalized_clips, offsets, lower_quantile, uncertainty, temperature


def load_calibrated_ensemble(
    calibration_dir: Path,
    *,
    dataset: RoleDatasetAccess,
    run_id: str,
    device: torch.device | str,
    formal: bool,
) -> dict[str, Any]:
    """Strictly verify and load one calibrated v4 ensemble."""
    require_formal_collection_boundary(dataset, formal=formal)
    root = calibration_dir.resolve()
    artifact = _load_json(root / "artifact_manifest.json", field="artifact manifest")
    if (
        artifact.get("schema") != ARTIFACT_MANIFEST_SCHEMA
        or artifact.get("run_id") != run_id
        or artifact.get("policy_roles_opened") is not False
        or artifact.get("deployment_policy_value") is not False
        or artifact.get("strength_evidence") is not False
    ):
        raise ValueError("invalid v4 ensemble calibration artifact manifest")
    _verify_file_contracts(
        root,
        artifact,
        expected=EXPECTED_CALIBRATION_FILES,
        field="v4 calibration artifact",
    )
    ensemble_path = root / "ensemble_checkpoint_manifest.json"
    calibration_path = root / "calibration.json"
    report_path = root / "calibration_report.json"
    authorization_path = root / "checkpoint_authorization.json"
    ensemble = _load_json(ensemble_path, field="v4 ensemble manifest")
    calibration = _load_json(calibration_path, field="v4 ensemble calibration")
    report = _load_json(report_path, field="v4 calibration report")
    authorization = _load_json(
        authorization_path, field="v4 ensemble checkpoint authorization"
    )
    ensemble_sha256 = _sha256(ensemble_path)
    current_tool_sha256 = _sha256(Path(__file__).resolve())
    unsigned = dict(calibration)
    payload_sha256 = str(unsigned.pop("payload_sha256", ""))
    members_raw = ensemble.get("members")
    expected_training = {
        role: dataset._role_artifact_sha256(role)
        for role in MODEL_TRAINING_ROLES
    }
    expected_calibration_artifact = dataset._role_artifact_sha256(
        MODEL_CALIBRATION_ROLE
    )
    expected_calibration_opponents = list(dataset.roles[MODEL_CALIBRATION_ROLE])
    source_complete = dataset.manifest.get("source_collection_complete") is True
    if (
        ensemble.get("schema") != ENSEMBLE_MANIFEST_SCHEMA
        or ensemble.get("run_id") != run_id
        or ensemble.get("role_manifest_sha256") != dataset.manifest_sha256
        or ensemble.get("model_format") != MODEL_FORMAT
        or ensemble.get("selection_key_order") != list(SELECTION_KEY_ORDER)
        or ensemble.get("selection_method") != SELECTION_METHOD
        or ensemble.get("scaling_tool_sha256") != _sha256(_scaling_tool_path())
        or ensemble.get("source_collection_complete") is not source_complete
        or ensemble.get("deployment_policy_value") is not False
        or ensemble.get("strength_evidence") is not False
        or not isinstance(members_raw, list)
        or not members_raw
        or calibration.get("schema") != ENSEMBLE_CALIBRATION_SCHEMA
        or calibration.get("run_id") != run_id
        or calibration.get("role_manifest_sha256") != dataset.manifest_sha256
        or calibration.get("checkpoint_sha256") != ensemble_sha256
        or calibration.get("calibration_role") != MODEL_CALIBRATION_ROLE
        or calibration.get("calibration_artifact_sha256")
        != expected_calibration_artifact
        or calibration.get("opponents") != expected_calibration_opponents
        or calibration.get("source_collection_complete") is not source_complete
        or calibration.get("policy_evidence_used") is not False
        or calibration.get("deployment_policy_value") is not False
        or calibration.get("strength_evidence") is not False
        or payload_sha256 != _canonical_sha256(unsigned)
        or report.get("schema") != CALIBRATION_REPORT_SCHEMA
        or report.get("run_id") != run_id
        or report.get("role_manifest_sha256") != dataset.manifest_sha256
        or report.get("ensemble_manifest_sha256") != ensemble_sha256
        or report.get("calibration_payload_sha256") != payload_sha256
        or report.get("calibration_tool_sha256") != current_tool_sha256
        or report.get("opened_roles")
        != [*MODEL_TRAINING_ROLES, MODEL_CALIBRATION_ROLE]
        or report.get("policy_roles_opened") is not False
        or report.get("source_collection_complete") is not source_complete
        or report.get("deployment_policy_value") is not False
        or report.get("strength_evidence") is not False
        or report.get("native_tcp_evaluated") is not False
        or authorization.get("schema") != FROZEN_CHECKPOINT_SCHEMA
        or authorization.get("frozen") is not True
        or authorization.get("early_stop_complete") is not True
        or authorization.get("run_id") != run_id
        or authorization.get("role_manifest_sha256") != dataset.manifest_sha256
        or authorization.get("training_roles") != list(MODEL_TRAINING_ROLES)
        or authorization.get("training_artifact_sha256") != expected_training
        or authorization.get("checkpoint_sha256") != ensemble_sha256
        or artifact.get("calibration_tool_sha256") != current_tool_sha256
    ):
        raise ValueError("v4 ensemble calibration bindings are invalid")
    selected = ensemble.get("selected_configuration")
    if not isinstance(selected, dict):
        raise ValueError("v4 ensemble manifest has no selected configuration")
    if formal:
        validate_formal_grid_verification(
            ensemble.get("formal_grid_verification"),
            role_manifest_sha256=dataset.manifest_sha256,
            training_artifact_sha256=expected_training,
            selected_configuration=selected,
        )
    elif ensemble.get("formal_grid_verification") is not None:
        raise ValueError("incomplete v4 ensemble carries a formal grid proof")
    run_rows = [
        {
            "seed": int(member["seed"]),
            "run_id": member["run_id"],
            "output_dir": member["output_dir"],
            "completed": True,
            "checkpoint_sha256": member["checkpoint_sha256"],
            "role_manifest_sha256": member["role_manifest_sha256"],
            "selection_key": member["early_stop_selection_key"],
            "selection_key_order": member["selection_key_order"],
            "parameters": member["parameters"],
            "source_completed_passes": member["source_completed_passes"],
            "source_requested_passes": member["source_requested_passes"],
            "source_collection_complete": member["source_collection_complete"],
            "incomplete_smoke": member["incomplete_smoke"],
            "training_device": member["training_device"],
            "scale": member["scale"],
            "encoder": member["encoder"],
        }
        for member in members_raw
    ]
    if any(
        row["scale"] != selected.get("scale")
        or row["encoder"] != selected.get("encoder")
        or row["role_manifest_sha256"] != dataset.manifest_sha256
        for row in run_rows
    ):
        raise ValueError("v4 ensemble members do not match the selected configuration")
    members = verify_members(
        run_rows,
        role_manifest_sha256=dataset.manifest_sha256,
        training_artifact_sha256=expected_training,
        device=device,
        formal=formal,
    )
    member_contract = [
        {"seed": member["seed"], "checkpoint_sha256": member["checkpoint_sha256"]}
        for member in members
    ]
    member_seeds = [member["seed"] for member in members]
    member_keys = [member["early_stop_selection_key"] for member in members]
    expected_median_key = [
        statistics.median(key[index] for key in member_keys)
        for index in range(len(SELECTION_KEY_ORDER))
    ]
    expected_mean_key = [
        statistics.mean(key[index] for key in member_keys)
        for index in range(len(SELECTION_KEY_ORDER))
    ]
    expected_worst_key = [
        max(key[index] for key in member_keys)
        for index in range(len(SELECTION_KEY_ORDER))
    ]
    ensemble_contract = calibration.get("ensemble")
    if (
        not isinstance(ensemble_contract, dict)
        or ensemble_contract.get("manifest_sha256") != ensemble_sha256
        or ensemble_contract.get("members") != member_contract
        or ensemble_contract.get("value_lower_aggregation")
        != "mean_member_quantile_minus_mean_value_std"
        or ensemble_contract.get("response_aggregation")
        != "mean_member_logits_then_temperature"
        or ensemble_contract.get("outcome_aggregation")
        != OUTCOME_AGGREGATION_METHOD
        or report.get("member_checkpoint_sha256")
        != [member["checkpoint_sha256"] for member in members]
        or sorted(int(seed) for seed in selected.get("requested_seeds", []))
        != sorted(member_seeds)
        or selected.get("median_selection_key") != expected_median_key
        or selected.get("mean_selection_key") != expected_mean_key
        or selected.get("worst_selection_key") != expected_worst_key
    ):
        raise ValueError("v4 ensemble member or aggregation binding changed")
    raw_outcomes = calibration.get("outcome_calibrations")
    if not isinstance(raw_outcomes, list) or len(raw_outcomes) != len(members):
        raise ValueError("v4 outcome calibrations do not cover every member")
    outcome_calibrations = []
    role_opponents = list(calibration["opponents"])
    for member, raw in zip(members, raw_outcomes, strict=True):
        validated = validate_calibration_artifact(
            raw,
            checkpoint_sha256=member["checkpoint_sha256"],
            model_format=MODEL_FORMAT,
        )
        if (
            validated.get("run_id") != run_id
            or validated.get("member_seed") != member["seed"]
            or validated.get("role_manifest_sha256") != dataset.manifest_sha256
            or validated.get("calibration_role") != MODEL_CALIBRATION_ROLE
            or validated.get("model_calibration_artifact_sha256")
            != expected_calibration_artifact
            or validated.get("model_calibration_opponents") != role_opponents
            or validated.get("policy_evidence_used") is not False
            or validated.get("source_collection_complete")
            is not calibration.get("source_collection_complete")
        ):
            raise ValueError("v4 member outcome calibration binding changed")
        outcome_calibrations.append(validated)
        member["outcome_calibration"] = validated
    if ensemble_contract.get("outcome_calibration_payload_sha256") != [
        calibration["payload_sha256"] for calibration in outcome_calibrations
    ]:
        raise ValueError("v4 outcome calibration payload ordering changed")
    if formal and (
        ensemble.get("source_collection_complete") is not True
        or calibration.get("source_collection_complete") is not True
        or report.get("source_collection_complete") is not True
        or report.get("formal_selection") is not True
        or report.get("incomplete_smoke") is not False
        or len(members) < 3
    ):
        raise ValueError("formal v4 policy selection requires a complete ensemble")
    clips, offsets, lower_quantile, uncertainty, response_temperature = (
        _parse_value_response_calibration(calibration)
    )
    outcome_uncertainty = _finite(
        ensemble_contract.get("outcome_uncertainty_std_weight"),
        field="outcome_uncertainty_std_weight",
    )
    require_formal_uncertainty_contract(
        uncertainty, outcome_uncertainty, formal=formal
    )
    return {
        "root": root,
        "artifact_manifest_sha256": _sha256(root / "artifact_manifest.json"),
        "ensemble": ensemble,
        "ensemble_manifest_sha256": ensemble_sha256,
        "calibration": calibration,
        "calibration_file_sha256": _sha256(calibration_path),
        "calibration_report_sha256": _sha256(report_path),
        "calibration_payload_sha256": payload_sha256,
        "members": members,
        "models": [member["model"] for member in members],
        "clips": clips,
        "offsets": offsets,
        "lower_quantile": lower_quantile,
        "uncertainty_std_weight": uncertainty,
        "response_temperature": response_temperature,
        "outcome_calibrations": outcome_calibrations,
        "outcome_uncertainty_std_weight": outcome_uncertainty,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scaling-summary", required=True, type=Path)
    parser.add_argument("--role-manifest", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--allow-incomplete-smoke", action="store_true")
    parser.add_argument("--lower-quantile", type=float, default=0.20)
    parser.add_argument("--uncertainty-std-weight", type=float, default=1.0)
    parser.add_argument("--outcome-uncertainty-std-weight", type=float, default=1.0)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--min-rows-per-action", type=int, default=20)
    parser.add_argument("--min-ess-per-action", type=float, default=10.0)
    parser.add_argument("--outcome-steps", type=int, default=500)
    parser.add_argument("--outcome-learning-rate", type=float, default=0.05)
    parser.add_argument("--outcome-l2", type=float, default=1.0e-4)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args(argv)
    if args.lower_quantile not in QUANTILE_LEVELS:
        raise SystemExit("lower quantile must be a model quantile")
    numeric = (
        args.uncertainty_std_weight,
        args.outcome_uncertainty_std_weight,
        args.min_ess_per_action,
        args.outcome_learning_rate,
        args.outcome_l2,
    )
    if (
        any(not math.isfinite(value) for value in numeric)
        or min(args.uncertainty_std_weight, args.outcome_uncertainty_std_weight) < 0.0
        or min(args.batch_size, args.min_rows_per_action, args.outcome_steps) < 1
        or args.min_ess_per_action <= 0.0
        or args.outcome_learning_rate <= 0.0
        or args.outcome_l2 < 0.0
    ):
        raise SystemExit("invalid v4 ensemble calibration configuration")
    formal = not args.allow_incomplete_smoke
    try:
        require_formal_uncertainty_contract(
            args.uncertainty_std_weight,
            args.outcome_uncertainty_std_weight,
            formal=formal,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if str(args.device).startswith("cuda") and not torch.cuda.is_available():
        raise SystemExit("CUDA was requested but is unavailable")
    summary_path = args.scaling_summary.resolve()
    summary = _load_json(summary_path, field="v4 scaling summary")
    try:
        selected, run_rows = selected_scaling_runs(
            summary, allow_incomplete_smoke=args.allow_incomplete_smoke
        )
        dataset = RoleDatasetAccess(
            args.role_manifest,
            ledger_path=args.ledger,
            run_id=args.run_id,
            require_complete=formal,
        )
        require_formal_collection_boundary(dataset, formal=formal)
        training_artifacts = {
            role: dataset._role_artifact_sha256(role)
            for role in MODEL_TRAINING_ROLES
        }
        formal_grid_verification = None
        if formal:
            formal_grid_verification = verify_formal_scaling_artifacts(
                summary,
                role_manifest_sha256=dataset.manifest_sha256,
                training_artifact_sha256=training_artifacts,
            )
            selected = formal_grid_verification["selected_configuration"]
            run_rows = [
                row
                for row in formal_grid_verification["verified_runs"]
                if row["scale"] == selected["scale"]
                and row["encoder"] == selected["encoder"]
            ]
        members = verify_members(
            run_rows,
            role_manifest_sha256=dataset.manifest_sha256,
            training_artifact_sha256=training_artifacts,
            device=args.device,
            formal=formal,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    clips = dict(members[0]["training_config"]["clips"])
    out_dir = args.out_dir.resolve()
    if out_dir.exists():
        raise SystemExit(f"output directory already exists: {out_dir}")
    temporary = out_dir.with_name(f".{out_dir.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise SystemExit(f"temporary output directory already exists: {temporary}")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.mkdir()
    try:
        source_complete = dataset.manifest.get("source_collection_complete") is True
        ensemble_manifest = {
            "schema": ENSEMBLE_MANIFEST_SCHEMA,
            "run_id": args.run_id,
            "role_manifest_sha256": dataset.manifest_sha256,
            "scaling_summary_sha256": _sha256(summary_path),
            "selected_configuration": selected,
            "formal_grid_verification": formal_grid_verification,
            "members": [
                {
                    key: member[key]
                    for key in (
                        "seed",
                        "run_id",
                        "output_dir",
                        "checkpoint_sha256",
                        "checkpoint_authorization_sha256",
                        "training_report_sha256",
                        "training_artifact_manifest_sha256",
                        "scale",
                        "encoder",
                        "parameters",
                        "role_manifest_sha256",
                        "selection_key_order",
                        "early_stop_selection_key",
                        "source_completed_passes",
                        "source_requested_passes",
                        "source_collection_complete",
                        "incomplete_smoke",
                        "training_device",
                    )
                }
                for member in members
            ],
            "model_format": MODEL_FORMAT,
            "selection_key_order": list(SELECTION_KEY_ORDER),
            "selection_method": SELECTION_METHOD,
            "scaling_tool_sha256": _sha256(_scaling_tool_path()),
            "lower_quantile": args.lower_quantile,
            "uncertainty_std_weight": args.uncertainty_std_weight,
            "outcome_uncertainty_std_weight": args.outcome_uncertainty_std_weight,
            "source_collection_complete": source_complete,
            "deployment_policy_value": False,
            "strength_evidence": False,
        }
        ensemble_path = temporary / "ensemble_checkpoint_manifest.json"
        _write_json(ensemble_path, ensemble_manifest)
        ensemble_sha256 = _sha256(ensemble_path)
        training_phase, authorization, calibration_phase = (
            prepare_ensemble_calibration_phase(
                dataset,
                members,
                ensemble_manifest_sha256=ensemble_sha256,
            )
        )
        del training_phase
        _write_json(temporary / "checkpoint_authorization.json", authorization)
        raw_role = calibration_phase["roles"][MODEL_CALIBRATION_ROLE]
        role = v3_calibration._encoded_calibration_role(raw_role)
        value_observations, response_rows, diagnostics = (
            v3_calibration.ensemble_calibration_predictions(
                [member["model"] for member in members],
                role,
                clips=clips,
                batch_size=args.batch_size,
                device=args.device,
                lower_quantile=args.lower_quantile,
                uncertainty_std_weight=args.uncertainty_std_weight,
            )
        )
        value_lower = calibrate_value_lower_offsets(
            value_observations,
            value_fields=VALUE_FIELDS,
            num_actions=len(LABELS),
            quantile=args.lower_quantile,
            min_rows_per_action=args.min_rows_per_action,
            min_ess_per_action=args.min_ess_per_action,
        )
        value_lower["target_preprocessing"] = "symmetric_clip_before_residual"
        value_lower["target_clips"] = clips
        response_temperature = calibrate_response_temperature(response_rows)
        base = build_calibration_artifact(
            calibration_phase,
            value_lower=value_lower,
            response_temperature=response_temperature,
        )
        outcome_calibrations = []
        for member in members:
            observations = outcome_fit.collect_predictions(
                member["model"],
                role["value"],
                batch_size=args.batch_size,
                device=args.device,
            )
            outcome_calibrations.append(_member_outcome_calibration(
                member,
                observations,
                raw_role,
                run_id=args.run_id,
                role_manifest_sha256=dataset.manifest_sha256,
                source_collection_complete=source_complete,
                steps=args.outcome_steps,
                learning_rate=args.outcome_learning_rate,
                l2=args.outcome_l2,
            ))
        calibration = _ensemble_calibration_artifact(
            base,
            ensemble_manifest_sha256=ensemble_sha256,
            members=members,
            outcome_calibrations=outcome_calibrations,
            lower_quantile=args.lower_quantile,
            uncertainty_std_weight=args.uncertainty_std_weight,
            outcome_uncertainty_std_weight=args.outcome_uncertainty_std_weight,
            diagnostics=diagnostics,
            source_collection_complete=source_complete,
        )
        _write_json(temporary / "calibration.json", calibration)
        report = {
            "schema": CALIBRATION_REPORT_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_id": args.run_id,
            "command": [sys.executable, *sys.argv],
            "scaling_summary": str(summary_path),
            "role_manifest": str(args.role_manifest.resolve()),
            "role_manifest_sha256": dataset.manifest_sha256,
            "ensemble_manifest_sha256": ensemble_sha256,
            "selected_configuration": selected,
            "formal_selection": formal,
            "member_checkpoint_sha256": [
                member["checkpoint_sha256"] for member in members
            ],
            "opened_roles": [*MODEL_TRAINING_ROLES, MODEL_CALIBRATION_ROLE],
            "policy_roles_opened": False,
            "source_collection_complete": source_complete,
            "incomplete_smoke": args.allow_incomplete_smoke,
            "value_observations": len(value_observations),
            "response_rows": len(response_rows),
            "outcome_members": [
                {
                    "seed": member["seed"],
                    "checkpoint_sha256": member["checkpoint_sha256"],
                    "calibration_payload_sha256": outcome["payload_sha256"],
                    "metrics": outcome["metrics"],
                }
                for member, outcome in zip(
                    members, outcome_calibrations, strict=True
                )
            ],
            "diagnostics": diagnostics,
            "calibration_tool_sha256": _sha256(Path(__file__).resolve()),
            "calibration_payload_sha256": calibration["payload_sha256"],
            "deployment_policy_value": False,
            "strength_evidence": False,
            "native_tcp_evaluated": False,
        }
        _write_json(temporary / "calibration_report.json", report)
        _write_json(temporary / "artifact_manifest.json", {
            "schema": ARTIFACT_MANIFEST_SCHEMA,
            "run_id": args.run_id,
            "files": {
                name: {
                    "bytes": (temporary / name).stat().st_size,
                    "sha256": _sha256(temporary / name),
                }
                for name in sorted(EXPECTED_CALIBRATION_FILES)
            },
            "ensemble_manifest_sha256": ensemble_sha256,
            "calibration_payload_sha256": calibration["payload_sha256"],
            "calibration_tool_sha256": _sha256(Path(__file__).resolve()),
            "source_collection_complete": source_complete,
            "policy_roles_opened": False,
            "deployment_policy_value": False,
            "strength_evidence": False,
        })
        temporary.replace(out_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(json.dumps({
        "out_dir": str(out_dir),
        "members": len(members),
        "ensemble_manifest_sha256": ensemble_sha256,
        "calibration_payload_sha256": calibration["payload_sha256"],
        "outcome_calibration_payload_sha256": [
            item["payload_sha256"] for item in outcome_calibrations
        ],
        "source_collection_complete": source_complete,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
