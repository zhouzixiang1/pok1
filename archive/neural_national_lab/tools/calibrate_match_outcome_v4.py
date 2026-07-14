#!/usr/bin/env python3
"""Fit protected probability calibration for one frozen v4 checkpoint."""
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
import torch.nn.functional as F

from feature_spec import LABELS
from match_outcome_calibration import (
    CALIBRATION_METHOD,
    CALIBRATION_SCHEMA,
    calibration_payload_sha256,
    calibration_parameters,
)
from multitask_training_data import (
    MODEL_TRAINING_ROLES,
    MULTITASK_TRAINING_DATA_SCHEMA,
    encode_prepared_row,
    prepare_model_calibration,
)
from opponent_multitask_batch_v4 import collate_encoded_rows
from opponent_multitask_model_v4 import MODEL_FORMAT
from role_dataset_access import RoleDatasetAccess
from train_opponent_multitask_v4 import load_checkpoint
import train_opponent_multitask_v3 as v3


REPORT_SCHEMA = "match_outcome_calibration_report_v1"
ARTIFACT_SCHEMA = "match_outcome_calibration_artifacts_v1"
FORMAL_COLLECTION_PASSES = 160


def _weighted_mean(values: torch.Tensor, weights: torch.Tensor) -> torch.Tensor:
    total = weights.sum()
    if not bool(total > 0.0):
        raise ValueError("calibration weights must be positive")
    return (values * weights).sum() / total


def fit_probability_calibration(
    logits: torch.Tensor,
    targets: torch.Tensor,
    weights: torch.Tensor,
    *,
    steps: int,
    learning_rate: float,
    l2: float,
) -> dict[str, Any]:
    logits = logits.detach().float().cpu().reshape(-1)
    targets = targets.detach().float().cpu().reshape(-1)
    weights = weights.detach().float().cpu().reshape(-1)
    if not (len(logits) == len(targets) == len(weights)) or len(logits) < 2:
        raise ValueError("calibration tensors have incompatible lengths")
    if not bool(torch.isfinite(logits).all()) or not bool(torch.isfinite(weights).all()):
        raise ValueError("calibration tensors must be finite")
    if not bool((weights > 0.0).all()):
        raise ValueError("calibration weights must be positive")
    if not bool(((targets == 0.0) | (targets == 1.0)).all()):
        raise ValueError("calibration targets must be binary")
    if len(torch.unique(targets)) != 2:
        raise ValueError("calibration data must contain both outcome classes")
    if steps < 1 or learning_rate <= 0.0 or l2 < 0.0:
        raise ValueError("invalid probability calibration configuration")

    log_scale = torch.zeros((), requires_grad=True)
    bias = torch.zeros((), requires_grad=True)
    optimizer = torch.optim.Adam([log_scale, bias], lr=learning_rate)
    best = None
    history = []
    for step in range(1, steps + 1):
        scale = torch.exp(log_scale.clamp(-4.0, 4.0))
        calibrated = scale * logits + bias
        nll = _weighted_mean(
            F.binary_cross_entropy_with_logits(
                calibrated, targets, reduction="none"
            ),
            weights,
        )
        penalty = l2 * (log_scale.square() + bias.square())
        loss = nll + penalty
        value = float(loss.detach().item())
        if not math.isfinite(value):
            raise RuntimeError("probability calibration became non-finite")
        if best is None or value < best[0]:
            best = (
                value,
                float(log_scale.detach().clamp(-4.0, 4.0).item()),
                float(bias.detach().item()),
                step,
            )
        if step == 1 or step == steps or step % 50 == 0:
            history.append({
                "step": step,
                "objective": value,
                "nll": float(nll.detach().item()),
            })
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        optimizer.step()
    if best is None:
        raise RuntimeError("probability calibration did not produce parameters")
    result = {
        "schema": CALIBRATION_SCHEMA,
        "method": CALIBRATION_METHOD,
        "scale": math.exp(best[1]),
        "bias": best[2],
        "fit": {
            "optimizer": "adam",
            "steps": steps,
            "learning_rate": learning_rate,
            "l2": l2,
            "best_step": best[3],
            "history": history,
        },
    }
    calibration_parameters(result)
    return result


def probability_metrics(
    logits: torch.Tensor,
    targets: torch.Tensor,
    weights: torch.Tensor,
    *,
    scale: float,
    bias: float,
    bins: int = 10,
) -> dict[str, Any]:
    logits = logits.float().cpu().reshape(-1)
    targets = targets.float().cpu().reshape(-1)
    weights = weights.float().cpu().reshape(-1)
    if not (len(logits) == len(targets) == len(weights)) or len(logits) < 1:
        raise ValueError("metric tensors have incompatible lengths")
    if bins < 1 or not bool(torch.isfinite(logits).all()):
        raise ValueError("metric configuration is invalid")
    if not bool(torch.isfinite(weights).all()) or not bool((weights > 0.0).all()):
        raise ValueError("metric weights must be finite and positive")
    if not bool(((targets == 0.0) | (targets == 1.0)).all()):
        raise ValueError("metric targets must be binary")
    calibrated = scale * logits + bias
    probabilities = torch.sigmoid(calibrated)
    nll = _weighted_mean(
        F.binary_cross_entropy_with_logits(
            calibrated, targets, reduction="none"
        ),
        weights,
    )
    brier = _weighted_mean((probabilities - targets).square(), weights)
    predicted = probabilities >= 0.5
    class_accuracy = []
    for target in (0.0, 1.0):
        selected = targets == target
        selected_weight = weights * selected.float()
        class_accuracy.append(
            float(
                _weighted_mean(
                    (predicted == bool(target)).float(), selected_weight
                ).item()
            )
            if bool(selected.any()) else None
        )
    valid_accuracy = [value for value in class_accuracy if value is not None]
    ece = 0.0
    total_weight = float(weights.sum().item())
    bin_rows = []
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        selected = (probabilities >= lower) & (
            probabilities <= upper if index == bins - 1 else probabilities < upper
        )
        selected_weight = weights * selected.float()
        weight = float(selected_weight.sum().item())
        if weight <= 0.0:
            continue
        confidence = float(
            _weighted_mean(probabilities, selected_weight).item()
        )
        frequency = float(_weighted_mean(targets, selected_weight).item())
        ece += weight / total_weight * abs(confidence - frequency)
        bin_rows.append({
            "lower": lower,
            "upper": upper,
            "weight": weight,
            "confidence": confidence,
            "positive_rate": frequency,
        })
    return {
        "nll": float(nll.item()),
        "brier": float(brier.item()),
        "balanced_accuracy": (
            sum(valid_accuracy) / len(valid_accuracy) if valid_accuracy else None
        ),
        "class_accuracy": {
            "nonpositive": class_accuracy[0],
            "positive": class_accuracy[1],
        },
        "ece": ece,
        "bins": bin_rows,
        "effective_weight": total_weight,
        "observations": len(logits),
    }


def collect_predictions(
    model: Any,
    rows: list[dict[str, Any]],
    *,
    batch_size: int,
    device: torch.device | str,
) -> dict[str, Any]:
    logits = []
    targets = []
    weights = []
    action_ids = []
    model.eval()
    with torch.no_grad():
        for indices in v3._chunks(list(range(len(rows))), batch_size):
            selected = [rows[index] for index in indices]
            batch = collate_encoded_rows(
                selected, response=False, device=device
            )
            output = model.forward_match_outcome(**batch["inputs"])
            target = batch["supervision"]["match_positive_targets"]
            mask = batch["supervision"]["match_positive_target_mask"].bool()
            row_weight = batch["supervision"]["row_weight"].unsqueeze(1)
            expanded_weight = row_weight.expand_as(target)
            action = torch.arange(len(LABELS), device=target.device).unsqueeze(0)
            action = action.expand_as(target)
            logits.append(output[mask].cpu())
            targets.append(target[mask].cpu())
            weights.append(expanded_weight[mask].cpu())
            action_ids.append(action[mask].cpu())
    if not logits:
        raise ValueError("model calibration role has no value rows")
    result = {
        "logits": torch.cat(logits),
        "targets": torch.cat(targets),
        "weights": torch.cat(weights),
        "action_ids": torch.cat(action_ids),
        "source_rows": len(rows),
    }
    if len(result["logits"]) < 1:
        raise ValueError("model calibration role has no outcome supervision")
    return result


def _training_phase_from_checkpoint(
    dataset: RoleDatasetAccess, checkpoint: dict[str, Any]
) -> dict[str, Any]:
    artifacts = checkpoint.get("training_artifact_sha256")
    if not isinstance(artifacts, dict) or set(artifacts) != set(MODEL_TRAINING_ROLES):
        raise ValueError("checkpoint training artifacts are invalid")
    roles = {}
    for role in MODEL_TRAINING_ROLES:
        expected = dataset._role_artifact_sha256(role)
        if artifacts.get(role) != expected:
            raise ValueError("checkpoint training artifact does not match role data")
        roles[role] = {"provenance": {"artifact_sha256": expected}}
    return {
        "schema": MULTITASK_TRAINING_DATA_SCHEMA,
        "phase": "training",
        "run_id": dataset.run_id,
        "role_manifest_sha256": dataset.manifest_sha256,
        "opened_roles": list(MODEL_TRAINING_ROLES),
        "roles": roles,
    }


def _load_json(path: Path, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {field}: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--checkpoint-authorization", required=True, type=Path)
    parser.add_argument("--role-manifest", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--allow-incomplete-smoke", action="store_true")
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--steps", type=int, default=500)
    parser.add_argument("--learning-rate", type=float, default=0.05)
    parser.add_argument("--l2", type=float, default=1.0e-4)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args(argv)
    if (
        min(args.batch_size, args.steps) < 1
        or args.learning_rate <= 0.0
        or args.l2 < 0.0
    ):
        raise SystemExit("invalid outcome calibration configuration")
    if str(args.device).startswith("cuda") and not torch.cuda.is_available():
        raise SystemExit("CUDA was requested but is unavailable")

    out_dir = args.out_dir.resolve()
    if out_dir.exists():
        raise SystemExit(f"output directory already exists: {out_dir}")
    temporary = out_dir.with_name(f".{out_dir.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise SystemExit(f"temporary output directory already exists: {temporary}")

    checkpoint_path = args.checkpoint.resolve()
    checkpoint_sha256 = v3._sha256(checkpoint_path)
    try:
        dataset = RoleDatasetAccess(
            args.role_manifest,
            ledger_path=args.ledger,
            run_id=args.run_id,
            require_complete=not args.allow_incomplete_smoke,
        )
        if not args.allow_incomplete_smoke:
            dataset.require_collection_boundary(FORMAL_COLLECTION_PASSES)
        model, checkpoint = load_checkpoint(
            checkpoint_path, device=args.device
        )
        authorization = _load_json(
            args.checkpoint_authorization.resolve(),
            field="checkpoint authorization",
        )
        if (
            checkpoint.get("role_manifest_sha256") != dataset.manifest_sha256
            or authorization.get("checkpoint_sha256") != checkpoint_sha256
            or authorization.get("run_id") != args.run_id
            or authorization.get("role_manifest_sha256")
            != dataset.manifest_sha256
        ):
            raise ValueError("checkpoint is not bound to this calibration run")
        training_phase = _training_phase_from_checkpoint(dataset, checkpoint)
        calibration_phase = prepare_model_calibration(
            dataset, training_phase, authorization
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    role = calibration_phase["roles"]["model_calibration"]
    encoded = [
        encode_prepared_row(row, response=False) for row in role["value"]
    ]
    observations = collect_predictions(
        model, encoded, batch_size=args.batch_size, device=args.device
    )
    calibration = fit_probability_calibration(
        observations["logits"],
        observations["targets"],
        observations["weights"],
        steps=args.steps,
        learning_rate=args.learning_rate,
        l2=args.l2,
    )
    before = probability_metrics(
        observations["logits"],
        observations["targets"],
        observations["weights"],
        scale=1.0,
        bias=0.0,
    )
    after = probability_metrics(
        observations["logits"],
        observations["targets"],
        observations["weights"],
        scale=float(calibration["scale"]),
        bias=float(calibration["bias"]),
    )
    action_counts = {
        label: int((observations["action_ids"] == index).sum().item())
        for index, label in enumerate(LABELS)
    }
    incomplete = dataset.manifest.get("source_collection_complete") is not True
    calibration.update({
        "run_id": args.run_id,
        "model_format": MODEL_FORMAT,
        "checkpoint_sha256": checkpoint_sha256,
        "role_manifest_sha256": dataset.manifest_sha256,
        "model_calibration_artifact_sha256": role["provenance"][
            "artifact_sha256"
        ],
        "model_calibration_opponents": list(role["opponents"]),
        "source_collection_complete": not incomplete,
        "metrics": {"before": before, "after": after},
        "action_observations": action_counts,
        "deployment_policy_value": False,
        "strength_evidence": False,
    })
    calibration["payload_sha256"] = calibration_payload_sha256(calibration)

    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.mkdir()
    try:
        calibration_path = temporary / "outcome_calibration.json"
        v3._write_json(calibration_path, calibration)
        report = {
            "schema": REPORT_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_id": args.run_id,
            "command": [sys.executable, *sys.argv],
            "checkpoint": str(checkpoint_path),
            "checkpoint_sha256": checkpoint_sha256,
            "role_manifest": str(args.role_manifest.resolve()),
            "role_manifest_sha256": dataset.manifest_sha256,
            "ledger": str(args.ledger.resolve()),
            "opened_roles": ["model_calibration"],
            "policy_roles_opened": False,
            "source_rows": observations["source_rows"],
            "observations": len(observations["logits"]),
            "action_observations": action_counts,
            "parameters": {
                "scale": calibration["scale"],
                "bias": calibration["bias"],
            },
            "metrics": calibration["metrics"],
            "source_collection_complete": not incomplete,
            "incomplete_smoke": incomplete,
            "calibration_payload_sha256": calibration["payload_sha256"],
            "deployment_policy_value": False,
            "strength_evidence": False,
        }
        report_path = temporary / "calibration_report.json"
        v3._write_json(report_path, report)
        files = ("outcome_calibration.json", "calibration_report.json")
        v3._write_json(temporary / "artifact_manifest.json", {
            "schema": ARTIFACT_SCHEMA,
            "run_id": args.run_id,
            "files": {
                name: {
                    "bytes": (temporary / name).stat().st_size,
                    "sha256": v3._sha256(temporary / name),
                }
                for name in files
            },
            "checkpoint_sha256": checkpoint_sha256,
            "calibration_payload_sha256": calibration["payload_sha256"],
            "source_collection_complete": not incomplete,
            "deployment_policy_value": False,
            "strength_evidence": False,
        })
        temporary.replace(out_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(json.dumps({
        "out_dir": str(out_dir),
        "scale": calibration["scale"],
        "bias": calibration["bias"],
        "before_nll": before["nll"],
        "after_nll": after["nll"],
        "before_ece": before["ece"],
        "after_ece": after["ece"],
        "source_collection_complete": not incomplete,
        "strength_evidence": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
