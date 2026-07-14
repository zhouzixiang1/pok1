import itertools
import json
import os
import random
import sys

N_PLAYERS = 2
INITIAL_CHIPS = 20000
SMALL_BLIND = 50
BIG_BLIND = 100


def seed_from_env(style):
    seed = os.environ.get("HL_SEED")
    if seed is None:
        return
    try:
        value = int(seed)
    except ValueError:
        value = sum(ord(ch) for ch in seed)
    random.seed(value + sum(ord(ch) for ch in style))


def card_rank(card):
    return card // 4 + 2


def card_suit(card):
    return card % 4


def next_player(player):
    return (player + 1) % N_PLAYERS


def clamp(value, low, high):
    return max(low, min(high, value))


def round_from_public(public_cards):
    count = len(public_cards)
    if count >= 5:
        return 3
    if count == 4:
        return 2
    if count == 3:
        return 1
    return 0


def preflop_score(cards):
    ranks = sorted([card_rank(c) for c in cards], reverse=True)
    suited = card_suit(cards[0]) == card_suit(cards[1])
    pair = ranks[0] == ranks[1]
    gap = ranks[0] - ranks[1]
    score = 0.12 + (ranks[0] - 2) / 18.0 + (ranks[1] - 2) / 28.0
    if pair:
        score += 0.28 + (ranks[0] - 2) / 26.0
    if suited:
        score += 0.055
    if gap == 1:
        score += 0.045
    elif gap == 2:
        score += 0.025
    elif gap >= 5 and not pair:
        score -= 0.055
    if ranks[0] >= 13:
        score += 0.035
    return clamp(score, 0.05, 0.98)


def evaluate_5(cards):
    ranks = sorted((card_rank(c) for c in cards), reverse=True)
    suits = [card_suit(c) for c in cards]
    counts = {}
    for rank in ranks:
        counts[rank] = counts.get(rank, 0) + 1
    groups = sorted(((count, rank) for rank, count in counts.items()), reverse=True)
    unique = sorted(set(ranks), reverse=True)
    wheel = set([14, 5, 4, 3, 2])
    is_flush = len(set(suits)) == 1
    is_straight = len(unique) == 5 and (unique[0] - unique[-1] == 4 or set(unique) == wheel)
    straight_high = 5 if set(unique) == wheel else unique[0]
    if is_flush and is_straight:
        return (8, straight_high)
    if groups[0][0] == 4:
        return (7, groups[0][1], max(r for r in ranks if r != groups[0][1]))
    if groups[0][0] == 3 and groups[1][0] == 2:
        return (6, groups[0][1], groups[1][1])
    if is_flush:
        return (5, *ranks)
    if is_straight:
        return (4, straight_high)
    if groups[0][0] == 3:
        trips = groups[0][1]
        return (3, trips, *[r for r in ranks if r != trips])
    if groups[0][0] == 2 and groups[1][0] == 2:
        high_pair = max(groups[0][1], groups[1][1])
        low_pair = min(groups[0][1], groups[1][1])
        kicker = max(r for r in ranks if r not in (high_pair, low_pair))
        return (2, high_pair, low_pair, kicker)
    if groups[0][0] == 2:
        pair = groups[0][1]
        return (1, pair, *[r for r in ranks if r != pair])
    return (0, *ranks)


def best_score(cards):
    if len(cards) < 5:
        return (0, *sorted((card_rank(c) for c in cards), reverse=True))
    best = None
    for combo in itertools.combinations(cards, 5):
        score = evaluate_5(combo)
        if best is None or score > best:
            best = score
    return best


def board_texture(public_cards):
    if len(public_cards) < 3:
        return {"wet": 0.0, "paired": False, "flushy": False}
    ranks = [card_rank(c) for c in public_cards]
    suits = [card_suit(c) for c in public_cards]
    paired = len(set(ranks)) < len(ranks)
    flushy = max(suits.count(s) for s in set(suits)) >= min(3, len(public_cards))
    span = max(ranks) - min(ranks)
    connected = span <= 5
    wet = 0.2 + (0.28 if flushy else 0.0) + (0.22 if connected else 0.0) + (0.12 if paired else 0.0)
    return {"wet": clamp(wet, 0.0, 1.0), "paired": paired, "flushy": flushy}


def postflop_score(my_cards, public_cards):
    full = my_cards + public_cards
    score = best_score(full)
    category = score[0]
    high = max(card_rank(c) for c in my_cards)
    made = category / 8.0
    if category == 1:
        made += 0.04 if high >= 12 else 0.0
    if category == 0:
        made += (high - 2) / 80.0
    return clamp(made, 0.02, 0.98), category


def reconstruct_state(req):
    my_id = req["my_id"]
    dealer_id = req["dealer_id"]
    public_cards = req.get("public_cards", [])
    current_round = round_from_public(public_cards)
    sb = next_player(dealer_id)
    bb = dealer_id
    round_bet = BIG_BLIND if current_round == 0 else 0
    round_raise = BIG_BLIND if current_round == 0 else BIG_BLIND // 2
    round_contrib = [0, 0]
    if current_round == 0:
        round_contrib[sb] = SMALL_BLIND
        round_contrib[bb] = BIG_BLIND

    for item in req.get("history", []):
        item_round = item.get("round")
        if item_round is None or item_round != current_round:
            continue
        player = item.get("player_id")
        action = int(item.get("action", 0))
        if action == -1:
            round_contrib[player] = -1
        elif action == -2:
            round_contrib[player] = -2
            round_bet = -2
        elif action == 0:
            if round_bet >= 0:
                round_contrib[player] = max(round_contrib[player], round_bet)
        else:
            round_contrib[player] = max(0, round_contrib[player]) + action
            round_raise = max(round_raise, action)
            round_bet = max(round_bet, round_contrib[player])

    my_contrib = max(0, round_contrib[my_id])
    to_call = 0 if round_bet < 0 else max(0, round_bet - my_contrib)
    min_raise = max(BIG_BLIND, round_raise * 2 - my_contrib)
    min_raise = max(min_raise, to_call + BIG_BLIND if to_call > 0 else BIG_BLIND)
    return {
        "round": current_round,
        "to_call": to_call,
        "pot": estimate_pot(req, round_contrib),
        "round_bet": round_bet,
        "round_raise": round_raise,
        "my_contrib": my_contrib,
        "min_raise": min_raise,
    }


def estimate_pot(req, round_contrib):
    pot = SMALL_BLIND + BIG_BLIND
    for item in req.get("history", []):
        action = int(item.get("action", 0))
        if action > 0:
            pot += action
        elif action == -2:
            pot += max(0, req.get("my_chips", 0))
    pot += sum(v for v in round_contrib if v > 0)
    return max(BIG_BLIND, pot)


def pressure_profile(req, my_id):
    totals = req.get("total_win_chips", [0, 0])
    hand = req.get("hand", 0)
    max_hand = req.get("max_hand", 50)
    remaining = max(1, max_hand - hand)
    lead = totals[my_id] if len(totals) > my_id else 0
    behind = max(0, -lead)
    ahead = max(0, lead)
    chase = clamp((behind / remaining - 120) / 420.0, 0.0, 1.0)
    protect = clamp((ahead / remaining - 120) / 380.0, 0.0, 1.0)
    return chase, protect, remaining


def legalize(action, state, my_chips):
    if my_chips <= 0:
        return -1
    if action == -2:
        return -2
    if action == -1:
        return -1
    if action <= 0:
        if state["round_bet"] == -2:
            return -1
        if state["to_call"] >= my_chips:
            return -1
        return 0
    if action >= my_chips:
        return -2
    if action < state["min_raise"]:
        return 0 if state["to_call"] < my_chips else -1
    return int(action)


def raise_size(state, my_chips, ratio, floor=100):
    amount = int(max(state["min_raise"], state["pot"] * ratio, floor))
    if amount >= my_chips:
        return -2
    return amount


def choose_action(style, req):
    seed_from_env(style)
    my_cards = req["my_cards"]
    public_cards = req.get("public_cards", [])
    my_chips = req.get("my_chips", INITIAL_CHIPS)
    state = reconstruct_state(req)
    chase, protect, remaining = pressure_profile(req, req["my_id"])
    street = state["round"]
    texture = board_texture(public_cards)
    if street == 0:
        strength = preflop_score(my_cards)
        category = 0
    else:
        strength, category = postflop_score(my_cards, public_cards)
    to_call_ratio = state["to_call"] / max(1, state["pot"])

    if style == "tight_value":
        if state["to_call"] > 0:
            if strength < 0.48 + 0.10 * to_call_ratio and chase < 0.55:
                return legalize(-1, state, my_chips)
            if strength >= 0.76:
                return legalize(raise_size(state, my_chips, 0.72), state, my_chips)
            return legalize(0, state, my_chips)
        if strength >= (0.66 if street == 0 else 0.62):
            return legalize(raise_size(state, my_chips, 0.55), state, my_chips)
        return legalize(0, state, my_chips)

    if style == "loose_aggressive":
        aggression = 0.44 + 0.22 * chase - 0.08 * protect
        if state["to_call"] > 0:
            if strength >= 0.58 or (strength >= 0.38 and random.random() < aggression):
                return legalize(raise_size(state, my_chips, 0.85), state, my_chips)
            if strength >= 0.30 or state["to_call"] <= BIG_BLIND * 2:
                return legalize(0, state, my_chips)
            return legalize(-1, state, my_chips)
        if strength >= 0.32 or random.random() < aggression:
            return legalize(raise_size(state, my_chips, 0.70), state, my_chips)
        return legalize(0, state, my_chips)

    if style == "calling_station":
        if state["to_call"] > 0:
            if state["to_call"] >= my_chips:
                return legalize(-2 if strength >= 0.62 else -1, state, my_chips)
            if to_call_ratio <= 0.85 or strength >= 0.28:
                return legalize(0, state, my_chips)
            return legalize(-1, state, my_chips)
        if strength >= 0.82:
            return legalize(raise_size(state, my_chips, 0.45), state, my_chips)
        return legalize(0, state, my_chips)

    if style == "probe_bluffer":
        if state["to_call"] > 0:
            if strength >= 0.70:
                return legalize(raise_size(state, my_chips, 0.60), state, my_chips)
            if strength >= 0.42 and to_call_ratio <= 0.45:
                return legalize(0, state, my_chips)
            return legalize(-1, state, my_chips)
        checked_to = req.get("history", []) and req.get("history", [])[-1].get("action_type") == "check"
        dry_board = texture["wet"] <= 0.48
        if street > 0 and checked_to and dry_board and strength < 0.62:
            return legalize(raise_size(state, my_chips, 0.32, floor=100), state, my_chips)
        if strength >= 0.68:
            return legalize(raise_size(state, my_chips, 0.58), state, my_chips)
        return legalize(0, state, my_chips)

    if style == "pressure_jammer":
        urgent = chase > 0.50 or remaining <= 8
        if urgent and strength >= 0.38:
            return legalize(-2, state, my_chips)
        if state["to_call"] > 0:
            if strength >= 0.63:
                return legalize(-2 if to_call_ratio > 0.55 else raise_size(state, my_chips, 1.05), state, my_chips)
            if strength >= 0.36 and to_call_ratio <= 0.35:
                return legalize(0, state, my_chips)
            return legalize(-1, state, my_chips)
        if strength >= 0.54:
            return legalize(raise_size(state, my_chips, 1.05), state, my_chips)
        return legalize(0, state, my_chips)

    return legalize(0, state, my_chips)


def main(style):
    payload = json.loads(sys.stdin.read())
    req = payload["requests"][-1]
    action = choose_action(style, req)
    print(json.dumps({"response": int(action)}, separators=(",", ":")))
