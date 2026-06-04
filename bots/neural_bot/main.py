"""Neural Bot v3 — 6-class discrete policy + conservative rule fusion.

Uses discrete policy network (fold/call/raise_half/raise_pot/raise_2pot/allin)
as auxiliary signal, with claude_v49 rules as primary decision engine.
"""

import json
import os
import sys
import numpy as np

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_REF_DIR = os.path.join(os.path.dirname(_BOT_DIR), 'claude_v5')
sys.path.insert(0, _REF_DIR)
sys.path.insert(0, _BOT_DIR)

from state import reconstruct_state, infer_remaining_hands_from_requests
from strategy import get_action as rule_get_action
from neural_inference import load_network
from generate_data import encode_policy_features


def sanitize_action(action, state, my_chips):
    if state["opponent_allin"]:
        return action if action in (-1, -2) else -1
    if state["to_call"] >= my_chips:
        return -2 if action == -2 else -1
    if action > 0:
        if action >= my_chips:
            return -2
        if action < state["round_raise"] or action <= state["to_call"]:
            return 0 if state["to_call"] == 0 else -1
    if action == 0 and state["to_call"] > 0:
        return 0
    return action


# 离散类别名
DISCRETE_NAMES = ['fold', 'call', 'raise_half', 'raise_pot', 'raise_2pot', 'allin']

# 加载网络（模块级别，只加载一次）
_discrete_net = None
_policy_net = None


def _get_discrete_net():
    global _discrete_net
    if _discrete_net is None:
        data_dir = os.path.join(os.path.dirname(__file__), 'data')
        _discrete_net = load_network('policy_discrete', data_dir)
    return _discrete_net


def _get_policy_net():
    global _policy_net
    if _policy_net is None:
        data_dir = os.path.join(os.path.dirname(__file__), 'data')
        _policy_net = load_network('policy_action', data_dir)
    return _policy_net


def nn_opinion(req, state):
    """获取两个网络的预测。返回 (discrete_label, discrete_conf, policy_label, policy_conf)。"""
    display = {
        'pot': state['pot'],
        'player_chips': state['stacks'],
        'round_bet': state['round_bet'],
        'round_player_bet': state['player_bets'],
    }
    features = encode_policy_features(req, display)

    d_label, d_conf = None, 0.0
    p_label, p_conf = None, 0.0

    dnet = _get_discrete_net()
    if dnet is not None:
        probs = dnet.forward(features.reshape(1, -1))[0]
        d_label = int(np.argmax(probs))
        d_conf = float(probs[d_label])

    pnet = _get_policy_net()
    if pnet is not None:
        probs = pnet.forward(features.reshape(1, -1))[0]
        p_label = int(np.argmax(probs))
        p_conf = float(probs[p_label])

    return d_label, d_conf, p_label, p_conf


def discrete_to_action(category, state, my_chips, pot, to_call, my_round_bet):
    """将离散类别转换为具体动作整数。"""
    if category == 0:  # fold
        return -1
    if category == 1:  # call/check
        return 0
    if category == 5:  # allin
        return -2

    # raise sizes: 2=half, 3=pot, 4=2pot
    round_raise = state.get('round_raise', state.get('judge_round_raise', 100))
    min_raise_action = state.get('min_raise_action', round_raise)
    pot_after_call = pot + to_call

    if category == 2:
        target = int(to_call + pot_after_call * 0.5)
    elif category == 3:
        target = int(to_call + pot_after_call * 1.0)
    else:
        target = int(to_call + pot_after_call * 2.0)

    target = max(min_raise_action, target)
    if target >= my_chips:
        return -2
    if target <= to_call:
        return 0
    return target


def decide_action(payload):
    if isinstance(payload, dict) and "requests" in payload:
        requests = list(payload.get("requests") or [])
    elif isinstance(payload, dict):
        requests = [payload]
    else:
        return -1

    if not requests:
        return -1

    req = dict(requests[-1])
    if "remaining_hands" not in req:
        req["remaining_hands"] = infer_remaining_hands_from_requests(requests)

    state = reconstruct_state(req)
    my_chips = req["my_chips"]
    to_call = state["to_call"]
    pot = max(1, state["pot"])

    # 基础决策：规则引擎
    rule_action = rule_get_action(req, requests)

    # NN 意见
    d_label, d_conf, p_label, p_conf = nn_opinion(req, state)

    if d_label is None and p_label is None:
        return int(sanitize_action(rule_action, state, my_chips))

    # ── 融合策略（保守） ──

    # 两个网络都同意 fold → fold（除非 free check）
    both_fold = (d_label == 0 or p_label == 0) and (d_label is None or d_label == 0) and (p_label is None or p_label == 0)
    if both_fold and d_conf >= 0.80 and p_conf >= 0.80:
        if to_call == 0:
            return 0
        return -1

    # 两个网络都同意 raise，且高置信度 → 用离散网络的大小
    both_raise = (d_label is not None and d_label >= 2) and (p_label is not None and p_label == 2)
    if both_raise and d_conf >= 0.85 and p_conf >= 0.85:
        if state['opponent_allin']:
            return -1
        action = discrete_to_action(d_label, state, my_chips, pot, to_call, state['my_round_bet'])
        return int(sanitize_action(action, state, my_chips))

    # 离散网络说 fold（高置信度）+ 规则说 raise → 降级到 call
    if d_label == 0 and d_conf >= 0.90 and rule_action > 0:
        if to_call > 0:
            return 0

    # 离散网络说 raise_2pot/allin（高置信度）+ 规则说 call → 考虑加注
    if d_label in (4, 5) and d_conf >= 0.90 and rule_action == 0:
        if not state['opponent_allin'] and to_call < my_chips:
            action = discrete_to_action(d_label, state, my_chips, pot, to_call, state['my_round_bet'])
            result = sanitize_action(action, state, my_chips)
            if result > 0:
                return int(result)

    # 默认：信任规则
    return int(sanitize_action(rule_action, state, my_chips))


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            break
        payload = json.loads(line)
        action = decide_action(payload)
        print(json.dumps({"response": int(action)}))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
