#!/usr/bin/env python3
"""Select a calibrated v3 ensemble policy on its protected evidence role."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
from typing import Any

import torch

from calibrate_opponent_multitask_v3_ensemble import (
    ARTIFACT_MANIFEST_SCHEMA as CALIBRATION_ARTIFACT_MANIFEST_SCHEMA,
    CALIBRATION_REPORT_SCHEMA,
    ENSEMBLE_CALIBRATION_SCHEMA,
    ENSEMBLE_MANIFEST_SCHEMA,
    verify_members,
)
from evaluate_multitask_offline_policy import (
    OFFLINE_ESTIMAND,
    select_offline_policy,
)
from feature_spec import LABELS, label_action
from match_outcome_schema import (
    MATCH_OUTCOME_ESTIMAND,
    candidate_outcome,
    derive_match_outcome_supervision,
    policy_outcome_context,
)
from multitask_training_data import (
    ENCODED_CONTEXT_SCHEMA,
    VALUE_FIELDS,
    encode_response_inference_row,
    encode_value_inference_row,
)
from opponent_multitask_batch_v3 import collate_inference_rows
from opponent_multitask_model_v3 import QUANTILE_LEVELS
from opponent_response_schema import OPPONENT_ACTION_LABELS
from policy_role_evidence import (
    build_policy_selection_result,
    open_policy_selection,
    write_selection_result,
)
from role_dataset_access import RoleDatasetAccess
from sampling_weights import decision_sampling_weight


POLICY_CANDIDATE_SCHEMA = "opponent_multitask_v3_policy_candidate_v2"
POLICY_EVALUATION_SCHEMA = "opponent_multitask_v3_policy_evaluation_v2"
POLICY_REPORT_SCHEMA = "opponent_multitask_v3_policy_selection_report_v2"
POLICY_ARTIFACT_SCHEMA = "opponent_multitask_v3_policy_artifacts_v2"
RESPONSE_SIGNAL_SCHEMA = "opponent_response_risk_signal_v2"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


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
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


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


def _parse_grid(raw: str, *, field: str, nonnegative: bool = True) -> list[float]:
    values = sorted({
        _finite(item.strip(), field=field)
        for item in str(raw).split(",")
        if item.strip()
    })
    if not values or (nonnegative and any(value < 0.0 for value in values)):
        raise ValueError(f"{field} must be a non-empty nonnegative grid")
    return values


def _verify_file_contracts(root: Path, artifact: dict[str, Any]) -> None:
    files = artifact.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("calibration artifact manifest has no files")
    for name, contract in files.items():
        path = root / str(name)
        if (
            not isinstance(contract, dict)
            or not path.is_file()
            or path.stat().st_size != int(contract.get("bytes", -1))
            or _sha256(path) != contract.get("sha256")
        ):
            raise ValueError(f"calibration artifact changed: {path}")


def _role_artifact_sha256(dataset: RoleDatasetAccess, role: str) -> str:
    contract = {
        filename: dataset.outputs[filename]["sha256"]
        for filename in (f"cf_{role}.jsonl", f"opponent_actions_{role}.jsonl")
    }
    return _canonical_sha256(contract)


def load_calibrated_ensemble(
    calibration_dir: Path,
    *,
    dataset: RoleDatasetAccess,
    run_id: str,
    device: torch.device | str,
    formal: bool,
) -> dict[str, Any]:
    """Verify every calibration binding before loading frozen members."""
    root = calibration_dir.resolve()
    artifact = _load_json(root / "artifact_manifest.json", field="artifact manifest")
    if (
        artifact.get("schema") != CALIBRATION_ARTIFACT_MANIFEST_SCHEMA
        or artifact.get("run_id") != run_id
        or artifact.get("deployment_policy_value") is not False
        or artifact.get("strength_evidence") is not False
    ):
        raise ValueError("invalid ensemble calibration artifact manifest")
    _verify_file_contracts(root, artifact)

    ensemble_path = root / "ensemble_checkpoint_manifest.json"
    calibration_path = root / "calibration.json"
    report_path = root / "calibration_report.json"
    ensemble = _load_json(ensemble_path, field="ensemble manifest")
    calibration = _load_json(calibration_path, field="ensemble calibration")
    report = _load_json(report_path, field="calibration report")
    ensemble_sha256 = _sha256(ensemble_path)
    calibration_file_sha256 = _sha256(calibration_path)
    calibration_payload = dict(calibration)
    payload_sha256 = str(calibration_payload.pop("payload_sha256", ""))
    selected = ensemble.get("selected_configuration")
    members_raw = ensemble.get("members")
    if (
        ensemble.get("schema") != ENSEMBLE_MANIFEST_SCHEMA
        or ensemble.get("role_manifest_sha256") != dataset.manifest_sha256
        or ensemble.get("strength_evidence") is not False
        or not isinstance(selected, dict)
        or not isinstance(members_raw, list)
        or not members_raw
        or calibration.get("schema") != ENSEMBLE_CALIBRATION_SCHEMA
        or calibration.get("run_id") != run_id
        or calibration.get("role_manifest_sha256") != dataset.manifest_sha256
        or calibration.get("checkpoint_sha256") != ensemble_sha256
        or calibration.get("policy_evidence_used") is not False
        or calibration.get("deployment_policy_value") is not False
        or calibration.get("strength_evidence") is not False
        or payload_sha256 != _canonical_sha256(calibration_payload)
        or report.get("schema") != CALIBRATION_REPORT_SCHEMA
        or report.get("run_id") != run_id
        or report.get("role_manifest_sha256") != dataset.manifest_sha256
        or report.get("ensemble_manifest_sha256") != ensemble_sha256
        or report.get("calibration_payload_sha256") != payload_sha256
        or report.get("opened_roles")
        != ["train", "early_stop", "model_calibration"]
        or report.get("policy_roles_opened") is not False
        or report.get("deployment_policy_value") is not False
        or report.get("strength_evidence") is not False
    ):
        raise ValueError("ensemble calibration bindings are invalid")

    ensemble_contract = calibration.get("ensemble")
    if (
        not isinstance(ensemble_contract, dict)
        or ensemble_contract.get("manifest_sha256") != ensemble_sha256
        or ensemble_contract.get("value_lower_aggregation")
        != "mean_member_quantile_minus_mean_value_std"
        or ensemble_contract.get("response_aggregation")
        != "mean_member_logits_then_temperature"
    ):
        raise ValueError("unsupported ensemble aggregation contract")
    lower_quantile = _finite(
        ensemble_contract.get("lower_quantile"), field="lower quantile"
    )
    if lower_quantile not in QUANTILE_LEVELS:
        raise ValueError("calibrated lower quantile is unsupported")
    uncertainty_std_weight = _finite(
        ensemble_contract.get("uncertainty_std_weight"),
        field="uncertainty_std_weight",
    )
    if uncertainty_std_weight < 0.0:
        raise ValueError("uncertainty_std_weight must be nonnegative")

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
        raise ValueError("invalid value calibration contract")
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
    response_temperature = calibration.get("response_temperature")
    if not isinstance(response_temperature, dict):
        raise ValueError("response temperature calibration is missing")
    temperature = _positive(
        response_temperature.get("temperature"), field="response temperature"
    )

    expected_calibration_artifact = _role_artifact_sha256(
        dataset, "model_calibration"
    )
    if calibration.get("calibration_artifact_sha256") != expected_calibration_artifact:
        raise ValueError("calibration role artifact changed")
    if formal and (
        ensemble.get("source_collection_complete") is not True
        or report.get("source_collection_complete") is not True
        or report.get("formal_selection") is not True
        or report.get("incomplete_smoke") is not False
        or len(members_raw) < 3
    ):
        raise ValueError("formal policy selection requires a complete ensemble")

    scale = str(selected.get("scale", ""))
    encoder = str(selected.get("encoder", ""))
    run_rows = [
        {
            "seed": int(member["seed"]),
            "output_dir": member["output_dir"],
            "checkpoint_sha256": member["checkpoint_sha256"],
            "scale": scale,
            "encoder": encoder,
        }
        for member in members_raw
    ]
    members = verify_members(
        run_rows,
        role_manifest_sha256=dataset.manifest_sha256,
        device=device,
        formal=formal,
    )
    member_contract = [
        {"seed": member["seed"], "checkpoint_sha256": member["checkpoint_sha256"]}
        for member in members
    ]
    if (
        member_contract != ensemble_contract.get("members")
        or [member["checkpoint_sha256"] for member in members]
        != report.get("member_checkpoint_sha256")
    ):
        raise ValueError("ensemble member binding changed")
    return {
        "root": root,
        "artifact_manifest_sha256": _sha256(root / "artifact_manifest.json"),
        "ensemble": ensemble,
        "ensemble_manifest_sha256": ensemble_sha256,
        "calibration": calibration,
        "calibration_file_sha256": calibration_file_sha256,
        "calibration_report_sha256": _sha256(report_path),
        "calibration_payload_sha256": payload_sha256,
        "members": members,
        "models": [member["model"] for member in members],
        "clips": normalized_clips,
        "offsets": offsets,
        "lower_quantile": lower_quantile,
        "uncertainty_std_weight": uncertainty_std_weight,
        "response_temperature": temperature,
    }


def _chunks(length: int, batch_size: int) -> list[list[int]]:
    return [
        list(range(start, min(length, start + batch_size)))
        for start in range(0, length, batch_size)
    ]


def aggregate_value_predictions(
    models: list[Any],
    rows: list[dict[str, Any]],
    *,
    clips: dict[str, float],
    offsets: dict[str, list[float]],
    lower_quantile: float,
    uncertainty_std_weight: float,
    batch_size: int,
    device: torch.device | str,
) -> list[dict[str, dict[str, list[float]]]]:
    lower_index = QUANTILE_LEVELS.index(lower_quantile)
    result: list[dict[str, dict[str, list[float]]]] = []
    for model in models:
        model.eval()
    with torch.no_grad():
        for indices in _chunks(len(rows), batch_size):
            selected = [rows[index] for index in indices]
            batch = collate_inference_rows(
                selected, response=False, device=device
            )
            outputs = [model.forward_value(**batch["inputs"]) for model in models]
            batch_rows = [dict() for _ in selected]
            for field in VALUE_FIELDS:
                member_mean = torch.stack([
                    output[field]["mean"] * clips[field] for output in outputs
                ])
                member_lower = torch.stack([
                    output[field]["quantiles"][:, :, lower_index] * clips[field]
                    for output in outputs
                ])
                mean = member_mean.mean(dim=0)
                lower = (
                    member_lower.mean(dim=0)
                    - uncertainty_std_weight
                    * member_mean.std(dim=0, unbiased=False)
                    + torch.tensor(
                        offsets[field], dtype=torch.float32, device=device
                    ).unsqueeze(0)
                )
                for row_index in range(len(selected)):
                    batch_rows[row_index][field] = {
                        "mean": mean[row_index].cpu().tolist(),
                        "lower": lower[row_index].cpu().tolist(),
                    }
            result.extend(batch_rows)
    return result


def _response_summary(
    logits: torch.Tensor,
    size: torch.Tensor,
    legal: torch.Tensor,
    *,
    temperature: float,
) -> dict[str, Any]:
    allowed = legal.bool()
    scaled = logits / temperature
    scaled = scaled.masked_fill(~allowed, -1.0e9)
    probabilities = torch.softmax(scaled, dim=0)
    legal_count = int(allowed.sum().item())
    entropy = -float(
        (probabilities[allowed] * probabilities[allowed].clamp_min(1.0e-12).log())
        .sum()
        .item()
    )
    normalized_entropy = (
        entropy / math.log(legal_count) if legal_count > 1 else 0.0
    )
    return {
        "probabilities": {
            label: float(probabilities[index].item())
            for index, label in enumerate(OPPONENT_ACTION_LABELS)
        },
        "normalized_entropy": normalized_entropy,
        "aggressive_increment_pot_log": float(size[0].item()),
        "aggressive_stack_fraction": float(size[1].item()),
    }


def _response_signal(
    response: dict[str, Any], row: dict[str, Any], action: int
) -> float:
    probabilities = response["probabilities"]
    state = row.get("state") or {}
    request = row.get("request") or {}
    pot = max(1.0, float(state.get("pot", request.get("pot", 150)) or 150))
    my_bet = max(
        0.0,
        float(state.get("my_round_bet", request.get("my_stage_bet", 0)) or 0),
    )
    stack = max(0.0, float(request.get("my_chips", 20_000) or 20_000))
    opponent_stack = max(
        0.0, float(request.get("opponent_chips", 20_000) or 20_000)
    )
    committed = stack if action == -2 else max(0.0, float(action) - my_bet)
    fold_gain = float(probabilities["fold"]) * pot
    aggression = float(probabilities["raise"]) + float(
        probabilities["allin"]
    )
    predicted_raise = (
        float(response["aggressive_stack_fraction"]) * opponent_stack
    )
    aggression_risk = aggression * min(
        stack, max(pot, committed, predicted_raise)
    )
    entropy_penalty = 0.25 * float(response["normalized_entropy"]) * pot
    return fold_gain - aggression_risk - entropy_penalty


def aggregate_response_predictions(
    models: list[Any],
    jobs: list[dict[str, Any]],
    *,
    temperature: float,
    batch_size: int,
    device: torch.device | str,
) -> None:
    for model in models:
        model.eval()
    with torch.no_grad():
        for indices in _chunks(len(jobs), batch_size):
            selected = [jobs[index] for index in indices]
            batch = collate_inference_rows(
                [job["encoded"] for job in selected],
                response=True,
                device=device,
            )
            outputs = [model.forward_response(**batch["inputs"]) for model in models]
            logits = torch.stack([output["logits"] for output in outputs]).mean(0)
            sizes = torch.stack([output["size"] for output in outputs]).mean(0)
            legal = batch["inputs"]["legal_action_mask"]
            for row_index, job in enumerate(selected):
                response = _response_summary(
                    logits[row_index],
                    sizes[row_index],
                    legal[row_index],
                    temperature=temperature,
                )
                candidate = job["candidate"]
                candidate["response_signal"] = _response_signal(
                    response, job["source"], candidate["action"]
                )
                candidate["response_prediction"] = response


def prepare_policy_rows(
    raw_rows: list[dict[str, Any]],
    calibrated: dict[str, Any],
    *,
    batch_size: int,
    device: torch.device | str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    encoded = [encode_value_inference_row(row) for row in raw_rows]
    predictions = aggregate_value_predictions(
        calibrated["models"],
        encoded,
        clips=calibrated["clips"],
        offsets=calibrated["offsets"],
        lower_quantile=calibrated["lower_quantile"],
        uncertainty_std_weight=calibrated["uncertainty_std_weight"],
        batch_size=batch_size,
        device=device,
    )
    prepared = []
    response_jobs = []
    skipped_unconfirmed = skipped_no_alternative = 0
    for source_row_index, (raw, context, values) in enumerate(
        zip(raw_rows, encoded, predictions, strict=True)
    ):
        match_supervision = derive_match_outcome_supervision(raw, required=True)
        match_outcome = policy_outcome_context(match_supervision)
        rule_id = int(context["rule_label_id"])
        candidates = []
        for probe in raw.get("probes") or []:
            if probe.get("status") != "ok" or probe.get("force_confirmed") is not True:
                skipped_unconfirmed += 1
                continue
            label_name = str(probe.get("forced_label", ""))
            if label_name not in LABELS:
                raise ValueError("confirmed probe has an unknown forced label")
            label_id = LABELS.index(label_name)
            if label_id == rule_id:
                continue
            if not context["legal_action_mask"][label_id]:
                raise ValueError("confirmed probe selected an illegal value action")
            action = int(probe.get("forced_action"))
            if label_action(action, dict(raw.get("request") or {})) != label_id:
                raise ValueError("confirmed probe action and label disagree")
            candidate = {
                "label_id": label_id,
                "label": label_name,
                "action": action,
                "response_signal": 0.0,
                "response_prediction": None,
                "hand_delta": _finite(
                    probe.get("delta_vs_rule"), field="probe hand delta"
                ),
                "tail_delta": _finite(
                    probe.get("tail_delta_vs_rule"), field="probe tail delta"
                ),
                "match_delta": _finite(
                    probe.get("match_delta_vs_rule"), field="probe match delta"
                ),
            }
            candidate.update(candidate_outcome(match_supervision, label_id))
            candidates.append(candidate)
            response_source = dict(raw)
            response_source["hero_action"] = action
            response_source["hero_action_label_id"] = label_id
            response_context = encode_response_inference_row(response_source)
            if response_context is not None:
                response_jobs.append({
                    "encoded": response_context,
                    "source": raw,
                    "candidate": candidate,
                })
        if not candidates:
            skipped_no_alternative += 1
            continue
        opponent = str(raw.get("_opponent_label") or raw.get("opponent") or "")
        if not opponent:
            raise ValueError("policy row is missing opponent")
        prepared.append({
            "source_row_index": source_row_index,
            "opponent": opponent,
            "cluster": "|".join((
                opponent,
                str(raw.get("deck_seed_base")),
                str(raw.get("bot_seed_base")),
            )),
            "rule_id": rule_id,
            "sampling_weight": decision_sampling_weight(raw),
            "match_outcome": match_outcome,
            "decision": {
                key: raw.get(key)
                for key in (
                    "deck_seed_base",
                    "bot_seed_base",
                    "hand",
                    "stage",
                    "hand_decision_index",
                    "decision_serial",
                    "rule_label",
                    "rule_final",
                    "rule_value",
                )
            },
            "values": values,
            "candidates": candidates,
        })
    aggregate_response_predictions(
        calibrated["models"],
        response_jobs,
        temperature=calibrated["response_temperature"],
        batch_size=batch_size,
        device=device,
    )
    return prepared, {
        "source_rows": len(raw_rows),
        "prepared_rows": len(prepared),
        "response_predictions": len(response_jobs),
        "skipped_unconfirmed_probes": skipped_unconfirmed,
        "skipped_rows_without_alternative": skipped_no_alternative,
        "match_outcome_rows": len(prepared),
        "match_outcome_estimand": MATCH_OUTCOME_ESTIMAND,
    }


def _best_diagnostic(grid: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(
        grid,
        key=lambda result: (
            result[
                "match_positive_rate_opponent_stratified_cluster_ci"
            ]["lower"],
            result["match_positive_rate_cluster_bootstrap_ci"]["lower"],
            result[
                "match_positive_uplift_opponent_stratified_cluster_ci"
            ]["lower"],
            result["match_positive_uplift_cluster_bootstrap_ci"]["lower"],
            result["match_positive_rate"],
            result["match_opponent_stratified_cluster_ci"]["lower"],
            result["match_cluster_bootstrap_mean_ci"]["lower"],
            result["match_mean_per_opportunity"],
            result["override_clusters"],
            -result["negative_override_rate"],
            -result["config"]["margin"],
        ),
    ) if grid else None


def policy_evaluation(
    selection: dict[str, Any], *, incomplete_smoke: bool
) -> dict[str, Any]:
    selected = selection.get("selected")
    diagnostic = selected or _best_diagnostic(selection.get("grid") or [])
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
        "offline_estimand": OFFLINE_ESTIMAND,
        "match_outcome_estimand": MATCH_OUTCOME_ESTIMAND,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "selected_policy": None if incomplete_smoke else provisional,
        "provisional_selected_policy": provisional if incomplete_smoke else None,
        "source_collection_complete": not incomplete_smoke,
        "grid_size": len(selection.get("grid") or []),
        "selection_failure": selection.get("selection_failure"),
    })
    return evaluation


def _code_artifacts() -> dict[str, dict[str, Any]]:
    module_names = {
        "batch": "opponent_multitask_batch_v3",
        "calibration_loader": "calibrate_opponent_multitask_v3_ensemble",
        "feature_spec": "feature_spec",
        "model": "opponent_multitask_model_v3",
        "match_outcome": "match_outcome_schema",
        "offline_policy": "evaluate_multitask_offline_policy",
        "opponent_response": "opponent_response_schema",
        "policy_evidence": "policy_role_evidence",
        "role_dataset_access": "role_dataset_access",
        "sampling_weights": "sampling_weights",
        "training_data": "multitask_training_data",
    }
    modules = {"selector": Path(__file__).resolve()}
    modules.update({
        name: Path(sys.modules[module_name].__file__).resolve()
        for name, module_name in module_names.items()
    })
    return {
        name: {"bytes": path.stat().st_size, "sha256": _sha256(path)}
        for name, path in sorted(modules.items())
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-dir", required=True, type=Path)
    parser.add_argument("--role-manifest", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--allow-incomplete-smoke", action="store_true")
    parser.add_argument("--margin-grid", default="0,25,50,100,200,400")
    parser.add_argument("--hand-weight-grid", default="0.25,0.5,0.75")
    parser.add_argument("--tail-weight-grid", default="0,0.25")
    parser.add_argument("--response-weight-grid", default="0,0.05,0.1")
    parser.add_argument("--min-match-weight", type=float, default=0.25)
    parser.add_argument("--min-hand-lcb", type=float, default=0.0)
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
    parser.add_argument("--bootstrap-seed", type=int, default=20260711)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args(argv)
    try:
        margins = _parse_grid(args.margin_grid, field="margin grid")
        hand_weights = _parse_grid(args.hand_weight_grid, field="hand weight grid")
        tail_weights = _parse_grid(args.tail_weight_grid, field="tail weight grid")
        response_weights = _parse_grid(
            args.response_weight_grid, field="response weight grid"
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if (
        not 0.0 <= args.min_match_weight <= 1.0
        or not math.isfinite(args.min_hand_lcb)
        or min(
            args.min_overrides,
            args.min_selection_clusters,
            args.min_override_clusters,
            args.min_overrides_per_opponent,
            args.bootstrap_samples,
            args.batch_size,
        ) < 1
    ):
        raise SystemExit("invalid policy selection thresholds")
    if (
        not 0.5 <= args.min_match_positive_rate_ci_lower <= 1.0
        or not 0.0 <= args.min_match_positive_uplift_ci_lower <= 1.0
        or not 0.5 <= args.min_opponent_match_positive_rate <= 1.0
    ):
        raise SystemExit("win-first thresholds cannot be weakened")
    if str(args.device).startswith("cuda") and not torch.cuda.is_available():
        raise SystemExit("CUDA was requested but is unavailable")

    try:
        dataset = RoleDatasetAccess(
            args.role_manifest,
            ledger_path=args.ledger,
            run_id=args.run_id,
            require_complete=not args.allow_incomplete_smoke,
        )
        calibrated = load_calibrated_ensemble(
            args.calibration_dir,
            dataset=dataset,
            run_id=args.run_id,
            device=args.device,
            formal=not args.allow_incomplete_smoke,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    policy_contract = {
        "margins": margins,
        "hand_weights": hand_weights,
        "tail_weights": tail_weights,
        "response_weights": response_weights,
        "min_match_weight": args.min_match_weight,
        "min_hand_lcb": args.min_hand_lcb,
        "min_overrides": args.min_overrides,
        "min_selection_clusters": args.min_selection_clusters,
        "min_override_clusters": args.min_override_clusters,
        "min_overrides_per_opponent": args.min_overrides_per_opponent,
        "min_override_hand_mean": args.min_override_hand_mean,
        "min_cluster_ci_lower": args.min_ci_lower,
        "min_opponent_stratified_ci_lower": args.min_ci_lower,
        "require_win_first": True,
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
        "offline_estimand": OFFLINE_ESTIMAND,
        "response_signal_schema": RESPONSE_SIGNAL_SCHEMA,
    }
    out_dir = args.out_dir.resolve()
    if out_dir.exists():
        raise SystemExit(f"output directory already exists: {out_dir}")
    temporary = out_dir.with_name(f".{out_dir.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise SystemExit(f"temporary output directory already exists: {temporary}")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.mkdir()
    try:
        candidate_manifest = {
            "schema": POLICY_CANDIDATE_SCHEMA,
            "run_id": args.run_id,
            "role_manifest_sha256": dataset.manifest_sha256,
            "ensemble_manifest_sha256": calibrated["ensemble_manifest_sha256"],
            "calibration_artifact_manifest_sha256": calibrated[
                "artifact_manifest_sha256"
            ],
            "calibration_file_sha256": calibrated["calibration_file_sha256"],
            "calibration_report_sha256": calibrated[
                "calibration_report_sha256"
            ],
            "calibration_payload_sha256": calibrated[
                "calibration_payload_sha256"
            ],
            "member_checkpoint_sha256": [
                member["checkpoint_sha256"] for member in calibrated["members"]
            ],
            "encoded_context_schema": ENCODED_CONTEXT_SCHEMA,
            "inference_contract": {
                "device": str(args.device),
                "batch_size": args.batch_size,
                "torch_version": torch.__version__,
                "lower_quantile": calibrated["lower_quantile"],
                "uncertainty_std_weight": calibrated[
                    "uncertainty_std_weight"
                ],
                "response_temperature": calibrated["response_temperature"],
                "value_aggregation": (
                    "mean_member_quantile_minus_mean_value_std_plus_calibration"
                ),
                "response_aggregation": (
                    "mean_member_logits_then_temperature"
                ),
            },
            "policy_contract": policy_contract,
            "code_artifacts": _code_artifacts(),
            "source_collection_complete": dataset.manifest.get(
                "source_collection_complete"
            ),
            "formal_selection": not args.allow_incomplete_smoke,
            "deployment_policy_value": False,
            "strength_evidence": False,
        }
        candidate_path = temporary / "candidate_manifest.json"
        _write_json(candidate_path, candidate_manifest)
        candidate_sha256 = _sha256(candidate_path)

        phase = open_policy_selection(
            dataset,
            candidate_sha256=candidate_sha256,
            calibration_payload_sha256=calibrated[
                "calibration_payload_sha256"
            ],
        )
        prepared_rows, preparation = prepare_policy_rows(
            phase["value"],
            calibrated,
            batch_size=args.batch_size,
            device=args.device,
        )
        selection = select_offline_policy(
            prepared_rows,
            margins=margins,
            hand_weights=hand_weights,
            tail_weights=tail_weights,
            response_weights=response_weights,
            min_match_weight=args.min_match_weight,
            min_hand_lcb=args.min_hand_lcb,
            min_overrides=args.min_overrides,
            min_selection_clusters=args.min_selection_clusters,
            min_override_clusters=args.min_override_clusters,
            min_overrides_per_opponent=args.min_overrides_per_opponent,
            min_override_hand_mean=args.min_override_hand_mean,
            require_nonnegative_opponent_mean=True,
            bootstrap_samples=args.bootstrap_samples,
            bootstrap_seed=args.bootstrap_seed,
            min_cluster_ci_lower=args.min_ci_lower,
            min_opponent_stratified_ci_lower=args.min_ci_lower,
            require_win_first=True,
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
        evaluation_path = temporary / "policy_evaluation.json"
        _write_json(evaluation_path, evaluation)
        thresholds = {
            "min_overrides": args.min_overrides,
            "min_override_clusters": args.min_override_clusters,
            "min_overrides_per_opponent": args.min_overrides_per_opponent,
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
            phase, evaluation, thresholds=thresholds
        )
        result["source_collection_complete"] = dataset.manifest.get(
            "source_collection_complete"
        )
        result["formal_selection"] = not args.allow_incomplete_smoke
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
            "selection_passed": result["passed"],
            "selection_errors": result["errors"],
            "selection_result_sha256": result_sha256,
            "source_collection_complete": dataset.manifest.get(
                "source_collection_complete"
            ),
            "incomplete_smoke": args.allow_incomplete_smoke,
            "deployment_policy_value": False,
            "strength_evidence": False,
            "native_tcp_evaluated": False,
        }
        report_path = temporary / "policy_selection_report.json"
        _write_json(report_path, report)
        files = (
            "candidate_manifest.json",
            "policy_evaluation.json",
            "policy_selection_result.json",
            "policy_selection_report.json",
        )
        _write_json(temporary / "artifact_manifest.json", {
            "schema": POLICY_ARTIFACT_SCHEMA,
            "run_id": args.run_id,
            "files": {
                name: {
                    "bytes": (temporary / name).stat().st_size,
                    "sha256": _sha256(temporary / name),
                }
                for name in files
            },
            "candidate_sha256": candidate_sha256,
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
        "strength_evidence": False,
    }, indent=2, sort_keys=True))
    if args.allow_incomplete_smoke:
        return 0
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
