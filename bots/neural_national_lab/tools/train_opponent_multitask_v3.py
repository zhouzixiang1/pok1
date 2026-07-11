#!/usr/bin/env python3
"""Train v3 on role-isolated data without opening policy evidence roles."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import shutil
import subprocess
import sys
from typing import Any

import torch
import torch.nn.functional as F

from feature_spec import LABELS
from multitask_calibration import (
    build_calibration_artifact,
    calibrate_response_temperature,
    calibrate_value_lower_offsets,
)
from multitask_training_data import (
    FROZEN_CHECKPOINT_SCHEMA,
    MODEL_TRAINING_ROLES,
    VALUE_FIELDS,
    combine_model_development,
    encode_prepared_row,
    prepare_model_calibration,
    prepare_training_phase,
    training_data_metadata,
)
from opponent_multitask_batch_v3 import collate_encoded_rows
from opponent_multitask_model_v3 import (
    MODEL_SCALES,
    QUANTILE_LEVELS,
    OpponentAwareMultiTaskNetV3,
)
from opponent_response_schema import OPPONENT_ACTION_LABELS
from role_dataset_access import RoleDatasetAccess


TRAINER_SCHEMA = "opponent_multitask_trainer_v3"
CHECKPOINT_SCHEMA = "opponent_multitask_torch_checkpoint_v3"
REPORT_SCHEMA = "opponent_multitask_training_report_v3"
ARTIFACT_MANIFEST_SCHEMA = "opponent_multitask_training_artifacts_v1"
DEFAULT_CLIPS = {
    "delta_vs_rule": 2_000.0,
    "tail_delta_vs_rule": 2_000.0,
    "match_delta_vs_rule": 2_000.0,
}
DEFAULT_FIELD_WEIGHTS = {
    "delta_vs_rule": 1.0,
    "tail_delta_vs_rule": 0.5,
    "match_delta_vs_rule": 1.0,
}
CODE_MODULES = (
    "multitask_calibration",
    "multitask_training_data",
    "opponent_multitask_batch_v3",
    "opponent_multitask_model_v3",
    "opponent_profile_schema",
    "role_dataset_access",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _git_commit() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and len(value) == 40 else None


def _code_artifacts() -> dict[str, dict[str, Any]]:
    paths = {"train_opponent_multitask_v3": Path(__file__).resolve()}
    for module_name in CODE_MODULES:
        module = sys.modules.get(module_name)
        raw_path = getattr(module, "__file__", None)
        if raw_path is None:
            raise RuntimeError(f"training code module is not loaded: {module_name}")
        paths[module_name] = Path(raw_path).resolve()
    return {
        name: {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for name, path in sorted(paths.items())
    }


def _seed_everything(seed: int) -> None:
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    torch.use_deterministic_algorithms(True, warn_only=True)
    if hasattr(torch.backends, "cudnn"):
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True


def _weighted_mean(values: torch.Tensor, weights: torch.Tensor) -> torch.Tensor:
    total = weights.sum()
    if not bool(total > 0.0):
        return values.sum() * 0.0
    return (values * weights).sum() / total


def _chunks(order: list[int], batch_size: int) -> list[list[int]]:
    return [
        order[start : start + batch_size]
        for start in range(0, len(order), batch_size)
    ]


def _encoded_role(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        "value": [
            encode_prepared_row(row, response=False) for row in payload["value"]
        ],
        "behavior": [
            encode_prepared_row(row, response=True) for row in payload["behavior"]
        ],
    }


def _response_class_weights(
    rows: list[dict[str, Any]], *, device: torch.device | str
) -> torch.Tensor:
    counts = [0.0] * len(OPPONENT_ACTION_LABELS)
    for row in rows:
        counts[int(row["response_target"])] += float(row["row_weight"])
    present = [value for value in counts if value > 0.0]
    total = sum(present)
    weights = [
        math.sqrt(total / (len(present) * value)) if value > 0.0 else 0.0
        for value in counts
    ]
    mean = sum(value for value in weights if value > 0.0) / max(1, len(present))
    weights = [value / mean if value > 0.0 else 0.0 for value in weights]
    return torch.tensor(weights, dtype=torch.float32, device=device)


def value_objective(
    output: dict[str, dict[str, torch.Tensor]],
    batch: dict[str, Any],
    *,
    clips: dict[str, float],
    field_weights: dict[str, float],
    mean_weight: float,
    quantile_weight: float,
    ranking_weight: float,
    lower_ranking_weight: float,
    ranking_margin: float,
    ranking_temperature: float,
) -> tuple[torch.Tensor, dict[str, float]]:
    supervision = batch["supervision"]
    row_weight = supervision["row_weight"].unsqueeze(1)
    quantiles = torch.tensor(
        QUANTILE_LEVELS,
        dtype=torch.float32,
        device=row_weight.device,
    ).view(1, 1, -1)
    total = output[VALUE_FIELDS[0]]["mean"].sum() * 0.0
    metrics: dict[str, float] = {}
    for field in VALUE_FIELDS:
        target = supervision["targets"][field]
        mask = supervision["target_masks"][field]
        weights = row_weight * mask
        normalized = target.clamp(-clips[field], clips[field]) / clips[field]
        mean_loss = _weighted_mean(
            F.smooth_l1_loss(
                output[field]["mean"], normalized, reduction="none"
            ),
            weights,
        )
        error = normalized.unsqueeze(2) - output[field]["quantiles"]
        pinball = torch.maximum(quantiles * error, (quantiles - 1.0) * error)
        quantile_loss = _weighted_mean(
            pinball, weights.unsqueeze(2).expand_as(pinball)
        )
        field_loss = mean_weight * mean_loss + quantile_weight * quantile_loss
        total = total + field_weights[field] * field_loss
        metrics[f"{field}.mean"] = float(mean_loss.detach().item())
        metrics[f"{field}.quantile"] = float(quantile_loss.detach().item())

    match_target = supervision["targets"]["match_delta_vs_rule"]
    valid = supervision["target_masks"]["match_delta_vs_rule"].bool()
    rule_ids = batch["inputs"]["rule_action"].argmax(dim=1)
    action_ids = torch.arange(len(LABELS), device=match_target.device).unsqueeze(0)
    valid &= action_ids != rule_ids.unsqueeze(1)
    valid &= match_target.abs() > ranking_margin
    ranking_weights = row_weight.expand_as(match_target)[valid]
    if bool(valid.any()):
        labels = (match_target[valid] > 0.0).float()

        def ranking_loss(prediction: torch.Tensor) -> torch.Tensor:
            raw = F.binary_cross_entropy_with_logits(
                prediction[valid] / ranking_temperature,
                labels,
                reduction="none",
            )
            return _weighted_mean(raw, ranking_weights)

        mean_ranking = ranking_loss(output["match_delta_vs_rule"]["mean"])
        lower_index = QUANTILE_LEVELS.index(0.20)
        lower_ranking = ranking_loss(
            output["match_delta_vs_rule"]["quantiles"][:, :, lower_index]
        )
    else:
        mean_ranking = total * 0.0
        lower_ranking = total * 0.0
    total = (
        total
        + ranking_weight * mean_ranking
        + lower_ranking_weight * lower_ranking
    )
    metrics["match_ranking"] = float(mean_ranking.detach().item())
    metrics["match_q20_ranking"] = float(lower_ranking.detach().item())
    return total, metrics


def response_objective(
    output: dict[str, torch.Tensor],
    batch: dict[str, Any],
    *,
    class_weights: torch.Tensor,
    size_weight: float,
) -> tuple[torch.Tensor, dict[str, float]]:
    supervision = batch["supervision"]
    row_weight = supervision["row_weight"]
    action_loss = _weighted_mean(
        F.cross_entropy(
            output["logits"],
            supervision["target"],
            weight=class_weights,
            reduction="none",
        ),
        row_weight,
    )
    size_mask = supervision["size_target_mask"]
    size_loss = _weighted_mean(
        F.smooth_l1_loss(
            output["size"], supervision["size_targets"], reduction="none"
        ),
        row_weight.unsqueeze(1) * size_mask,
    )
    return action_loss + size_weight * size_loss, {
        "response.action": float(action_loss.detach().item()),
        "response.size": float(size_loss.detach().item()),
    }


def _weighted_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0.0 else None


def _balanced_accuracy(classes: list[dict[str, float]]) -> float | None:
    values = [
        item["correct"] / item["weight"]
        for item in classes
        if item["weight"] > 0.0
    ]
    return sum(values) / len(values) if values else None


def evaluate_model(
    model: OpponentAwareMultiTaskNetV3,
    role: dict[str, list[dict[str, Any]]],
    *,
    clips: dict[str, float],
    batch_size: int,
    device: torch.device | str,
    ranking_margin: float,
) -> dict[str, Any]:
    model.eval()
    value_stats = {
        field: {
            "absolute": 0.0,
            "raw_absolute": 0.0,
            "weight": 0.0,
            "direction": [
                {"correct": 0.0, "weight": 0.0},
                {"correct": 0.0, "weight": 0.0},
            ],
            "coverage": [
                {"covered": 0.0, "weight": 0.0}
                for _ in QUANTILE_LEVELS
            ],
        }
        for field in VALUE_FIELDS
    }
    response_stats = {
        "nll": 0.0,
        "weight": 0.0,
        "classes": [
            {"correct": 0.0, "weight": 0.0}
            for _ in OPPONENT_ACTION_LABELS
        ],
        "size_absolute": 0.0,
        "size_weight": 0.0,
    }
    with torch.no_grad():
        for indices in _chunks(list(range(len(role["value"]))), batch_size):
            rows = [role["value"][index] for index in indices]
            batch = collate_encoded_rows(rows, response=False, device=device)
            output = model.forward_value(**batch["inputs"])
            row_weight = batch["supervision"]["row_weight"].unsqueeze(1)
            rule = batch["inputs"]["rule_action"].bool()
            for field in VALUE_FIELDS:
                target = batch["supervision"]["targets"][field]
                valid = batch["supervision"]["target_masks"][field].bool() & ~rule
                weights = row_weight.expand_as(target) * valid.float()
                prediction = output[field]["mean"] * clips[field]
                clipped_target = target.clamp(-clips[field], clips[field])
                stats = value_stats[field]
                stats["absolute"] += float(
                    ((prediction - clipped_target).abs() * weights).sum().item()
                )
                stats["raw_absolute"] += float(
                    ((prediction - target).abs() * weights).sum().item()
                )
                stats["weight"] += float(weights.sum().item())
                direction_valid = valid & (target.abs() > ranking_margin)
                for class_index, positive in enumerate((False, True)):
                    selected = direction_valid & ((target > 0.0) == positive)
                    selected_weight = row_weight.expand_as(target) * selected.float()
                    correct = (prediction > 0.0) == positive
                    stats["direction"][class_index]["correct"] += float(
                        (correct.float() * selected_weight).sum().item()
                    )
                    stats["direction"][class_index]["weight"] += float(
                        selected_weight.sum().item()
                    )
                for quantile_index, _ in enumerate(QUANTILE_LEVELS):
                    quantile_prediction = (
                        output[field]["quantiles"][:, :, quantile_index]
                        * clips[field]
                    )
                    stats["coverage"][quantile_index]["covered"] += float(
                        (
                            (clipped_target <= quantile_prediction).float()
                            * weights
                        ).sum().item()
                    )
                    stats["coverage"][quantile_index]["weight"] += float(
                        weights.sum().item()
                    )

        for indices in _chunks(list(range(len(role["behavior"]))), batch_size):
            rows = [role["behavior"][index] for index in indices]
            batch = collate_encoded_rows(rows, response=True, device=device)
            output = model.forward_response(**batch["inputs"])
            target = batch["supervision"]["target"]
            weights = batch["supervision"]["row_weight"]
            nll = F.cross_entropy(output["logits"], target, reduction="none")
            response_stats["nll"] += float((nll * weights).sum().item())
            response_stats["weight"] += float(weights.sum().item())
            predicted = output["logits"].argmax(dim=1)
            for class_index in range(len(OPPONENT_ACTION_LABELS)):
                selected = target == class_index
                selected_weight = weights * selected.float()
                response_stats["classes"][class_index]["correct"] += float(
                    ((predicted == target).float() * selected_weight).sum().item()
                )
                response_stats["classes"][class_index]["weight"] += float(
                    selected_weight.sum().item()
                )
            size_weights = (
                weights.unsqueeze(1) * batch["supervision"]["size_target_mask"]
            )
            response_stats["size_absolute"] += float(
                (
                    (output["size"] - batch["supervision"]["size_targets"]).abs()
                    * size_weights
                ).sum().item()
            )
            response_stats["size_weight"] += float(size_weights.sum().item())

    value_report = {}
    coverage_errors = []
    for field, stats in value_stats.items():
        coverages = []
        for index, item in enumerate(stats["coverage"]):
            coverage = _weighted_ratio(item["covered"], item["weight"])
            coverages.append(coverage)
            if coverage is not None:
                coverage_errors.append(abs(coverage - QUANTILE_LEVELS[index]))
        value_report[field] = {
            "clipped_mae": _weighted_ratio(
                stats["absolute"], stats["weight"]
            ),
            "raw_mae_diagnostic": _weighted_ratio(
                stats["raw_absolute"], stats["weight"]
            ),
            "normalized_mae": _weighted_ratio(
                stats["absolute"], stats["weight"] * clips[field]
            ),
            "direction_balanced_accuracy": _balanced_accuracy(stats["direction"]),
            "quantile_coverage": {
                str(level): coverages[index]
                for index, level in enumerate(QUANTILE_LEVELS)
            },
            "effective_weight": stats["weight"],
        }
    response_balanced = _balanced_accuracy(response_stats["classes"])
    response_nll = _weighted_ratio(
        response_stats["nll"], response_stats["weight"]
    )
    response_report = {
        "nll": response_nll,
        "balanced_accuracy": response_balanced,
        "size_mae": _weighted_ratio(
            response_stats["size_absolute"], response_stats["size_weight"]
        ),
        "per_class": {
            label: {
                "accuracy": _weighted_ratio(item["correct"], item["weight"]),
                "effective_weight": item["weight"],
            }
            for label, item in zip(
                OPPONENT_ACTION_LABELS, response_stats["classes"], strict=True
            )
        },
    }
    normalized_mae = [
        report["normalized_mae"]
        for report in value_report.values()
        if report["normalized_mae"] is not None
    ]
    match_direction = value_report["match_delta_vs_rule"][
        "direction_balanced_accuracy"
    ]
    score_terms = {
        "value_normalized_mae": sum(normalized_mae) / len(normalized_mae)
        if normalized_mae
        else 2.0,
        "match_direction_penalty": 1.0 - match_direction
        if match_direction is not None
        else 1.0,
        "response_nll": min(5.0, response_nll)
        if response_nll is not None
        else 5.0,
        "response_balanced_penalty": 1.0 - response_balanced
        if response_balanced is not None
        else 1.0,
        "quantile_calibration_error": sum(coverage_errors) / len(coverage_errors)
        if coverage_errors
        else 1.0,
    }
    selection_score = (
        score_terms["value_normalized_mae"]
        + 0.50 * score_terms["match_direction_penalty"]
        + 0.25 * score_terms["response_nll"]
        + 0.25 * score_terms["response_balanced_penalty"]
        + 0.25 * score_terms["quantile_calibration_error"]
    )
    return {
        "value": value_report,
        "response": response_report,
        "selection_score": selection_score,
        "selection_score_terms": score_terms,
        "selection_score_is_strength_evidence": False,
    }


def train_model(
    model: OpponentAwareMultiTaskNetV3,
    train: dict[str, list[dict[str, Any]]],
    early_stop: dict[str, list[dict[str, Any]]],
    *,
    config: dict[str, Any],
    device: torch.device | str,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    model.to(device)
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=config["learning_rate"],
        weight_decay=config["weight_decay"],
    )
    class_weights = _response_class_weights(train["behavior"], device=device)
    rng = random.Random(config["seed"])
    best_state = None
    best_epoch = 0
    best_score = float("inf")
    stale = 0
    history = []
    for epoch in range(1, config["epochs"] + 1):
        model.train()
        value_order = list(range(len(train["value"])))
        behavior_order = list(range(len(train["behavior"])))
        rng.shuffle(value_order)
        rng.shuffle(behavior_order)
        value_batches = _chunks(value_order, config["batch_size"])
        behavior_batches = _chunks(behavior_order, config["batch_size"])
        steps = max(len(value_batches), len(behavior_batches))
        component_sums: dict[str, float] = {}
        epoch_loss = 0.0
        for step in range(steps):
            losses = []
            value_rows = [
                train["value"][index]
                for index in value_batches[step % len(value_batches)]
            ]
            value_batch = collate_encoded_rows(
                value_rows, response=False, device=device
            )
            value_output = model.forward_value(**value_batch["inputs"])
            value_loss, value_components = value_objective(
                value_output,
                value_batch,
                clips=config["clips"],
                field_weights=config["field_weights"],
                mean_weight=config["mean_loss_weight"],
                quantile_weight=config["quantile_loss_weight"],
                ranking_weight=config["match_ranking_weight"],
                lower_ranking_weight=config["match_q20_ranking_weight"],
                ranking_margin=config["ranking_margin"],
                ranking_temperature=config["ranking_temperature"],
            )
            losses.append(value_loss)
            behavior_rows = [
                train["behavior"][index]
                for index in behavior_batches[step % len(behavior_batches)]
            ]
            behavior_batch = collate_encoded_rows(
                behavior_rows, response=True, device=device
            )
            behavior_output = model.forward_response(**behavior_batch["inputs"])
            behavior_loss, behavior_components = response_objective(
                behavior_output,
                behavior_batch,
                class_weights=class_weights,
                size_weight=config["response_size_weight"],
            )
            losses.append(config["response_loss_weight"] * behavior_loss)
            loss = sum(losses)
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(
                model.parameters(), config["gradient_clip_norm"]
            )
            optimizer.step()
            epoch_loss += float(loss.detach().item())
            for name, value in {**value_components, **behavior_components}.items():
                component_sums[name] = component_sums.get(name, 0.0) + value
        validation = evaluate_model(
            model,
            early_stop,
            clips=config["clips"],
            batch_size=config["batch_size"],
            device=device,
            ranking_margin=config["ranking_margin"],
        )
        score = float(validation["selection_score"])
        improved = score < best_score - config["minimum_improvement"]
        if improved:
            best_score = score
            best_epoch = epoch
            best_state = {
                name: tensor.detach().cpu().clone()
                for name, tensor in model.state_dict().items()
            }
            stale = 0
        else:
            stale += 1
        record = {
            "epoch": epoch,
            "train_loss": epoch_loss / max(1, steps),
            "train_components": {
                name: value / max(1, steps)
                for name, value in sorted(component_sums.items())
            },
            "early_stop": validation,
            "improved": improved,
        }
        history.append(record)
        print(
            f"[v3] epoch={epoch} loss={record['train_loss']:.6f} "
            f"early_score={score:.6f}{' *best' if improved else ''}",
            flush=True,
        )
        if stale >= config["patience"]:
            break
    if best_state is None:
        raise RuntimeError("training did not produce an early-stop checkpoint")
    model.load_state_dict(best_state)
    model.to(device)
    final_early_stop = evaluate_model(
        model,
        early_stop,
        clips=config["clips"],
        batch_size=config["batch_size"],
        device=device,
        ranking_margin=config["ranking_margin"],
    )
    return history, best_epoch, final_early_stop


def checkpoint_authorization(
    dataset: RoleDatasetAccess,
    training_phase: dict[str, Any],
    *,
    checkpoint_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": FROZEN_CHECKPOINT_SCHEMA,
        "frozen": True,
        "early_stop_complete": True,
        "run_id": dataset.run_id,
        "role_manifest_sha256": dataset.manifest_sha256,
        "training_roles": list(MODEL_TRAINING_ROLES),
        "training_artifact_sha256": {
            role: training_phase["roles"][role]["provenance"]["artifact_sha256"]
            for role in MODEL_TRAINING_ROLES
        },
        "checkpoint_sha256": checkpoint_sha256,
    }


def load_checkpoint(
    path: Path, *, device: torch.device | str = "cpu"
) -> tuple[OpponentAwareMultiTaskNetV3, dict[str, Any]]:
    """Recreate a v3 model and reject metadata/state drift."""
    payload = torch.load(path, map_location="cpu", weights_only=False)
    if not isinstance(payload, dict) or payload.get("schema") != CHECKPOINT_SCHEMA:
        raise ValueError("unsupported v3 checkpoint")
    metadata = payload.get("model_metadata")
    state = payload.get("state_dict")
    if not isinstance(metadata, dict) or not isinstance(state, dict):
        raise ValueError("v3 checkpoint is missing model metadata or state")
    model = OpponentAwareMultiTaskNetV3(
        scale=str(metadata.get("scale")),
        cross_encoder=str(metadata.get("cross_encoder")),
        moe_experts=int(metadata.get("moe_experts", 0)),
        dropout=float(metadata.get("dropout", -1.0)),
    )
    if model.metadata() != metadata:
        raise ValueError("v3 checkpoint model metadata does not reproduce")
    model.load_state_dict(state, strict=True)
    model.to(device)
    model.eval()
    return model, payload


def calibration_predictions(
    model: OpponentAwareMultiTaskNetV3,
    role: dict[str, list[dict[str, Any]]],
    *,
    clips: dict[str, float],
    batch_size: int,
    device: torch.device | str,
    lower_quantile: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lower_index = QUANTILE_LEVELS.index(lower_quantile)
    value_observations = []
    response_rows = []
    model.eval()
    with torch.no_grad():
        for indices in _chunks(list(range(len(role["value"]))), batch_size):
            rows = [role["value"][index] for index in indices]
            batch = collate_encoded_rows(rows, response=False, device=device)
            output = model.forward_value(**batch["inputs"])
            for row_index, row in enumerate(rows):
                rule_id = int(row["rule_label_id"])
                for field in VALUE_FIELDS:
                    for action_id, observed in enumerate(
                        row["value_target_masks"][field]
                    ):
                        if not observed or action_id == rule_id:
                            continue
                        prediction = float(
                            output[field]["quantiles"][
                                row_index, action_id, lower_index
                            ].item()
                            * clips[field]
                        )
                        observed_target = max(
                            -clips[field],
                            min(
                                clips[field],
                                float(row["value_targets"][field][action_id]),
                            ),
                        )
                        value_observations.append({
                            "field": field,
                            "action_id": action_id,
                            "residual": observed_target - prediction,
                            "weight": row["row_weight"],
                            "opponent": row["opponent"],
                        })
        for indices in _chunks(list(range(len(role["behavior"]))), batch_size):
            rows = [role["behavior"][index] for index in indices]
            batch = collate_encoded_rows(rows, response=True, device=device)
            output = model.forward_response(**batch["inputs"])
            logits = output["logits"].detach().cpu().tolist()
            for row_index, row in enumerate(rows):
                response_rows.append({
                    "logits": logits[row_index],
                    "legal_action_mask": row["response_legal_action_mask"],
                    "target": row["response_target"],
                    "weight": row["row_weight"],
                    "opponent": row["opponent"],
                })
    return value_observations, response_rows


def _environment(device: str) -> dict[str, Any]:
    cuda = str(device).startswith("cuda") and torch.cuda.is_available()
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "torch": torch.__version__,
        "device": device,
        "cuda_available": torch.cuda.is_available(),
        "cuda_version": torch.version.cuda,
        "gpu": torch.cuda.get_device_name(torch.device(device)) if cuda else None,
        "git_commit": _git_commit(),
    }


def _config(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "trainer_schema": TRAINER_SCHEMA,
        "scale": args.scale,
        "cross_encoder": args.cross_encoder,
        "moe_experts": args.moe_experts,
        "dropout": args.dropout,
        "epochs": args.epochs,
        "patience": args.patience,
        "minimum_improvement": args.minimum_improvement,
        "batch_size": args.batch_size,
        "learning_rate": args.learning_rate,
        "weight_decay": args.weight_decay,
        "gradient_clip_norm": args.gradient_clip_norm,
        "clips": {
            "delta_vs_rule": args.hand_clip,
            "tail_delta_vs_rule": args.tail_clip,
            "match_delta_vs_rule": args.match_clip,
        },
        "field_weights": dict(DEFAULT_FIELD_WEIGHTS),
        "mean_loss_weight": args.mean_loss_weight,
        "quantile_loss_weight": args.quantile_loss_weight,
        "match_ranking_weight": args.match_ranking_weight,
        "match_q20_ranking_weight": args.match_q20_ranking_weight,
        "ranking_margin": args.ranking_margin,
        "ranking_temperature": args.ranking_temperature,
        "response_loss_weight": args.response_loss_weight,
        "response_size_weight": args.response_size_weight,
        "lower_calibration_quantile": args.lower_calibration_quantile,
        "min_calibration_rows_per_action": args.min_calibration_rows_per_action,
        "min_calibration_ess_per_action": args.min_calibration_ess_per_action,
        "task_batch_balancing": "cycle_shorter_modality",
        "seed": args.seed,
    }


def _validate_args(args: argparse.Namespace) -> None:
    positive = (
        "epochs",
        "patience",
        "batch_size",
        "learning_rate",
        "gradient_clip_norm",
        "hand_clip",
        "tail_clip",
        "match_clip",
        "ranking_temperature",
        "min_calibration_rows_per_action",
        "min_calibration_ess_per_action",
    )
    if any(float(getattr(args, name)) <= 0.0 for name in positive):
        raise SystemExit("training counts, clips, rates, and thresholds must be positive")
    nonnegative = (
        "weight_decay",
        "minimum_improvement",
        "mean_loss_weight",
        "quantile_loss_weight",
        "match_ranking_weight",
        "match_q20_ranking_weight",
        "ranking_margin",
        "response_loss_weight",
        "response_size_weight",
    )
    if any(float(getattr(args, name)) < 0.0 for name in nonnegative):
        raise SystemExit("loss weights and margins must be non-negative")
    if args.lower_calibration_quantile not in QUANTILE_LEVELS:
        raise SystemExit("lower calibration quantile must be a model quantile")
    if not 0.0 <= args.dropout < 1.0:
        raise SystemExit("dropout must be in [0, 1)")
    if str(args.device).startswith("cuda") and not torch.cuda.is_available():
        raise SystemExit("CUDA was requested but is unavailable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role-manifest", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--allow-incomplete-smoke", action="store_true")
    parser.add_argument(
        "--open-model-calibration",
        action="store_true",
        help="Explicitly open model-calibration after the checkpoint freezes.",
    )
    parser.add_argument("--scale", choices=tuple(MODEL_SCALES), default="medium")
    parser.add_argument(
        "--cross-encoder",
        choices=("none", "deep_set", "gru", "gru_moe"),
        default="deep_set",
    )
    parser.add_argument("--moe-experts", type=int, default=4)
    parser.add_argument("--dropout", type=float, default=0.10)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--patience", type=int, default=15)
    parser.add_argument("--minimum-improvement", type=float, default=1.0e-5)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--learning-rate", type=float, default=5.0e-4)
    parser.add_argument("--weight-decay", type=float, default=1.0e-5)
    parser.add_argument("--gradient-clip-norm", type=float, default=1.0)
    parser.add_argument("--hand-clip", type=float, default=DEFAULT_CLIPS["delta_vs_rule"])
    parser.add_argument("--tail-clip", type=float, default=DEFAULT_CLIPS["tail_delta_vs_rule"])
    parser.add_argument("--match-clip", type=float, default=DEFAULT_CLIPS["match_delta_vs_rule"])
    parser.add_argument("--mean-loss-weight", type=float, default=1.0)
    parser.add_argument("--quantile-loss-weight", type=float, default=1.0)
    parser.add_argument("--match-ranking-weight", type=float, default=0.5)
    parser.add_argument("--match-q20-ranking-weight", type=float, default=0.25)
    parser.add_argument("--ranking-margin", type=float, default=100.0)
    parser.add_argument("--ranking-temperature", type=float, default=0.25)
    parser.add_argument("--response-loss-weight", type=float, default=1.0)
    parser.add_argument("--response-size-weight", type=float, default=0.25)
    parser.add_argument("--lower-calibration-quantile", type=float, default=0.20)
    parser.add_argument("--min-calibration-rows-per-action", type=int, default=20)
    parser.add_argument("--min-calibration-ess-per-action", type=float, default=10.0)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--seed", type=int, default=101)
    args = parser.parse_args(argv)
    _validate_args(args)
    config = _config(args)
    _seed_everything(args.seed)

    out_dir = args.out_dir.resolve()
    if out_dir.exists():
        raise SystemExit(f"output directory already exists: {out_dir}")
    temporary = out_dir.with_name(f".{out_dir.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise SystemExit(f"temporary output directory already exists: {temporary}")
    try:
        dataset = RoleDatasetAccess(
            args.role_manifest,
            ledger_path=args.ledger,
            run_id=args.run_id,
            require_complete=not args.allow_incomplete_smoke,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    incomplete = dataset.manifest.get("source_collection_complete") is not True
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.mkdir()
    try:
        training_phase = prepare_training_phase(dataset)
        train = _encoded_role(training_phase["roles"]["train"])
        early_stop = _encoded_role(training_phase["roles"]["early_stop"])
        model = OpponentAwareMultiTaskNetV3(
            scale=args.scale,
            cross_encoder=args.cross_encoder,
            moe_experts=args.moe_experts,
            dropout=args.dropout,
        )
        history, best_epoch, final_early_stop = train_model(
            model,
            train,
            early_stop,
            config=config,
            device=args.device,
        )
        checkpoint_path = temporary / "checkpoint.pt"
        training_artifacts = {
            role: training_phase["roles"][role]["provenance"]["artifact_sha256"]
            for role in MODEL_TRAINING_ROLES
        }
        code_artifacts = _code_artifacts()
        torch.save({
            "schema": CHECKPOINT_SCHEMA,
            "role_manifest_sha256": dataset.manifest_sha256,
            "training_artifact_sha256": training_artifacts,
            "source_completed_passes": dataset.manifest.get(
                "source_completed_passes"
            ),
            "source_collection_complete": not incomplete,
            "code_artifacts": code_artifacts,
            "model_metadata": model.metadata(),
            "training_data": training_data_metadata(),
            "training_config": config,
            "best_epoch": best_epoch,
            "state_dict": {
                name: tensor.detach().cpu()
                for name, tensor in model.state_dict().items()
            },
        }, checkpoint_path)
        checkpoint_sha256 = _sha256(checkpoint_path)
        authorization = checkpoint_authorization(
            dataset,
            training_phase,
            checkpoint_sha256=checkpoint_sha256,
        )
        _write_json(temporary / "checkpoint_authorization.json", authorization)
        report_roles = training_phase["roles"]
        opened_roles = list(training_phase["opened_roles"])
        calibration_payload_sha256 = None
        calibration_summary = None
        artifact_files = ["checkpoint.pt", "checkpoint_authorization.json"]
        if args.open_model_calibration:
            calibration_phase = prepare_model_calibration(
                dataset, training_phase, authorization
            )
            calibration_role = _encoded_role(
                calibration_phase["roles"]["model_calibration"]
            )
            combined = combine_model_development(
                training_phase, calibration_phase
            )
            report_roles = combined["roles"]
            opened_roles = list(combined["opened_roles"])
            value_observations, response_rows = calibration_predictions(
                model,
                calibration_role,
                clips=config["clips"],
                batch_size=config["batch_size"],
                device=args.device,
                lower_quantile=args.lower_calibration_quantile,
            )
            value_lower = calibrate_value_lower_offsets(
                value_observations,
                value_fields=VALUE_FIELDS,
                num_actions=len(LABELS),
                quantile=args.lower_calibration_quantile,
                min_rows_per_action=args.min_calibration_rows_per_action,
                min_ess_per_action=args.min_calibration_ess_per_action,
            )
            value_lower["target_preprocessing"] = (
                "symmetric_clip_before_residual"
            )
            value_lower["target_clips"] = dict(config["clips"])
            response_temperature = calibrate_response_temperature(response_rows)
            calibration = build_calibration_artifact(
                calibration_phase,
                value_lower=value_lower,
                response_temperature=response_temperature,
            )
            _write_json(temporary / "calibration.json", calibration)
            calibration_payload_sha256 = calibration["payload_sha256"]
            calibration_summary = {
                "lower_quantile": args.lower_calibration_quantile,
                "value_observations": len(value_observations),
                "response_rows": len(response_rows),
                "response_temperature": response_temperature["temperature"],
            }
            artifact_files.append("calibration.json")
        report = {
            "schema": REPORT_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_id": args.run_id,
            "command": [sys.executable, *sys.argv],
            "role_manifest": str(args.role_manifest.resolve()),
            "role_manifest_sha256": dataset.manifest_sha256,
            "ledger": str(args.ledger.resolve()),
            "source_completed_passes": dataset.manifest.get(
                "source_completed_passes"
            ),
            "source_requested_passes": dataset.manifest.get(
                "source_requested_passes"
            ),
            "source_collection_complete": not incomplete,
            "incomplete_smoke": incomplete,
            "opened_roles": opened_roles,
            "model_calibration_opened": bool(args.open_model_calibration),
            "policy_roles_opened": False,
            "role_counts": {
                role: {
                    "opponents": list(payload["opponents"]),
                    "value": len(payload["value"]),
                    "behavior": len(payload["behavior"]),
                    "provenance": payload["provenance"],
                }
                for role, payload in report_roles.items()
            },
            "model": model.metadata(),
            "config": config,
            "environment": _environment(args.device),
            "code_artifacts": code_artifacts,
            "history": history,
            "best_epoch": best_epoch,
            "early_stop": final_early_stop,
            "checkpoint_sha256": checkpoint_sha256,
            "checkpoint_authorization": authorization,
            "calibration_payload_sha256": calibration_payload_sha256,
            "calibration_summary": calibration_summary,
            "deployment_policy_value": False,
            "strength_evidence": False,
            "native_tcp_evaluated": False,
        }
        _write_json(temporary / "training_report.json", report)
        artifact_files.append("training_report.json")
        _write_json(temporary / "artifact_manifest.json", {
            "schema": ARTIFACT_MANIFEST_SCHEMA,
            "run_id": args.run_id,
            "files": {
                name: {
                    "bytes": (temporary / name).stat().st_size,
                    "sha256": _sha256(temporary / name),
                }
                for name in artifact_files
            },
            "deployment_policy_value": False,
            "strength_evidence": False,
        })
        temporary.replace(out_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(json.dumps({
        "out_dir": str(out_dir),
        "best_epoch": best_epoch,
        "early_stop_score": final_early_stop["selection_score"],
        "checkpoint_sha256": checkpoint_sha256,
        "model_calibration_opened": bool(args.open_model_calibration),
        "strength_evidence": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
