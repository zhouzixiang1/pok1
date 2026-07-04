#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import random
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent))
from blueprint_contract import CONTRACT_VERSION  # noqa: E402


def _load(path: Path, target_scale: float) -> tuple[list[list[float]], list[float], list[float], list[float]]:
    features: list[list[float]] = []
    targets: list[float] = []
    raw_delta: list[float] = []
    weights: list[float] = []
    scale = max(1.0, float(target_scale))
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        delta = float(row["delta"])
        features.append([float(v) for v in row["features"]])
        targets.append(math.tanh(delta / scale))
        raw_delta.append(delta)
        try:
            weights.append(max(0.05, min(5.0, float(row.get("weight", 1.0)))))
        except (TypeError, ValueError):
            weights.append(1.0)
    if features:
        dim = len(features[0])
        bad = [len(row) for row in features if len(row) != dim]
        if bad:
            raise ValueError(f"inconsistent feature dimensions: {bad[:3]} != {dim}")
    return features, targets, raw_delta, weights


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--metrics", type=Path)
    parser.add_argument("--hidden", type=int, default=16)
    parser.add_argument("--epochs", type=int, default=300)
    parser.add_argument("--lr", type=float, default=0.004)
    parser.add_argument("--batch-size", type=int, default=0)
    parser.add_argument("--seed", type=int, default=24)
    parser.add_argument("--target-scale", type=float, default=1000.0)
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

    features, targets, raw_delta, sample_weights = _load(args.input, args.target_scale)
    if len(targets) < 20:
        raise SystemExit(f"need at least 20 samples, got {len(targets)}")
    x = torch.tensor(np.asarray(features, dtype=np.float32), device=device)
    y = torch.tensor(np.asarray(targets, dtype=np.float32), device=device).view(-1, 1)
    w = torch.tensor(np.asarray(sample_weights, dtype=np.float32), device=device).view(-1, 1)
    raw = torch.tensor(np.asarray(raw_delta, dtype=np.float32), device=device).view(-1, 1)

    idx = list(range(len(targets)))
    random.shuffle(idx)
    split = max(1, int(len(idx) * 0.8))
    tr = torch.tensor(idx[:split], dtype=torch.long, device=device)
    va = torch.tensor(idx[split:] or idx[:1], dtype=torch.long, device=device)

    model = nn.Sequential(nn.Linear(x.shape[1], args.hidden), nn.ReLU(), nn.Linear(args.hidden, 1)).to(device)
    loss_fn = nn.SmoothL1Loss(reduction="none")
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    batch_size = len(tr) if args.batch_size <= 0 else max(1, int(args.batch_size))
    for _ in range(args.epochs):
        order = tr
        if batch_size < len(tr):
            order = tr[torch.randperm(len(tr), device=device)]
        for start in range(0, len(order), batch_size):
            batch = order[start : start + batch_size]
            loss = (loss_fn(model(x[batch]), y[batch]) * w[batch]).mean()
            opt.zero_grad()
            loss.backward()
            opt.step()

    with torch.no_grad():
        pred = model(x)
        val_pred = pred[va]
        val_y = y[va]
        val_raw = raw[va]
        train_pred = pred[tr]
        train_y = y[tr]
        sign_pred = val_pred >= 0.0
        sign_true = val_raw > 0.0
        metrics = {
            "samples": len(targets),
            "input_dim": int(x.shape[1]),
            "hidden_dim": args.hidden,
            "target": "tanh_delta",
            "target_scale": float(args.target_scale),
            "positive": int(sum(1 for delta in raw_delta if delta > 0.0)),
            "negative": int(sum(1 for delta in raw_delta if delta <= 0.0)),
            "mean_delta": float(sum(raw_delta) / max(1, len(raw_delta))),
            "train_mae": float((train_pred - train_y).abs().mean().item()),
            "val_mae": float((val_pred - val_y).abs().mean().item()),
            "val_rmse": float(torch.sqrt(((val_pred - val_y) ** 2).mean()).item()),
            "val_sign_acc": float((sign_pred == sign_true).float().mean().item()),
            "avg_pred": float(pred.mean().item()),
            "device": str(device),
            "batch_size": batch_size,
            "contract": CONTRACT_VERSION,
        }

    l1, _, l2 = list(model)
    artifact = {
        "format": "tiny_mlp_value_gate_v1",
        "contract": CONTRACT_VERSION,
        "input_dim": int(x.shape[1]),
        "hidden_dim": args.hidden,
        "target": "tanh_delta",
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
