"""Neural Bot — 混合神经-符号扑克 Bot。

策略：完全复用 claude_v49 的决策逻辑，但用策略网络信号辅助修正。
主要改进点：
1. 策略网络作为辅助信号，当网络高置信度且规则边界时采纳
2. 保留完整的规则决策链（胜率估算、对手建模、锦标赛压力等）
"""

import json
import os
import sys

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_REF_DIR = os.path.join(os.path.dirname(_BOT_DIR), 'claude_v49')
sys.path.insert(0, _REF_DIR)
sys.path.insert(0, _BOT_DIR)

from state import reconstruct_state, infer_remaining_hands_from_requests
from strategy import get_action as rule_get_action
from nn_strategy import get_strategy
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


def nn_opinion(req):
    """获取策略网络的看法。返回 (label, confidence) 或 None。"""
    strategy = get_strategy()
    if not strategy.is_available():
        return None

    state = reconstruct_state(req)
    display = {
        'pot': state['pot'],
        'player_chips': state['stacks'],
        'round_bet': state['round_bet'],
        'round_player_bet': state['player_bets'],
    }
    features = encode_policy_features(req, display)
    label, confidence = strategy.get_action(features)
    return label, confidence


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

    # 基础决策：完全使用 claude_v49 的规则逻辑
    rule_action = rule_get_action(req, requests)

    # 获取 NN 意见作为辅助信号
    nn = nn_opinion(req)
    if nn is None:
        state = reconstruct_state(req)
        return int(sanitize_action(rule_action, state, req["my_chips"]))

    nn_label, nn_conf = nn
    state = reconstruct_state(req)
    my_chips = req["my_chips"]
    to_call = state["to_call"]
    pot = max(1, state["pot"])

    # ── NN 辅助修正规则 ──

    # 1. 规则说 fold，但 NN 高置信度说 call/raise，且 to_call 不大
    if rule_action == -1 and nn_label >= 1 and nn_conf >= 0.85:
        if to_call > 0 and to_call < pot * 0.3 and to_call < my_chips * 0.05:
            # 小注情况下，NN 认为值得继续
            return 0

    # 2. 规则说 call，但 NN 高置信度说 raise，且我们确实应该加注
    if rule_action == 0 and nn_label == 2 and nn_conf >= 0.90:
        round_raise = state.get('round_raise', state.get('judge_round_raise', 100))
        min_raise_action = state.get('min_raise_action', round_raise)
        if not state['opponent_allin'] and to_call < my_chips:
            # 用适度加注（0.6x pot）
            pot_after_call = pot + to_call
            amount = int(to_call + pot_after_call * 0.6)
            amount = max(min_raise_action, amount)
            if amount < my_chips and amount > to_call:
                return int(sanitize_action(amount, state, my_chips))

    # 3. 规则说 raise，但 NN 高置信度说 fold（规则可能过度激进）
    if rule_action > 0 and nn_label == 0 and nn_conf >= 0.92:
        if to_call > 0:
            return 0  # 降级为 call

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
