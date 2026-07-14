#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blueprint_contract import CONTRACT_VERSION  # noqa: E402
from feature_spec import LABELS, feature_dim  # noqa: E402


def _load(path: Path) -> tuple[list[list[float]], list[int], list[list[float]], list[float]]:
    x, y, masks, weights = [], [], [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        feat = [float(v) for v in row["features"]]
        if len(feat) != feature_dim():
            raise ValueError(f"feature dim {len(feat)} != {feature_dim()}")
        x.append(feat)
        label = int(row["label"])
        y.append(label)
        mask = row.get("legal_mask")
        if isinstance(mask, list) and len(mask) == len(LABELS):
            masks.append([1.0 if float(v) > 0 else 0.0 for v in mask])
        else:
            fallback = [1.0] * len(LABELS)
            if 0 <= label < len(fallback):
                fallback[label] = 1.0
            masks.append(fallback)
        try:
            weights.append(max(0.05, float(row.get("weight", 1.0))))
        except (TypeError, ValueError):
            weights.append(1.0)
    return x, y, masks, weights


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
    parser.add_argument("--batch-size", type=int, default=0, help="0 keeps full-batch training")
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
    features, labels, legal_masks, sample_weights = _load(args.input)
    if len(labels) < 20:
        raise SystemExit(f"need at least 20 samples, got {len(labels)}")
    x = torch.tensor(np.asarray(features, dtype=np.float32), device=device)
    y = torch.tensor(np.asarray(labels, dtype=np.int64), device=device)
    mask_tensor = torch.tensor(np.asarray(legal_masks, dtype=np.float32), device=device)
    weight_tensor = torch.tensor(np.asarray(sample_weights, dtype=np.float32), device=device)
    idx = list(range(len(labels)))
    random.shuffle(idx)
    split = max(1, int(len(idx) * 0.8))
    tr = torch.tensor(idx[:split], dtype=torch.long, device=device)
    va = torch.tensor(idx[split:] or idx[:1], dtype=torch.long, device=device)
    model = nn.Sequential(nn.Linear(x.shape[1], args.hidden), nn.ReLU(), nn.Linear(args.hidden, len(LABELS))).to(device)
    class_weights = torch.tensor(_weights(labels), dtype=torch.float32, device=device)
    loss_fn = nn.CrossEntropyLoss(weight=class_weights, reduction="none")
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    batch_size = len(tr) if args.batch_size <= 0 else max(1, int(args.batch_size))
    for _ in range(args.epochs):
        order = tr
        if batch_size < len(tr):
            order = tr[torch.randperm(len(tr), device=device)]
        for start in range(0, len(order), batch_size):
            batch = order[start : start + batch_size]
            loss = (loss_fn(model(x[batch]), y[batch]) * weight_tensor[batch]).mean()
            opt.zero_grad()
            loss.backward()
            opt.step()
    with torch.no_grad():
        logits = model(x)
        pred = logits.argmax(dim=1)
        masked_logits = logits.masked_fill(mask_tensor <= 0, -1e9)
        probs = torch.softmax(masked_logits, dim=1)
        masked_pred = probs.argmax(dim=1)
        metrics = {
            "samples": len(labels),
            "train_acc": float((pred[tr] == y[tr]).float().mean().item()),
            "val_acc": float((pred[va] == y[va]).float().mean().item()),
            "masked_train_acc": float((masked_pred[tr] == y[tr]).float().mean().item()),
            "masked_val_acc": float((masked_pred[va] == y[va]).float().mean().item()),
            "avg_conf": float(probs.max(dim=1).values.mean().item()),
            "class_weights": _weights(labels),
            "contract": CONTRACT_VERSION,
            "legal_mask_rows": int(sum(1 for row in legal_masks if any(v <= 0 for v in row))),
            "device": str(device),
            "batch_size": batch_size,
        }
    l1, _, l2 = list(model)
    artifact = {
        "format": "tiny_mlp_policy_v2",
        "contract": CONTRACT_VERSION,
        "input_dim": int(x.shape[1]),
        "hidden_dim": args.hidden,
        "labels": list(LABELS),
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
