#!/usr/bin/env python3
"""训练离散化动作策略网络。

动作空间（6 类，参考 neuron_poker）:
  0 = fold
  1 = check/call
  2 = raise_half_pot (0.5x pot)
  3 = raise_pot (1x pot)
  4 = raise_2pot (2x pot)
  5 = all-in

输入特征包含 equity 估计（如果 equity 网络可用）。
"""

import argparse
import os
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset, random_split


# 动作映射: (action_int) -> category
# -1 -> 0 (fold)
# -2 -> 5 (allin)
# 0 -> 1 (call/check)
# >0 -> 2/3/4 根据大小分桶
def action_to_discrete(action_int, to_call, pot, my_chips, my_round_bet):
    """将连续动作映射到 6 个离散类别。"""
    if action_int == -1:
        return 0  # fold
    if action_int == -2:
        return 5  # allin
    if action_int == 0:
        return 1  # call/check

    # raise — 按相对大小分桶
    raise_amount = action_int  # total raise-to amount
    pot_after_call = pot + to_call
    if pot_after_call <= 0:
        pot_after_call = 1
    # raise_total 是加注到的金额，减去当前已投入的就是额外加注
    extra = raise_amount - my_round_bet
    if extra <= 0:
        return 1  # 无效 raise -> call

    ratio = extra / pot_after_call

    if ratio <= 0.75:
        return 2  # raise_half_pot
    elif ratio <= 1.5:
        return 3  # raise_pot
    else:
        return 4  # raise_2pot


def discrete_to_action(category, state, my_chips, pot, to_call, my_round_bet):
    """将离散类别转换回动作整数。"""
    if category == 0:
        return -1  # fold
    if category == 1:
        return 0   # call/check
    if category == 5:
        return -2  # allin

    # raise sizes
    round_raise = state.get('round_raise', state.get('judge_round_raise', 100))
    min_raise_action = state.get('min_raise_action', round_raise)
    pot_after_call = pot + to_call

    if category == 2:  # half pot
        target = int(to_call + pot_after_call * 0.5)
    elif category == 3:  # pot
        target = int(to_call + pot_after_call * 1.0)
    else:  # 2x pot
        target = int(to_call + pot_after_call * 2.0)

    target = max(min_raise_action, target)
    if target >= my_chips:
        return -2  # allin
    if target <= to_call:
        return 0
    return target


class DiscretePolicyNet(nn.Module):
    """6-class policy network with equity input."""
    def __init__(self, input_dim=201):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, 256),
            nn.ReLU(),
            nn.Linear(256, 128),
            nn.ReLU(),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Linear(64, 6),
        )

    def forward(self, x):
        return self.net(x)


def add_equity_feature(features, equity_data_path, data_features_path):
    """加载 equity 网络预测并作为额外特征添加。"""
    try:
        import sys
        sys.path.insert(0, os.path.dirname(__file__))
        from neural_inference import load_network
        net = load_network('equity', os.path.join(os.path.dirname(__file__), 'data'))
        if net is None:
            return features, features.shape[1]

        data = np.load(data_features_path)
        X = data['features']
        n = len(X)
        equities = np.zeros(n, dtype=np.float32)
        batch = 1000
        for i in range(0, n, batch):
            end = min(i + batch, n)
            equities[i:end] = net.forward(X[i:end]).flatten()

        return np.column_stack([features, equities]), features.shape[1] + 1
    except Exception:
        return features, features.shape[1]


def extract_discrete_labels(data_path, output_path):
    """从回放数据重新提取 6 类离散标签。"""
    print("加载策略数据...")
    data = np.load(data_path)
    X = data['features']
    y_action = data['labels'].astype(int)

    # 需要从原始回放重新提取完整信息来计算 to_call/pot
    # 简化：从特征中反推
    # 特征布局: [0:123] = cards, [123:135] = game state, ...
    # offset 123+3 = to_call (normalized), offset 123+4 = round_bet
    bb = 100.0
    to_calls = (X[:, 123 + 3] * bb * 50.0).astype(int)  # 反归一化
    pots = (X[:, 123 + 0] * bb * 100.0).astype(int)
    my_chips = (X[:, 123 + 1] * bb * 200.0).astype(int)
    my_round_bets = (X[:, 123 + 5] * bb * 50.0).astype(int)

    discrete = np.array([
        action_to_discrete(
            int(y_action[i]),
            max(0, int(to_calls[i])),
            max(1, int(pots[i])),
            max(1, int(my_chips[i])),
            max(0, int(my_round_bets[i]))
        )
        for i in range(len(y_action))
    ], dtype=np.int64)

    # 添加 equity 特征
    X_aug, new_dim = add_equity_feature(X, None, data_path)

    np.savez_compressed(output_path, features=X_aug, labels=discrete)
    names = ['fold', 'call', 'raise_half', 'raise_pot', 'raise_2pot', 'allin']
    unique, counts = np.unique(discrete, return_counts=True)
    print(f"保存 {len(X_aug)} 样本到 {output_path}")
    print(f"特征维度: {X_aug.shape[1]}")
    for val, cnt in zip(unique, counts):
        print(f"  {names[int(val)]}: {cnt} ({cnt/len(discrete)*100:.1f}%)")
    return X_aug, discrete


def train_discrete(data_path, epochs=40, batch_size=512, lr=1e-3):
    print(f"加载离散标签数据: {data_path}")
    data = np.load(data_path)
    X = data['features']
    y = data['labels'].astype(np.int64)

    n = len(X)
    input_dim = X.shape[1]
    n_train = int(n * 0.8)
    dataset = TensorDataset(
        torch.tensor(X, dtype=torch.float32),
        torch.tensor(y, dtype=torch.long),
    )
    train_ds, val_ds = random_split(dataset, [n_train, n - n_train],
                                    generator=torch.Generator().manual_seed(42))
    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=batch_size * 2)

    model = DiscretePolicyNet(input_dim)
    device = 'mps' if torch.backends.mps.is_available() else 'cpu'
    model = model.to(device)

    class_counts = np.bincount(y, minlength=6)
    class_weights = 1.0 / (class_counts + 1)
    class_weights = class_weights / class_weights.sum() * 6
    criterion = nn.CrossEntropyLoss(weight=torch.tensor(class_weights, dtype=torch.float32).to(device))

    optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-5)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    best_val_acc = 0
    best_state = None

    for epoch in range(epochs):
        model.train()
        train_loss = 0
        correct = 0
        for xb, yb in train_loader:
            xb, yb = xb.to(device), yb.to(device)
            logits = model(xb)
            loss = criterion(logits, yb)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            train_loss += loss.item() * len(xb)
            correct += (logits.argmax(1) == yb).sum().item()
        train_loss /= n_train
        train_acc = correct / n_train
        scheduler.step()

        model.eval()
        val_correct = 0
        n_val = 0
        with torch.no_grad():
            for xb, yb in val_loader:
                xb, yb = xb.to(device), yb.to(device)
                logits = model(xb)
                val_correct += (logits.argmax(1) == yb).sum().item()
                n_val += len(xb)
        val_acc = val_correct / n_val

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_state = {k: v.clone() for k, v in model.state_dict().items()}

        if (epoch + 1) % 5 == 0 or epoch == 0:
            print(f"  Epoch {epoch+1}/{epochs}  loss={train_loss:.4f} "
                  f"train_acc={train_acc:.3f}  val_acc={val_acc:.3f}")

    model.load_state_dict(best_state)

    # 导出 NumPy 权重
    save_dir = os.path.join(os.path.dirname(data_path) or 'data')
    weights = {}
    idx = 0
    for layer in model.net:
        if isinstance(layer, nn.Linear):
            weights[f'w{idx}'] = layer.weight.detach().cpu().numpy().T
            weights[f'b{idx}'] = layer.bias.detach().cpu().numpy()
            idx += 1
    save_path = os.path.join(save_dir, 'policy_discrete_weights.npz')
    np.savez(save_path, **weights)
    print(f"已导出: {save_path}")

    # 分类报告
    model.eval()
    names = ['fold', 'call', 'raise_half', 'raise_pot', 'raise_2pot', 'allin']
    all_pred, all_true = [], []
    with torch.no_grad():
        for xb, yb in val_loader:
            xb = xb.to(device)
            pred = model(xb).argmax(1).cpu().numpy()
            all_pred.extend(pred)
            all_true.extend(yb.numpy())
    all_pred, all_true = np.array(all_pred), np.array(all_true)
    print(f"\n最佳验证准确率: {best_val_acc:.3f}")
    for i, name in enumerate(names):
        mask = all_true == i
        if mask.sum() > 0:
            acc = (all_pred[mask] == i).mean()
            print(f"  {name}: {acc:.3f} ({mask.sum()} 样本)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=str, default=None)
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    data_dir = os.path.join(os.path.dirname(__file__), 'data')

    if args.data is None:
        args.data = os.path.join(data_dir, 'policy_data.npz')

    output = args.output or os.path.join(data_dir, 'policy_discrete_data.npz')
    if not os.path.isabs(output):
        output = os.path.join(os.path.dirname(__file__), output)

    X, y = extract_discrete_labels(args.data, output)
    train_discrete(output, args.epochs)


if __name__ == "__main__":
    main()
