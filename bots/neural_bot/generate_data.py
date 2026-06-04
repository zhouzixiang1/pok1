#!/usr/bin/env python3
"""
从现有回放数据提取训练样本。

两种模式：
1. 胜率数据: --mode equity — 提取 (手牌特征, 胜率) 对
2. 策略数据: --mode policy — 提取 (状态特征, 动作) 对

用法:
    python generate_data.py --mode equity --output data/equity_data.npz
    python generate_data.py --mode policy --output data/policy_data.npz
"""

import argparse
import itertools
import json
import os
import random
import sys
import numpy as np

# 复用 claude_v49 的牌型评估和状态重建
_BOT_DIR = os.path.join(os.path.dirname(__file__), '..', 'claude_v49')
sys.path.insert(0, _BOT_DIR)
from card_utils import evaluate_7, card_number, card_suit

REPLAY_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'web', 'core', 'results', 'match_replay'))

# ──── 特征编码 ────

RANK_DIM = 13
SUIT_DIM = 4
CARD_DIM = RANK_DIM + SUIT_DIM  # 17
HOLE_DIM = 2 * CARD_DIM  # 34
PUBLIC_DIM = 5 * CARD_DIM  # 85
STAGE_DIM = 4

EQUITY_FEATURE_DIM = HOLE_DIM + PUBLIC_DIM + STAGE_DIM  # 123


def card_to_onehot(card):
    rank = card // 4
    suit = card % 4
    vec = np.zeros(CARD_DIM, dtype=np.float32)
    vec[rank] = 1.0
    vec[RANK_DIM + suit] = 1.0
    return vec


def encode_cards(hole_cards, public_cards):
    """编码手牌+公共牌+阶段为特征向量 (123 维)。"""
    features = np.zeros(EQUITY_FEATURE_DIM, dtype=np.float32)
    offset = 0
    for i, card in enumerate(hole_cards):
        features[offset + i * CARD_DIM: offset + (i + 1) * CARD_DIM] = card_to_onehot(card)
    offset += HOLE_DIM
    for i, card in enumerate(public_cards):
        features[offset + i * CARD_DIM: offset + (i + 1) * CARD_DIM] = card_to_onehot(card)
    offset += PUBLIC_DIM
    n_pub = len(public_cards)
    stage_idx = 0 if n_pub == 0 else 1 if n_pub == 3 else 2 if n_pub == 4 else 3
    features[offset + stage_idx] = 1.0
    return features


def compute_equity_mc(hole_cards, public_cards, iterations=3000):
    """蒙特卡洛胜率估算。"""
    used = set(hole_cards + public_cards)
    deck = [c for c in range(52) if c not in used]
    need_public = 5 - len(public_cards)
    wins = 0.0
    for _ in range(iterations):
        random.shuffle(deck)
        opp = deck[:2]
        rest = deck[2:2 + need_public]
        board = public_cards + rest
        my = evaluate_7(hole_cards + board)
        op = evaluate_7(opp + board)
        if my > op:
            wins += 1.0
        elif my == op:
            wins += 0.5
    return wins / iterations


def compute_equity_exact(hole_cards, public_cards):
    """精确枚举胜率（river 阶段）。"""
    used = set(hole_cards + public_cards)
    deck = [c for c in range(52) if c not in used]
    my_score = evaluate_7(hole_cards + public_cards)
    wins = 0.0
    total = 0.0
    for opp in itertools.combinations(deck, 2):
        opp_score = evaluate_7(list(opp) + public_cards)
        if my_score > opp_score:
            wins += 1.0
        elif my_score == opp_score:
            wins += 0.5
        total += 1.0
    return wins / total if total > 0 else 0.5


# ──── 策略特征编码 ────

POLICY_FEATURE_DIM = 200


def encode_policy_features(req, display):
    """编码完整游戏状态为策略特征向量 (200 维)。

    特征布局:
      - 手牌+公共牌+阶段: 123 维 (同 equity)
      - 游戏状态: 12 维 (pot, my_chips, opp_chips, to_call, round_bet, position, ... 归一化)
      - 对手模型: 5 维 (从历史统计)
      - 牌面纹理: 5 维
      - 手牌强度: 3 维
      - 底池赔率+SPR: 2 维
      - 其他: 50 维 (padding)
    """
    features = np.zeros(POLICY_FEATURE_DIM, dtype=np.float32)

    # 1. 牌面特征 (123 维)
    hole = req.get('my_cards', [])
    pub = req.get('public_cards', [])
    features[:EQUITY_FEATURE_DIM] = encode_cards(hole, pub)

    # 2. 游戏状态 (从 display 获取更完整的信息)
    offset = EQUITY_FEATURE_DIM
    pot = display.get('pot', 0)
    my_chips = req.get('my_chips', 20000)
    player_chips = display.get('player_chips', [20000, 20000])
    my_id = req.get('my_id', 0)
    opp_chips = player_chips[1 - my_id] if len(player_chips) > 1 else 20000
    round_bet = display.get('round_bet', 100)
    round_player_bet = display.get('round_player_bet', [0, 0])
    my_bet = round_player_bet[my_id] if len(round_player_bet) > my_id else 0
    to_call = max(0, round_bet - my_bet)

    # 归一化（以 big_blind=100 为单位）
    bb = 100.0
    features[offset + 0] = pot / bb / 100.0
    features[offset + 1] = my_chips / bb / 200.0
    features[offset + 2] = opp_chips / bb / 200.0
    features[offset + 3] = to_call / bb / 50.0
    features[offset + 4] = round_bet / bb / 50.0
    features[offset + 5] = my_bet / bb / 50.0

    # 位置
    dealer_id = req.get('dealer_id', 0)
    sb = (dealer_id + 1) % 2
    features[offset + 6] = 1.0 if my_id == sb else 0.0  # is_sb
    features[offset + 7] = 1.0 if my_id != sb else 0.0  # is_bb (has position in HU)

    # 剩余手牌
    remaining = req.get('remaining_hands', None)
    if remaining is None:
        hand = req.get('hand', 0)
        max_hand = req.get('max_hand', 70)
        remaining = max(0, max_hand - hand)
    features[offset + 8] = remaining / 70.0

    # 底池赔率
    features[offset + 9] = (to_call / (pot + to_call)) / 1.0 if (pot + to_call) > 0 else 0.0

    # SPR (stack-to-pot ratio)
    features[offset + 10] = (my_chips / pot) / 100.0 if pot > 0 else my_chips / 20000.0

    # 总筹码领先/落后
    total_win = req.get('total_win_chips', [0, 0])
    my_win = total_win[my_id] if len(total_win) > my_id else 0
    features[offset + 11] = my_win / bb / 100.0

    # 3-6: 从历史统计对手行为
    history = req.get('history', [])
    opp_id = 1 - my_id
    opp_raises = sum(1 for h in history if h.get('player_id') == opp_id and h.get('action_type') in ('raise', 'allin'))
    opp_folds = sum(1 for h in history if h.get('player_id') == opp_id and h.get('action_type') == 'fold')
    opp_calls = sum(1 for h in history if h.get('player_id') == opp_id and h.get('action_type') == 'call')
    opp_checks = sum(1 for h in history if h.get('player_id') == opp_id and h.get('action_type') == 'check')
    opp_total = opp_raises + opp_folds + opp_calls + opp_checks

    features[offset + 12] = opp_raises / max(opp_total, 1)  # aggression rate
    features[offset + 13] = opp_folds / max(opp_total, 1)
    features[offset + 14] = opp_calls / max(opp_total, 1)
    features[offset + 15] = opp_checks / max(opp_total, 1)
    features[offset + 16] = opp_total / 20.0  # 样本量

    return features


def action_to_label(action_int):
    """将动作整数映射为类别标签: 0=fold, 1=call/check, 2=raise。"""
    if action_int == -1:
        return 0  # fold
    elif action_int == -2:
        return 2  # allin (视为 raise)
    elif action_int == 0:
        return 1  # call/check
    else:
        return 2  # raise


def action_to_discrete_label(action_int, display, my_id):
    """将动作整数映射为 6 类离散标签 (参考 neuron_poker)。

    0=fold, 1=call/check, 2=raise_half_pot, 3=raise_pot, 4=raise_2pot, 5=allin
    """
    if action_int == -1:
        return 0
    if action_int == -2:
        return 5
    if action_int == 0:
        return 1

    # raise — 需要计算 raise 大小占 pot 的比例
    round_player_bet = display.get('round_player_bet', [0, 0])
    round_bet = display.get('round_bet', 100)
    pot = display.get('pot', 0)
    my_bet = round_player_bet[my_id] if len(round_player_bet) > my_id else 0
    to_call = max(0, round_bet - my_bet)
    pot_after_call = pot + to_call
    if pot_after_call <= 0:
        pot_after_call = 1

    # action_int > 0 = raise-to-total
    # 额外加注 = action_int - my_bet
    extra = action_int - my_bet
    if extra <= 0:
        return 1  # 无效

    ratio = extra / pot_after_call
    if ratio <= 0.75:
        return 2  # raise_half_pot
    elif ratio <= 1.5:
        return 3  # raise_pot
    else:
        return 4  # raise_2pot


# ──── 数据提取 ────

def extract_from_replays(mode='equity', mc_iterations=3000):
    """从所有回放文件提取训练数据。"""
    files = sorted([f for f in os.listdir(REPLAY_DIR) if f.endswith('.json')])
    print(f"找到 {len(files)} 个回放文件")

    features_list = []
    labels_list = []

    for file_idx, fname in enumerate(files):
        fpath = os.path.join(REPLAY_DIR, fname)
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        for g in data.get('games', []):
            logs = g.get('logs', [])
            if not logs:
                continue

            winner = g.get('winner', -1)
            bot0_chips = g.get('bot0_chips', 0)

            for i in range(0, len(logs) - 1, 2):
                judge_out = logs[i]
                resp_in = logs[i + 1]

                content = judge_out.get('output', {}).get('content', {})
                display = judge_out.get('output', {}).get('display', {})
                if not content:
                    continue

                for pid_str, req in content.items():
                    # 获取 response
                    resp = resp_in.get(pid_str)
                    if not resp or not isinstance(resp, dict):
                        continue
                    try:
                        action = int(resp.get('response', -1))
                    except (TypeError, ValueError):
                        continue

                    pid = int(pid_str)

                    if mode == 'equity':
                        hole = req.get('my_cards', [])
                        pub = req.get('public_cards', [])
                        if not hole or len(hole) < 2:
                            continue

                        feat = encode_cards(hole, pub)

                        # 判断这步决策最终是否赢了这个游戏
                        # 简化标签: 该玩家是否最终赢了这局 (用 game winner)
                        # 但这不够精确 — 用 MC 估算当前胜率
                        n_pub = len(pub)
                        if n_pub == 5:
                            equity = compute_equity_exact(hole, pub)
                        elif n_pub == 0:
                            # Preflop: 用快速 MC (较少迭代，更快)
                            equity = compute_equity_mc(hole, pub, 1000)
                        else:
                            equity = compute_equity_mc(hole, pub, mc_iterations)

                        features_list.append(feat)
                        labels_list.append(equity)

                    elif mode == 'policy':
                        feat = encode_policy_features(req, display)
                        label = action_to_label(action)
                        features_list.append(feat)
                        labels_list.append(label)

                    elif mode == 'discrete':
                        feat = encode_policy_features(req, display)
                        label = action_to_discrete_label(action, display, pid)
                        features_list.append(feat)
                        labels_list.append(label)

        if (file_idx + 1) % 10 == 0:
            print(f"  已处理 {file_idx + 1}/{len(files)} 个文件, 累计 {len(features_list)} 样本")

    print(f"  完成! 共 {len(features_list)} 样本")
    return np.array(features_list, dtype=np.float32), np.array(labels_list, dtype=np.float32)


def main():
    parser = argparse.ArgumentParser(description="从回放数据提取训练样本")
    parser.add_argument("--mode", choices=['equity', 'policy', 'discrete'], default='equity',
                        help="数据模式: equity (胜率) 或 policy (策略)")
    parser.add_argument("--output", type=str, default=None, help="输出路径")
    parser.add_argument("--mc-iters", type=int, default=2000,
                        help="非 river 阶段的 MC 迭代数 (仅 equity 模式)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    random.seed(args.seed)

    output = args.output
    if output is None:
        output = f"data/{args.mode}_data.npz"
    if not os.path.isabs(output):
        output = os.path.join(os.path.dirname(__file__), output)

    print(f"提取 {args.mode} 训练数据...")
    X, y = extract_from_replays(args.mode, args.mc_iters)

    os.makedirs(os.path.dirname(output), exist_ok=True)
    np.savez_compressed(output, features=X, labels=y)
    print(f"已保存 {len(X)} 样本到 {output}")
    print(f"  特征维度: {X.shape}")
    print(f"  标签分布: mean={y.mean():.3f}, std={y.std():.3f}, "
          f"min={y.min():.3f}, max={y.max():.3f}")

    if args.mode == 'policy':
        unique, counts = np.unique(y, return_counts=True)
        label_names = {0: 'fold', 1: 'call', 2: 'raise'}
        for val, cnt in zip(unique, counts):
            print(f"  {label_names.get(int(val), val)}: {cnt} ({cnt/len(y)*100:.1f}%)")


if __name__ == "__main__":
    main()
