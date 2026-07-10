#!/usr/bin/env python3
"""Train a catastrophe classifier on a frozen opponent-aware latent space.

The head predicts, for every legal action, (1) the probability that the
immediate hand delta versus the rule action is catastrophically negative and
(2) the loss severity conditional on that event. The exported JSON is bound to
the exact base model hash and runs without torch through
``opp_catastrophe_runtime.py``.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import random
import sys
from typing import Any

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from train_opponent_multitask_net import (  # noqa: E402
    NUM_ACTIONS,
    OpponentAwareMultiTaskNet,
    _context_tensors,
    _rule_action_tensor,
    build_value_sample,
)
from train_opponent_value_net import load_jsonl  # noqa: E402


class CatastropheRiskHead(nn.Module):
    def __init__(self, latent_dim: int, hidden: int) -> None:
        super().__init__()
        self.head = nn.Sequential(
            nn.Linear(latent_dim, hidden),
            nn.ReLU(),
            nn.Linear(hidden, NUM_ACTIONS * 2),
        )

    def forward(self, latent: torch.Tensor) -> torch.Tensor:
        return self.head(latent)


def _chunks(indices: list[int], width: int) -> list[list[int]]:
    return [indices[start:start + width] for start in range(0, len(indices), width)]


def _manifest(path: str | Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    source = Path(path)
    return {
        "path": str(source.resolve()),
        "bytes": source.stat().st_size,
        "rows": len(rows),
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }


def _cluster_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("_opponent_label") or row.get("opponent") or "unknown"),
        str(row.get("deck_seed_base")),
        str(row.get("bot_seed_base")),
    )


def _cluster_bootstrap(
    rows: list[dict[str, Any]], *, seed: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_opponent: dict[str, dict[tuple[str, str, str], list[dict[str, Any]]]] = (
        defaultdict(lambda: defaultdict(list))
    )
    for row in rows:
        key = _cluster_key(row)
        by_opponent[key[0]][key].append(row)
    rng = random.Random(seed)
    sampled = []
    unique = set()
    draws = 0
    for opponent in sorted(by_opponent):
        groups = by_opponent[opponent]
        keys = sorted(groups)
        if not keys:
            continue
        for _ in range(len(keys)):
            key = keys[rng.randrange(len(keys))]
            sampled.extend(groups[key])
            unique.add(key)
            draws += 1
    return sampled, {
        "enabled": True,
        "scheme": "opponent_stratified_match_cluster_v1",
        "seed": int(seed),
        "source_clusters": sum(len(groups) for groups in by_opponent.values()),
        "sampled_draws": draws,
        "unique_sampled_clusters": len(unique),
        "effective_rows": len(sampled),
    }


def _load_base_model(
    path: str | Path, *, device: str
) -> tuple[OpponentAwareMultiTaskNet, dict[str, Any], str]:
    source = Path(path)
    source_bytes = source.read_bytes()
    payload = json.loads(source_bytes)
    meta = payload.get("meta") or {}
    if meta.get("format") not in {"opp_multitask_gru_v1", "opp_multitask_gru_v2"}:
        raise ValueError("unsupported base opponent model format")
    model_meta = meta.get("model") or {}
    model = OpponentAwareMultiTaskNet(
        int(meta["state_dim"]),
        int(meta["profile_dim"]),
        gru_hidden=int(model_meta["gru_hidden"]),
        hidden=int(model_meta["hidden"]),
        latent=int(model_meta["latent"]),
        cross_hidden=int(model_meta["cross_hidden"]),
        head_hidden=int(model_meta["head_hidden"]),
        dropout=float(model_meta.get("dropout", 0.0)),
        cross_sequence_hidden=int(model_meta.get("cross_sequence_hidden", 0)),
        cross_sequence_encoder=str(
            model_meta.get("cross_sequence_encoder", "gru")
        ),
        cross_transformer_heads=int(
            model_meta.get("cross_transformer_heads", 4)
        ),
        cross_moe_experts=int(model_meta.get("cross_moe_experts", 4)),
    )
    expected = model.state_dict()
    state = {}
    for key, tensor in expected.items():
        if key not in (payload.get("weights") or {}):
            raise ValueError(f"base model is missing weight {key}")
        state[key] = torch.tensor(
            payload["weights"][key], dtype=tensor.dtype
        )
    model.load_state_dict(state, strict=True)
    model.to(device).eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    return model, meta, hashlib.sha256(source_bytes).hexdigest()


def _encode_rows(
    model: OpponentAwareMultiTaskNet,
    rows: list[dict[str, Any]],
    *,
    max_hist: int,
    batch_size: int,
    device: str,
    catastrophe_threshold: float,
    severity_clip: float,
) -> dict[str, torch.Tensor]:
    samples = [build_value_sample(row, max_hist=max_hist) for row in rows]
    latent_rows = []
    target_rows = []
    rule_rows = []
    with torch.no_grad():
        for indices in _chunks(list(range(len(samples))), batch_size):
            batch = [samples[index] for index in indices]
            (
                state,
                profile,
                history,
                lengths,
                cross,
                cross_sequence,
                cross_lengths,
            ) = _context_tensors(batch, max_hist=max_hist, device=device)
            latent = model.encode(
                state,
                profile,
                history,
                lengths,
                cross,
                rule_action=_rule_action_tensor(batch, device),
                cross_sequence=cross_sequence,
                cross_lengths=cross_lengths,
            )
            latent_rows.append(latent.detach().cpu())
            target_rows.extend(
                sample["value_targets"]["delta_vs_rule"] for sample in batch
            )
            rule_rows.extend(int(sample.get("rule_id", 0)) for sample in batch)
    latent_tensor = torch.cat(latent_rows, dim=0) if latent_rows else torch.empty(0, 0)
    hand_delta = torch.tensor(target_rows, dtype=torch.float32)
    valid = torch.isfinite(hand_delta)
    for row_index, rule_id in enumerate(rule_rows):
        if 0 <= rule_id < NUM_ACTIONS:
            valid[row_index, rule_id] = False
    clean_delta = torch.where(valid, hand_delta, torch.zeros_like(hand_delta))
    catastrophe = (clean_delta <= -float(catastrophe_threshold)).float()
    severity = torch.clamp(-clean_delta, min=0.0, max=float(severity_clip))
    return {
        "latent": latent_tensor,
        "valid": valid,
        "catastrophe": catastrophe,
        "severity": severity,
    }


def _auc(labels: np.ndarray, scores: np.ndarray) -> float | None:
    positives = int(labels.sum())
    negatives = len(labels) - positives
    if not positives or not negatives:
        return None
    order = np.argsort(scores, kind="mergesort")
    sorted_scores = scores[order]
    ranks = np.empty(len(scores), dtype=np.float64)
    start = 0
    while start < len(scores):
        stop = start + 1
        while stop < len(scores) and sorted_scores[stop] == sorted_scores[start]:
            stop += 1
        ranks[order[start:stop]] = 0.5 * (start + 1 + stop)
        start = stop
    positive_rank_sum = float(ranks[labels.astype(bool)].sum())
    return (
        positive_rank_sum - positives * (positives + 1) / 2
    ) / (positives * negatives)


def _average_precision(labels: np.ndarray, scores: np.ndarray) -> float | None:
    positives = int(labels.sum())
    if not positives:
        return None
    order = np.argsort(-scores, kind="mergesort")
    ordered = labels[order]
    cumulative = np.cumsum(ordered)
    positive_positions = np.flatnonzero(ordered) + 1
    return float(np.mean(cumulative[positive_positions - 1] / positive_positions))


def _binary_metrics(
    logits: np.ndarray,
    labels: np.ndarray,
    severity_prediction: np.ndarray,
    severity_target: np.ndarray,
    *,
    scale: float = 1.0,
    bias: float = 0.0,
) -> dict[str, Any]:
    if not len(labels):
        return {"samples": 0}
    calibrated = np.clip(scale * logits + bias, -60.0, 60.0)
    probability = 1.0 / (1.0 + np.exp(-calibrated))
    eps = 1e-12
    nll = float(np.mean(
        -labels * np.log(np.maximum(probability, eps))
        - (1.0 - labels) * np.log(np.maximum(1.0 - probability, eps))
    ))
    predicted = probability >= 0.5
    positive = labels.astype(bool)
    negative = ~positive
    true_positive_rate = (
        float(predicted[positive].mean()) if bool(positive.any()) else None
    )
    true_negative_rate = (
        float((~predicted[negative]).mean()) if bool(negative.any()) else None
    )
    balanced = (
        0.5 * (true_positive_rate + true_negative_rate)
        if true_positive_rate is not None and true_negative_rate is not None
        else None
    )
    thresholds = {}
    for threshold in (0.05, 0.1, 0.2, 0.3):
        selected = probability >= threshold
        tp = int(np.logical_and(selected, positive).sum())
        fp = int(np.logical_and(selected, negative).sum())
        thresholds[str(threshold)] = {
            "recall": tp / int(positive.sum()) if bool(positive.any()) else None,
            "precision": tp / (tp + fp) if tp + fp else None,
            "false_positive_rate": (
                fp / int(negative.sum()) if bool(negative.any()) else None
            ),
        }
    severity_mae = (
        float(np.mean(np.abs(severity_prediction[positive] - severity_target[positive])))
        if bool(positive.any()) else None
    )
    return {
        "samples": len(labels),
        "positives": int(positive.sum()),
        "prevalence": float(labels.mean()),
        "nll": nll,
        "brier": float(np.mean((probability - labels) ** 2)),
        "auroc": _auc(labels, probability),
        "average_precision": _average_precision(labels, probability),
        "balanced_accuracy_at_0_5": balanced,
        "severity_mae": severity_mae,
        "thresholds": thresholds,
    }


def _predict_arrays(
    head: CatastropheRiskHead,
    split: dict[str, torch.Tensor],
    *,
    severity_clip: float,
    device: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    head.eval()
    with torch.no_grad():
        raw = head(split["latent"].to(device)).cpu()
    logits = raw[:, :NUM_ACTIONS]
    severity = torch.sigmoid(raw[:, NUM_ACTIONS:]) * float(severity_clip)
    valid = split["valid"]
    return (
        logits[valid].numpy().astype(np.float64),
        split["catastrophe"][valid].numpy().astype(np.float64),
        severity[valid].numpy().astype(np.float64),
        split["severity"][valid].numpy().astype(np.float64),
        valid.numpy(),
    )


def _evaluate(
    head: CatastropheRiskHead,
    split: dict[str, torch.Tensor],
    *,
    severity_clip: float,
    device: str,
    calibration: dict[str, float] | None = None,
) -> dict[str, Any]:
    logits, labels, severity, severity_target, valid_matrix = _predict_arrays(
        head, split, severity_clip=severity_clip, device=device
    )
    calibration = calibration or {"scale": 1.0, "bias": 0.0}
    result = _binary_metrics(
        logits,
        labels,
        severity,
        severity_target,
        scale=float(calibration.get("scale", 1.0)),
        bias=float(calibration.get("bias", 0.0)),
    )
    raw = head(split["latent"].to(device)).detach().cpu()
    all_logits = raw[:, :NUM_ACTIONS].numpy().astype(np.float64)
    all_severity = (
        torch.sigmoid(raw[:, NUM_ACTIONS:]) * float(severity_clip)
    ).numpy().astype(np.float64)
    labels_matrix = split["catastrophe"].numpy().astype(np.float64)
    severity_matrix = split["severity"].numpy().astype(np.float64)
    per_action = {}
    for action_id in range(NUM_ACTIONS):
        mask = valid_matrix[:, action_id]
        per_action[str(action_id)] = _binary_metrics(
            all_logits[mask, action_id],
            labels_matrix[mask, action_id],
            all_severity[mask, action_id],
            severity_matrix[mask, action_id],
            scale=float(calibration.get("scale", 1.0)),
            bias=float(calibration.get("bias", 0.0)),
        )
    result["per_action"] = per_action
    auroc = result.get("auroc")
    severity_mae = result.get("severity_mae")
    result["selection_score"] = float(
        result.get("nll", 10.0)
        + 0.5 * (1.0 - float(auroc) if auroc is not None else 1.0)
        + 0.1 * (
            float(severity_mae) / float(severity_clip)
            if severity_mae is not None else 1.0
        )
    )
    return result


def _fit_platt(
    logits: np.ndarray, labels: np.ndarray
) -> dict[str, Any]:
    if not len(labels) or labels.min() == labels.max():
        return {
            "scale": 1.0,
            "bias": 0.0,
            "samples": len(labels),
            "positives": int(labels.sum()),
            "reason": "insufficient_class_support",
        }
    scales = np.geomspace(0.25, 4.0, 33)
    biases = np.linspace(-6.0, 6.0, 121)
    best = (float("inf"), 1.0, 0.0)
    for scale in scales:
        adjusted = scale * logits[:, None] + biases[None, :]
        losses = np.logaddexp(0.0, adjusted) - labels[:, None] * adjusted
        index = int(np.argmin(losses.mean(axis=0)))
        score = float(losses[:, index].mean())
        if score < best[0]:
            best = (score, float(scale), float(biases[index]))
    raw_nll = float(np.mean(np.logaddexp(0.0, logits) - labels * logits))
    return {
        "scale": best[1],
        "bias": best[2],
        "samples": len(labels),
        "positives": int(labels.sum()),
        "nll_before": raw_nll,
        "nll_after": best[0],
        "method": "global_platt_grid_v1",
    }


def _positive_weights(
    split: dict[str, torch.Tensor], *, mode: str
) -> tuple[torch.Tensor, dict[str, Any]]:
    valid = split["valid"]
    labels = split["catastrophe"]
    weights = []
    report = {}
    for action_id in range(NUM_ACTIONS):
        action_labels = labels[:, action_id][valid[:, action_id]]
        positives = int(action_labels.sum().item())
        negatives = len(action_labels) - positives
        weight = 1.0
        if mode == "sqrt_balanced" and positives and negatives:
            weight = min(10.0, math.sqrt(negatives / positives))
        weights.append(weight)
        report[str(action_id)] = {
            "samples": len(action_labels),
            "positives": positives,
            "negatives": negatives,
            "positive_weight": weight,
        }
    return torch.tensor(weights, dtype=torch.float32), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-model", required=True)
    parser.add_argument("--train", required=True)
    parser.add_argument("--val", required=True)
    parser.add_argument("--calibration", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--catastrophe-threshold", type=float, default=5000.0)
    parser.add_argument("--severity-clip", type=float, default=20000.0)
    parser.add_argument("--hidden", type=int, default=64)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--patience", type=int, default=15)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--lr", type=float, default=5e-4)
    parser.add_argument("--weight-decay", type=float, default=1e-5)
    parser.add_argument("--severity-loss-weight", type=float, default=0.25)
    parser.add_argument(
        "--positive-weight-mode",
        choices=("none", "sqrt_balanced"),
        default="none",
    )
    parser.add_argument("--cluster-bootstrap", action="store_true")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    if min(
        args.catastrophe_threshold,
        args.severity_clip,
        args.hidden,
        args.epochs,
        args.patience,
        args.batch_size,
    ) <= 0:
        raise SystemExit("risk thresholds and training dimensions must be positive")
    if args.severity_loss_weight < 0:
        raise SystemExit("severity loss weight must be non-negative")

    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)
    torch.use_deterministic_algorithms(True, warn_only=True)
    if hasattr(torch.backends, "cudnn"):
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
    device = (
        args.device
        if not str(args.device).startswith("cuda") or torch.cuda.is_available()
        else "cpu"
    )

    base, base_meta, base_sha256 = _load_base_model(
        args.base_model, device=device
    )
    model_meta = base_meta.get("model") or {}
    max_hist = int(model_meta.get("max_hist", 16))
    latent_dim = int(model_meta.get("latent", 0))
    raw_train = load_jsonl(args.train)
    raw_val = load_jsonl(args.val)
    raw_calibration = load_jsonl(args.calibration)
    bootstrap = {
        "enabled": False,
        "scheme": "opponent_stratified_match_cluster_v1",
        "seed": int(args.seed),
        "source_clusters": len({_cluster_key(row) for row in raw_train}),
        "effective_rows": len(raw_train),
    }
    train_rows = list(raw_train)
    if args.cluster_bootstrap:
        train_rows, bootstrap = _cluster_bootstrap(raw_train, seed=args.seed)
    encoded = {
        "train": _encode_rows(
            base,
            train_rows,
            max_hist=max_hist,
            batch_size=args.batch_size,
            device=device,
            catastrophe_threshold=args.catastrophe_threshold,
            severity_clip=args.severity_clip,
        ),
        "val": _encode_rows(
            base,
            raw_val,
            max_hist=max_hist,
            batch_size=args.batch_size,
            device=device,
            catastrophe_threshold=args.catastrophe_threshold,
            severity_clip=args.severity_clip,
        ),
        "calibration": _encode_rows(
            base,
            raw_calibration,
            max_hist=max_hist,
            batch_size=args.batch_size,
            device=device,
            catastrophe_threshold=args.catastrophe_threshold,
            severity_clip=args.severity_clip,
        ),
    }
    positive_weights, class_report = _positive_weights(
        encoded["train"], mode=args.positive_weight_mode
    )
    positive_weights = positive_weights.to(device)
    head = CatastropheRiskHead(latent_dim, args.hidden).to(device)
    optimizer = torch.optim.AdamW(
        head.parameters(), lr=args.lr, weight_decay=args.weight_decay
    )
    train = {key: value.to(device) for key, value in encoded["train"].items()}
    rng = random.Random(args.seed)
    best_score = float("inf")
    best_epoch = 0
    best_state = None
    stale = 0
    print(
        f"[catastrophe] device={device} base={base_sha256[:12]} "
        f"latent={latent_dim} train={len(train_rows)} val={len(raw_val)} "
        f"calibration={len(raw_calibration)} bootstrap={args.cluster_bootstrap}",
        flush=True,
    )
    for epoch in range(1, args.epochs + 1):
        head.train()
        order = list(range(len(train_rows)))
        rng.shuffle(order)
        epoch_loss = 0.0
        batches = _chunks(order, args.batch_size)
        for indices in batches:
            index = torch.tensor(indices, dtype=torch.long, device=device)
            raw = head(train["latent"].index_select(0, index))
            logits = raw[:, :NUM_ACTIONS]
            severity_prediction = torch.sigmoid(raw[:, NUM_ACTIONS:])
            valid = train["valid"].index_select(0, index)
            labels = train["catastrophe"].index_select(0, index)
            severity_target = (
                train["severity"].index_select(0, index) / args.severity_clip
            )
            bce = F.binary_cross_entropy_with_logits(
                logits,
                labels,
                pos_weight=positive_weights,
                reduction="none",
            )
            classification_loss = bce[valid].mean()
            catastrophe_mask = valid & labels.bool()
            severity_loss = (
                F.smooth_l1_loss(
                    severity_prediction[catastrophe_mask],
                    severity_target[catastrophe_mask],
                )
                if bool(catastrophe_mask.any())
                else raw.sum() * 0.0
            )
            loss = classification_loss + args.severity_loss_weight * severity_loss
            optimizer.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(head.parameters(), 1.0)
            optimizer.step()
            epoch_loss += float(loss.item())

        validation = _evaluate(
            head,
            encoded["val"],
            severity_clip=args.severity_clip,
            device=device,
        )
        score = float(validation["selection_score"])
        improved = score < best_score
        if improved:
            best_score = score
            best_epoch = epoch
            best_state = {
                key: tensor.detach().cpu().clone()
                for key, tensor in head.state_dict().items()
            }
            stale = 0
        else:
            stale += 1
        if epoch == 1 or epoch % 5 == 0 or improved:
            print(
                f"[catastrophe] ep={epoch} "
                f"loss={epoch_loss/max(1, len(batches)):.5f} "
                f"val_score={score:.5f} auroc={validation.get('auroc')} "
                f"ap={validation.get('average_precision')}"
                f"{' *best' if improved else ''}",
                flush=True,
            )
        if stale >= args.patience:
            break
    if best_state is None:
        raise SystemExit("catastrophe training produced no checkpoint")
    head.load_state_dict(best_state)

    calibration_arrays = _predict_arrays(
        head,
        encoded["calibration"],
        severity_clip=args.severity_clip,
        device=device,
    )
    calibration = _fit_platt(calibration_arrays[0], calibration_arrays[1])
    validation = _evaluate(
        head,
        encoded["val"],
        severity_clip=args.severity_clip,
        device=device,
        calibration=calibration,
    )
    calibration_evaluation = _evaluate(
        head,
        encoded["calibration"],
        severity_clip=args.severity_clip,
        device=device,
        calibration=calibration,
    )
    payload = {
        "meta": {
            "format": "opp_catastrophe_head_v1",
            "labels": list(base_meta.get("labels") or []),
            "latent_dim": latent_dim,
            "base_model": {
                "path": str(Path(args.base_model).resolve()),
                "sha256": base_sha256,
                "format": base_meta.get("format"),
            },
            "model": {
                "hidden": args.hidden,
                "parameters": sum(p.numel() for p in head.parameters()),
            },
            "risk": {
                "target": "delta_vs_rule",
                "catastrophe_threshold": args.catastrophe_threshold,
                "severity_clip": args.severity_clip,
                "probability_semantics": "p(delta_vs_rule<=-threshold)",
                "severity_semantics": "clipped_negative_delta_given_catastrophe",
                "calibration": calibration,
            },
            "training": {
                "seed": args.seed,
                "best_epoch": best_epoch,
                "epochs_requested": args.epochs,
                "patience": args.patience,
                "batch_size": args.batch_size,
                "learning_rate": args.lr,
                "weight_decay": args.weight_decay,
                "severity_loss_weight": args.severity_loss_weight,
                "positive_weight_mode": args.positive_weight_mode,
                "class_report": class_report,
                "cluster_bootstrap": bootstrap,
                "device_requested": args.device,
                "device_effective": str(device),
                "torch_version": torch.__version__,
                "numpy_version": np.__version__,
                "trainer_sha256": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
                "data": {
                    "train": _manifest(args.train, raw_train),
                    "val": _manifest(args.val, raw_val),
                    "calibration": _manifest(
                        args.calibration, raw_calibration
                    ),
                },
            },
            "validation": validation,
            "calibration": calibration_evaluation,
        },
        "weights": {
            key: tensor.detach().cpu().tolist()
            for key, tensor in head.state_dict().items()
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    print(
        f"[catastrophe] wrote={out} best_epoch={best_epoch} "
        f"val_score={validation['selection_score']:.5f}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
