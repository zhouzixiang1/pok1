"""Probe bot: check-raiser.

Checks when facing no bet (first to act postflop), then raises big when bet into.
Preflop: calls or min-raises.
Purpose: Tests whether the target bot over-folds to aggression.
"""
import json
import sys


def _compute_state(req):
    """Extract key state variables from the judge request."""
    my_id = req["my_id"]
    dealer_id = req["dealer_id"]
    history = req.get("history", [])
    public_cards = req.get("public_cards", [])
    my_chips = req["my_chips"]

    npc = len(public_cards)
    if npc == 0:
        current_round = 0
    elif npc == 3:
        current_round = 1
    elif npc == 4:
        current_round = 2
    else:
        current_round = 3

    round_bet = 0
    last_raise_to = 100
    round_contrib = [0, 0]
    opponent_folded = False
    opponent_allin = False
    actions_this_round = 0

    if current_round == 0:
        sb = dealer_id
        bb = 1 - dealer_id
        round_contrib[sb] = 50
        round_contrib[bb] = 100
        round_bet = 100
        last_raise_to = 100
    else:
        last_raise_to = 50

    prev_round = -1
    for record in history:
        rec_round = record["round"]
        pid = record["player_id"]
        action_type = record["action_type"]
        action = record["action"]

        if rec_round != prev_round:
            if rec_round > 0:
                round_bet = 0
                last_raise_to = 50
                round_contrib = [0, 0]
                actions_this_round = 0
            prev_round = rec_round

        actions_this_round += 1

        if action_type == "fold":
            if pid != my_id:
                opponent_folded = True
            continue
        if action_type == "allin":
            round_contrib[pid] += 20000
            round_bet = max(round_bet, round_contrib[pid])
            if pid != my_id:
                opponent_allin = True
            continue
        if action_type in ("call", "check"):
            need = max(0, round_bet - round_contrib[pid])
            round_contrib[pid] += need
        elif action_type == "raise":
            target = action
            add = max(0, target - round_contrib[pid])
            round_contrib[pid] += add
            round_bet = max(round_bet, round_contrib[pid])
            last_raise_to = max(last_raise_to, target)

    my_round_bet = round_contrib[my_id]
    to_call = max(0, round_bet - my_round_bet)

    return {
        "round": current_round,
        "round_bet": round_bet,
        "last_raise_to": last_raise_to,
        "my_round_bet": my_round_bet,
        "to_call": to_call,
        "my_chips": my_chips,
        "actions_this_round": actions_this_round,
        "opponent_folded": opponent_folded,
        "opponent_allin": opponent_allin,
    }


def get_action(req):
    """Check-raise strategy: check first, raise big when bet into."""
    state = _compute_state(req)

    if state["opponent_folded"]:
        return 0

    if state["opponent_allin"]:
        return 0

    to_call = state["to_call"]
    my_chips = state["my_chips"]
    my_round_bet = state["my_round_bet"]
    last_raise_to = state["last_raise_to"]
    round_bet = state["round_bet"]
    actions_this_round = state["actions_this_round"]
    current_round = state["round"]

    # Compute minimum legal raise
    if current_round == 0:
        baseline = 100
    else:
        baseline = 50

    if last_raise_to > baseline:
        min_raise_to = last_raise_to * 2 + 1
    else:
        min_raise_to = baseline * 2

    # Preflop: just call (or check if BB)
    if current_round == 0:
        if to_call == 0:
            return 0  # check (BB)
        if to_call >= my_chips:
            return -1  # fold
        return 0  # call

    # Postflop logic
    if to_call == 0:
        # No bet to call: check (to set trap)
        return 0

    # Facing a bet: raise big (3x the bet amount)
    raise_to = round_bet + to_call * 3
    raise_to = max(raise_to, min_raise_to, round_bet + 1)

    additional = raise_to - my_round_bet

    if additional >= my_chips:
        return -2  # all-in

    if to_call >= my_chips:
        return -1  # fold if can't afford

    return raise_to


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            break
        payload = json.loads(line)
        req = dict(payload["requests"][-1])
        action = get_action(req)
        print(json.dumps({"response": int(action)}))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
