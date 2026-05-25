"""Claude v1 Texas Hold'em bot — entry point.

Reads JSON from stdin, delegates to preflop/postflop modules, outputs action to stdout.
"""
import json
import os
import sys

# Make sibling modules importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from preflop import preflop_action
from postflop import postflop_action


# ── Constants ─────────────────────────────────────────────────────────────────

N_PLAYERS = 2
INITIAL_CHIPS = 20000
SMALL_BLIND = 50
BIG_BLIND = 100


# ── State reconstruction ─────────────────────────────────────────────────────

def next_player(pid, offset=1):
    return (pid + offset) % N_PLAYERS


def reconstruct_state(req):
    """Reconstruct the current betting state from the request's history."""
    my_id = req["my_id"]
    dealer_id = req["dealer_id"]

    stacks = [INITIAL_CHIPS] * N_PLAYERS
    committed = [0] * N_PLAYERS
    sb = next_player(dealer_id, 1)
    bb = next_player(dealer_id, 2)

    stacks[sb] -= SMALL_BLIND
    stacks[bb] -= BIG_BLIND
    committed[sb] += SMALL_BLIND
    committed[bb] += BIG_BLIND

    current_round = 0
    round_bet = BIG_BLIND
    round_contrib = [0] * N_PLAYERS
    round_contrib[sb] = SMALL_BLIND
    round_contrib[bb] = BIG_BLIND
    alive = [True] * N_PLAYERS
    allin = [False] * N_PLAYERS

    for record in req.get("history", []):
        record_round = record["round"]
        action_type = record["action_type"]
        pid = record["player_id"]
        action = record["action"]

        if record_round != current_round:
            current_round = record_round
            round_bet = 0
            round_contrib = [0] * N_PLAYERS

        if action_type == "fold":
            alive[pid] = False
            continue
        if not alive[pid] or allin[pid]:
            continue

        if action_type == "allin":
            add = stacks[pid]
            stacks[pid] = 0
            committed[pid] += add
            round_contrib[pid] += add
            allin[pid] = True
            round_bet = max(round_bet, round_contrib[pid])
        elif action_type in ("call", "check"):
            need = max(0, round_bet - round_contrib[pid])
            need = min(need, stacks[pid])
            stacks[pid] -= need
            committed[pid] += need
            round_contrib[pid] += need
        elif action_type == "raise":
            add = max(0, min(action, stacks[pid]))
            stacks[pid] -= add
            committed[pid] += add
            round_contrib[pid] += add
            round_bet = max(round_bet, round_contrib[pid])

    public_cards = req.get("public_cards", [])
    n_public = len(public_cards)
    round_idx = 0 if n_public == 0 else 1 if n_public == 3 else 2 if n_public == 4 else 3

    if current_round != round_idx:
        round_bet = 0
        round_contrib = [0] * N_PLAYERS

    opponent_id = next_player(my_id)
    opponent_allin = allin[opponent_id] and alive[opponent_id]
    my_round_bet = round_contrib[my_id] if alive[my_id] and not allin[my_id] else 0
    to_call = max(0, round_bet - my_round_bet)

    return {
        "round": round_idx,
        "round_bet": round_bet,
        "round_contrib": round_contrib,
        "stacks": stacks,
        "committed": committed,
        "pot": committed[0] + committed[1],
        "to_call": to_call,
        "opponent_allin": opponent_allin,
        "my_round_bet": my_round_bet,
    }


# ── Action sanitization ──────────────────────────────────────────────────────

def sanitize_action(action, state, my_chips):
    """Ensure the action is legal given the current game state."""
    # If opponent is all-in, we can only call, fold, or go all-in
    if state["opponent_allin"]:
        if action == -2:
            return -2
        if action == 0:
            return 0  # call the all-in
        return -1  # fold if we don't want to call

    # Can't afford a call => fold or all-in
    if state["to_call"] >= my_chips:
        return -2 if action == -2 else 0  # call puts us all-in

    # Raise validation
    if action > 0:
        if action >= my_chips:
            return -2  # convert to all-in
        # Raise must be at least to_call or the minimum raise
        if action <= state["to_call"]:
            return 0 if state["to_call"] == 0 else -1
        return action

    # Call/check
    if action == 0:
        return 0

    # Fold
    return -1


# ── Decision dispatch ────────────────────────────────────────────────────────

def decide_action(payload):
    """Core decision: parse request, choose action."""
    requests = payload.get("requests", [])
    responses = payload.get("responses", [])
    if not requests:
        return 0  # safe default: check/call

    req = dict(requests[-1])
    my_cards = req["my_cards"]
    public_cards = req.get("public_cards", [])
    my_id = req["my_id"]
    dealer_id = req["dealer_id"]
    my_chips = req.get("my_chips", INITIAL_CHIPS)

    state = reconstruct_state(req)
    position_is_sb = (my_id == (dealer_id + 1) % N_PLAYERS)

    # Determine street
    n_public = len(public_cards)

    if n_public == 0:
        # Preflop
        action = preflop_action(my_cards, state, my_chips, position_is_sb)
    else:
        # Postflop (flop / turn / river)
        action = postflop_action(my_cards, public_cards, state, my_chips)

    action = sanitize_action(action, state, my_chips)
    return int(action)


# ── Main entry point ─────────────────────────────────────────────────────────

def main():
    payload = json.loads(sys.stdin.read())
    action = decide_action(payload)
    print(json.dumps({"response": action}))


if __name__ == "__main__":
    main()
