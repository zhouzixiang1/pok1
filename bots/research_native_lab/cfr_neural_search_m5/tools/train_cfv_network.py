"""Train the RangeCFVNet on oracle-generated CFV labels."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import torch
import torch.nn as nn

from bots.research_native_lab.cfr_neural_search_m5.cfv.label_generator import (
    CFVLabel,
    generate_labels,
)
from bots.research_native_lab.cfr_neural_search_m5.cfv.range_cfv_network import (
    RangeCFVNet,
    RangeCFVNetConfig,
    build_cfv_model,
    encode_public_state,
)


def labels_to_tensors(labels: list[CFVLabel], device: torch.device):
    """Convert CFVLabel list to batched tensors."""
    n = len(labels)
    pub_dim = encode_public_state(labels[0].public_state).shape[0]

    public_features = torch.zeros(n, pub_dim, dtype=torch.float32)
    ranges_p0 = torch.zeros(n, 1326, dtype=torch.float32)
    ranges_p1 = torch.zeros(n, 1326, dtype=torch.float32)
    targets_p0 = torch.zeros(n, 1326, dtype=torch.float32)
    targets_p1 = torch.zeros(n, 1326, dtype=torch.float32)

    for i, label in enumerate(labels):
        public_features[i] = encode_public_state(label.public_state)
        ranges_p0[i] = torch.tensor(label.range_p0, dtype=torch.float32)
        ranges_p1[i] = torch.tensor(label.range_p1, dtype=torch.float32)
        targets_p0[i] = torch.tensor(label.target_cfv_p0, dtype=torch.float32)
        targets_p1[i] = torch.tensor(label.target_cfv_p1, dtype=torch.float32)

    return {
        "public_features": public_features.to(device),
        "ranges_p0": ranges_p0.to(device),
        "ranges_p1": ranges_p1.to(device),
        "targets_p0": targets_p0.to(device),
        "targets_p1": targets_p1.to(device),
    }


def train_cfv_network(
    labels: list[CFVLabel],
    config: dict | None = None,
    output_path: Path | None = None,
) -> dict:
    """Train RangeCFVNet on the given labels."""
    if config is None:
        config = {"hidden_dim": 128, "layers": 3, "lr": 1e-3, "epochs": 50}

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    torch.manual_seed(config.get("seed", 42))
    if device.type == "cuda":
        torch.cuda.manual_seed_all(config.get("seed", 42))

    net_config = RangeCFVNetConfig(
        trunk_hidden=config.get("hidden_dim", 128),
        trunk_layers=config.get("layers", 3),
    )
    model = build_cfv_model(net_config, seed=config.get("seed", 42)).to(device)
    model.train()
    optimizer = torch.optim.Adam(model.parameters(), lr=config["lr"])

    data = labels_to_tensors(labels, device)
    n = len(labels)
    batch_size = min(config.get("batch_size", 32), n)

    loss_history = []
    t0 = time.time()

    for epoch in range(config["epochs"]):
        perm = torch.randperm(n)
        epoch_loss = 0.0
        n_batches = 0

        for start in range(0, n, batch_size):
            idx = perm[start:start + batch_size]
            pf = data["public_features"][idx]
            r0 = data["ranges_p0"][idx]
            r1 = data["ranges_p1"][idx]
            tp0 = data["targets_p0"][idx]
            tp1 = data["targets_p1"][idx]

            pred = model(pf, r0, r1)  # (batch, 2, 1326)
            pred_p0 = pred[:, 0, :]
            pred_p1 = pred[:, 1, :]
            loss = nn.functional.mse_loss(pred_p0, tp0) + nn.functional.mse_loss(pred_p1, tp1)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            epoch_loss += loss.item()
            n_batches += 1

        avg_loss = epoch_loss / max(n_batches, 1)
        loss_history.append(avg_loss)
        if (epoch + 1) % 10 == 0 or epoch == 0:
            print(f"  Epoch {epoch+1}/{config['epochs']}: loss={avg_loss:.6f}")

    elapsed = time.time() - t0

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        torch.save({
            "model_state_dict": model.state_dict(),
            "config": config,
            "loss_history": loss_history,
            "n_labels": n,
            "elapsed_seconds": elapsed,
        }, output_path)

    return {
        "final_loss": loss_history[-1] if loss_history else float("inf"),
        "loss_history": loss_history,
        "n_labels": n,
        "elapsed_seconds": elapsed,
        "device": str(device),
    }


def main():
    parser = argparse.ArgumentParser(description="Train CFV network")
    parser.add_argument("--n-labels", type=int, default=100)
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--output", default=None)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print(f"Generating {args.n_labels} CFV labels...")
    labels = generate_labels(args.n_labels, seed=args.seed, max_showdown=3)
    print(f"  Generated {len(labels)} labels")

    config = {
        "hidden_dim": 128,
        "layers": 3,
        "lr": 1e-3,
        "epochs": args.epochs,
        "seed": args.seed,
        "batch_size": 32,
    }

    output = Path(args.output) if args.output else None
    print(f"Training for {args.epochs} epochs...")
    result = train_cfv_network(labels, config, output)
    print(f"Done: final_loss={result['final_loss']:.6f}, "
          f"device={result['device']}, "
          f"elapsed={result['elapsed_seconds']:.1f}s")


if __name__ == "__main__":
    main()
