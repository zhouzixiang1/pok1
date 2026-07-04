#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent))
from blueprint_contract import CONTRACT_VERSION  # noqa: E402


def _load(path: Path) -> tuple[list[list[float]], list[int], list[float]]:
    features: list[list[float]] = []
    targets: list[int] = []
    weights: list[float] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        features.append([float(v) for v in row["features"]])
        targets.append(1 if int(row["target"]) else 0)
        try:
            weights.append(max(0.05, float(row.get("weight", 1.0))))
        except (TypeError, ValueError):
            weights.append(1.0)
    if features:
        dim = len(features[0])
        bad = [len(row) for row in features if len(row) != dim]
        if bad:
            raise ValueError(f"inconsistent feature dimensions: {bad[:3]} != {dim}")
    return features, targets, weights


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--metrics", type=Path)
    parser.add_argument("--hidden", type=int, default=24)
    parser.add_argument("--epochs", type=int, default=220)
    parser.add_argument("--lr", type=float, default=0.006)
    parser.add_argument("--batch-size", type=int, default=0)
    parser.add_argument("--seed", type=int, default=24)
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
    device = torch.device("cuda" if args.device == "auto" and torch.cuda.is_available() else args.device)

    features, targets, sample_weights = _load(args.input)
    if len(targets) < 20:
        raise SystemExit(f"need at least 20 samples, got {len(targets)}")
    x = torch.tensor(np.asarray(features, dtype=np.float32), device=device)
    y = torch.tensor(np.asarray(targets, dtype=np.int64), device=device)
    w = torch.tensor(np.asarray(sample_weights, dtype=np.float32), device=device)

    idx = list(range(len(targets)))
    random.shuffle(idx)
    split = max(1, int(len(idx) * 0.8))
    tr = torch.tensor(idx[:split], dtype=torch.long, device=device)
    va = torch.tensor(idx[split:] or idx[:1], dtype=torch.long, device=device)

    positives = max(1, sum(targets))
    negatives = max(1, len(targets) - sum(targets))
    total = positives + negatives
    class_weights = torch.tensor(
        [total / (2.0 * negatives), total / (2.0 * positives)],
        dtype=torch.float32,
        device=device,
    )
    model = nn.Sequential(nn.Linear(x.shape[1], args.hidden), nn.ReLU(), nn.Linear(args.hidden, 2)).to(device)
    loss_fn = nn.CrossEntropyLoss(weight=class_weights, reduction="none")
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
        logits = model(x)
        probs = torch.softmax(logits, dim=1)
        pred = probs.argmax(dim=1)
        metrics = {
            "samples": len(targets),
            "input_dim": int(x.shape[1]),
            "hidden_dim": args.hidden,
            "positive": int(sum(targets)),
            "negative": int(len(targets) - sum(targets)),
            "positive_rate": float(sum(targets) / max(1, len(targets))),
            "train_acc": float((pred[tr] == y[tr]).float().mean().item()),
            "val_acc": float((pred[va] == y[va]).float().mean().item()),
            "avg_good_prob": float(probs[:, 1].mean().item()),
            "device": str(device),
            "batch_size": batch_size,
            "contract": CONTRACT_VERSION,
        }

    l1, _, l2 = list(model)
    artifact = {
        "format": "tiny_mlp_advantage_gate_v1",
        "contract": CONTRACT_VERSION,
        "input_dim": int(x.shape[1]),
        "hidden_dim": args.hidden,
        "labels": ["bad", "good"],
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
