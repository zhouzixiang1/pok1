"""Probe bot: always-caller.

Never folds, never raises, always calls or checks.
Purpose: Tests whether the target bot's bluffs are efficient (can it win pots
against a calling station?).
"""
import json
import sys


def get_action(req):
    """Always call or check — never fold, never raise."""
    my_id = req["my_id"]
    dealer_id = req["dealer_id"]
    history = req.get("history", [])
    public_cards = req.get("public_cards", [])
    my_chips = req["my_chips"]

    # Determine round
    npc = len(public_cards)
    if npc == 0:
        current_round = 0
    elif npc == 3:
        current_round = 1
    elif npc == 4:
        current_round = 2
    else:
        current_round = 3

    # Replay history to track bets
    round_bet = 0
    round_contrib = [0, 0]

    if current_round == 0:
        sb = dealer_id
        bb = 1 - dealer_id
        round_contrib[sb] = 50
        round_contrib[bb] = 100
        round_bet = 100

    prev_round = -1
    for record in history:
        rec_round = record["round"]
        pid = record["player_id"]
        action_type = record["action_type"]
        action = record["action"]

        if rec_round != prev_round:
            if rec_round > 0:
                round_bet = 0
                round_contrib = [0, 0]
            prev_round = rec_round

        if action_type == "fold":
            continue
        if action_type == "allin":
            round_contrib[pid] += 20000
            round_bet = max(round_bet, round_contrib[pid])
            continue
        if action_type in ("call", "check"):
            need = max(0, round_bet - round_contrib[pid])
            round_contrib[pid] += need
        elif action_type == "raise":
            target = action
            add = max(0, target - round_contrib[pid])
            round_contrib[pid] += add
            round_bet = max(round_bet, round_contrib[pid])

    my_round_bet = round_contrib[my_id]
    to_call = max(0, round_bet - my_round_bet)

    # If can't afford to call, go all-in (never fold)
    if to_call > 0 and to_call >= my_chips:
        return -2  # all-in (calling station never folds)

    return 0  # call or check


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
