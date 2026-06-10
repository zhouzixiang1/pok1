"""Probe bot: min-bettor.

Always raises to the minimum legal amount when possible, otherwise calls.
Purpose: Tests whether the target bot over-folds to small bets.
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

    # Determine round from public cards
    npc = len(public_cards)
    if npc == 0:
        current_round = 0  # preflop
    elif npc == 3:
        current_round = 1  # flop
    elif npc == 4:
        current_round = 2  # turn
    else:
        current_round = 3  # river

    # Replay history to track bets
    round_bet = 0
    last_raise_to = 100  # big blind baseline for preflop
    my_round_bet = 0
    opponent_folded = False
    opponent_allin = False

    if current_round == 0:
        # Preflop: blinds already posted
        sb = dealer_id  # heads-up: dealer is SB
        bb = 1 - dealer_id
        round_bet = 100
        last_raise_to = 100
        my_round_bet = 100 if my_id == bb else 50
        round_contrib = [0, 0]
        round_contrib[sb] = 50
        round_contrib[bb] = 100
    else:
        last_raise_to = 50  # postflop baseline
        round_contrib = [0, 0]

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
            prev_round = rec_round

        if action_type == "fold":
            if pid != my_id:
                opponent_folded = True
            continue
        if action_type == "allin":
            round_contrib[pid] += 20000  # approximate
            round_bet = max(round_bet, round_contrib[pid])
            if pid != my_id:
                opponent_allin = True
            continue
        if action_type in ("call", "check"):
            need = max(0, round_bet - round_contrib[pid])
            round_contrib[pid] += need
        elif action_type == "raise":
            target = action  # raise-to-total
            add = max(0, target - round_contrib[pid])
            round_contrib[pid] += add
            round_bet = max(round_bet, round_contrib[pid])
            last_raise_to = max(last_raise_to, target)

    # Recompute my_round_bet for current round
    my_round_bet = round_contrib[my_id] if round_contrib[my_id] >= 0 else 0
    to_call = max(0, round_bet - my_round_bet)

    return {
        "round": current_round,
        "round_bet": round_bet,
        "last_raise_to": last_raise_to,
        "my_round_bet": my_round_bet,
        "to_call": to_call,
        "my_chips": my_chips,
        "opponent_folded": opponent_folded,
        "opponent_allin": opponent_allin,
    }


def get_action(req):
    """Return minimum legal raise when possible, otherwise call."""
    state = _compute_state(req)

    if state["opponent_folded"]:
        return 0  # check (hand already won)

    if state["opponent_allin"]:
        return 0  # call vs allin

    to_call = state["to_call"]
    my_chips = state["my_chips"]
    my_round_bet = state["my_round_bet"]
    last_raise_to = state["last_raise_to"]
    round_bet = state["round_bet"]

    # Can't afford to call -> fold
    if to_call > 0 and to_call >= my_chips:
        return -1  # fold

    # Compute minimum legal raise-to-total
    if state["round"] == 0:
        baseline = 100  # big blind
    else:
        baseline = 50  # big blind // 2

    if last_raise_to > baseline:
        min_raise_to = last_raise_to * 2 + 1  # strictly > 2x last raise
    else:
        min_raise_to = baseline * 2  # first raise: >= 2x baseline

    # Ensure raise_to exceeds current round_bet
    min_raise_to = max(min_raise_to, round_bet + 1)

    additional = min_raise_to - my_round_bet

    if additional >= my_chips:
        # Can't raise (would be all-in or more), just call
        return 0

    if additional > 0:
        return min_raise_to

    # No bet to raise, can check
    return 0


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
