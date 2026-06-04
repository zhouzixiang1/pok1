#!/usr/bin/env python3
"""Retrain all networks with fresh replay data from v3-v5 bots.

Usage:
    python train_v2.py                          # Train all
    python train_v2.py --only discrete          # Only discrete net
    python train_v2.py --only policy            # Only 3-class policy
    python train_v2.py --epochs 80              # More epochs
"""

import argparse
import os
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset, random_split


class ImprovedPolicyNet(nn.Module):
    """Policy network with residual connection and dropout."""
    def __init__(self, input_dim, output_dim, hidden=[256, 128, 64], dropout=0.15):
        super().__init__()
        layers = []
        prev = input_dim
        for h in hidden:
            layers.append(nn.Linear(prev, h))
            layers.append(nn.BatchNorm1d(h))
            layers.append(nn.ReLU())
            layers.append(nn.Dropout(dropout))
            prev = h
        layers.append(nn.Linear(prev, output_dim))
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        return self.net(x)


def train_network(X, y, output_dim, name, data_dir, epochs=60, batch_size=512, lr=1e-3):
    n = len(X)
    input_dim = X.shape[1]
    n_train = int(n * 0.85)

    dataset = TensorDataset(
        torch.tensor(X, dtype=torch.float32),
        torch.tensor(y, dtype=torch.long if output_dim > 1 else torch.float32),
    )
    train_ds, val_ds = random_split(dataset, [n_train, n - n_train],
                                    generator=torch.Generator().manual_seed(42))
    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True, drop_last=True)
    val_loader = DataLoader(val_ds, batch_size=batch_size * 2)

    model = ImprovedPolicyNet(input_dim, output_dim)
    device = 'mps' if torch.backends.mps.is_available() else ('cuda' if torch.cuda.is_available() else 'cpu')
    model = model.to(device)
    print(f"  Device: {device}")

    # Class-weighted loss for imbalanced data
    if output_dim > 1:
        class_counts = np.bincount(y.astype(int), minlength=output_dim)
        class_weights = 1.0 / (class_counts + 1)
        class_weights = class_weights / class_weights.sum() * output_dim
        criterion = nn.CrossEntropyLoss(
            weight=torch.tensor(class_weights, dtype=torch.float32).to(device),
            label_smoothing=0.1
        )
    else:
        criterion = nn.MSELoss()

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(optimizer, T_0=20, T_mult=2)

    best_val_acc = 0
    best_state = None
    patience = 15
    no_improve = 0

    for epoch in range(epochs):
        model.train()
        train_loss = 0
        correct = 0
        n_tr = 0
        for xb, yb in train_loader:
            xb, yb = xb.to(device), yb.to(device)
            logits = model(xb)
            if output_dim > 1:
                loss = criterion(logits, yb)
                correct += (logits.argmax(1) == yb).sum().item()
            else:
                loss = criterion(logits.squeeze(), yb)
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            train_loss += loss.item() * len(xb)
            n_tr += len(xb)
        scheduler.step()
        train_loss /= n_tr
        train_acc = correct / n_tr if output_dim > 1 else 0

        model.eval()
        val_correct = 0
        n_val = 0
        with torch.no_grad():
            for xb, yb in val_loader:
                xb, yb = xb.to(device), yb.to(device)
                logits = model(xb)
                if output_dim > 1:
                    val_correct += (logits.argmax(1) == yb).sum().item()
                n_val += len(xb)
        val_acc = val_correct / n_val if output_dim > 1 else 0

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            no_improve = 0
        else:
            no_improve += 1

        if (epoch + 1) % 10 == 0 or epoch == 0:
            print(f"  Epoch {epoch+1}/{epochs}  loss={train_loss:.4f} "
                  f"train_acc={train_acc:.3f}  val_acc={val_acc:.3f}  best={best_val_acc:.3f}")

        if no_improve >= patience:
            print(f"  Early stop at epoch {epoch+1}")
            break

    model.load_state_dict(best_state)

    # Export NumPy weights with BN folding into Linear layers
    # BN(x) = gamma * (x - mean) / sqrt(var + eps) + beta
    # When preceded by Linear: y = Wx + b, then BN(y) = gamma * (Wx + b - mean) / sqrt(var + eps) + beta
    # This is equivalent to: y' = (gamma / sqrt(var+eps)) * W @ x + gamma*(b - mean)/sqrt(var+eps) + beta
    weights = {}
    idx = 0
    layers = list(model.net)
    for i, layer in enumerate(layers):
        if isinstance(layer, nn.Linear):
            w = layer.weight.detach().cpu().numpy()  # (out, in)
            b = layer.bias.detach().cpu().numpy()     # (out,)

            # Check if next layer is BatchNorm1d
            if i + 1 < len(layers) and isinstance(layers[i + 1], nn.BatchNorm1d):
                bn = layers[i + 1]
                gamma = bn.weight.detach().cpu().numpy()    # (out,)
                beta = bn.bias.detach().cpu().numpy()        # (out,)
                mean = bn.running_mean.detach().cpu().numpy()  # (out,)
                var = bn.running_var.detach().cpu().numpy()    # (out,)
                eps = bn.eps

                scale = gamma / np.sqrt(var + eps)  # (out,)
                w = (scale[:, None] * w)  # scale each row
                b = scale * (b - mean) + beta

            weights[f'w{idx}'] = w.T.astype(np.float32)  # (in, out) for numpy @
            weights[f'b{idx}'] = b.astype(np.float32)
            idx += 1
    save_path = os.path.join(data_dir, f'{name}_weights.npz')
    np.savez(save_path, **weights)
    print(f"  Exported: {save_path}")

    # Classification report
    if output_dim > 1:
        model.eval()
        all_pred, all_true = [], []
        with torch.no_grad():
            for xb, yb in val_loader:
                xb = xb.to(device)
                pred = model(xb).argmax(1).cpu().numpy()
                all_pred.extend(pred)
                all_true.extend(yb.numpy())
        all_pred, all_true = np.array(all_pred), np.array(all_true)
        print(f"\n  Best val accuracy: {best_val_acc:.3f}")
        return best_val_acc

    return best_val_acc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=['discrete', 'policy', 'all'], default='all')
    parser.add_argument("--epochs", type=int, default=60)
    parser.add_argument("--lr", type=float, default=1e-3)
    args = parser.parse_args()

    data_dir = os.path.join(os.path.dirname(__file__), 'data')

    if args.only in ('discrete', 'all'):
        print("\n=== Training Discrete Policy Net (6-class) ===")
        data_path = os.path.join(data_dir, 'policy_discrete_v2.npz')
        if not os.path.exists(data_path):
            print(f"  Data not found: {data_path}")
            print("  Run: python generate_data.py --mode discrete --output <abs_path>")
            return
        d = np.load(data_path)
        X, y = d['features'], d['labels'].astype(int)
        print(f"  Data: {X.shape[0]} samples, {X.shape[1]} features")
        train_network(X, y, 6, 'policy_discrete', data_dir, args.epochs, lr=args.lr)

    if args.only in ('policy', 'all'):
        print("\n=== Training 3-class Policy Net ===")
        data_path = os.path.join(data_dir, 'policy_v2.npz')
        if not os.path.exists(data_path):
            print(f"  Data not found: {data_path}")
            return
        d = np.load(data_path)
        X, y = d['features'], d['labels'].astype(int)
        print(f"  Data: {X.shape[0]} samples, {X.shape[1]} features")
        train_network(X, y, 3, 'policy_action', data_dir, args.epochs, lr=args.lr)

    print("\nDone!")


if __name__ == "__main__":
    main()
