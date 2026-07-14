"""Probe bot: overbettor.

Always bets 2x pot when possible, otherwise goes all-in.
Purpose: Tests whether the target bot over-folds to large bets.
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

    pot = 0
    round_bet = 0
    last_raise_to = 100
    round_contrib = [0, 0]
    opponent_folded = False
    opponent_allin = False

    # Reset round-scope trackers each hand. The round_bet/last_raise_to are
    # re-zeroed at every round transition inside the replay loop below, but the
    # pre-replay baseline must be set from the CURRENT round so that replay
    # history drift (a previous hand's last_raise_to leaking in) cannot make the
    # opening postflop min-raise / sizing math stale.
    if current_round == 0:
        sb = dealer_id
        bb = 1 - dealer_id
        round_contrib[sb] = 50
        round_contrib[bb] = 100
        pot = 150
        round_bet = 100
        last_raise_to = 100
    else:
        # Postflop: open the round with a clean baseline (round_bet=0,
        # last_raise_to=50=BB//2). Do NOT inherit preflop's last_raise_to here.
        round_bet = 0
        last_raise_to = 50
        round_contrib = [0, 0]

    prev_round = -1
    for record in history:
        rec_round = record["round"]
        pid = record["player_id"]
        action_type = record["action_type"]
        action = record["action"]

        if rec_round != prev_round:
            if rec_round > 0 and prev_round != rec_round:
                round_bet = 0
                last_raise_to = 50
                round_contrib = [0, 0]
            prev_round = rec_round

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
            pot += need
        elif action_type == "raise":
            target = action
            add = max(0, target - round_contrib[pid])
            round_contrib[pid] += add
            round_bet = max(round_bet, round_contrib[pid])
            last_raise_to = max(last_raise_to, target)
            pot += add

    my_round_bet = round_contrib[my_id]
    to_call = max(0, round_bet - my_round_bet)

    return {
        "round": current_round,
        "round_bet": round_bet,
        "last_raise_to": last_raise_to,
        "my_round_bet": my_round_bet,
        "to_call": to_call,
        "my_chips": my_chips,
        "pot": pot,
        "opponent_folded": opponent_folded,
        "opponent_allin": opponent_allin,
    }


def get_action(req):
    """Return 2x pot bet when possible, otherwise all-in."""
    state = _compute_state(req)

    if state["opponent_folded"]:
        return 0

    if state["opponent_allin"]:
        return 0  # call

    to_call = state["to_call"]
    my_chips = state["my_chips"]
    my_round_bet = state["my_round_bet"]
    pot = state["pot"]
    last_raise_to = state["last_raise_to"]
    round_bet = state["round_bet"]

    # Target: raise to 2x pot worth above current round_bet
    # raise_to = round_bet + 2 * pot  (2x pot sizing)
    raise_to = round_bet + 2 * pot

    # Must satisfy minimum raise rules
    if state["round"] == 0:
        baseline = 100
    else:
        baseline = 50

    if last_raise_to > baseline:
        min_raise_to = last_raise_to * 2 + 1
    else:
        min_raise_to = baseline * 2

    raise_to = max(raise_to, min_raise_to, round_bet + 1)

    additional = raise_to - my_round_bet

    if additional <= 0:
        return 0  # check

    if additional >= my_chips:
        return -2  # all-in

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
