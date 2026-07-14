#!/usr/bin/env python3
"""Train v4 with 70-hand positive outcome as the lexicographic priority."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
from typing import Any

import torch
import torch.nn.functional as F

from feature_spec import LABELS
from match_outcome_schema import MATCH_OUTCOME_ESTIMAND, MATCH_OUTCOME_SCHEMA
from multitask_training_data import (
    MODEL_TRAINING_ROLES,
    prepare_training_phase,
    training_data_metadata,
)
from opponent_multitask_batch_v4 import collate_encoded_rows
import opponent_multitask_batch_v3 as parent_batch
import opponent_multitask_model_v3 as parent_model
from opponent_multitask_model_v4 import (
    MODEL_FORMAT,
    MODEL_SCALES,
    OpponentAwareMultiTaskNetV4,
)
from role_dataset_access import RoleDatasetAccess
import train_opponent_multitask_v3 as v3


TRAINER_SCHEMA = "opponent_multitask_trainer_v4_win_first"
CHECKPOINT_SCHEMA = "opponent_multitask_torch_checkpoint_v4"
REPORT_SCHEMA = "opponent_multitask_training_report_v4"
ARTIFACT_MANIFEST_SCHEMA = "opponent_multitask_training_artifacts_v4"
FORMAL_COLLECTION_PASSES = 160


def require_formal_collection_boundary(
    dataset: RoleDatasetAccess, *, allow_incomplete_smoke: bool
) -> None:
    if not allow_incomplete_smoke:
        dataset.require_collection_boundary(
            expected_passes=FORMAL_COLLECTION_PASSES
        )


def training_environment(
    device: str, *, pythonhashseed: str | None = None
) -> dict[str, Any]:
    environment = v3._environment(device)
    environment["torch"] = str(environment.get("torch"))
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        check=False,
        text=True,
    )
    git_commit = result.stdout.strip()
    if result.returncode != 0 or len(git_commit) != 40:
        raise RuntimeError("training checkout has no valid Git commit")
    environment.update({
        "git_commit": git_commit,
        "pythonhashseed": (
            os.environ.get("PYTHONHASHSEED")
            if pythonhashseed is None
            else str(pythonhashseed)
        ),
        "cublas_workspace_config": os.environ.get(
            "CUBLAS_WORKSPACE_CONFIG", ":4096:8"
        ),
        "omp_num_threads": os.environ.get("OMP_NUM_THREADS"),
        "mkl_num_threads": os.environ.get("MKL_NUM_THREADS"),
        "openblas_num_threads": os.environ.get("OPENBLAS_NUM_THREADS"),
    })
    return environment


def outcome_objective(
    logits: torch.Tensor,
    batch: dict[str, Any],
    *,
    pairwise_weight: float,
    pairwise_temperature: float,
) -> tuple[torch.Tensor, dict[str, float]]:
    supervision = batch["supervision"]
    targets = supervision["match_positive_targets"]
    mask = supervision["match_positive_target_mask"]
    row_weight = supervision["row_weight"].unsqueeze(1)
    weights = row_weight * mask
    bce = v3._weighted_mean(
        F.binary_cross_entropy_with_logits(logits, targets, reduction="none"),
        weights,
    )

    positive = (targets > 0.5) & mask.bool()
    negative = (targets <= 0.5) & mask.bool()
    pair_mask = positive.unsqueeze(2) & negative.unsqueeze(1)
    difference = logits.unsqueeze(2) - logits.unsqueeze(1)
    pair_weights = row_weight.unsqueeze(2) * pair_mask.float()
    pairwise = v3._weighted_mean(
        F.softplus(-difference / pairwise_temperature),
        pair_weights,
    )
    total = bce + pairwise_weight * pairwise
    return total, {
        "match_outcome.bce": float(bce.detach().item()),
        "match_outcome.pairwise": float(pairwise.detach().item()),
        "match_outcome.observed_weight": float(weights.sum().detach().item()),
        "match_outcome.pair_weight": float(pair_weights.sum().detach().item()),
    }


def _outcome_report(
    model: OpponentAwareMultiTaskNetV4,
    rows: list[dict[str, Any]],
    *,
    batch_size: int,
    device: torch.device | str,
) -> dict[str, Any]:
    stats = {
        "nll": 0.0,
        "brier": 0.0,
        "weight": 0.0,
        "classes": [
            {"correct": 0.0, "weight": 0.0},
            {"correct": 0.0, "weight": 0.0},
        ],
        "flip_classes": [
            {"correct": 0.0, "weight": 0.0},
            {"correct": 0.0, "weight": 0.0},
        ],
        "per_action": [
            {"correct": 0.0, "weight": 0.0, "positive": 0.0}
            for _ in LABELS
        ],
    }
    model.eval()
    with torch.no_grad():
        for indices in v3._chunks(list(range(len(rows))), batch_size):
            selected = [rows[index] for index in indices]
            batch = collate_encoded_rows(
                selected, response=False, device=device
            )
            logits = model.forward_match_outcome(**batch["inputs"])
            targets = batch["supervision"]["match_positive_targets"]
            mask = batch["supervision"]["match_positive_target_mask"]
            weights = batch["supervision"]["row_weight"].unsqueeze(1) * mask
            probabilities = torch.sigmoid(logits)
            predicted = probabilities > 0.5
            correct = predicted == targets.bool()
            nll = F.binary_cross_entropy_with_logits(
                logits, targets, reduction="none"
            )
            stats["nll"] += float((nll * weights).sum().item())
            stats["brier"] += float(
                (((probabilities - targets) ** 2) * weights).sum().item()
            )
            stats["weight"] += float(weights.sum().item())
            baseline = batch["supervision"]["baseline_match_positive"].unsqueeze(1)
            flip = (targets != baseline) & mask.bool()
            for class_index, positive in enumerate((False, True)):
                class_mask = mask.bool() & ((targets > 0.5) == positive)
                class_weight = weights * class_mask.float()
                stats["classes"][class_index]["correct"] += float(
                    (correct.float() * class_weight).sum().item()
                )
                stats["classes"][class_index]["weight"] += float(
                    class_weight.sum().item()
                )
                flip_mask = flip & ((targets > 0.5) == positive)
                flip_weight = weights * flip_mask.float()
                stats["flip_classes"][class_index]["correct"] += float(
                    (correct.float() * flip_weight).sum().item()
                )
                stats["flip_classes"][class_index]["weight"] += float(
                    flip_weight.sum().item()
                )
            for action_id in range(len(LABELS)):
                action_weight = weights[:, action_id]
                item = stats["per_action"][action_id]
                item["correct"] += float(
                    (correct[:, action_id].float() * action_weight).sum().item()
                )
                item["weight"] += float(action_weight.sum().item())
                item["positive"] += float(
                    (targets[:, action_id] * action_weight).sum().item()
                )

    balanced = v3._balanced_accuracy(stats["classes"])
    flip_balanced = v3._balanced_accuracy(stats["flip_classes"])
    return {
        "estimand": MATCH_OUTCOME_ESTIMAND,
        "supervision_schema": MATCH_OUTCOME_SCHEMA,
        "nll": v3._weighted_ratio(stats["nll"], stats["weight"]),
        "brier": v3._weighted_ratio(stats["brier"], stats["weight"]),
        "balanced_accuracy": balanced,
        "flip_balanced_accuracy": flip_balanced,
        "effective_weight": stats["weight"],
        "flip_effective_weight": sum(
            item["weight"] for item in stats["flip_classes"]
        ),
        "per_class": {
            label: {
                "accuracy": v3._weighted_ratio(item["correct"], item["weight"]),
                "effective_weight": item["weight"],
            }
            for label, item in zip(("nonpositive", "positive"), stats["classes"])
        },
        "per_action": {
            label: {
                "accuracy": v3._weighted_ratio(item["correct"], item["weight"]),
                "positive_rate": v3._weighted_ratio(
                    item["positive"], item["weight"]
                ),
                "effective_weight": item["weight"],
            }
            for label, item in zip(LABELS, stats["per_action"], strict=True)
        },
    }


def evaluate_model(
    model: OpponentAwareMultiTaskNetV4,
    role: dict[str, list[dict[str, Any]]],
    *,
    clips: dict[str, float],
    batch_size: int,
    device: torch.device | str,
    ranking_margin: float,
) -> dict[str, Any]:
    secondary = v3.evaluate_model(
        model,
        role,
        clips=clips,
        batch_size=batch_size,
        device=device,
        ranking_margin=ranking_margin,
    )
    outcome = _outcome_report(
        model, role["value"], batch_size=batch_size, device=device
    )
    flip_balanced = outcome["flip_balanced_accuracy"]
    balanced = outcome["balanced_accuracy"]
    nll = outcome["nll"]
    selection_key = [
        1.0 - flip_balanced if flip_balanced is not None else 1.0,
        1.0 - balanced if balanced is not None else 1.0,
        float(nll) if nll is not None else 10.0,
        float(secondary["selection_score"]),
    ]
    return {
        **secondary,
        "match_outcome": outcome,
        "selection_key": selection_key,
        "selection_key_order": [
            "match_flip_balanced_error",
            "match_balanced_error",
            "match_nll",
            "secondary_v3_value_response_score",
        ],
        "selection_key_is_lexicographic": True,
        "selection_score_is_strength_evidence": False,
    }


def _key_improved(
    current: list[float], best: list[float] | None, minimum: float
) -> bool:
    if best is None:
        return True
    for current_value, best_value in zip(current, best, strict=True):
        if current_value < best_value - minimum:
            return True
        if current_value > best_value + minimum:
            return False
    return False


def train_model(
    model: OpponentAwareMultiTaskNetV4,
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
    class_weights = v3._response_class_weights(train["behavior"], device=device)
    rng = random.Random(config["seed"])
    best_state = None
    best_key = None
    best_epoch = 0
    stale = 0
    history = []
    for epoch in range(1, config["epochs"] + 1):
        model.train()
        value_order = list(range(len(train["value"])))
        behavior_order = list(range(len(train["behavior"])))
        rng.shuffle(value_order)
        rng.shuffle(behavior_order)
        value_batches = v3._chunks(value_order, config["batch_size"])
        behavior_batches = v3._chunks(behavior_order, config["batch_size"])
        steps = max(len(value_batches), len(behavior_batches))
        component_sums: dict[str, float] = {}
        epoch_loss = 0.0
        for step in range(steps):
            value_rows = [
                train["value"][index]
                for index in value_batches[step % len(value_batches)]
            ]
            value_batch = collate_encoded_rows(
                value_rows, response=False, device=device
            )
            joint = model.forward_joint_value(**value_batch["inputs"])
            value_loss, value_components = v3.value_objective(
                joint["values"],
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
            outcome_loss, outcome_components = outcome_objective(
                joint["match_positive_logits"],
                value_batch,
                pairwise_weight=config["outcome_pairwise_weight"],
                pairwise_temperature=config["outcome_pairwise_temperature"],
            )
            behavior_rows = [
                train["behavior"][index]
                for index in behavior_batches[step % len(behavior_batches)]
            ]
            behavior_batch = collate_encoded_rows(
                behavior_rows, response=True, device=device
            )
            behavior_output = model.forward_response(**behavior_batch["inputs"])
            behavior_loss, behavior_components = v3.response_objective(
                behavior_output,
                behavior_batch,
                class_weights=class_weights,
                size_weight=config["response_size_weight"],
            )
            loss = (
                value_loss
                + config["outcome_loss_weight"] * outcome_loss
                + config["response_loss_weight"] * behavior_loss
            )
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(
                model.parameters(), config["gradient_clip_norm"]
            )
            optimizer.step()
            epoch_loss += float(loss.detach().item())
            components = {
                **value_components,
                **outcome_components,
                **behavior_components,
            }
            for name, value in components.items():
                component_sums[name] = component_sums.get(name, 0.0) + value

        validation = evaluate_model(
            model,
            early_stop,
            clips=config["clips"],
            batch_size=config["batch_size"],
            device=device,
            ranking_margin=config["ranking_margin"],
        )
        key = [float(value) for value in validation["selection_key"]]
        improved = _key_improved(key, best_key, config["minimum_improvement"])
        if improved:
            best_key = key
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
            f"[v4] epoch={epoch} loss={record['train_loss']:.6f} "
            f"early_key={key}{' *best' if improved else ''}",
            flush=True,
        )
        if stale >= config["patience"]:
            break
    if best_state is None:
        raise RuntimeError("training did not produce an early-stop checkpoint")
    model.load_state_dict(best_state)
    model.to(device)
    final = evaluate_model(
        model,
        early_stop,
        clips=config["clips"],
        batch_size=config["batch_size"],
        device=device,
        ranking_margin=config["ranking_margin"],
    )
    return history, best_epoch, final


def load_checkpoint(
    path: Path, *, device: torch.device | str = "cpu"
) -> tuple[OpponentAwareMultiTaskNetV4, dict[str, Any]]:
    payload = torch.load(path, map_location="cpu", weights_only=True)
    if not isinstance(payload, dict) or payload.get("schema") != CHECKPOINT_SCHEMA:
        raise ValueError("unsupported v4 checkpoint")
    metadata = payload.get("model_metadata")
    state = payload.get("state_dict")
    if not isinstance(metadata, dict) or not isinstance(state, dict):
        raise ValueError("v4 checkpoint is missing model metadata or state")
    raw_transformer_heads = metadata.get("cross_transformer_heads", 4)
    if str(metadata.get("cross_encoder")) == "transformer":
        raw_transformer_layers = metadata.get("cross_transformer_layers")
        if (
            isinstance(raw_transformer_heads, bool)
            or not isinstance(raw_transformer_heads, int)
            or raw_transformer_heads <= 0
            or isinstance(raw_transformer_layers, bool)
            or not isinstance(raw_transformer_layers, int)
            or raw_transformer_layers != 1
            or metadata.get("cross_transformer_pooling")
            != "last_valid_position"
        ):
            raise ValueError("checkpoint has invalid transformer metadata")
    model = OpponentAwareMultiTaskNetV4(
        scale=str(metadata.get("scale")),
        cross_encoder=str(metadata.get("cross_encoder")),
        moe_experts=int(metadata.get("moe_experts", 0)),
        transformer_heads=raw_transformer_heads,
        dropout=float(metadata.get("dropout", -1.0)),
    )
    if model.metadata() != metadata or metadata.get("format") != MODEL_FORMAT:
        raise ValueError("v4 checkpoint model metadata does not reproduce")
    model.load_state_dict(state, strict=True)
    model.to(device)
    model.eval()
    return model, payload


def _config(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "trainer_schema": TRAINER_SCHEMA,
        "scale": args.scale,
        "cross_encoder": args.cross_encoder,
        "moe_experts": args.moe_experts,
        "cross_transformer_heads": args.cross_transformer_heads,
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
        "field_weights": dict(v3.DEFAULT_FIELD_WEIGHTS),
        "mean_loss_weight": args.mean_loss_weight,
        "quantile_loss_weight": args.quantile_loss_weight,
        "match_ranking_weight": args.match_ranking_weight,
        "match_q20_ranking_weight": args.match_q20_ranking_weight,
        "ranking_margin": args.ranking_margin,
        "ranking_temperature": args.ranking_temperature,
        "outcome_loss_weight": args.outcome_loss_weight,
        "outcome_pairwise_weight": args.outcome_pairwise_weight,
        "outcome_pairwise_temperature": args.outcome_pairwise_temperature,
        "response_loss_weight": args.response_loss_weight,
        "response_size_weight": args.response_size_weight,
        "task_batch_balancing": "cycle_shorter_modality",
        "early_stop_selection": "lexicographic_match_outcome_before_value_v1",
        "seed": args.seed,
    }


def _validate_args(args: argparse.Namespace) -> None:
    positive = (
        "epochs", "patience", "batch_size", "learning_rate",
        "gradient_clip_norm", "hand_clip", "tail_clip", "match_clip",
        "ranking_temperature", "outcome_pairwise_temperature",
    )
    if any(float(getattr(args, name)) <= 0.0 for name in positive):
        raise SystemExit("training counts, clips, rates, and temperatures must be positive")
    nonnegative = (
        "weight_decay", "minimum_improvement", "mean_loss_weight",
        "quantile_loss_weight", "match_ranking_weight",
        "match_q20_ranking_weight", "ranking_margin", "outcome_loss_weight",
        "outcome_pairwise_weight", "response_loss_weight",
        "response_size_weight",
    )
    if any(float(getattr(args, name)) < 0.0 for name in nonnegative):
        raise SystemExit("loss weights and margins must be non-negative")
    if args.outcome_loss_weight <= 0.0:
        raise SystemExit("outcome-loss-weight must be positive")
    if not 0.0 <= args.dropout < 1.0:
        raise SystemExit("dropout must be in [0, 1)")
    if args.cross_encoder == "transformer" and (
        args.cross_transformer_heads <= 0
        or MODEL_SCALES[args.scale]["cross_hidden"]
        % args.cross_transformer_heads
    ):
        raise SystemExit(
            "transformer heads must positively divide the cross hidden size"
        )
    if str(args.device).startswith("cuda") and not torch.cuda.is_available():
        raise SystemExit("CUDA was requested but is unavailable")


def _code_artifacts() -> dict[str, dict[str, Any]]:
    paths = {
        "trainer": Path(__file__).resolve(),
        "model": Path(sys.modules["opponent_multitask_model_v4"].__file__).resolve(),
        "batch": Path(sys.modules["opponent_multitask_batch_v4"].__file__).resolve(),
        "match_outcome": Path(sys.modules["match_outcome_schema"].__file__).resolve(),
        "training_data": Path(sys.modules["multitask_training_data"].__file__).resolve(),
        "parent_trainer": Path(v3.__file__).resolve(),
        "parent_model": Path(parent_model.__file__).resolve(),
        "parent_batch": Path(parent_batch.__file__).resolve(),
    }
    for module_name in (
        "audit_oppmodel_dataset",
        "cross_hand_sequence",
        "decision_context_features",
        "feature_spec",
        "freeze_opponent_role_dataset",
        "freeze_oppmodel_dataset",
        "hand_context_features",
        "history_feature_schema",
        "longrun_collect_oppmodel",
        "match_outcome_schema",
        "model_input_schema",
        "multitask_calibration",
        "multitask_training_data",
        "opponent_exposure_ledger",
        "opponent_profile_schema",
        "opponent_response_schema",
        "role_dataset_access",
        "sampling_weights",
        "state_feature_schema",
        "strategy_context_schema",
    ):
        module = sys.modules.get(module_name)
        raw_path = getattr(module, "__file__", None)
        if raw_path is None:
            raise RuntimeError(
                f"training dependency module is not loaded: {module_name}"
            )
        paths[f"dependency:{module_name}"] = Path(raw_path).resolve()
    paths["dependency:sever_validator"] = (
        Path(__file__).resolve().parents[3] / "sever" / "engine" / "validator.py"
    )
    return {
        name: {"bytes": path.stat().st_size, "sha256": v3._sha256(path)}
        for name, path in sorted(paths.items())
    }


def _verify_code_artifacts_unchanged(
    startup: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if _code_artifacts() != startup:
        raise RuntimeError("training code changed while v4 training was running")
    return startup


def _verify_environment_unchanged(startup: dict[str, Any]) -> dict[str, Any]:
    if training_environment(str(startup.get("device"))) != startup:
        raise RuntimeError("training environment changed while v4 training was running")
    return startup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role-manifest", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--allow-incomplete-smoke", action="store_true")
    parser.add_argument("--scale", choices=tuple(MODEL_SCALES), default="medium")
    parser.add_argument(
        "--cross-encoder",
        choices=("none", "deep_set", "gru", "gru_moe", "transformer"),
        default="deep_set",
    )
    parser.add_argument("--moe-experts", type=int, default=4)
    parser.add_argument("--cross-transformer-heads", type=int, default=4)
    parser.add_argument("--dropout", type=float, default=0.10)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--patience", type=int, default=15)
    parser.add_argument("--minimum-improvement", type=float, default=1.0e-5)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--learning-rate", type=float, default=5.0e-4)
    parser.add_argument("--weight-decay", type=float, default=1.0e-5)
    parser.add_argument("--gradient-clip-norm", type=float, default=1.0)
    parser.add_argument(
        "--hand-clip", type=float, default=v3.DEFAULT_CLIPS["delta_vs_rule"]
    )
    parser.add_argument(
        "--tail-clip", type=float, default=v3.DEFAULT_CLIPS["tail_delta_vs_rule"]
    )
    parser.add_argument(
        "--match-clip", type=float, default=v3.DEFAULT_CLIPS["match_delta_vs_rule"]
    )
    parser.add_argument("--mean-loss-weight", type=float, default=1.0)
    parser.add_argument("--quantile-loss-weight", type=float, default=1.0)
    parser.add_argument("--match-ranking-weight", type=float, default=0.5)
    parser.add_argument("--match-q20-ranking-weight", type=float, default=0.25)
    parser.add_argument("--ranking-margin", type=float, default=100.0)
    parser.add_argument("--ranking-temperature", type=float, default=0.25)
    parser.add_argument("--outcome-loss-weight", type=float, default=2.0)
    parser.add_argument("--outcome-pairwise-weight", type=float, default=0.5)
    parser.add_argument("--outcome-pairwise-temperature", type=float, default=1.0)
    parser.add_argument("--response-loss-weight", type=float, default=1.0)
    parser.add_argument("--response-size-weight", type=float, default=0.25)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--seed", type=int, default=101)
    args = parser.parse_args(argv)
    _validate_args(args)
    config = _config(args)
    v3._seed_everything(args.seed)
    startup_code_artifacts = _code_artifacts()
    startup_environment = training_environment(args.device)

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
        require_formal_collection_boundary(
            dataset, allow_incomplete_smoke=args.allow_incomplete_smoke
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    incomplete = dataset.manifest.get("source_collection_complete") is not True
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.mkdir()
    try:
        training_phase = prepare_training_phase(dataset)
        train = v3._encoded_role(training_phase["roles"]["train"])
        early_stop = v3._encoded_role(training_phase["roles"]["early_stop"])
        model = OpponentAwareMultiTaskNetV4(
            scale=args.scale,
            cross_encoder=args.cross_encoder,
            moe_experts=args.moe_experts,
            transformer_heads=args.cross_transformer_heads,
            dropout=args.dropout,
        )
        history, best_epoch, final_early_stop = train_model(
            model, train, early_stop, config=config, device=args.device
        )
        code_artifacts = _verify_code_artifacts_unchanged(
            startup_code_artifacts
        )
        training_environment_artifact = _verify_environment_unchanged(
            startup_environment
        )
        checkpoint_path = temporary / "checkpoint.pt"
        training_artifacts = {
            role: training_phase["roles"][role]["provenance"]["artifact_sha256"]
            for role in MODEL_TRAINING_ROLES
        }
        torch.save({
            "schema": CHECKPOINT_SCHEMA,
            "role_manifest_sha256": dataset.manifest_sha256,
            "training_artifact_sha256": training_artifacts,
            "source_completed_passes": dataset.manifest.get(
                "source_completed_passes"
            ),
            "source_requested_passes": dataset.manifest.get(
                "source_requested_passes"
            ),
            "source_collection_complete": not incomplete,
            "code_artifacts": code_artifacts,
            "training_environment": training_environment_artifact,
            "model_metadata": model.metadata(),
            "training_data": training_data_metadata(),
            "training_config": config,
            "best_epoch": best_epoch,
            "state_dict": {
                name: tensor.detach().cpu()
                for name, tensor in model.state_dict().items()
            },
        }, checkpoint_path)
        checkpoint_sha256 = v3._sha256(checkpoint_path)
        authorization = v3.checkpoint_authorization(
            dataset,
            training_phase,
            checkpoint_sha256=checkpoint_sha256,
        )
        v3._write_json(
            temporary / "checkpoint_authorization.json", authorization
        )
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
            "opened_roles": list(training_phase["opened_roles"]),
            "model_calibration_opened": False,
            "policy_roles_opened": False,
            "role_counts": {
                role: {
                    "opponents": list(payload["opponents"]),
                    "value": len(payload["value"]),
                    "behavior": len(payload["behavior"]),
                    "provenance": payload["provenance"],
                }
                for role, payload in training_phase["roles"].items()
            },
            "model": model.metadata(),
            "config": config,
            "environment": training_environment_artifact,
            "code_artifacts": code_artifacts,
            "history": history,
            "best_epoch": best_epoch,
            "early_stop": final_early_stop,
            "checkpoint_sha256": checkpoint_sha256,
            "checkpoint_authorization": authorization,
            "deployment_policy_value": False,
            "strength_evidence": False,
            "native_tcp_evaluated": False,
        }
        v3._write_json(temporary / "training_report.json", report)
        artifact_files = (
            "checkpoint.pt",
            "checkpoint_authorization.json",
            "training_report.json",
        )
        v3._write_json(temporary / "artifact_manifest.json", {
            "schema": ARTIFACT_MANIFEST_SCHEMA,
            "run_id": args.run_id,
            "files": {
                name: {
                    "bytes": (temporary / name).stat().st_size,
                    "sha256": v3._sha256(temporary / name),
                }
                for name in artifact_files
            },
            "source_collection_complete": not incomplete,
            "deployment_policy_value": False,
            "strength_evidence": False,
        })
        _verify_code_artifacts_unchanged(startup_code_artifacts)
        _verify_environment_unchanged(startup_environment)
        temporary.replace(out_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(json.dumps({
        "out_dir": str(out_dir),
        "checkpoint_sha256": checkpoint_sha256,
        "best_epoch": best_epoch,
        "selection_key": final_early_stop["selection_key"],
        "source_collection_complete": not incomplete,
        "strength_evidence": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
