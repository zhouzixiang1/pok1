import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from state import reconstruct_state, infer_remaining_hands_from_requests
from strategy import get_action


def sanitize_action(action, state, my_chips):
    if state["opponent_allin"]:
        # Only fold or call are valid vs allin in heads-up;
        # convert any non-fold (call, raise, allin) to call.
        return -1 if action == -1 else 0

    if state["to_call"] >= my_chips:
        return 0 if action == 0 else (-2 if action == -2 else -1)

    if action > 0:
        raise_to_total = action + state["my_round_bet"]
        min_raise = state.get("min_raise_action", state["round_raise"])
        if action >= my_chips:
            return -2
        if action < min_raise or raise_to_total <= state["round_bet"]:
            return 0
        return raise_to_total

    if action == 0 and state["to_call"] > 0:
        return 0

    return action


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            break
        payload = json.loads(line)
        requests = payload["requests"]
        req = dict(requests[-1])
        if "remaining_hands" not in req:
            req["remaining_hands"] = infer_remaining_hands_from_requests(requests)
        action = get_action(req, requests)
        state = reconstruct_state(req)
        action = sanitize_action(action, state, req["my_chips"])
        print(json.dumps({"response": int(action)}))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
