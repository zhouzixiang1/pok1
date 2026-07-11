#!/usr/bin/env python3
"""Calibrate the selected multi-seed v3 ensemble on its protected role."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

import torch

from feature_spec import LABELS
from multitask_calibration import (
    build_calibration_artifact,
    calibrate_response_temperature,
    calibrate_value_lower_offsets,
)
from multitask_training_data import (
    VALUE_FIELDS,
    encode_prepared_row,
    prepare_model_calibration,
    prepare_training_phase,
)
from opponent_multitask_batch_v3 import collate_encoded_rows
from opponent_multitask_model_v3 import QUANTILE_LEVELS
from role_dataset_access import RoleDatasetAccess
from run_opponent_multitask_v3_scaling import SUMMARY_SCHEMA
from train_opponent_multitask_v3 import (
    REPORT_SCHEMA as TRAINING_REPORT_SCHEMA,
    checkpoint_authorization,
    load_checkpoint,
)


ENSEMBLE_MANIFEST_SCHEMA = "opponent_multitask_v3_ensemble_checkpoint_v1"
ENSEMBLE_CALIBRATION_SCHEMA = "opponent_multitask_ensemble_calibration_v1"
CALIBRATION_REPORT_SCHEMA = "opponent_multitask_ensemble_calibration_report_v1"
ARTIFACT_MANIFEST_SCHEMA = "opponent_multitask_ensemble_artifacts_v1"
TRAINING_ARTIFACT_SCHEMA = "opponent_multitask_training_artifacts_v1"


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


def selected_scaling_runs(
    summary: dict[str, Any], *, allow_incomplete_smoke: bool
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if (
        summary.get("schema") != SUMMARY_SCHEMA
        or summary.get("model_calibration_opened") is not False
        or summary.get("policy_roles_opened") is not False
        or summary.get("strength_evidence") is not False
    ):
        raise ValueError("invalid v3 scaling summary")
    if allow_incomplete_smoke:
        selected = summary.get("provisional_best_configuration")
    else:
        if summary.get("selection_eligible") is not True:
            raise ValueError("scaling summary is not eligible for formal calibration")
        selected = summary.get("selected_configuration")
    if not isinstance(selected, dict):
        raise ValueError("scaling summary has no selected configuration")
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
        raise ValueError("selected scaling runs do not cover every requested seed")
    if not allow_incomplete_smoke and len(runs) < 3:
        raise ValueError("formal ensemble calibration requires at least three seeds")
    if len({row.get("checkpoint_sha256") for row in runs}) != len(runs):
        raise ValueError("selected scaling runs reuse a checkpoint")
    return selected, sorted(runs, key=lambda row: int(row["seed"]))


def _verified_member(
    row: dict[str, Any],
    *,
    role_manifest_sha256: str,
    device: torch.device | str,
) -> dict[str, Any]:
    root = Path(str(row.get("output_dir", ""))).resolve()
    artifact = _load_json(root / "artifact_manifest.json", field="artifact manifest")
    if artifact.get("schema") != TRAINING_ARTIFACT_SCHEMA:
        raise ValueError("member has the wrong training artifact schema")
    for name, contract in (artifact.get("files") or {}).items():
        path = root / name
        if (
            not isinstance(contract, dict)
            or not path.is_file()
            or path.stat().st_size != int(contract.get("bytes", -1))
            or _sha256(path) != contract.get("sha256")
        ):
            raise ValueError(f"member artifact changed: {path}")
    report = _load_json(root / "training_report.json", field="training report")
    checkpoint_path = root / "checkpoint.pt"
    checkpoint_sha256 = _sha256(checkpoint_path)
    if (
        report.get("schema") != TRAINING_REPORT_SCHEMA
        or report.get("opened_roles") != ["train", "early_stop"]
        or report.get("model_calibration_opened") is not False
        or report.get("policy_roles_opened") is not False
        or report.get("strength_evidence") is not False
        or report.get("role_manifest_sha256") != role_manifest_sha256
        or report.get("checkpoint_sha256") != checkpoint_sha256
        or row.get("checkpoint_sha256") != checkpoint_sha256
        or (report.get("model") or {}).get("scale") != row.get("scale")
        or (report.get("model") or {}).get("cross_encoder")
        != row.get("encoder")
        or int((report.get("config") or {}).get("seed", -1)) != int(row["seed"])
    ):
        raise ValueError("member training report is not selection-safe")
    authorization = _load_json(
        root / "checkpoint_authorization.json", field="checkpoint authorization"
    )
    if (
        authorization.get("checkpoint_sha256") != checkpoint_sha256
        or authorization.get("role_manifest_sha256") != role_manifest_sha256
    ):
        raise ValueError("member checkpoint authorization does not match")
    model, checkpoint = load_checkpoint(checkpoint_path, device=device)
    if checkpoint.get("role_manifest_sha256") != role_manifest_sha256:
        raise ValueError("member checkpoint belongs to another role dataset")
    return {
        "seed": int(row["seed"]),
        "run_id": report["run_id"],
        "output_dir": str(root),
        "checkpoint_path": str(checkpoint_path),
        "checkpoint_sha256": checkpoint_sha256,
        "early_stop_score": report["early_stop"]["selection_score"],
        "model_metadata": report["model"],
        "training_config": report["config"],
        "training_artifact_sha256": checkpoint["training_artifact_sha256"],
        "code_artifacts": checkpoint["code_artifacts"],
        "source_collection_complete": report["source_collection_complete"],
        "model": model,
    }


def verify_members(
    rows: list[dict[str, Any]],
    *,
    role_manifest_sha256: str,
    device: torch.device | str,
    formal: bool,
) -> list[dict[str, Any]]:
    members = [
        _verified_member(
            row,
            role_manifest_sha256=role_manifest_sha256,
            device=device,
        )
        for row in rows
    ]
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
            or member["model_metadata"]["scale"]
            != first["model_metadata"]["scale"]
            or member["model_metadata"]["cross_encoder"]
            != first["model_metadata"]["cross_encoder"]
        ):
            raise ValueError("ensemble members do not share one training contract")
    if formal:
        if any(
            member["training_config"].get("seed") is None for member in members
        ):
            raise ValueError("formal ensemble member is missing its seed")
        if any(
            member["source_collection_complete"] is not True
            for member in members
        ):
            raise ValueError("formal ensemble member used incomplete source data")
    return members


def _encoded_calibration_role(
    payload: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    return {
        "value": [
            encode_prepared_row(row, response=False) for row in payload["value"]
        ],
        "behavior": [
            encode_prepared_row(row, response=True) for row in payload["behavior"]
        ],
    }


def _chunks(length: int, batch_size: int) -> list[list[int]]:
    return [
        list(range(start, min(length, start + batch_size)))
        for start in range(0, length, batch_size)
    ]


def ensemble_calibration_predictions(
    models: list[Any],
    role: dict[str, list[dict[str, Any]]],
    *,
    clips: dict[str, float],
    batch_size: int,
    device: torch.device | str,
    lower_quantile: float,
    uncertainty_std_weight: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    lower_index = QUANTILE_LEVELS.index(lower_quantile)
    value_observations = []
    response_rows = []
    disagreement = {
        field: {"weighted_sum": 0.0, "weight": 0.0}
        for field in VALUE_FIELDS
    }
    response_disagreement = {"weighted_sum": 0.0, "weight": 0.0}
    for model in models:
        model.eval()
    with torch.no_grad():
        for indices in _chunks(len(role["value"]), batch_size):
            rows = [role["value"][index] for index in indices]
            batch = collate_encoded_rows(rows, response=False, device=device)
            outputs = [model.forward_value(**batch["inputs"]) for model in models]
            for field in VALUE_FIELDS:
                member_lower = torch.stack([
                    output[field]["quantiles"][:, :, lower_index] * clips[field]
                    for output in outputs
                ])
                member_mean = torch.stack([
                    output[field]["mean"] * clips[field] for output in outputs
                ])
                epistemic_std = member_mean.std(dim=0, unbiased=False)
                aggregate_lower = (
                    member_lower.mean(dim=0)
                    - uncertainty_std_weight * epistemic_std
                )
                for row_index, row in enumerate(rows):
                    rule_id = int(row["rule_label_id"])
                    for action_id, observed in enumerate(
                        row["value_target_masks"][field]
                    ):
                        if not observed or action_id == rule_id:
                            continue
                        target = max(
                            -clips[field],
                            min(
                                clips[field],
                                float(row["value_targets"][field][action_id]),
                            ),
                        )
                        weight = float(row["row_weight"])
                        value_observations.append({
                            "field": field,
                            "action_id": action_id,
                            "residual": target
                            - float(aggregate_lower[row_index, action_id].item()),
                            "weight": weight,
                            "opponent": row["opponent"],
                        })
                        disagreement[field]["weighted_sum"] += (
                            float(epistemic_std[row_index, action_id].item()) * weight
                        )
                        disagreement[field]["weight"] += weight
        for indices in _chunks(len(role["behavior"]), batch_size):
            rows = [role["behavior"][index] for index in indices]
            batch = collate_encoded_rows(rows, response=True, device=device)
            outputs = [model.forward_response(**batch["inputs"]) for model in models]
            member_logits = torch.stack([output["logits"] for output in outputs])
            aggregate_logits = member_logits.mean(dim=0)
            legal = batch["inputs"]["legal_action_mask"].bool()
            logit_std = member_logits.std(dim=0, unbiased=False)
            for row_index, row in enumerate(rows):
                weight = float(row["row_weight"])
                legal_std = logit_std[row_index][legal[row_index]]
                response_disagreement["weighted_sum"] += (
                    float(legal_std.mean().item()) * weight
                )
                response_disagreement["weight"] += weight
                response_rows.append({
                    "logits": aggregate_logits[row_index].cpu().tolist(),
                    "legal_action_mask": row["response_legal_action_mask"],
                    "target": row["response_target"],
                    "weight": weight,
                    "opponent": row["opponent"],
                })
    diagnostics = {
        "value_mean_epistemic_std": {
            field: item["weighted_sum"] / item["weight"]
            if item["weight"] > 0.0
            else None
            for field, item in disagreement.items()
        },
        "response_legal_logit_epistemic_std": (
            response_disagreement["weighted_sum"]
            / response_disagreement["weight"]
            if response_disagreement["weight"] > 0.0
            else None
        ),
    }
    return value_observations, response_rows, diagnostics


def _ensemble_calibration_artifact(
    base: dict[str, Any],
    *,
    ensemble_manifest_sha256: str,
    members: list[dict[str, Any]],
    lower_quantile: float,
    uncertainty_std_weight: float,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(base)
    payload.pop("payload_sha256", None)
    payload["schema"] = ENSEMBLE_CALIBRATION_SCHEMA
    payload["ensemble"] = {
        "manifest_sha256": ensemble_manifest_sha256,
        "members": [
            {
                "seed": member["seed"],
                "checkpoint_sha256": member["checkpoint_sha256"],
            }
            for member in members
        ],
        "value_lower_aggregation": "mean_member_quantile_minus_mean_value_std",
        "lower_quantile": lower_quantile,
        "uncertainty_std_weight": uncertainty_std_weight,
        "response_aggregation": "mean_member_logits_then_temperature",
        "diagnostics": diagnostics,
    }
    payload["deployment_policy_value"] = False
    payload["strength_evidence"] = False
    return {**payload, "payload_sha256": _canonical_sha256(payload)}


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
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--min-rows-per-action", type=int, default=20)
    parser.add_argument("--min-ess-per-action", type=float, default=10.0)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args(argv)
    if args.lower_quantile not in QUANTILE_LEVELS:
        raise SystemExit("lower quantile must be a model quantile")
    if (
        args.uncertainty_std_weight < 0.0
        or args.batch_size < 1
        or args.min_rows_per_action < 1
        or args.min_ess_per_action <= 0.0
    ):
        raise SystemExit("invalid ensemble calibration thresholds")
    if str(args.device).startswith("cuda") and not torch.cuda.is_available():
        raise SystemExit("CUDA was requested but is unavailable")
    summary = _load_json(args.scaling_summary, field="scaling summary")
    try:
        selected, run_rows = selected_scaling_runs(
            summary, allow_incomplete_smoke=args.allow_incomplete_smoke
        )
        dataset = RoleDatasetAccess(
            args.role_manifest,
            ledger_path=args.ledger,
            run_id=args.run_id,
            require_complete=not args.allow_incomplete_smoke,
        )
        members = verify_members(
            run_rows,
            role_manifest_sha256=dataset.manifest_sha256,
            device=args.device,
            formal=not args.allow_incomplete_smoke,
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
        ensemble_manifest = {
            "schema": ENSEMBLE_MANIFEST_SCHEMA,
            "role_manifest_sha256": dataset.manifest_sha256,
            "scaling_summary_sha256": _sha256(args.scaling_summary),
            "selected_configuration": selected,
            "members": [
                {
                    key: member[key]
                    for key in (
                        "seed",
                        "run_id",
                        "output_dir",
                        "checkpoint_sha256",
                        "early_stop_score",
                    )
                }
                for member in members
            ],
            "lower_quantile": args.lower_quantile,
            "uncertainty_std_weight": args.uncertainty_std_weight,
            "response_aggregation": "mean_member_logits",
            "source_collection_complete": dataset.manifest.get(
                "source_collection_complete"
            ),
            "strength_evidence": False,
        }
        ensemble_path = temporary / "ensemble_checkpoint_manifest.json"
        _write_json(ensemble_path, ensemble_manifest)
        ensemble_sha256 = _sha256(ensemble_path)
        training_phase = prepare_training_phase(dataset)
        authorization = checkpoint_authorization(
            dataset,
            training_phase,
            checkpoint_sha256=ensemble_sha256,
        )
        _write_json(temporary / "checkpoint_authorization.json", authorization)
        calibration_phase = prepare_model_calibration(
            dataset, training_phase, authorization
        )
        role = _encoded_calibration_role(
            calibration_phase["roles"]["model_calibration"]
        )
        observations, response_rows, diagnostics = (
            ensemble_calibration_predictions(
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
            observations,
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
        calibration = _ensemble_calibration_artifact(
            base,
            ensemble_manifest_sha256=ensemble_sha256,
            members=members,
            lower_quantile=args.lower_quantile,
            uncertainty_std_weight=args.uncertainty_std_weight,
            diagnostics=diagnostics,
        )
        _write_json(temporary / "calibration.json", calibration)
        report = {
            "schema": CALIBRATION_REPORT_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_id": args.run_id,
            "command": [sys.executable, *sys.argv],
            "scaling_summary": str(args.scaling_summary.resolve()),
            "role_manifest": str(args.role_manifest.resolve()),
            "role_manifest_sha256": dataset.manifest_sha256,
            "ensemble_manifest_sha256": ensemble_sha256,
            "selected_configuration": selected,
            "formal_selection": not args.allow_incomplete_smoke,
            "member_checkpoint_sha256": [
                member["checkpoint_sha256"] for member in members
            ],
            "opened_roles": ["train", "early_stop", "model_calibration"],
            "policy_roles_opened": False,
            "source_collection_complete": dataset.manifest.get(
                "source_collection_complete"
            ),
            "incomplete_smoke": args.allow_incomplete_smoke,
            "value_observations": len(observations),
            "response_rows": len(response_rows),
            "diagnostics": diagnostics,
            "calibration_tool_sha256": _sha256(Path(__file__)),
            "calibration_payload_sha256": calibration["payload_sha256"],
            "deployment_policy_value": False,
            "strength_evidence": False,
            "native_tcp_evaluated": False,
        }
        _write_json(temporary / "calibration_report.json", report)
        files = (
            "ensemble_checkpoint_manifest.json",
            "checkpoint_authorization.json",
            "calibration.json",
            "calibration_report.json",
        )
        _write_json(temporary / "artifact_manifest.json", {
            "schema": ARTIFACT_MANIFEST_SCHEMA,
            "run_id": args.run_id,
            "files": {
                name: {
                    "bytes": (temporary / name).stat().st_size,
                    "sha256": _sha256(temporary / name),
                }
                for name in files
            },
            "calibration_tool_sha256": _sha256(Path(__file__)),
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
        "strength_evidence": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
