#!/usr/bin/env python3
"""Select a protected v4 win-first policy on its opponent-disjoint role."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import sys
from typing import Any

import torch

from evaluate_multitask_offline_policy import (
    _evaluate_config,
    _selection_eligibility,
)
from match_outcome_calibration import (
    apply_calibration,
    validate_calibration_artifact,
)
from match_outcome_schema import MATCH_OUTCOME_ESTIMAND
from multitask_training_data import encode_value_inference_row
from opponent_multitask_batch_v3 import collate_inference_rows
from opponent_multitask_model_v4 import MODEL_FORMAT
from policy_role_evidence import (
    build_bootstrap_contract,
    build_policy_selection_result,
    open_policy_selection,
    verify_formal_v4_selection_evidence,
    write_selection_result,
)
from role_dataset_access import (
    POLICY_OFFLINE_ESTIMAND_V4,
    POLICY_SELECTION_RESULT_SCHEMA_V4,
    RoleDatasetAccess,
)
import select_opponent_multitask_v3_policy as v3
from win_first_policy_v4 import (
    OUTCOME_AGGREGATION_METHOD,
    POLICY_SCHEMA,
    SELECTION_PRIORITY,
    aggregate_member_probabilities,
    normalize_policy,
    select_candidate,
)


POLICY_CANDIDATE_SCHEMA = "opponent_multitask_v4_policy_candidate_v1"
POLICY_EVALUATION_SCHEMA = "opponent_multitask_v4_policy_evaluation_v1"
POLICY_REPORT_SCHEMA = "opponent_multitask_v4_policy_selection_report_v1"
POLICY_ARTIFACT_SCHEMA = "opponent_multitask_v4_policy_artifacts_v1"
EVIDENCE_CONTRACT = "v4"

MIN_POSITIVE_PROBABILITY_LCB = 0.5
MIN_PROBABILITY_UPLIFT_LCB = 0.0
MIN_HAND_LCB = 0.0
FORMAL_COLLECTION_PASSES = 160
FORMAL_MIN_OVERRIDES = 12
FORMAL_MIN_SELECTION_CLUSTERS = 8
FORMAL_MIN_OVERRIDE_CLUSTERS = 8
FORMAL_MIN_OVERRIDES_PER_OPPONENT = 4
FORMAL_MIN_BOOTSTRAP_SAMPLES = 2000
FORMAL_MAX_POLICY_GRID_SIZE = 10_000

POLICY_CONTRACT_FIELDS = {
    "schema",
    "selection_priority",
    "min_positive_probability_lcb",
    "min_probability_uplift_lcb",
    "min_hand_lcb",
    "chip_margins",
    "hand_weights",
    "tail_weights",
    "response_weights",
    "min_match_weight",
    "outcome_aggregation",
    "outcome_uncertainty_std_weight",
    "min_overrides",
    "min_selection_clusters",
    "min_override_clusters",
    "min_overrides_per_opponent",
    "min_override_hand_mean",
    "min_cluster_ci_lower",
    "min_opponent_stratified_ci_lower",
    "min_match_positive_rate_ci_lower",
    "min_match_positive_uplift_ci_lower",
    "min_opponent_match_positive_rate",
    "bootstrap_samples",
    "bootstrap_seed",
    "offline_estimand",
}


def normalize_selected_policy(raw: Any) -> dict[str, Any] | None:
    """Validate the canonical v4 policy and its non-tunable safety floors."""
    normalized = normalize_policy(raw)
    if normalized is None:
        return None
    if (
        normalized["min_positive_probability_lcb"]
        != MIN_POSITIVE_PROBABILITY_LCB
        or normalized["min_probability_uplift_lcb"]
        != MIN_PROBABILITY_UPLIFT_LCB
        or normalized["min_hand_lcb"] != MIN_HAND_LCB
    ):
        raise ValueError("formal v4 action safety floors are fixed")
    return normalized


def _contract_float(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, float):
        raise ValueError(f"{field} must be a canonical JSON float")
    if not math.isfinite(value):
        raise ValueError(f"{field} must be finite")
    return value


def _contract_grid(
    value: Any,
    *,
    field: str,
    upper: float | None = None,
) -> list[float]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty canonical grid")
    result = [
        _contract_float(item, field=f"{field}[{index}]")
        for index, item in enumerate(value)
    ]
    if result != sorted(set(result)):
        raise ValueError(f"{field} must be sorted and unique")
    if any(item < 0.0 or (upper is not None and item > upper) for item in result):
        raise ValueError(f"{field} is outside its legal range")
    return result


def validate_formal_policy_contract(
    raw: Any,
    *,
    calibrated: dict[str, Any],
) -> dict[str, Any]:
    """Return the one canonical replay contract accepted by a formal gate."""
    if not isinstance(raw, dict) or set(raw) != POLICY_CONTRACT_FIELDS:
        raise ValueError("formal v4 policy contract has unknown or missing fields")
    if (
        raw.get("schema") != POLICY_SCHEMA
        or raw.get("selection_priority") != SELECTION_PRIORITY
        or raw.get("outcome_aggregation") != OUTCOME_AGGREGATION_METHOD
        or raw.get("offline_estimand") != POLICY_OFFLINE_ESTIMAND_V4
    ):
        raise ValueError("formal v4 policy contract identity changed")
    fixed_floors = {
        "min_positive_probability_lcb": MIN_POSITIVE_PROBABILITY_LCB,
        "min_probability_uplift_lcb": MIN_PROBABILITY_UPLIFT_LCB,
        "min_hand_lcb": MIN_HAND_LCB,
    }
    for field, expected in fixed_floors.items():
        if _contract_float(raw.get(field), field=field) != expected:
            raise ValueError("formal v4 action safety floors are fixed")
    chip_margins = _contract_grid(raw.get("chip_margins"), field="chip_margins")
    hand_weights = _contract_grid(
        raw.get("hand_weights"), field="hand_weights", upper=1.0
    )
    tail_weights = _contract_grid(
        raw.get("tail_weights"), field="tail_weights", upper=1.0
    )
    response_weights = _contract_grid(
        raw.get("response_weights"), field="response_weights", upper=1.0
    )
    min_match_weight = _contract_float(
        raw.get("min_match_weight"), field="min_match_weight"
    )
    uncertainty = _contract_float(
        raw.get("outcome_uncertainty_std_weight"),
        field="outcome_uncertainty_std_weight",
    )
    if not 0.0 <= min_match_weight <= 1.0 or uncertainty < 0.0:
        raise ValueError("formal v4 policy weights are invalid")
    if uncertainty != calibrated.get("outcome_uncertainty_std_weight"):
        raise ValueError("formal v4 policy uncertainty binding changed")
    valid_weight_pairs = [
        (hand, tail)
        for hand in hand_weights
        for tail in tail_weights
        if 1.0 - hand - tail >= 0.0
        and 1.0 - hand - tail + 1.0e-9 >= min_match_weight
    ]
    grid_size = (
        len(chip_margins)
        * len(valid_weight_pairs)
        * len(response_weights)
    )
    if not valid_weight_pairs or not 1 <= grid_size <= FORMAL_MAX_POLICY_GRID_SIZE:
        raise ValueError("formal v4 policy grid has no legal bounded configuration")
    integer_floors = {
        "min_overrides": FORMAL_MIN_OVERRIDES,
        "min_selection_clusters": FORMAL_MIN_SELECTION_CLUSTERS,
        "min_override_clusters": FORMAL_MIN_OVERRIDE_CLUSTERS,
        "min_overrides_per_opponent": FORMAL_MIN_OVERRIDES_PER_OPPONENT,
        "bootstrap_samples": FORMAL_MIN_BOOTSTRAP_SAMPLES,
    }
    integers: dict[str, int] = {}
    for field, floor in integer_floors.items():
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < floor:
            raise ValueError(f"formal v4 policy {field} was weakened")
        integers[field] = value
    bootstrap_seed = raw.get("bootstrap_seed")
    if (
        isinstance(bootstrap_seed, bool)
        or not isinstance(bootstrap_seed, int)
        or bootstrap_seed < 0
    ):
        raise ValueError("formal v4 policy bootstrap seed is invalid")
    numeric = {
        field: _contract_float(raw.get(field), field=field)
        for field in (
            "min_override_hand_mean",
            "min_cluster_ci_lower",
            "min_opponent_stratified_ci_lower",
            "min_match_positive_rate_ci_lower",
            "min_match_positive_uplift_ci_lower",
            "min_opponent_match_positive_rate",
        )
    }
    if (
        min(
            numeric["min_override_hand_mean"],
            numeric["min_cluster_ci_lower"],
            numeric["min_opponent_stratified_ci_lower"],
        ) < 0.0
        or not 0.5 <= numeric["min_match_positive_rate_ci_lower"] <= 1.0
        or not 0.0 <= numeric["min_match_positive_uplift_ci_lower"] <= 1.0
        or not 0.5 <= numeric["min_opponent_match_positive_rate"] <= 1.0
    ):
        raise ValueError("formal v4 policy evidence thresholds are invalid")
    normalized = {
        "schema": POLICY_SCHEMA,
        "selection_priority": SELECTION_PRIORITY,
        **fixed_floors,
        "chip_margins": chip_margins,
        "hand_weights": hand_weights,
        "tail_weights": tail_weights,
        "response_weights": response_weights,
        "min_match_weight": min_match_weight,
        "outcome_aggregation": OUTCOME_AGGREGATION_METHOD,
        "outcome_uncertainty_std_weight": uncertainty,
        **integers,
        **numeric,
        "bootstrap_seed": bootstrap_seed,
        "offline_estimand": POLICY_OFFLINE_ESTIMAND_V4,
    }
    if raw != normalized:
        raise ValueError("formal v4 policy contract is not canonical")
    return normalized


def _selection_thresholds_from_contract(
    contract: dict[str, Any],
) -> dict[str, Any]:
    return {
        "min_overrides": contract["min_overrides"],
        "min_override_clusters": contract["min_override_clusters"],
        "min_overrides_per_opponent": contract["min_overrides_per_opponent"],
        "min_cluster_ci_lower": contract["min_cluster_ci_lower"],
        "min_opponent_stratified_ci_lower": contract[
            "min_opponent_stratified_ci_lower"
        ],
        "min_match_outcome_coverage": 1.0,
        "min_match_positive_rate_ci_lower": contract[
            "min_match_positive_rate_ci_lower"
        ],
        "min_match_positive_uplift_ci_lower": contract[
            "min_match_positive_uplift_ci_lower"
        ],
        "min_opponent_match_positive_rate": contract[
            "min_opponent_match_positive_rate"
        ],
        "min_selection_clusters": contract["min_selection_clusters"],
        "min_override_hand_mean": contract["min_override_hand_mean"],
        "bootstrap_samples": contract["bootstrap_samples"],
    }


def _selection_kwargs_from_contract(
    contract: dict[str, Any],
) -> dict[str, Any]:
    return {
        "chip_margins": contract["chip_margins"],
        "hand_weights": contract["hand_weights"],
        "tail_weights": contract["tail_weights"],
        "response_weights": contract["response_weights"],
        "min_match_weight": contract["min_match_weight"],
        "min_overrides": contract["min_overrides"],
        "min_selection_clusters": contract["min_selection_clusters"],
        "min_override_clusters": contract["min_override_clusters"],
        "min_overrides_per_opponent": contract[
            "min_overrides_per_opponent"
        ],
        "min_override_hand_mean": contract["min_override_hand_mean"],
        "bootstrap_samples": contract["bootstrap_samples"],
        "bootstrap_seed": contract["bootstrap_seed"],
        "min_cluster_ci_lower": contract["min_cluster_ci_lower"],
        "min_opponent_stratified_ci_lower": contract[
            "min_opponent_stratified_ci_lower"
        ],
        "min_match_positive_rate_ci_lower": contract[
            "min_match_positive_rate_ci_lower"
        ],
        "min_match_positive_uplift_ci_lower": contract[
            "min_match_positive_uplift_ci_lower"
        ],
        "min_opponent_match_positive_rate": contract[
            "min_opponent_match_positive_rate"
        ],
    }


def load_calibrated_ensemble(
    calibration_dir: Path,
    *,
    dataset: RoleDatasetAccess,
    run_id: str,
    device: torch.device | str,
    formal: bool,
) -> dict[str, Any]:
    """Load through the calibration owner without creating an import cycle."""
    from calibrate_opponent_multitask_v4_ensemble import (  # noqa: PLC0415
        load_calibrated_ensemble as load,
    )

    return load(
        calibration_dir,
        dataset=dataset,
        run_id=run_id,
        device=device,
        formal=formal,
    )


def _validated_calibrated_ensemble(
    calibrated: dict[str, Any],
    *,
    dataset: RoleDatasetAccess,
    run_id: str,
    formal: bool,
) -> dict[str, Any]:
    if not isinstance(calibrated, dict):
        raise ValueError("v4 calibrated ensemble is invalid")
    members = calibrated.get("members")
    models = calibrated.get("models")
    raw_calibrations = calibrated.get("outcome_calibrations")
    if raw_calibrations is None and isinstance(members, list):
        raw_calibrations = [
            member.get("outcome_calibration")
            if isinstance(member, dict) else None
            for member in members
        ]
    if (
        not isinstance(members, list)
        or not members
        or not isinstance(models, list)
        or len(models) != len(members)
        or not isinstance(raw_calibrations, list)
        or len(raw_calibrations) != len(members)
    ):
        raise ValueError("v4 calibrated ensemble members are incomplete")
    checkpoint_sha256 = [str(member.get("checkpoint_sha256", "")) for member in members]
    if len(set(checkpoint_sha256)) != len(checkpoint_sha256):
        raise ValueError("v4 calibrated ensemble reuses a checkpoint")
    seeds = [member.get("seed") for member in members]
    expected_role_artifact = dataset._role_artifact_sha256("model_calibration")
    expected_opponents = sorted(dataset.roles["model_calibration"])
    calibrations = []
    signatures = set()
    for index, (checkpoint, raw) in enumerate(
        zip(checkpoint_sha256, raw_calibrations, strict=True)
    ):
        try:
            payload = validate_calibration_artifact(
                raw,
                checkpoint_sha256=checkpoint,
                model_format=MODEL_FORMAT,
            )
        except ValueError as exc:
            raise ValueError(
                f"v4 outcome calibration member {index} is invalid"
            ) from exc
        signature = (
            payload.get("run_id"),
            payload.get("role_manifest_sha256"),
            payload.get("model_calibration_artifact_sha256"),
            tuple(sorted(payload.get("model_calibration_opponents") or [])),
            payload.get("source_collection_complete"),
        )
        signatures.add(signature)
        calibrations.append(payload)
    expected_signature = (
        run_id,
        dataset.manifest_sha256,
        expected_role_artifact,
        tuple(expected_opponents),
        dataset.manifest.get("source_collection_complete") is True,
    )
    if signatures != {expected_signature}:
        raise ValueError("v4 members do not use one protected calibration role")
    try:
        outcome_std_weight = float(calibrated["outcome_uncertainty_std_weight"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("v4 outcome uncertainty contract is missing") from exc
    if not math.isfinite(outcome_std_weight) or outcome_std_weight < 0.0:
        raise ValueError("v4 outcome uncertainty weight must be nonnegative")
    if formal and (
        len(members) < 3
        or any(isinstance(seed, bool) or not isinstance(seed, int) for seed in seeds)
        or len(set(seeds)) != len(seeds)
        or dataset.manifest.get("source_collection_complete") is not True
        or any(
            calibration.get("source_collection_complete") is not True
            for calibration in calibrations
        )
    ):
        raise ValueError("formal v4 selection requires a complete three-seed ensemble")
    result = dict(calibrated)
    result["outcome_calibrations"] = calibrations
    result["outcome_uncertainty_std_weight"] = outcome_std_weight
    return result


def aggregate_outcome_predictions(
    models: list[Any],
    calibrations: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    uncertainty_std_weight: float,
    batch_size: int,
    device: torch.device | str,
) -> list[dict[str, Any]]:
    """Use the exact stdlib calibration and win-first aggregation semantics."""
    if len(models) != len(calibrations) or not models:
        raise ValueError("outcome models and calibrations do not align")
    result: list[dict[str, Any]] = []
    for model in models:
        model.eval()
    with torch.no_grad():
        for indices in v3._chunks(len(rows), batch_size):
            selected = [rows[index] for index in indices]
            batch = collate_inference_rows(
                selected, response=False, device=device
            )
            member_logits = [
                model.forward_match_outcome(**batch["inputs"])
                for model in models
            ]
            if any(
                output.ndim != 2
                or output.shape[0] != len(selected)
                or output.shape[1] != len(v3.LABELS)
                for output in member_logits
            ):
                raise ValueError("v4 outcome prediction has the wrong shape")
            for row_index in range(len(selected)):
                probabilities = [
                    apply_calibration(
                        output[row_index].detach().cpu().tolist(), calibration
                    )["probabilities"]
                    for output, calibration in zip(
                        member_logits, calibrations, strict=True
                    )
                ]
                result.append(aggregate_member_probabilities(
                    probabilities,
                    uncertainty_std_weight=uncertainty_std_weight,
                ))
    return result


def prepare_policy_rows(
    raw_rows: list[dict[str, Any]],
    calibrated: dict[str, Any],
    *,
    batch_size: int,
    device: torch.device | str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    prepared, summary = v3.prepare_policy_rows(
        raw_rows,
        calibrated,
        batch_size=batch_size,
        device=device,
    )
    encoded = [encode_value_inference_row(row) for row in raw_rows]
    outcomes = aggregate_outcome_predictions(
        calibrated["models"],
        calibrated["outcome_calibrations"],
        encoded,
        uncertainty_std_weight=calibrated["outcome_uncertainty_std_weight"],
        batch_size=batch_size,
        device=device,
    )
    for row in prepared:
        source_index = int(row["source_row_index"])
        row["outcomes"] = outcomes[source_index]
    return prepared, {
        **summary,
        "outcome_predictions": len(outcomes),
        "outcome_aggregation": OUTCOME_AGGREGATION_METHOD,
    }


def select_win_first_candidate(
    row: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any] | None:
    """Single shared action path for Torch selection and stdlib deployment."""
    return select_candidate(
        policy,
        row["outcomes"],
        row["values"],
        row["candidates"],
        rule_label_id=int(row["rule_id"]),
    )


def _evidence_key(result: dict[str, Any]) -> tuple[Any, ...]:
    return (
        result["match_positive_rate_opponent_stratified_cluster_ci"]["lower"],
        result["match_positive_rate_cluster_bootstrap_ci"]["lower"],
        result["match_positive_uplift_opponent_stratified_cluster_ci"]["lower"],
        result["match_positive_uplift_cluster_bootstrap_ci"]["lower"],
        result["match_positive_rate"],
        result["match_opponent_stratified_cluster_ci"]["lower"],
        result["match_cluster_bootstrap_mean_ci"]["lower"],
        result["match_mean_per_opportunity"],
        result["override_clusters"],
        -result["negative_override_rate"],
        -result["config"]["chip_margin"],
    )


def select_policy(
    rows: list[dict[str, Any]],
    *,
    chip_margins: list[float],
    hand_weights: list[float],
    tail_weights: list[float],
    response_weights: list[float],
    min_match_weight: float,
    min_overrides: int,
    min_selection_clusters: int,
    min_override_clusters: int,
    min_overrides_per_opponent: int,
    min_override_hand_mean: float,
    bootstrap_samples: int,
    bootstrap_seed: int,
    min_cluster_ci_lower: float,
    min_opponent_stratified_ci_lower: float,
    min_match_positive_rate_ci_lower: float,
    min_match_positive_uplift_ci_lower: float,
    min_opponent_match_positive_rate: float,
) -> dict[str, Any]:
    grid = []
    bootstrap_contract = build_bootstrap_contract(
        samples=bootstrap_samples, seed=bootstrap_seed
    )
    for chip_margin in chip_margins:
        for hand_weight in hand_weights:
            for tail_weight in tail_weights:
                match_weight = 1.0 - hand_weight - tail_weight
                if match_weight + 1.0e-9 < min_match_weight:
                    continue
                for response_weight in response_weights:
                    policy = normalize_policy({
                        "schema": POLICY_SCHEMA,
                        "selection_priority": SELECTION_PRIORITY,
                        "min_positive_probability_lcb": (
                            MIN_POSITIVE_PROBABILITY_LCB
                        ),
                        "min_probability_uplift_lcb": (
                            MIN_PROBABILITY_UPLIFT_LCB
                        ),
                        "chip_margin": chip_margin,
                        "hand_weight": hand_weight,
                        "tail_weight": tail_weight,
                        "match_weight": match_weight,
                        "response_weight": response_weight,
                        "min_hand_lcb": MIN_HAND_LCB,
                        "use_lower": True,
                    })
                    assert policy is not None
                    result = _evaluate_config(
                        rows,
                        policy,
                        bootstrap_samples=bootstrap_samples,
                        bootstrap_seed=bootstrap_seed,
                        candidate_selector=select_win_first_candidate,
                    )
                    result["estimand"] = POLICY_OFFLINE_ESTIMAND_V4
                    result["bootstrap_contract"] = dict(bootstrap_contract)
                    result["eligibility_errors"] = _selection_eligibility(
                        result,
                        min_overrides=min_overrides,
                        min_selection_clusters=min_selection_clusters,
                        min_override_clusters=min_override_clusters,
                        min_overrides_per_opponent=min_overrides_per_opponent,
                        min_override_hand_mean=min_override_hand_mean,
                        require_nonnegative_opponent_mean=True,
                        min_cluster_ci_lower=min_cluster_ci_lower,
                        min_opponent_stratified_ci_lower=(
                            min_opponent_stratified_ci_lower
                        ),
                        require_win_first=True,
                        min_match_positive_rate_ci_lower=(
                            min_match_positive_rate_ci_lower
                        ),
                        min_match_positive_uplift_ci_lower=(
                            min_match_positive_uplift_ci_lower
                        ),
                        min_opponent_match_positive_rate=(
                            min_opponent_match_positive_rate
                        ),
                    )
                    result["eligible"] = not result["eligibility_errors"]
                    grid.append(result)
    eligible = [result for result in grid if result["eligible"]]
    selected = max(eligible, key=_evidence_key) if eligible else None
    return {
        "estimand": POLICY_OFFLINE_ESTIMAND_V4,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "rows": len(rows),
        "bootstrap_contract": bootstrap_contract,
        "grid": grid,
        "selected": selected,
        "selection_failure": (
            None if selected is not None else
            "no v4 win-first policy met observed cluster evidence constraints"
        ),
    }


def policy_evaluation(
    selection: dict[str, Any], *, incomplete_smoke: bool
) -> dict[str, Any]:
    selected = selection.get("selected")
    grid = selection.get("grid") or []
    diagnostic = selected or (max(grid, key=_evidence_key) if grid else None)
    if diagnostic is None:
        diagnostic = {
            "rows": int(selection.get("rows", 0)),
            "overrides": 0,
            "override_clusters": 0,
            "match_cluster_bootstrap_mean_ci": {
                "lower": 0.0, "mean": 0.0, "upper": 0.0,
            },
            "match_opponent_stratified_cluster_ci": {
                "lower": 0.0, "mean": 0.0, "upper": 0.0,
            },
            "match_positive_rate_cluster_bootstrap_ci": {
                "lower": 0.0, "mean": 0.0, "upper": 0.0,
            },
            "match_positive_rate_opponent_stratified_cluster_ci": {
                "lower": 0.0, "mean": 0.0, "upper": 0.0,
            },
            "match_positive_uplift_cluster_bootstrap_ci": {
                "lower": 0.0, "mean": 0.0, "upper": 0.0,
            },
            "match_positive_uplift_opponent_stratified_cluster_ci": {
                "lower": 0.0, "mean": 0.0, "upper": 0.0,
            },
            "match_positive_rate": 0.0,
            "by_opponent": {},
        }
    evaluation = dict(diagnostic)
    provisional = selected.get("config") if isinstance(selected, dict) else None
    evaluation.update({
        "schema": POLICY_EVALUATION_SCHEMA,
        "offline_estimand": POLICY_OFFLINE_ESTIMAND_V4,
        "match_outcome_estimand": MATCH_OUTCOME_ESTIMAND,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "selected_policy": None if incomplete_smoke else provisional,
        "provisional_selected_policy": provisional if incomplete_smoke else None,
        "source_collection_complete": not incomplete_smoke,
        "grid_size": len(grid),
        "selection_failure": selection.get("selection_failure"),
        "bootstrap_contract": dict(selection["bootstrap_contract"]),
    })
    selected = evaluation.get("selected_policy")
    if selected is not None:
        normalized = normalize_selected_policy(selected)
        if normalized != selected:
            raise ValueError("selected v4 policy did not normalize exactly")
    return evaluation


def _verify_artifact_files(root: Path, artifact: dict[str, Any]) -> None:
    expected = {
        "candidate_manifest.json",
        "policy_evaluation.json",
        "policy_selection_result.json",
        "policy_selection_report.json",
    }
    files = artifact.get("files")
    if not isinstance(files, dict) or set(files) != expected:
        raise ValueError("v4 policy artifact file set is invalid")
    for name, contract in files.items():
        path = root / name
        if (
            not path.is_file()
            or not isinstance(contract, dict)
            or path.stat().st_size != int(contract.get("bytes", -1))
            or v3._sha256(path) != contract.get("sha256")
        ):
            raise ValueError(f"v4 policy artifact changed: {name}")


def verify_policy_artifacts(
    policy_dir: Path,
    *,
    calibrated: dict[str, Any],
    dataset: RoleDatasetAccess,
    run_id: str,
    formal: bool,
) -> dict[str, Any]:
    root = policy_dir.resolve()
    artifact = v3._load_json(root / "artifact_manifest.json", field="policy artifacts")
    if (
        artifact.get("schema") != POLICY_ARTIFACT_SCHEMA
        or artifact.get("run_id") != run_id
        or artifact.get("policy_gate_opened") is not False
        or artifact.get("deployment_policy_value") is not False
        or artifact.get("strength_evidence") is not False
    ):
        raise ValueError("invalid v4 policy artifact manifest")
    _verify_artifact_files(root, artifact)
    candidate_path = root / "candidate_manifest.json"
    evaluation_path = root / "policy_evaluation.json"
    result_path = root / "policy_selection_result.json"
    report_path = root / "policy_selection_report.json"
    candidate = v3._load_json(candidate_path, field="policy candidate")
    evaluation = v3._load_json(evaluation_path, field="policy evaluation")
    result = v3._load_json(result_path, field="policy selection result")
    report = v3._load_json(report_path, field="policy selection report")
    candidate_sha256 = v3._sha256(candidate_path)
    result_sha256 = v3._sha256(result_path)
    selected_policy = evaluation.get("selected_policy")
    selected_sha256 = (
        v3._canonical_sha256(selected_policy)
        if isinstance(selected_policy, dict) else None
    )
    checkpoint_sha256 = [
        member["checkpoint_sha256"] for member in calibrated["members"]
    ]
    outcome_sha256 = [
        calibration["payload_sha256"]
        for calibration in calibrated["outcome_calibrations"]
    ]
    current_code_artifacts = selector_code_artifacts()
    runtime_context = dataset.runtime_context_contract()
    expected_collection_boundary = (
        dataset.require_collection_boundary(FORMAL_COLLECTION_PASSES)
        if formal else None
    )
    if (
        candidate.get("schema") != POLICY_CANDIDATE_SCHEMA
        or candidate.get("run_id") != run_id
        or candidate.get("role_manifest_sha256") != dataset.manifest_sha256
        or candidate.get("ensemble_manifest_sha256")
        != calibrated["ensemble_manifest_sha256"]
        or candidate.get("calibration_artifact_manifest_sha256")
        != calibrated["artifact_manifest_sha256"]
        or candidate.get("calibration_payload_sha256")
        != calibrated["calibration_payload_sha256"]
        or candidate.get("member_checkpoint_sha256") != checkpoint_sha256
        or candidate.get("outcome_calibration_payload_sha256") != outcome_sha256
        or candidate.get("collection_boundary")
        != expected_collection_boundary
        or candidate.get("candidate_snapshot")
        != runtime_context["candidate_snapshot"]
        or candidate.get("strategy_context_runtime_mode")
        != runtime_context["strategy_context_runtime_mode"]
        or candidate.get("code_artifacts") != current_code_artifacts
        or candidate.get("source_collection_complete") is not formal
        or candidate.get("formal_selection") is not formal
        or candidate.get("deployment_policy_value") is not False
        or candidate.get("strength_evidence") is not False
        or artifact.get("candidate_sha256") != candidate_sha256
        or evaluation.get("schema") != POLICY_EVALUATION_SCHEMA
        or evaluation.get("offline_estimand") != POLICY_OFFLINE_ESTIMAND_V4
        or evaluation.get("deployment_policy_value") is not False
        or evaluation.get("strength_evidence") is not False
        or evaluation.get("code_artifacts") != current_code_artifacts
        or evaluation.get("candidate_snapshot")
        != runtime_context["candidate_snapshot"]
        or evaluation.get("strategy_context_runtime_mode")
        != runtime_context["strategy_context_runtime_mode"]
        or result.get("schema") != POLICY_SELECTION_RESULT_SCHEMA_V4
        or result.get("offline_estimand") != POLICY_OFFLINE_ESTIMAND_V4
        or result.get("run_id") != run_id
        or result.get("candidate_sha256") != candidate_sha256
        or result.get("role_manifest_sha256") != dataset.manifest_sha256
        or result.get("calibration_payload_sha256")
        != calibrated["calibration_payload_sha256"]
        or result.get("evaluation_report_sha256")
        != v3._canonical_sha256(evaluation)
        or result.get("selected_policy_sha256") != selected_sha256
        or result.get("policy_gate_opened") is not False
        or result.get("deployment_policy_value") is not False
        or result.get("strength_evidence") is not False
        or result.get("candidate_snapshot")
        != runtime_context["candidate_snapshot"]
        or result.get("strategy_context_runtime_mode")
        != runtime_context["strategy_context_runtime_mode"]
        or report.get("schema") != POLICY_REPORT_SCHEMA
        or report.get("run_id") != run_id
        or report.get("candidate_sha256") != candidate_sha256
        or report.get("calibration_payload_sha256")
        != calibrated["calibration_payload_sha256"]
        or report.get("policy_selection_opponents")
        != list(dataset.roles["policy_selection"])
        or report.get("selection_result_sha256") != result_sha256
        or report.get("selected_policy_sha256") != selected_sha256
        or report.get("selected_policy") != selected_policy
        or report.get("selection_passed") is not result.get("passed")
        or report.get("selection_errors") != result.get("errors")
        or report.get("policy_gate_opened") is not False
        or report.get("deployment_policy_value") is not False
        or report.get("strength_evidence") is not False
        or report.get("code_artifacts") != current_code_artifacts
        or report.get("candidate_snapshot")
        != runtime_context["candidate_snapshot"]
        or report.get("strategy_context_runtime_mode")
        != runtime_context["strategy_context_runtime_mode"]
        or artifact.get("candidate_snapshot")
        != runtime_context["candidate_snapshot"]
        or artifact.get("strategy_context_runtime_mode")
        != runtime_context["strategy_context_runtime_mode"]
    ):
        raise ValueError("v4 policy artifact bindings are invalid")
    if formal:
        normalized = normalize_selected_policy(selected_policy)
        if (
            result.get("passed") is not True
            or result.get("formal_selection") is not True
            or result.get("source_collection_complete") is not True
            or report.get("selection_passed") is not True
            or report.get("incomplete_smoke") is not False
            or report.get("source_collection_complete") is not True
            or normalized is None
            or normalized != selected_policy
        ):
            raise ValueError("formal v4 bundle requires passing policy selection")
        verify_formal_v4_selection_evidence(
            evaluation,
            result,
            expected_opponents=list(dataset.roles["policy_selection"]),
        )
    else:
        selected_policy = None
        selected_sha256 = None
    return {
        "root": root,
        "candidate_sha256": candidate_sha256,
        "evaluation_sha256": v3._sha256(evaluation_path),
        "result_sha256": result_sha256,
        "artifact_manifest_sha256": v3._sha256(root / "artifact_manifest.json"),
        "selected_policy": selected_policy,
        "selected_policy_sha256": selected_sha256,
        "selection_passed": bool(formal and result.get("passed") is True),
        **runtime_context,
    }


def recompute_and_verify_formal_policy_selection(
    policy_dir: Path,
    *,
    calibrated: dict[str, Any],
    dataset: RoleDatasetAccess,
    run_id: str,
    device: torch.device | str,
    batch_size: int,
) -> dict[str, Any]:
    """Replay the protected selection role and reject self-consistent forgery."""
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("formal v4 replay batch size must be a positive integer")
    verified = verify_policy_artifacts(
        policy_dir,
        calibrated=calibrated,
        dataset=dataset,
        run_id=run_id,
        formal=True,
    )
    root = verified["root"]
    candidate = v3._load_json(
        root / "candidate_manifest.json", field="policy candidate"
    )
    evaluation = v3._load_json(
        root / "policy_evaluation.json", field="policy evaluation"
    )
    result = v3._load_json(
        root / "policy_selection_result.json", field="policy selection result"
    )
    report = v3._load_json(
        root / "policy_selection_report.json", field="policy selection report"
    )
    contract = validate_formal_policy_contract(
        candidate.get("policy_contract"), calibrated=calibrated
    )
    expected_inference_contract = {
        "device": str(device),
        "batch_size": batch_size,
        "torch_version": torch.__version__,
        "value_aggregation": (
            "mean_member_quantile_minus_mean_value_std_plus_calibration"
        ),
        "response_aggregation": "mean_member_logits_then_temperature",
        "outcome_aggregation": OUTCOME_AGGREGATION_METHOD,
    }
    if candidate.get("inference_contract") != expected_inference_contract:
        raise ValueError("formal v4 selection inference contract changed")
    if not dataset._role_was_opened(
        "policy_selection", candidate_sha256=verified["candidate_sha256"]
    ):
        raise ValueError("formal v4 candidate was not previously opened on selection")
    phase = open_policy_selection(
        dataset,
        candidate_sha256=verified["candidate_sha256"],
        calibration_payload_sha256=calibrated["calibration_payload_sha256"],
        contract=EVIDENCE_CONTRACT,
    )
    if (
        result.get("policy_selection_artifact_sha256")
        != phase["policy_selection_artifact_sha256"]
        or report.get("policy_selection_artifact_sha256")
        != phase["policy_selection_artifact_sha256"]
    ):
        raise ValueError("formal v4 selection role artifact binding changed")
    prepared_rows, preparation = prepare_policy_rows(
        phase["value"],
        calibrated,
        batch_size=batch_size,
        device=device,
    )
    selection = select_policy(
        prepared_rows, **_selection_kwargs_from_contract(contract)
    )
    grid = selection.get("grid")
    if not isinstance(grid, list) or not grid:
        raise ValueError("formal v4 selection replay produced no policy grid")
    eligible = [
        item for item in grid
        if isinstance(item, dict) and item.get("eligible") is True
    ]
    replay_winner = max(eligible, key=_evidence_key) if eligible else None
    if replay_winner is None or selection.get("selected") != replay_winner:
        raise ValueError("formal v4 selection replay has no canonical winner")
    selected_policy = evaluation.get("selected_policy")
    grid_policies = [item.get("config") for item in grid]
    if (
        selected_policy not in grid_policies
        or selected_policy != replay_winner.get("config")
    ):
        raise ValueError("formal v4 selected policy is not the grid evidence winner")
    recomputed = policy_evaluation(selection, incomplete_smoke=False)
    recomputed.update({
        "preparation": preparation,
        "policy_contract": contract,
        "code_artifacts": selector_code_artifacts(),
        **dataset.runtime_context_contract(),
    })
    if evaluation != recomputed:
        raise ValueError("formal v4 policy evaluation differs from protected replay")
    expected_thresholds = _selection_thresholds_from_contract(contract)
    if result.get("thresholds") != expected_thresholds:
        raise ValueError("formal v4 result thresholds differ from policy contract")
    verify_formal_v4_selection_evidence(
        recomputed,
        result,
        expected_opponents=list(dataset.roles["policy_selection"]),
    )
    if (
        report.get("role_manifest_sha256") != dataset.manifest_sha256
        or report.get("preparation") != preparation
        or report.get("grid_size") != len(grid)
        or report.get("policy_selection_value_rows") != len(phase["value"])
        or report.get("policy_selection_behavior_rows") != len(phase["behavior"])
    ):
        raise ValueError("formal v4 selection report differs from protected replay")
    return {
        **verified,
        "policy_contract": contract,
        "policy_selection_artifact_sha256": phase[
            "policy_selection_artifact_sha256"
        ],
        "recomputed_evaluation_sha256": v3._canonical_sha256(recomputed),
    }


def selector_code_artifacts() -> dict[str, dict[str, Any]]:
    paths = {
        "inference_batch": Path(
            sys.modules["opponent_multitask_batch_v3"].__file__
        ).resolve(),
        "match_outcome_calibration": Path(
            sys.modules["match_outcome_calibration"].__file__
        ).resolve(),
        "match_outcome_schema": Path(
            sys.modules["match_outcome_schema"].__file__
        ).resolve(),
        "multitask_training_data": Path(
            sys.modules["multitask_training_data"].__file__
        ).resolve(),
        "selector": Path(__file__).resolve(),
        "observed_evidence": Path(
            sys.modules["evaluate_multitask_offline_policy"].__file__
        ).resolve(),
        "policy_evidence": Path(
            sys.modules["policy_role_evidence"].__file__
        ).resolve(),
        "role_dataset_access": Path(
            sys.modules["role_dataset_access"].__file__
        ).resolve(),
        "v3_inference": Path(v3.__file__).resolve(),
        "win_first_policy": Path(
            sys.modules["win_first_policy_v4"].__file__
        ).resolve(),
    }
    return {
        name: {"bytes": path.stat().st_size, "sha256": v3._sha256(path)}
        for name, path in sorted(paths.items())
    }


_code_artifacts = selector_code_artifacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-dir", required=True, type=Path)
    parser.add_argument("--role-manifest", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--allow-incomplete-smoke", action="store_true")
    parser.add_argument("--chip-margin-grid", default="0,25,50,100,200,400")
    parser.add_argument("--hand-weight-grid", default="0.25,0.5,0.75")
    parser.add_argument("--tail-weight-grid", default="0,0.25")
    parser.add_argument("--response-weight-grid", default="0,0.05,0.1")
    parser.add_argument("--min-match-weight", type=float, default=0.25)
    parser.add_argument("--min-overrides", type=int, default=12)
    parser.add_argument("--min-selection-clusters", type=int, default=8)
    parser.add_argument("--min-override-clusters", type=int, default=8)
    parser.add_argument("--min-overrides-per-opponent", type=int, default=4)
    parser.add_argument("--min-override-hand-mean", type=float, default=0.0)
    parser.add_argument("--min-ci-lower", type=float, default=0.0)
    parser.add_argument(
        "--min-match-positive-rate-ci-lower", type=float, default=0.5
    )
    parser.add_argument(
        "--min-match-positive-uplift-ci-lower", type=float, default=0.0
    )
    parser.add_argument(
        "--min-opponent-match-positive-rate", type=float, default=0.5
    )
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260713)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args(argv)
    formal = not args.allow_incomplete_smoke
    try:
        chip_margins = v3._parse_grid(
            args.chip_margin_grid, field="chip margin grid"
        )
        hand_weights = v3._parse_grid(
            args.hand_weight_grid, field="hand weight grid"
        )
        tail_weights = v3._parse_grid(
            args.tail_weight_grid, field="tail weight grid"
        )
        response_weights = v3._parse_grid(
            args.response_weight_grid, field="response weight grid"
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if (
        not 0.0 <= args.min_match_weight <= 1.0
        or min(
            args.min_overrides,
            args.min_selection_clusters,
            args.min_override_clusters,
            args.min_overrides_per_opponent,
            args.bootstrap_samples,
            args.batch_size,
        ) < 1
        or not math.isfinite(args.min_ci_lower)
        or args.min_ci_lower < 0.0
        or not math.isfinite(args.min_override_hand_mean)
        or args.min_override_hand_mean < 0.0
    ):
        raise SystemExit("invalid v4 policy selection thresholds")
    if (
        not 0.5 <= args.min_match_positive_rate_ci_lower <= 1.0
        or not 0.0 <= args.min_match_positive_uplift_ci_lower <= 1.0
        or not 0.5 <= args.min_opponent_match_positive_rate <= 1.0
    ):
        raise SystemExit("win-first evidence thresholds cannot be weakened")
    if formal and (
        args.min_overrides < FORMAL_MIN_OVERRIDES
        or args.min_selection_clusters < FORMAL_MIN_SELECTION_CLUSTERS
        or args.min_override_clusters < FORMAL_MIN_OVERRIDE_CLUSTERS
        or args.min_overrides_per_opponent
        < FORMAL_MIN_OVERRIDES_PER_OPPONENT
        or args.bootstrap_samples < FORMAL_MIN_BOOTSTRAP_SAMPLES
    ):
        raise SystemExit("formal v4 policy selection coverage cannot be weakened")
    if str(args.device).startswith("cuda") and not torch.cuda.is_available():
        raise SystemExit("CUDA was requested but is unavailable")

    try:
        dataset = RoleDatasetAccess(
            args.role_manifest,
            ledger_path=args.ledger,
            run_id=args.run_id,
            require_complete=formal,
        )
        collection_boundary = (
            dataset.require_collection_boundary(FORMAL_COLLECTION_PASSES)
            if formal else None
        )
        calibrated = _validated_calibrated_ensemble(
            load_calibrated_ensemble(
                args.calibration_dir,
                dataset=dataset,
                run_id=args.run_id,
                device=args.device,
                formal=formal,
            ),
            dataset=dataset,
            run_id=args.run_id,
            formal=formal,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    policy_contract = {
        "schema": POLICY_SCHEMA,
        "selection_priority": SELECTION_PRIORITY,
        "min_positive_probability_lcb": MIN_POSITIVE_PROBABILITY_LCB,
        "min_probability_uplift_lcb": MIN_PROBABILITY_UPLIFT_LCB,
        "min_hand_lcb": MIN_HAND_LCB,
        "chip_margins": chip_margins,
        "hand_weights": hand_weights,
        "tail_weights": tail_weights,
        "response_weights": response_weights,
        "min_match_weight": args.min_match_weight,
        "outcome_aggregation": OUTCOME_AGGREGATION_METHOD,
        "outcome_uncertainty_std_weight": calibrated[
            "outcome_uncertainty_std_weight"
        ],
        "min_overrides": args.min_overrides,
        "min_selection_clusters": args.min_selection_clusters,
        "min_override_clusters": args.min_override_clusters,
        "min_overrides_per_opponent": args.min_overrides_per_opponent,
        "min_override_hand_mean": args.min_override_hand_mean,
        "min_cluster_ci_lower": args.min_ci_lower,
        "min_opponent_stratified_ci_lower": args.min_ci_lower,
        "min_match_positive_rate_ci_lower": (
            args.min_match_positive_rate_ci_lower
        ),
        "min_match_positive_uplift_ci_lower": (
            args.min_match_positive_uplift_ci_lower
        ),
        "min_opponent_match_positive_rate": (
            args.min_opponent_match_positive_rate
        ),
        "bootstrap_samples": args.bootstrap_samples,
        "bootstrap_seed": args.bootstrap_seed,
        "offline_estimand": POLICY_OFFLINE_ESTIMAND_V4,
    }
    if formal:
        policy_contract = validate_formal_policy_contract(
            policy_contract, calibrated=calibrated
        )
    out_dir = args.out_dir.resolve()
    if out_dir.exists():
        raise SystemExit(f"output directory already exists: {out_dir}")
    temporary = out_dir.with_name(f".{out_dir.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise SystemExit(f"temporary output directory already exists: {temporary}")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.mkdir()
    try:
        code_artifacts = selector_code_artifacts()
        runtime_context = dataset.runtime_context_contract()
        candidate_manifest = {
            "schema": POLICY_CANDIDATE_SCHEMA,
            "run_id": args.run_id,
            "role_manifest_sha256": dataset.manifest_sha256,
            "ensemble_manifest_sha256": calibrated["ensemble_manifest_sha256"],
            "calibration_artifact_manifest_sha256": calibrated[
                "artifact_manifest_sha256"
            ],
            "calibration_payload_sha256": calibrated[
                "calibration_payload_sha256"
            ],
            "member_checkpoint_sha256": [
                member["checkpoint_sha256"] for member in calibrated["members"]
            ],
            "outcome_calibration_payload_sha256": [
                calibration["payload_sha256"]
                for calibration in calibrated["outcome_calibrations"]
            ],
            "inference_contract": {
                "device": str(args.device),
                "batch_size": args.batch_size,
                "torch_version": torch.__version__,
                "value_aggregation": (
                    "mean_member_quantile_minus_mean_value_std_plus_calibration"
                ),
                "response_aggregation": "mean_member_logits_then_temperature",
                "outcome_aggregation": OUTCOME_AGGREGATION_METHOD,
            },
            "policy_contract": policy_contract,
            "code_artifacts": code_artifacts,
            "source_collection_complete": dataset.manifest.get(
                "source_collection_complete"
            ),
            "collection_boundary": collection_boundary,
            **runtime_context,
            "formal_selection": formal,
            "deployment_policy_value": False,
            "strength_evidence": False,
        }
        candidate_path = temporary / "candidate_manifest.json"
        v3._write_json(candidate_path, candidate_manifest)
        candidate_sha256 = v3._sha256(candidate_path)
        phase = open_policy_selection(
            dataset,
            candidate_sha256=candidate_sha256,
            calibration_payload_sha256=calibrated[
                "calibration_payload_sha256"
            ],
            contract=EVIDENCE_CONTRACT,
        )
        prepared_rows, preparation = prepare_policy_rows(
            phase["value"],
            calibrated,
            batch_size=args.batch_size,
            device=args.device,
        )
        selection = select_policy(
            prepared_rows,
            chip_margins=chip_margins,
            hand_weights=hand_weights,
            tail_weights=tail_weights,
            response_weights=response_weights,
            min_match_weight=args.min_match_weight,
            min_overrides=args.min_overrides,
            min_selection_clusters=args.min_selection_clusters,
            min_override_clusters=args.min_override_clusters,
            min_overrides_per_opponent=args.min_overrides_per_opponent,
            min_override_hand_mean=args.min_override_hand_mean,
            bootstrap_samples=args.bootstrap_samples,
            bootstrap_seed=args.bootstrap_seed,
            min_cluster_ci_lower=args.min_ci_lower,
            min_opponent_stratified_ci_lower=args.min_ci_lower,
            min_match_positive_rate_ci_lower=(
                args.min_match_positive_rate_ci_lower
            ),
            min_match_positive_uplift_ci_lower=(
                args.min_match_positive_uplift_ci_lower
            ),
            min_opponent_match_positive_rate=(
                args.min_opponent_match_positive_rate
            ),
        )
        evaluation = policy_evaluation(
            selection, incomplete_smoke=args.allow_incomplete_smoke
        )
        evaluation["preparation"] = preparation
        evaluation["policy_contract"] = policy_contract
        evaluation["code_artifacts"] = code_artifacts
        evaluation.update(runtime_context)
        evaluation_path = temporary / "policy_evaluation.json"
        v3._write_json(evaluation_path, evaluation)
        thresholds = {
            "min_overrides": args.min_overrides,
            "min_selection_clusters": args.min_selection_clusters,
            "min_override_clusters": args.min_override_clusters,
            "min_overrides_per_opponent": args.min_overrides_per_opponent,
            "min_override_hand_mean": args.min_override_hand_mean,
            "bootstrap_samples": args.bootstrap_samples,
            "min_cluster_ci_lower": args.min_ci_lower,
            "min_opponent_stratified_ci_lower": args.min_ci_lower,
            "min_match_positive_rate_ci_lower": (
                args.min_match_positive_rate_ci_lower
            ),
            "min_match_positive_uplift_ci_lower": (
                args.min_match_positive_uplift_ci_lower
            ),
            "min_opponent_match_positive_rate": (
                args.min_opponent_match_positive_rate
            ),
        }
        result = build_policy_selection_result(
            phase,
            evaluation,
            thresholds=thresholds,
            contract=EVIDENCE_CONTRACT,
        )
        result["source_collection_complete"] = dataset.manifest.get(
            "source_collection_complete"
        )
        result["formal_selection"] = formal
        result.update(runtime_context)
        if args.allow_incomplete_smoke:
            result["passed"] = False
            if "source_collection_incomplete" not in result["errors"]:
                result["errors"].append("source_collection_incomplete")
        result_path = temporary / "policy_selection_result.json"
        result_sha256 = write_selection_result(result_path, result)
        report = {
            "schema": POLICY_REPORT_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_id": args.run_id,
            "command": [sys.executable, *sys.argv],
            "role_manifest": str(args.role_manifest.resolve()),
            "role_manifest_sha256": dataset.manifest_sha256,
            "candidate_sha256": candidate_sha256,
            "calibration_payload_sha256": calibrated[
                "calibration_payload_sha256"
            ],
            "policy_selection_artifact_sha256": phase[
                "policy_selection_artifact_sha256"
            ],
            "opened_roles": ["policy_selection"],
            "policy_gate_opened": False,
            "policy_selection_opponents": phase["opponents"],
            "policy_selection_value_rows": len(phase["value"]),
            "policy_selection_behavior_rows": len(phase["behavior"]),
            "preparation": preparation,
            "grid_size": len(selection["grid"]),
            "provisional_selected_policy": evaluation.get(
                "provisional_selected_policy"
            ),
            "selected_policy": evaluation.get("selected_policy"),
            "selected_policy_sha256": result.get("selected_policy_sha256"),
            "selection_passed": result["passed"],
            "selection_errors": result["errors"],
            "selection_result_sha256": result_sha256,
            "code_artifacts": code_artifacts,
            **runtime_context,
            "source_collection_complete": dataset.manifest.get(
                "source_collection_complete"
            ),
            "incomplete_smoke": args.allow_incomplete_smoke,
            "deployment_policy_value": False,
            "strength_evidence": False,
            "native_tcp_evaluated": False,
        }
        v3._write_json(temporary / "policy_selection_report.json", report)
        files = (
            "candidate_manifest.json",
            "policy_evaluation.json",
            "policy_selection_result.json",
            "policy_selection_report.json",
        )
        v3._write_json(temporary / "artifact_manifest.json", {
            "schema": POLICY_ARTIFACT_SCHEMA,
            "run_id": args.run_id,
            "files": {
                name: {
                    "bytes": (temporary / name).stat().st_size,
                    "sha256": v3._sha256(temporary / name),
                }
                for name in files
            },
            "candidate_sha256": candidate_sha256,
            **runtime_context,
            "policy_gate_opened": False,
            "deployment_policy_value": False,
            "strength_evidence": False,
        })
        temporary.replace(out_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(json.dumps({
        "out_dir": str(out_dir),
        "candidate_sha256": candidate_sha256,
        "prepared_rows": preparation["prepared_rows"],
        "grid_size": len(selection["grid"]),
        "selection_passed": result["passed"],
        "policy_gate_opened": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }, indent=2, sort_keys=True))
    if args.allow_incomplete_smoke:
        return 0
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
