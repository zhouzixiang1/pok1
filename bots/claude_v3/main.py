"""
Bot 3 - Multi-Style EXP3 Bot: slim entry point.

Loads all modules, provides sanitize_action, normalize_payload, decide_action, main.
"""
import json
import os
import sys
import time

# Ensure local modules are importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from constants import N_PLAYERS
from state import reconstruct_state, infer_remaining_hands_from_requests
import strategy


def sanitize_action(action, state, my_chips):
    if state["opponent_allin"]:
        return action if action in (-1, -2) else -1

    if state["to_call"] >= my_chips:
        return -2 if action == -2 else -1

    if action > 0:
        raise_to_total = action + state["my_round_bet"]
        if action >= my_chips:
            return -2
        if action < state["round_raise"] or raise_to_total <= state["round_bet"]:
            return 0
        return raise_to_total

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
    # Restore EXP3 state from serialized data
    saved_data = payload.get("data") if isinstance(payload, dict) else None
    if saved_data:
        try:
            strategy._exp3_learner = strategy.EXP3MetaLearner.from_dict(
                saved_data if isinstance(saved_data, dict) else json.loads(saved_data)
            )
        except Exception:
            strategy._exp3_learner = strategy.EXP3MetaLearner()

    # Innovation 13: Timing normalization
    start_time = time.time()

    requests, responses = normalize_payload(payload)
    if not requests:
        return -1
    req = dict(requests[-1])
    if "remaining_hands" not in req:
        req["remaining_hands"] = infer_remaining_hands_from_requests(requests)
    action = strategy.get_action(req, requests)
    state = reconstruct_state(req)
    action = sanitize_action(action, state, req["my_chips"])

    # Innovation 13: Timing normalization (anti timing-tell)
    # Sleep to 800ms +/- 100ms jitter to prevent opponents from
    # inferring hand strength from decision latency (Botzone 1s/step).
    # Commented out for local testing -- uncomment before Botzone upload.
    # elapsed_ms = (time.time() - start_time) * 1000.0
    # target_ms = 800.0 + random.randint(-100, 100)
    # import random
    # if elapsed_ms < target_ms:
    #     time.sleep(max(0.0, (target_ms - elapsed_ms) / 1000.0))

    return int(action), strategy._exp3_learner.to_dict()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            break
        payload = json.loads(line)
        action, exp3_state = decide_action(payload)
        print(json.dumps({"response": int(action), "data": exp3_state}))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
