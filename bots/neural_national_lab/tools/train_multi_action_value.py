#!/usr/bin/env python3
"""Train a compact multi-output value/regret head from vector targets."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import random
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent))
from blueprint_contract import CONTRACT_VERSION  # noqa: E402
from feature_spec import LABELS  # noqa: E402


def _load(path: Path, target_scale: float) -> tuple[list[list[float]], list[list[float]], list[list[float]], list[list[float]], list[float]]:
    features: list[list[float]] = []
    targets: list[list[float]] = []
    masks: list[list[float]] = []
    legal_masks: list[list[float]] = []
    weights: list[float] = []
    scale = max(1.0, float(target_scale))
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        feat = [float(value) for value in row["features"]]
        target = [math.tanh(float(value) / scale) for value in row["targets"]]
        mask = [1.0 if int(value) else 0.0 for value in row.get("target_mask", [1] * len(LABELS))]
        legal = [1.0 if int(value) else 0.0 for value in row.get("legal_mask", mask)]
        if len(target) != len(LABELS) or len(mask) != len(LABELS) or len(legal) != len(LABELS):
            raise ValueError(f"target/mask dimension does not match {len(LABELS)} labels")
        features.append(feat)
        targets.append(target)
        masks.append(mask)
        legal_masks.append(legal)
        try:
            weights.append(max(0.05, min(5.0, float(row.get("weight", 1.0)))))
        except (TypeError, ValueError):
            weights.append(1.0)
    if features:
        dim = len(features[0])
        bad = [len(row) for row in features if len(row) != dim]
        if bad:
            raise ValueError(f"inconsistent feature dimensions: {bad[:3]} != {dim}")
    return features, targets, masks, legal_masks, weights


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--metrics", type=Path)
    parser.add_argument("--hidden", type=int, default=32)
    parser.add_argument("--epochs", type=int, default=240)
    parser.add_argument("--lr", type=float, default=0.004)
    parser.add_argument("--batch-size", type=int, default=0)
    parser.add_argument("--seed", type=int, default=24)
    parser.add_argument("--target-scale", type=float, default=1000.0)
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    args = parser.parse_args()

    import numpy as np
    import torch
    import torch.nn as nn

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.device == "cuda" and not torch.cuda.is_available():
        raise SystemExit("CUDA requested but torch.cuda.is_available() is false")
    if args.device == "auto":
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(args.device)

    features, targets, masks, legal_masks, sample_weights = _load(args.input, args.target_scale)
    if len(targets) < args.min_samples:
        raise SystemExit(f"need at least {args.min_samples} samples, got {len(targets)}")
    x = torch.tensor(np.asarray(features, dtype=np.float32), device=device)
    y = torch.tensor(np.asarray(targets, dtype=np.float32), device=device)
    target_mask = torch.tensor(np.asarray(masks, dtype=np.float32), device=device)
    legal_mask = torch.tensor(np.asarray(legal_masks, dtype=np.float32), device=device)
    row_weight = torch.tensor(np.asarray(sample_weights, dtype=np.float32), device=device).view(-1, 1)

    idx = list(range(len(targets)))
    random.shuffle(idx)
    split = max(1, int(len(idx) * 0.8))
    tr = torch.tensor(idx[:split], dtype=torch.long, device=device)
    va = torch.tensor(idx[split:] or idx[:1], dtype=torch.long, device=device)

    model = nn.Sequential(nn.Linear(x.shape[1], args.hidden), nn.ReLU(), nn.Linear(args.hidden, len(LABELS))).to(device)
    loss_fn = nn.SmoothL1Loss(reduction="none")
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    batch_size = len(tr) if args.batch_size <= 0 else max(1, int(args.batch_size))
    for _ in range(args.epochs):
        order = tr
        if batch_size < len(tr):
            order = tr[torch.randperm(len(tr), device=device)]
        for start in range(0, len(order), batch_size):
            batch = order[start : start + batch_size]
            raw_loss = loss_fn(model(x[batch]), y[batch])
            weighted = raw_loss * target_mask[batch] * row_weight[batch]
            denom = torch.clamp((target_mask[batch] * row_weight[batch]).sum(), min=1.0)
            loss = weighted.sum() / denom
            opt.zero_grad()
            loss.backward()
            opt.step()

    with torch.no_grad():
        pred = model(x)

        def masked_mae(rows: torch.Tensor) -> float:
            mask = target_mask[rows]
            denom = torch.clamp(mask.sum(), min=1.0)
            return float(((pred[rows] - y[rows]).abs() * mask).sum().item() / denom.item())

        masked_pred = pred.masked_fill(legal_mask <= 0, -1e9)
        masked_target = y.masked_fill(legal_mask <= 0, -1e9)
        pred_best = masked_pred.argmax(dim=1)
        target_best = masked_target.argmax(dim=1)
        val_pred = pred[va]
        val_y = y[va]
        val_mask = target_mask[va]
        val_rmse = torch.sqrt((((val_pred - val_y) ** 2) * val_mask).sum() / torch.clamp(val_mask.sum(), min=1.0))
        metrics = {
            "samples": len(targets),
            "input_dim": int(x.shape[1]),
            "hidden_dim": args.hidden,
            "labels": list(LABELS),
            "target": "multi_action_tanh_value",
            "target_scale": float(args.target_scale),
            "train_mae": masked_mae(tr),
            "val_mae": masked_mae(va),
            "val_rmse": float(val_rmse.item()),
            "train_best_label_acc": float((pred_best[tr] == target_best[tr]).float().mean().item()),
            "val_best_label_acc": float((pred_best[va] == target_best[va]).float().mean().item()),
            "avg_pred": float(pred.mean().item()),
            "target_mask_density": float(target_mask.mean().item()),
            "legal_mask_density": float(legal_mask.mean().item()),
            "device": str(device),
            "batch_size": batch_size,
            "seed": args.seed,
            "contract": CONTRACT_VERSION,
        }

    l1, _, l2 = list(model)
    artifact = {
        "format": "tiny_mlp_multi_action_value_v1",
        "contract": CONTRACT_VERSION,
        "input_dim": int(x.shape[1]),
        "hidden_dim": args.hidden,
        "labels": list(LABELS),
        "target": "multi_action_tanh_value",
        "target_scale": float(args.target_scale),
        "w1": l1.weight.detach().cpu().numpy().astype(float).tolist(),
        "b1": l1.bias.detach().cpu().numpy().astype(float).tolist(),
        "w2": l2.weight.detach().cpu().numpy().astype(float).tolist(),
        "b2": l2.bias.detach().cpu().numpy().astype(float).tolist(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, separators=(",", ":")), encoding="utf-8")
    if args.metrics:
        args.metrics.parent.mkdir(parents=True, exist_ok=True)
        args.metrics.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
