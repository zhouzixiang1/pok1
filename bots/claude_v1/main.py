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
        # Convert increment to raise-to-total (engine protocol)
        raise_to_total = action + state["my_round_bet"]
        if action >= my_chips:
            return -2
        min_increment = state.get("min_raise_action", 0)
        if action < min_increment or raise_to_total <= state["round_bet"]:
            return 0
        return raise_to_total

    if action == 0 and state["to_call"] > 0:
        return 0

    return action


def main():
    payload = json.loads(input())
    requests = payload["requests"]
    req = dict(requests[-1])
    if "remaining_hands" not in req:
        req["remaining_hands"] = infer_remaining_hands_from_requests(requests)
    action = get_action(req, requests)
    state = reconstruct_state(req)
    action = sanitize_action(action, state, req["my_chips"])
    print(json.dumps({"response": int(action)}))


if __name__ == "__main__":
    main()
