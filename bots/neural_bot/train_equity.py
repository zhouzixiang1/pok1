#!/usr/bin/env python3
"""训练胜率估算网络和策略决策网络。

用法:
    python train_equity.py --data data/equity_data.npz --epochs 50
    python train_policy.py --data data/policy_data.npz --epochs 30
"""

import argparse
import os
import sys
import numpy as np

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset, random_split


# ──── 胜率网络 ────

class EquityNet(nn.Module):
    """3 层全连接: 123 → 128 → 64 → 32 → 1, sigmoid 输出。"""

    def __init__(self):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(123, 128),
            nn.ReLU(),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Linear(64, 32),
            nn.ReLU(),
            nn.Linear(32, 1),
            nn.Sigmoid(),
        )

    def forward(self, x):
        return self.net(x).squeeze(-1)


# ──── 策略网络 ────

class PolicyNet(nn.Module):
    """4 层全连接: 200 → 256 → 128 → 64 → 3 (动作分类) + 1 (加注大小回归)。"""

    def __init__(self):
        super().__init__()
        self.shared = nn.Sequential(
            nn.Linear(200, 256),
            nn.ReLU(),
            nn.Linear(256, 128),
            nn.ReLU(),
        )
        self.action_head = nn.Sequential(
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Linear(64, 3),
        )
        self.raise_head = nn.Sequential(
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Linear(64, 1),
            nn.Sigmoid(),
        )

    def forward(self, x):
        shared = self.shared(x)
        action_logits = self.action_head(shared)
        raise_frac = self.raise_head(shared).squeeze(-1)
        return action_logits, raise_frac


# ──── 导出 NumPy 权重 ────

def export_weights(module, prefix, save_path):
    """将 PyTorch 模型权重导出为 NumPy .npz 格式。"""
    weights = {}
    for i, layer in enumerate(module):
        if isinstance(layer, nn.Linear):
            weights[f'w{i // 2}'] = layer.weight.detach().cpu().numpy().T  # 转置适配 numpy @
            weights[f'b{i // 2}'] = layer.bias.detach().cpu().numpy()
    np.savez(save_path, **weights)
    print(f"已导出权重到 {save_path}")


def export_policy_weights(model, save_dir):
    """分别导出策略网络的动作头和加注头。"""
    # 动作头: shared + action_head 一起作为 200→256→128→64→3
    action_layers = list(model.shared) + list(model.action_head)
    export_weights_from_list(action_layers, os.path.join(save_dir, 'policy_action_weights.npz'))

    # 加注头: shared + raise_head 作为 200→256→128→64→1
    raise_layers = list(model.shared) + list(model.raise_head)
    export_weights_from_list(raise_layers, os.path.join(save_dir, 'policy_raise_weights.npz'))


def export_weights_from_list(layers, save_path):
    """从层列表导出权重。"""
    weights = {}
    idx = 0
    for layer in layers:
        if isinstance(layer, nn.Linear):
            weights[f'w{idx}'] = layer.weight.detach().cpu().numpy().T
            weights[f'b{idx}'] = layer.bias.detach().cpu().numpy()
            idx += 1
    np.savez(save_path, **weights)
    print(f"  已导出: {save_path}")


# ──── 训练 ────

def train_equity(data_path, epochs=50, batch_size=256, lr=1e-3, eval_mode=False):
    """训练胜率估算网络。"""
    print(f"加载胜率数据: {data_path}")
    data = np.load(data_path)
    X = data['features']
    y = data['labels'] if 'labels' in data else data['equities']

    X_tensor = torch.tensor(X, dtype=torch.float32)
    y_tensor = torch.tensor(y, dtype=torch.float32)

    # 80/20 划分
    n = len(X)
    n_train = int(n * 0.8)
    dataset = TensorDataset(X_tensor, y_tensor)
    train_ds, val_ds = random_split(dataset, [n_train, n - n_train],
                                    generator=torch.Generator().manual_seed(42))

    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=batch_size * 2)

    model = EquityNet()
    device = 'mps' if torch.backends.mps.is_available() else 'cpu'
    model = model.to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-5)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    criterion = nn.MSELoss()

    best_val_loss = float('inf')
    best_state = None

    for epoch in range(epochs):
        model.train()
        train_loss = 0
        for xb, yb in train_loader:
            xb, yb = xb.to(device), yb.to(device)
            pred = model(xb)
            loss = criterion(pred, yb)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            train_loss += loss.item() * len(xb)
        train_loss /= n_train

        scheduler.step()

        # 验证
        model.eval()
        val_loss = 0
        val_mae = 0
        n_val = 0
        with torch.no_grad():
            for xb, yb in val_loader:
                xb, yb = xb.to(device), yb.to(device)
                pred = model(xb)
                val_loss += criterion(pred, yb).item() * len(xb)
                val_mae += (pred - yb).abs().sum().item()
                n_val += len(xb)
        val_loss /= n_val
        val_mae /= n_val

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state = {k: v.clone() for k, v in model.state_dict().items()}

        if (epoch + 1) % 5 == 0 or epoch == 0:
            print(f"  Epoch {epoch+1}/{epochs}  train_loss={train_loss:.5f}  "
                  f"val_loss={val_loss:.5f}  val_MAE={val_mae:.4f}")

    # 恢复最佳模型并导出
    model.load_state_dict(best_state)
    save_dir = os.path.dirname(data_path) or 'data'
    export_weights(model.net, 'equity', os.path.join(save_dir, 'equity_weights.npz'))

    # 最终评估
    model.eval()
    all_errors = []
    with torch.no_grad():
        for xb, yb in val_loader:
            xb = xb.to(device)
            pred = model(xb).cpu().numpy()
            all_errors.extend(np.abs(pred - yb.numpy()).tolist())
    errors = np.array(all_errors)
    print(f"\n最终评估: MAE={errors.mean():.4f}, "
          f"中位数误差={np.median(errors):.4f}, "
          f"90th 百分位={np.percentile(errors, 90):.4f}")


def train_policy(data_path, epochs=30, batch_size=256, lr=1e-3):
    """训练策略决策网络。"""
    print(f"加载策略数据: {data_path}")
    data = np.load(data_path)
    X = data['features']
    y_action = data['labels'].astype(np.int64)

    # 动作分类
    n = len(X)
    X_tensor = torch.tensor(X, dtype=torch.float32)
    y_tensor = torch.tensor(y_action, dtype=torch.long)

    n_train = int(n * 0.8)
    dataset = TensorDataset(X_tensor, y_tensor)
    train_ds, val_ds = random_split(dataset, [n_train, n - n_train],
                                    generator=torch.Generator().manual_seed(42))

    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=batch_size * 2)

    model = PolicyNet()
    device = 'mps' if torch.backends.mps.is_available() else 'cpu'
    model = model.to(device)

    # 类别权重（平衡 fold/call/raise）
    class_counts = np.bincount(y_action, minlength=3)
    class_weights = 1.0 / (class_counts + 1)
    class_weights = class_weights / class_weights.sum() * 3
    criterion = nn.CrossEntropyLoss(weight=torch.tensor(class_weights, dtype=torch.float32).to(device))

    optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-5)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    best_val_acc = 0
    best_state = None

    for epoch in range(epochs):
        model.train()
        train_loss = 0
        train_correct = 0
        for xb, yb in train_loader:
            xb, yb = xb.to(device), yb.to(device)
            logits, _ = model(xb)
            loss = criterion(logits, yb)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            train_loss += loss.item() * len(xb)
            train_correct += (logits.argmax(dim=1) == yb).sum().item()
        train_loss /= n_train
        train_acc = train_correct / n_train

        scheduler.step()

        # 验证
        model.eval()
        val_correct = 0
        n_val = 0
        val_loss = 0
        with torch.no_grad():
            for xb, yb in val_loader:
                xb, yb = xb.to(device), yb.to(device)
                logits, _ = model(xb)
                loss = criterion(logits, yb)
                val_loss += loss.item() * len(xb)
                val_correct += (logits.argmax(dim=1) == yb).sum().item()
                n_val += len(xb)
        val_acc = val_correct / n_val
        val_loss /= n_val

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_state = {k: v.clone() for k, v in model.state_dict().items()}

        if (epoch + 1) % 5 == 0 or epoch == 0:
            print(f"  Epoch {epoch+1}/{epochs}  train_loss={train_loss:.4f} "
                  f"train_acc={train_acc:.3f}  val_acc={val_acc:.3f}")

    # 恢复最佳并导出
    model.load_state_dict(best_state)
    save_dir = os.path.dirname(data_path) or 'data'
    export_policy_weights(model, save_dir)
    print(f"\n最佳验证准确率: {best_val_acc:.3f}")

    # 分类报告
    model.eval()
    all_pred = []
    all_true = []
    with torch.no_grad():
        for xb, yb in val_loader:
            xb = xb.to(device)
            logits, _ = model(xb)
            pred = logits.argmax(dim=1).cpu().numpy()
            all_pred.extend(pred)
            all_true.extend(yb.numpy())

    all_pred = np.array(all_pred)
    all_true = np.array(all_true)
    names = ['fold', 'call', 'raise']
    for i, name in enumerate(names):
        mask = all_true == i
        if mask.sum() > 0:
            acc = (all_pred[mask] == i).mean()
            print(f"  {name}: 准确率={acc:.3f} ({mask.sum()} 样本)")


def main():
    parser = argparse.ArgumentParser(description="训练神经网络")
    parser.add_argument("--mode", choices=['equity', 'policy'], required=True)
    parser.add_argument("--data", type=str, required=True, help="训练数据 .npz 路径")
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--lr", type=float, default=1e-3)
    args = parser.parse_args()

    if args.mode == 'equity':
        train_equity(args.data, args.epochs, args.batch_size, args.lr)
    else:
        train_policy(args.data, args.epochs, args.batch_size, args.lr)


if __name__ == "__main__":
    main()
