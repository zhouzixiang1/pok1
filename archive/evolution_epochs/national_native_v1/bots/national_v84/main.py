"""Local decision-test wrapper.

The formal national-platform submission entry is ``national_bot.py``. This
module exists so the repository's legacy scenario tests can exercise the same
strategy through a Botzone-shaped stdin payload.
"""

import json

from strategy import (
    get_action,
    infer_remaining_hands_from_requests,
    reconstruct_state,
    sanitize_action,
)

__all__ = ["sanitize_action"]


def main() -> None:
    payload = json.loads(input())
    requests = payload["requests"]
    req = dict(requests[-1])
    if "remaining_hands" not in req:
        req["remaining_hands"] = infer_remaining_hands_from_requests(requests)
    action = get_action(req, requests)
    state = reconstruct_state(req)
    action = sanitize_action(action, state, req.get("my_chips", 0))
    print(json.dumps({"response": int(action)}))


if __name__ == "__main__":
    main()
