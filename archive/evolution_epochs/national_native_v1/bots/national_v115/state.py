from constants import (
    N_PLAYERS, INITIAL_CHIPS, SMALL_BLIND, BIG_BLIND, TOTAL_HANDS,
    TRASH_STRENGTH_THRESHOLD,
)
from card_utils import card_suit, card_number, next_player, clamp


def estimate_preflop_strength(my_cards):
    r1 = max(card_number(c) for c in my_cards)
    r2 = min(card_number(c) for c in my_cards)
    s1, s2 = card_suit(my_cards[0]), card_suit(my_cards[1])
    gap = r1 - r2
    suited = s1 == s2
    pair = r1 == r2

    if pair:
        return clamp(0.50 + (r1 - 2) / 12.0 * 0.35, 0.50, 0.85)

    high_contrib = (r1 - 2) / 12.0
    low_contrib = (r2 - 2) / 12.0
    base = high_contrib * 0.46 + low_contrib * 0.20

    if suited:
        base += 0.04
    if gap == 1:
        base += 0.025
    elif gap == 2:
        base += 0.015
    elif gap >= 5:
        base -= 0.025

    if r1 == 14:
        base += 0.025
        if r2 >= 10:
            base += 0.015

    return clamp(base, 0.05, 0.85)


def preflop_hand_profile(my_cards):
    ranks = sorted((card_number(card) for card in my_cards), reverse=True)
    suited = card_suit(my_cards[0]) == card_suit(my_cards[1])
    pair = ranks[0] == ranks[1]
    return {
        "high": ranks[0],
        "low": ranks[1],
        "suited": suited,
        "pair": pair,
    }


def is_preflop_3bet_candidate(my_cards):
    profile = preflop_hand_profile(my_cards)
    if profile["pair"]:
        return True
    return profile["high"] == 14 and profile["low"] >= 12


def is_preflop_trash_hand(my_cards, preflop_strength=None):
    profile = preflop_hand_profile(my_cards)
    if profile["pair"]:
        return False

    high = profile["high"]
    low = profile["low"]
    gap = high - low
    suited = profile["suited"]
    strength = estimate_preflop_strength(my_cards) if preflop_strength is None else preflop_strength

    if high == 14:
        return False
    if suited and gap <= 2 and high >= 6:
        return False
    if high >= 11 and low >= 8 and gap <= 4:
        return False

    if strength <= TRASH_STRENGTH_THRESHOLD:
        return True
    if not suited and high <= 10 and low <= 5 and gap >= 3:
        return True
    if not suited and high <= 12 and low <= 4 and gap >= 5:
        return True
    if suited and high <= 9 and low <= 4 and gap >= 4:
        return True
    return False


def preflop_domination_penalty(my_cards):
    """Strength penalty for offsuit broadway combos prone to domination in big pots.

    Crossover import from national_v73 (itself imported from national_v37).
    Hands like KQo/KJo/QJo/ATo enter big pots, flop dominated top-pair-good-kicker,
    and stack off to AK/AQ/AJ overpairs and sets. Returns a penalty subtracted
    from preflop_strength in the bb_vs_raise / sb_vs_reraise call decisions so
    these hands fold preflop instead of calling.

    Mutation (national_v110): penalty widened 0.040 -> 0.048 (+20% increase).
    Parent A (national_v70) carries a wider calling range than Parent B
    (national_v73), so a slightly larger domination penalty compensates by
    shedding more dominated hands from the wider range while preserving the
    richer opponent-model/postflop systems that give v70 its rating edge.
    """
    profile = preflop_hand_profile(my_cards)
    if profile['pair'] or profile['suited']:
        return 0.0
    high, low = profile['high'], profile['low']
    if high < 11 or low < 10:
        return 0.0
    if high == 14 and low >= 12:
        return 0.0
    return 0.048


def get_hand_index(req):
    for key in ("hand", "hand_id", "hand_index", "round_id", "round_index", "game_id", "game_index"):
        if key in req:
            try:
                return int(req[key])
            except (TypeError, ValueError):
                pass
    return None


def get_remaining_hands(req):
    if "hand" in req and "max_hand" in req:
        try:
            return max(0, int(req["max_hand"]) - int(req["hand"]))
        except (TypeError, ValueError):
            pass

    direct_keys = (
        "remaining_hands",
        "remain_hands",
        "hands_left",
        "left_hands",
        "remaining_rounds",
        "remain_rounds",
        "rounds_left",
        "left_rounds",
    )
    for key in direct_keys:
        if key in req:
            try:
                value = int(req[key])
                if value >= 0:
                    return value
            except (TypeError, ValueError):
                pass

    hand_idx = get_hand_index(req)
    if hand_idx is not None:
        candidates = [TOTAL_HANDS - hand_idx, TOTAL_HANDS - hand_idx + 1]
        candidates = [value for value in candidates if value >= 0]
        if candidates:
            return max(candidates)
    return None


def infer_remaining_hands_from_requests(requests):
    if not requests:
        return TOTAL_HANDS

    direct = get_remaining_hands(requests[-1])
    if direct is not None:
        return direct

    hand_indices = [get_hand_index(req) for req in requests]
    hand_indices = [value for value in hand_indices if value is not None]
    if hand_indices:
        return max(0, TOTAL_HANDS - max(hand_indices))

    started_hands = 0
    for req in requests:
        if len(req.get("public_cards", [])) == 0 and len(req.get("history", [])) == 0:
            started_hands += 1
    if started_hands <= 0:
        return TOTAL_HANDS
    return max(0, TOTAL_HANDS - started_hands + 1)


def reconstruct_state(req):
    my_id = req["my_id"]
    dealer_id = req["dealer_id"]

    stacks = [INITIAL_CHIPS] * N_PLAYERS
    committed = [0] * N_PLAYERS
    # Heads-up contract: dealer_id is the small blind; big blind is 1 - dealer_id.
    sb = dealer_id
    bb = 1 - dealer_id

    stacks[sb] -= SMALL_BLIND
    stacks[bb] -= BIG_BLIND
    committed[sb] += SMALL_BLIND
    committed[bb] += BIG_BLIND

    current_round = 0
    round_bet = BIG_BLIND
    last_raise_to = BIG_BLIND
    round_contrib = [0] * N_PLAYERS
    round_contrib[sb] = SMALL_BLIND
    round_contrib[bb] = BIG_BLIND
    alive = [True] * N_PLAYERS
    allin = [False] * N_PLAYERS

    for record in req["history"]:
        record_round = record["round"]
        pid = record["player_id"]
        action = record["action"]
        action_type = record["action_type"]

        if record_round != current_round:
            current_round = record_round
            round_bet = 0
            last_raise_to = SMALL_BLIND
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
            continue

        if action_type in ("call", "check"):
            need = max(0, round_bet - round_contrib[pid])
            need = min(need, stacks[pid])
            stacks[pid] -= need
            committed[pid] += need
            round_contrib[pid] += need
        elif action_type == "raise":
            target = max(round_bet, action)
            add = max(0, min(target - round_contrib[pid], stacks[pid]))
            stacks[pid] -= add
            committed[pid] += add
            round_contrib[pid] += add
            round_bet = max(round_bet, round_contrib[pid])
            last_raise_to = max(last_raise_to, target)

    public_cards = len(req["public_cards"])
    round_idx = 0 if public_cards == 0 else 1 if public_cards == 3 else 2 if public_cards == 4 else 3

    if current_round != round_idx:
        round_bet = 0
        last_raise_to = SMALL_BLIND
        round_contrib = [0] * N_PLAYERS

    player_bets = [0] * N_PLAYERS
    for pid in range(N_PLAYERS):
        if not alive[pid]:
            player_bets[pid] = -1
        elif allin[pid]:
            player_bets[pid] = -2
        else:
            player_bets[pid] = round_contrib[pid]

    opponent_id = next_player(my_id, 1)
    opponent_allin = allin[opponent_id] and alive[opponent_id]
    my_round_bet = 0 if player_bets[my_id] < 0 else player_bets[my_id]
    to_call = max(0, round_bet - my_round_bet)
    min_raise_action = max(0, 2 * last_raise_to + 1 - my_round_bet)
    allin_call_amount = max(
        0,
        min(committed[opponent_id], committed[my_id] + stacks[my_id]) - committed[my_id],
    )

    return {
        "round": round_idx,
        "round_bet": round_bet,
        "round_raise": last_raise_to,
        "judge_round_raise": last_raise_to,
        "min_raise_action": min_raise_action,
        "round_contrib": round_contrib,
        "player_bets": player_bets,
        "stacks": stacks,
        "committed": committed,
        "pot": committed[0] + committed[1],
        "to_call": to_call,
        "opponent_allin": opponent_allin,
        "allin_call_amount": allin_call_amount,
        "my_round_bet": my_round_bet,
    }


def forced_fold_loss_bound(req, state, my_id, remaining_hands):
    if remaining_hands is None or remaining_hands <= 0:
        return None

    loss = state["committed"][my_id]
    current_dealer = req["dealer_id"]
    for offset in range(1, remaining_hands):
        future_dealer = next_player(current_dealer, offset)
        # Heads-up contract: dealer is the small blind; big blind is the other seat.
        future_sb = future_dealer
        future_bb = 1 - future_dealer
        if my_id == future_sb:
            loss += SMALL_BLIND
        elif my_id == future_bb:
            loss += BIG_BLIND
    return loss


def collect_latest_requests_by_hand(requests):
    latest = {}
    fallback_hand = TOTAL_HANDS
    for req in requests:
        hand = get_hand_index(req)
        if hand is None:
            hand = fallback_hand
            fallback_hand += 1
        prev = latest.get(hand)
        if prev is None or len(req.get("history", [])) >= len(prev.get("history", [])):
            latest[hand] = req
    return [latest[hand] for hand in sorted(latest)]


def _self_test_preflop_strength():
    expected = {
        (14,14,True): 0.85, (13,13,True): 0.82, (12,12,True): 0.79,
        (11,11,True): 0.76, (10,10,True): 0.73, (8,8,True): 0.675,
        (5,5,True): 0.59, (2,2,True): 0.50,
        (14,13,True): 0.748, (14,13,False): 0.708,
        (14,12,True): 0.722, (14,12,False): 0.682,
        (14,11,True): 0.690, (14,11,False): 0.650,
        (14,10,True): 0.673, (14,10,False): 0.633,
        (13,12,True): 0.653, (13,12,False): 0.613,
        (13,11,True): 0.627, (13,11,False): 0.587,
        (12,11,True): 0.598, (11,10,True): 0.543,
        (10,9,True): 0.488, (9,8,True): 0.433,
        (8,7,True): 0.378, (7,6,True): 0.323,
        (14,5,True): 0.550, (14,2,False): 0.460,
        (13,9,False): 0.538, (12,9,False): 0.500,
        (7,2,False): 0.167, (11,2,False): 0.320,
    }
    for (r1, r2, suited), exp in expected.items():
        s1, s2 = 0, 0 if suited else 1
        cards = [(r1 - 2) * 4 + s1, (r2 - 2) * 4 + s2]
        actual = estimate_preflop_strength(cards)
        assert abs(actual - exp) < 0.02, f"{r1},{r2} s={suited}: expected {exp}, got {actual}"
    print(f"PREFLOP_STRENGTH_SELF_TEST OK ({len(expected)} hands)")


if __name__ == "__main__":
    _self_test_preflop_strength()
