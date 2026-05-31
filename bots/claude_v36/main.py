"""
Bot 2 - Bot23-based with 7 directed improvements:
1) Interface fix + decide_action (3 input formats)
2) Preflop 169-hand lookup table
3) CBet / Fold-to-CBet tracking
4) Opponent concept drift detection
5) Improved equity realization
6) 3Bet/4Bet dedicated logic
7) Safe exploitation framework
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
        if action < state["round_raise"] or action <= state["to_call"]:
            return 0 if state["to_call"] == 0 else -1

    if action == 0 and state["to_call"] > 0:
        return 0

    return action


# Improvement 1: normalize_payload supporting 3 input formats
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
    payload = json.loads(input())
    action = decide_action(payload)
    print(json.dumps({"response": int(action)}))


if __name__ == "__main__":
    main()
