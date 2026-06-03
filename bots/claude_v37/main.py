"""
Crossover child of claude_v7 (Alpha) x claude_v8 (Beta).
- Base: v7 dominant logic with v7's looser jam/shove buffers and deterministic bluff freq
- From v8: big_pot_safety_guard, pot-size EQR adjustments, must_continue_vs_raise with draw_strength,
            choose_overbet_bluff_river implementation, thin_static_showdown_control from round >= 1,
            big pot call margin, modular betting.py structure
- Mutation: SB open threshold lowered 0.49 -> 0.47 for more steal pressure
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from state import reconstruct_state, infer_remaining_hands_from_requests
from strategy import get_action


def sanitize_action(action, state, my_chips):
    if state["opponent_allin"]:
        return action if action in (-1, -2) else -1

    if state["to_call"] >= my_chips:
        return -2 if action == -2 else -1

    if action > 0:
        if action >= my_chips:
            return -2
        min_raise_action = state.get("min_raise_action", state["round_raise"])
        if action < min_raise_action or action <= state["to_call"]:
            return 0 if state["to_call"] == 0 else -1

    if action == 0 and state["to_call"] > 0:
        return 0

    return action


def normalize_payload(payload):
    if isinstance(payload, dict) and "requests" in payload:
        requests = list(payload.get("requests") or [])
        responses = list(payload.get("responses") or [])
        return requests, responses

    if isinstance(payload, dict) and payload.get("command") == "request" and "content" in payload:
        content = payload.get("content") or {}
        if content:
            key = next(iter(content.keys()))
            return [content[key]], []
        return [], []

    if isinstance(payload, dict):
        return [payload], []

    return [], []


def decide_action(payload):
    requests, responses = normalize_payload(payload)
    if not requests:
        return -1
    req = dict(requests[-1])
    if "remaining_hands" not in req:
        req["remaining_hands"] = infer_remaining_hands_from_requests(requests)
    action = get_action(req, requests)
    state = reconstruct_state(req)
    action = sanitize_action(action, state, req["my_chips"])
    return int(action)


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
