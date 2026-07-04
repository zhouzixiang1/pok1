#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from feature_spec import LABELS, feature_dim  # noqa: E402


def _load(path: Path) -> tuple[list[list[float]], list[int]]:
    x, y = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        feat = [float(v) for v in row["features"]]
        if len(feat) != feature_dim():
            raise ValueError(f"feature dim {len(feat)} != {feature_dim()}")
        x.append(feat)
        y.append(int(row["label"]))
    return x, y


def _weights(labels: list[int]) -> list[float]:
    counts = [0] * len(LABELS)
    for label in labels:
        counts[label] += 1
    total = max(1, sum(counts))
    raw = [total / max(1, c) for c in counts]
    mean = sum(raw) / len(raw)
    return [v / mean for v in raw]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--metrics", type=Path)
    parser.add_argument("--hidden", type=int, default=32)
    parser.add_argument("--epochs", type=int, default=180)
    parser.add_argument("--lr", type=float, default=0.008)
    parser.add_argument("--seed", type=int, default=214)
    args = parser.parse_args()
    import numpy as np
    import torch
    import torch.nn as nn
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    features, labels = _load(args.input)
    if len(labels) < 20:
        raise SystemExit(f"need at least 20 samples, got {len(labels)}")
    x = torch.tensor(np.asarray(features, dtype=np.float32))
    y = torch.tensor(np.asarray(labels, dtype=np.int64))
    idx = list(range(len(labels)))
    random.shuffle(idx)
    split = max(1, int(len(idx) * 0.8))
    tr = torch.tensor(idx[:split], dtype=torch.long)
    va = torch.tensor(idx[split:] or idx[:1], dtype=torch.long)
    model = nn.Sequential(nn.Linear(x.shape[1], args.hidden), nn.ReLU(), nn.Linear(args.hidden, len(LABELS)))
    loss_fn = nn.CrossEntropyLoss(weight=torch.tensor(_weights(labels), dtype=torch.float32))
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    for _ in range(args.epochs):
        loss = loss_fn(model(x[tr]), y[tr])
        opt.zero_grad()
        loss.backward()
        opt.step()
    with torch.no_grad():
        pred = model(x).argmax(dim=1)
        probs = torch.softmax(model(x), dim=1)
        metrics = {
            "samples": len(labels),
            "train_acc": float((pred[tr] == y[tr]).float().mean().item()),
            "val_acc": float((pred[va] == y[va]).float().mean().item()),
            "avg_conf": float(probs.max(dim=1).values.mean().item()),
            "class_weights": _weights(labels),
        }
    l1, _, l2 = list(model)
    artifact = {
        "format": "tiny_mlp_policy_v1",
        "input_dim": int(x.shape[1]),
        "hidden_dim": args.hidden,
        "labels": list(LABELS),
        "w1": l1.weight.detach().numpy().astype(float).tolist(),
        "b1": l1.bias.detach().numpy().astype(float).tolist(),
        "w2": l2.weight.detach().numpy().astype(float).tolist(),
        "b2": l2.bias.detach().numpy().astype(float).tolist(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, separators=(",", ":")), encoding="utf-8")
    if args.metrics:
        args.metrics.parent.mkdir(parents=True, exist_ok=True)
        args.metrics.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
