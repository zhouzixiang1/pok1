import bisect
import itertools
import json
import os
import random
import re
import socket
import sys
import time

N_PLAYERS = 2
INITIAL_CHIPS = 20000
SMALL_BLIND = 50
BIG_BLIND = 100
TOTAL_HANDS = 70
LOCK_WIN_MARGIN = 1500
GUOSAI_SEND_GAP_SECONDS = 0.08
GUOSAI_LOG_MAX_CHARS = 500
GUOSAI_DECISION_TIME_LIMIT_SECONDS = 55.0
GUOSAI_DECISION_TAIL_SAFETY_SECONDS = 0.5
MONTE_CARLO_BATCH_SIZE = 500
MONTE_CARLO_STOP_STANDARD_ERROR = 0.006
MONTE_CARLO_CLOSE_STANDARD_ERROR = 0.005
MONTE_CARLO_CRITICAL_STANDARD_ERROR = 0.004
MONTE_CARLO_DEADLINE_MARGIN_SECONDS = 0.05
EQUITY_GAP_NEAR = 0.10
EQUITY_GAP_CLOSE = 0.05
EQUITY_GAP_EXTREME = 0.02
EQUITY_REFINEMENT_MIN_SECONDS = 0.75

HAND_CLASS_SCORE = [0.08, 0.22, 0.40, 0.58, 0.69, 0.76, 0.84, 0.93, 0.98]
SIMULATIONS_BY_PUBLIC_COUNT = {
    0: 500,
    3: 700,
    4: 900,
    5: 0,
}
EXTRA_SIMULATIONS_BY_PUBLIC_COUNT = {
    0: 200,
    3: 220,
    4: 180,
}


def hl_trace_enabled():
    return os.environ.get("HL_TRACE") == "1"


def seed_hl_random():
    seed = os.environ.get("HL_SEED")
    if seed is None:
        return None
    try:
        seed_value = int(seed)
    except ValueError:
        seed_value = seed
    random.seed(seed_value)
    return seed


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def guosai_decision_time_limit():
    return max(
        0.1,
        env_float("GUOSAI_DECISION_TIME_LIMIT", GUOSAI_DECISION_TIME_LIMIT_SECONDS),
    )


def trace_float(value):
    if value is None:
        return None
    return round(float(value), 4)


def infer_trace_branch(trace, action):
    if trace.get("decision_branch"):
        return trace["decision_branch"]
    if trace.get("anti_lock_pressure"):
        return "anti_lock_pressure"
    if action == -2:
        return "allin_or_call_allin"
    if action == -1:
        return "fold_defense"
    if action > 0 and (trace.get("check_probe") or trace.get("small_probe")):
        return "probe_raise"
    if action > 0 and trace.get("semi_bluff"):
        return "semi_bluff_raise"
    if action > 0 and trace.get("blocker_bluff"):
        return "blocker_bluff_raise"
    if action > 0:
        return "value_or_pressure_raise"
    if trace.get("to_call", 0) > 0:
        return "call_defense"
    return "check_or_pot_control"


def clamp(value, low, high):
    return max(low, min(high, value))


def card_suit(card):
    return card % 4


def card_number(card):
    return card // 4 + 2


RANK_TO_LABEL = {
    14: "A",
    13: "K",
    12: "Q",
    11: "J",
    10: "T",
    9: "9",
    8: "8",
    7: "7",
    6: "6",
    5: "5",
    4: "4",
    3: "3",
    2: "2",
}


HEADS_UP_PREFLOP_WIN_TIE_PERCENT = """
AA 84.93 0.54
KK 82.11 0.55
QQ 79.63 0.58
JJ 77.15 0.63
TT 74.66 0.70
99 71.66 0.78
88 68.71 0.89
AKs 66.21 1.65
77 65.72 1.02
AQs 65.31 1.79
AJs 64.39 1.99
AKo 64.46 1.70
ATs 63.48 2.22
AQo 63.50 1.84
AJo 62.53 2.05
KQs 62.40 1.98
66 62.70 1.16
A9s 61.50 2.54
ATo 61.56 2.30
KJs 61.47 2.18
A8s 60.50 2.87
KTs 60.58 2.40
KQo 60.43 2.04
A7s 59.38 3.19
A9o 59.44 2.64
KJo 59.44 2.25
55 59.64 1.36
QJs 59.07 2.37
K9s 58.63 2.70
A5s 58.06 3.71
A6s 58.17 3.45
A8o 58.37 2.99
KTo 58.49 2.48
QTs 58.17 2.59
A4s 57.13 3.79
A7o 57.16 3.34
K8s 56.79 3.04
A3s 56.33 3.77
QJo 56.90 2.45
K9o 56.40 2.80
A5o 55.74 3.90
A6o 55.87 3.62
Q9s 56.22 2.88
K7s 55.84 3.38
JTs 56.15 2.74
A2s 55.50 3.74
QTo 55.94 2.68
44 56.25 1.53
A4o 54.73 3.99
K6s 54.80 3.67
K8o 54.43 3.17
Q8s 54.41 3.20
A3o 53.85 3.97
K5s 53.83 3.91
J9s 54.11 3.10
Q9o 53.86 2.99
JTo 53.82 2.84
K7o 53.41 3.54
A2o 52.94 3.96
K4s 52.88 3.99
Q7s 52.52 3.55
K6o 52.29 3.85
K3s 52.07 3.96
T9s 52.37 3.30
J8s 52.31 3.40
33 52.83 1.70
Q6s 51.67 3.86
Q8o 51.93 3.33
K5o 51.25 4.12
J9o 51.63 3.22
K2s 51.23 3.94
Q5s 50.71 4.11
T8s 50.50 3.65
K4o 50.22 4.20
J7s 50.45 3.74
Q4s 49.76 4.18
Q7o 49.90 3.72
T9o 49.81 3.43
J8o 49.71 3.55
K3o 49.33 4.18
Q6o 48.99 4.05
Q3s 48.93 4.16
98s 48.85 3.88
T7s 48.65 3.97
J6s 48.57 4.06
K2o 48.42 4.17
22 49.38 1.89
Q2s 48.10 4.13
Q5o 47.95 4.32
J5s 47.82 4.33
T8o 47.81 3.80
J7o 47.72 3.91
Q4o 46.92 4.40
97s 46.99 4.25
J4s 46.86 4.40
T6s 46.80 4.28
J3s 46.04 4.37
Q3o 46.02 4.38
98o 46.06 4.05
87s 45.68 4.50
T7o 45.82 4.15
J6o 45.71 4.26
96s 45.15 4.55
J2s 45.20 4.35
Q2o 45.10 4.37
T5s 44.93 4.55
J5o 44.90 4.55
T4s 44.20 4.65
97o 44.07 4.45
86s 43.81 4.84
J4o 43.86 4.63
T6o 43.84 4.48
95s 43.31 4.81
T3s 43.37 4.62
76s 42.82 5.08
J3o 42.96 4.61
87o 42.69 4.71
T2s 42.54 4.59
85s 41.99 5.10
96o 42.10 4.77
J2o 42.04 4.59
T5o 41.85 4.78
94s 41.40 4.90
75s 40.97 5.39
T4o 41.05 4.89
93s 40.80 4.91
86o 40.69 5.08
65s 40.34 5.57
84s 40.10 5.19
95o 40.13 5.06
T3o 40.15 4.87
92s 39.97 4.88
76o 39.65 5.33
74s 39.10 5.48
T2o 39.23 4.85
54s 38.53 5.84
85o 38.74 5.37
64s 38.48 5.70
83s 38.28 5.18
94o 38.08 5.17
75o 37.67 5.67
82s 37.67 5.18
73s 37.30 5.46
93o 37.42 5.18
65o 37.01 5.86
53s 36.75 5.86
63s 36.68 5.69
84o 36.70 5.47
92o 36.51 5.16
43s 35.72 5.82
74o 35.66 5.77
72s 35.43 5.43
54o 35.07 6.16
64o 35.00 6.01
52s 34.92 5.83
62s 34.83 5.66
83o 34.74 5.46
42s 33.91 5.82
82o 34.08 5.48
73o 33.71 5.76
53o 33.16 6.19
63o 33.06 6.01
32s 33.09 5.78
43o 32.06 6.15
72o 31.71 5.74
52o 31.19 6.18
62o 31.07 5.99
42o 30.11 6.16
32o 29.23 6.12
"""


def build_heads_up_preflop_equity_table():
    table = {}
    for line in HEADS_UP_PREFLOP_WIN_TIE_PERCENT.splitlines():
        parts = line.split()
        if not parts:
            continue
        hand, win_percent, tie_percent = parts
        table[hand] = (float(win_percent) + 0.5 * float(tie_percent)) / 100.0
    return table


HEADS_UP_PREFLOP_EQUITY = build_heads_up_preflop_equity_table()


def ranks_with_ace_low(ranks):
    expanded = set(ranks)
    if 14 in expanded:
        expanded.add(1)
    return expanded


def next_player(player, offset):
    return (player + offset) % N_PLAYERS


def evaluate_5(cards):
    ranks = sorted((card_number(c) for c in cards), reverse=True)
    suits = [card_suit(c) for c in cards]
    rank_counts = {}
    for rank in ranks:
        rank_counts[rank] = rank_counts.get(rank, 0) + 1
    groups = sorted(((count, rank) for rank, count in rank_counts.items()), reverse=True)

    is_flush = len(set(suits)) == 1
    unique_ranks = sorted(set(ranks), reverse=True)

    is_straight = False
    straight_high = 0
    expanded_ranks = ranks_with_ace_low(unique_ranks)
    for start in range(1, 11):
        window = set(range(start, start + 5))
        if window.issubset(expanded_ranks):
            is_straight = True
            straight_high = max(straight_high, 5 if start == 1 else start + 4)

    if is_flush and is_straight:
        return (8, straight_high)
    if groups[0][0] == 4:
        quad = groups[0][1]
        kicker = max(rank for rank in ranks if rank != quad)
        return (7, quad, kicker)
    if groups[0][0] == 3 and groups[1][0] == 2:
        return (6, groups[0][1], groups[1][1])
    if is_flush:
        return (5, *ranks)
    if is_straight:
        return (4, straight_high)
    if groups[0][0] == 3:
        trips = groups[0][1]
        kickers = sorted((rank for rank in ranks if rank != trips), reverse=True)
        return (3, trips, *kickers)
    if groups[0][0] == 2 and groups[1][0] == 2:
        high_pair = max(groups[0][1], groups[1][1])
        low_pair = min(groups[0][1], groups[1][1])
        kicker = max(rank for rank in ranks if rank not in (high_pair, low_pair))
        return (2, high_pair, low_pair, kicker)
    if groups[0][0] == 2:
        pair = groups[0][1]
        kickers = sorted((rank for rank in ranks if rank != pair), reverse=True)
        return (1, pair, *kickers)
    return (0, *ranks)


def evaluate_best(cards):
    if len(cards) == 5:
        return evaluate_5(cards)
    best = None
    for combo in itertools.combinations(cards, 5):
        score = evaluate_5(combo)
        if best is None or score > best:
            best = score
    return best


def evaluate_7(cards):
    return evaluate_best(cards)


def preflop_hand_key(my_cards):
    ranks = sorted((card_number(card) for card in my_cards), reverse=True)
    high, low = ranks
    if high == low:
        return RANK_TO_LABEL[high] + RANK_TO_LABEL[low]
    suited = card_suit(my_cards[0]) == card_suit(my_cards[1])
    return RANK_TO_LABEL[high] + RANK_TO_LABEL[low] + ("s" if suited else "o")


def estimate_preflop_strength(my_cards):
    return HEADS_UP_PREFLOP_EQUITY[preflop_hand_key(my_cards)]


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

    if strength <= 0.30:
        return True
    if not suited and high <= 10 and low <= 5 and gap >= 3:
        return True
    if not suited and high <= 12 and low <= 4 and gap >= 5:
        return True
    if suited and high <= 9 and low <= 4 and gap >= 4:
        return True
    return False


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
    sb = next_player(dealer_id, 1)
    bb = next_player(dealer_id, 2)

    stacks[sb] -= SMALL_BLIND
    stacks[bb] -= BIG_BLIND
    committed[sb] += SMALL_BLIND
    committed[bb] += BIG_BLIND

    current_round = 0
    round_bet = BIG_BLIND
    judge_round_raise = BIG_BLIND
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
            judge_round_raise = SMALL_BLIND
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
            add = max(0, min(action, stacks[pid]))
            stacks[pid] -= add
            committed[pid] += add
            round_contrib[pid] += add
            round_bet = max(round_bet, round_contrib[pid])
            judge_round_raise = max(judge_round_raise, add)

    public_cards = len(req["public_cards"])
    round_idx = 0 if public_cards == 0 else 1 if public_cards == 3 else 2 if public_cards == 4 else 3

    if current_round != round_idx:
        round_bet = 0
        judge_round_raise = SMALL_BLIND
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
    if round_bet > 0:
        min_raise_target = 2 * round_bet
    else:
        min_raise_target = BIG_BLIND
    min_raise_action = max(0, min_raise_target - my_round_bet)
    allin_call_amount = max(
        0,
        min(committed[opponent_id], committed[my_id] + stacks[my_id]) - committed[my_id],
    )

    return {
        "round": round_idx,
        "round_bet": round_bet,
        "round_raise": judge_round_raise,
        "judge_round_raise": judge_round_raise,
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
        future_sb = next_player(future_dealer, 1)
        future_bb = next_player(future_dealer, 2)
        if my_id == future_sb:
            loss += SMALL_BLIND
        elif my_id == future_bb:
            loss += BIG_BLIND
    return loss


def should_lock_win(req, state, my_id):
    total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
    if len(total_win_chips) <= my_id:
        return False

    try:
        lead = int(total_win_chips[my_id])
    except (TypeError, ValueError):
        return False

    remaining_hands = get_remaining_hands(req)
    if remaining_hands is None:
        return lead >= LOCK_WIN_MARGIN

    max_forced_loss = forced_fold_loss_bound(req, state, my_id, remaining_hands)
    if max_forced_loss is None:
        return lead >= LOCK_WIN_MARGIN
    return lead > max_forced_loss


def opponent_can_lock_win(req, my_id):
    opponent_id = next_player(my_id, 1)
    total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
    if len(total_win_chips) <= opponent_id:
        return False

    try:
        lead = int(total_win_chips[opponent_id])
    except (TypeError, ValueError):
        return False

    remaining_hands = get_remaining_hands(req)
    if remaining_hands is None:
        return lead >= LOCK_WIN_MARGIN

    state = reconstruct_state(req)
    max_forced_loss = forced_fold_loss_bound(req, state, opponent_id, remaining_hands)
    if max_forced_loss is None:
        return lead >= LOCK_WIN_MARGIN
    return lead > max_forced_loss + BIG_BLIND


def fold_gives_opponent_lock(req, state, my_id):
    opponent_id = next_player(my_id, 1)
    total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
    if len(total_win_chips) <= opponent_id:
        return False

    remaining_hands = get_remaining_hands(req)
    if remaining_hands is None or remaining_hands <= 1:
        return False

    try:
        opponent_lead = int(total_win_chips[opponent_id])
    except (TypeError, ValueError):
        return False

    opponent_lead_after_fold = opponent_lead + state["committed"][my_id]
    max_forced_loss = forced_fold_loss_bound(req, state, opponent_id, remaining_hands)
    if max_forced_loss is None:
        return opponent_lead_after_fold >= LOCK_WIN_MARGIN

    future_forced_loss = max(0, max_forced_loss - state["committed"][opponent_id])
    return opponent_lead_after_fold > future_forced_loss


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


def smooth_rate(successes, total, prior_mean, prior_weight):
    return (successes + prior_mean * prior_weight) / (total + prior_weight)


def build_opponent_model(requests, my_id):
    opponent_id = next_player(my_id, 1)
    hand_requests = collect_latest_requests_by_hand(requests)

    preflop_opportunities = 0
    voluntary_preflop = 0
    preflop_raise = 0
    total_actions = 0
    aggressive_actions = 0
    allin_actions = 0
    postflop_actions = 0
    postflop_aggressive = 0
    postflop_checks = 0
    fold_to_raise_opportunities = 0
    fold_to_raise = 0
    raise_sizes = []

    for req in hand_requests:
        if opponent_can_lock_win(req, my_id):
            continue

        history = req.get("history", [])
        if not history:
            continue

        saw_opponent_preflop_action = False
        pending_my_pressure = False

        for record in history:
            pid = record["player_id"]
            action_type = record["action_type"]
            action = record["action"]
            round_idx = record["round"]

            if pid == my_id and action_type in ("raise", "allin"):
                pending_my_pressure = True
                continue

            if pid != opponent_id:
                continue

            total_actions += 1
            if action_type in ("raise", "allin"):
                aggressive_actions += 1
            if action_type == "allin":
                allin_actions += 1

            if round_idx == 0 and not saw_opponent_preflop_action:
                saw_opponent_preflop_action = True
                preflop_opportunities += 1
                if action_type in ("call", "raise", "allin"):
                    voluntary_preflop += 1
                if action_type in ("raise", "allin"):
                    preflop_raise += 1

            if round_idx > 0:
                postflop_actions += 1
                if action_type in ("raise", "allin"):
                    postflop_aggressive += 1
                if action_type == "check":
                    postflop_checks += 1

            if action_type == "raise":
                raise_size = record.get("raise_total", action)
                raise_sizes.append(raise_size / BIG_BLIND)

            if pending_my_pressure:
                fold_to_raise_opportunities += 1
                if action_type == "fold":
                    fold_to_raise += 1
                pending_my_pressure = False

    confidence = clamp((total_actions - 5) / 35.0, 0.0, 1.0)
    avg_raise_bb = sum(raise_sizes) / len(raise_sizes) if raise_sizes else 2.6

    return {
        "confidence": confidence,
        "vpip": smooth_rate(voluntary_preflop, preflop_opportunities, 0.58, 4.0),
        "pfr": smooth_rate(preflop_raise, preflop_opportunities, 0.28, 4.0),
        "allin_rate": smooth_rate(allin_actions, total_actions, 0.05, 8.0),
        "postflop_aggr": smooth_rate(postflop_aggressive, postflop_actions, 0.36, 5.0),
        "postflop_check_rate": smooth_rate(postflop_checks, postflop_actions, 0.42, 5.0),
        "fold_to_raise": smooth_rate(fold_to_raise, fold_to_raise_opportunities, 0.44, 4.0),
        "aggression": smooth_rate(aggressive_actions, total_actions, 0.30, 6.0),
        "avg_raise_bb": avg_raise_bb,
    }


def analyze_current_spot(req, state):
    my_id = req["my_id"]
    opponent_id = next_player(my_id, 1)
    dealer_id = req["dealer_id"]
    sb = next_player(dealer_id, 1)
    bb = next_player(dealer_id, 2)
    history = req["history"]

    info = {
        "my_is_sb": my_id == sb,
        "my_is_bb": my_id == bb,
        "has_position": my_id == bb,
        "opp_preflop_raises": 0,
        "opp_round_raises": 0,
        "opp_total_raises": 0,
        "opp_postflop_bet_count": 0,
        "opp_current_round_bet_count": 0,
        "opp_postflop_check_count": 0,
        "opp_current_round_check_count": 0,
        "opp_prior_postflop_check_count": 0,
        "opp_prior_postflop_raise_count": 0,
        "opp_previous_round_raise_count": 0,
        "facing_raise": False,
        "facing_allin": state["opponent_allin"],
        "facing_postflop_aggression": False,
        "last_opp_action_type": None,
        "last_raise_bb": 0.0,
        "last_raise_pot_ratio": 0.0,
        "preflop_spot": "other",
    }

    for record in history:
        if record["player_id"] == opponent_id and record["round"] > 0 and record["action_type"] == "check":
            info["opp_postflop_check_count"] += 1
            if record["round"] == state["round"]:
                info["opp_current_round_check_count"] += 1
            elif record["round"] < state["round"]:
                info["opp_prior_postflop_check_count"] += 1

        if record["player_id"] != opponent_id or record["action_type"] not in ("raise", "allin"):
            continue
        info["opp_total_raises"] += 1
        if record["round"] == 0:
            info["opp_preflop_raises"] += 1
        if record["round"] > 0:
            info["opp_postflop_bet_count"] += 1
            if record["round"] < state["round"]:
                info["opp_prior_postflop_raise_count"] += 1
            if record["round"] == state["round"] - 1:
                info["opp_previous_round_raise_count"] += 1
        if record["round"] == state["round"]:
            info["opp_round_raises"] += 1
            if record["round"] > 0:
                info["opp_current_round_bet_count"] += 1

    if history and history[-1]["player_id"] == opponent_id:
        last = history[-1]
        info["last_opp_action_type"] = last["action_type"]
        if last["action_type"] in ("raise", "allin"):
            info["facing_raise"] = True
            info["facing_postflop_aggression"] = state["round"] > 0
            if last["action_type"] == "raise":
                last_raise_size = last.get("raise_total", last["action"])
                info["last_raise_bb"] = last_raise_size / BIG_BLIND
                info["last_raise_pot_ratio"] = last_raise_size / max(1, state["pot"])
            else:
                info["last_raise_bb"] = state["allin_call_amount"] / max(1, BIG_BLIND)
                info["last_raise_pot_ratio"] = state["allin_call_amount"] / max(1, state["pot"])

    if state["round"] == 0:
        if not history and info["my_is_sb"]:
            info["preflop_spot"] = "sb_open"
        elif history and info["my_is_bb"] and history[-1]["player_id"] == opponent_id:
            if history[-1]["action_type"] == "call":
                info["preflop_spot"] = "bb_vs_limp"
            elif history[-1]["action_type"] in ("raise", "allin"):
                info["preflop_spot"] = "bb_vs_raise"
        elif history and info["my_is_sb"] and history[-1]["player_id"] == opponent_id:
            if history[-1]["action_type"] in ("raise", "allin"):
                info["preflop_spot"] = "sb_vs_reraise"

    return info


def made_hand_metric(hole_cards, public_cards):
    if len(public_cards) < 3:
        return 0.0
    score = evaluate_best(hole_cards + public_cards)
    metric = HAND_CLASS_SCORE[score[0]]
    detail = 0.0
    for idx, rank in enumerate(score[1:4]):
        detail += rank / (16.0 * (2 ** idx))
    return clamp(metric + detail * 0.008, 0.0, 0.995)


def pair_board_profile(hole_cards, public_cards):
    info = {
        "made_class": -1,
        "pair_rank": None,
        "pair_type": "none",
        "kicker_rank": 0,
        "board_overcards": 0,
        "uses_hole_card": False,
        "weak_kicker": False,
    }

    if len(public_cards) < 3:
        return info

    score = evaluate_best(hole_cards + public_cards)
    info["made_class"] = score[0]
    if score[0] != 1:
        return info

    pair_rank = score[1]
    hole_ranks = [card_number(card) for card in hole_cards]
    board_ranks = [card_number(card) for card in public_cards]
    board_unique = sorted(set(board_ranks), reverse=True)

    info["pair_rank"] = pair_rank
    info["board_overcards"] = sum(1 for rank in set(board_ranks) if rank > pair_rank)

    uses_hole = pair_rank in hole_ranks
    info["uses_hole_card"] = uses_hole

    hole_kickers = [rank for rank in hole_ranks if rank != pair_rank]
    if hole_kickers:
        info["kicker_rank"] = max(hole_kickers)
    else:
        board_kickers = [rank for rank in board_ranks if rank != pair_rank]
        info["kicker_rank"] = max(board_kickers, default=0)

    info["weak_kicker"] = info["kicker_rank"] <= 9

    if not uses_hole:
        info["pair_type"] = "board_pair"
        return info

    pocket_pair = hole_ranks[0] == hole_ranks[1] and hole_ranks[0] == pair_rank
    if pocket_pair:
        if board_ranks and pair_rank > max(board_ranks):
            info["pair_type"] = "overpair"
        elif info["board_overcards"] >= 1:
            info["pair_type"] = "underpair"
        else:
            info["pair_type"] = "pocket_pair"
        return info

    if board_unique and pair_rank == board_unique[0]:
        info["pair_type"] = "top_pair"
    elif len(board_unique) >= 2 and pair_rank == board_unique[1]:
        info["pair_type"] = "middle_pair"
    else:
        info["pair_type"] = "bottom_pair"

    return info


def pair_domination_margin(pair_profile, spot_info, round_idx):
    if pair_profile is None or pair_profile["made_class"] != 1:
        return 0.0

    pair_type = pair_profile["pair_type"]
    margin = 0.0

    if pair_type == "top_pair":
        margin += 0.012 if pair_profile["weak_kicker"] else 0.004
    elif pair_type == "middle_pair":
        margin += 0.030
        if pair_profile["weak_kicker"]:
            margin += 0.012
    elif pair_type == "bottom_pair":
        margin += 0.050
        if pair_profile["weak_kicker"]:
            margin += 0.012
    elif pair_type == "underpair":
        margin += 0.045 + 0.010 * pair_profile["board_overcards"]
    elif pair_type == "board_pair":
        margin += 0.065

    if spot_info["facing_postflop_aggression"]:
        margin += 0.010
    if spot_info.get("opp_postflop_bet_count", 0) >= 2:
        margin += 0.012
    if round_idx == 3 and pair_type in ("middle_pair", "bottom_pair", "underpair", "board_pair"):
        margin += 0.015

    return clamp(margin, 0.0, 0.10)


def marginal_pair_under_pressure(pair_profile, board_texture):
    if pair_profile is None or pair_profile["made_class"] != 1:
        return False

    pair_type = pair_profile["pair_type"]
    if pair_type in ("middle_pair", "bottom_pair", "underpair", "board_pair"):
        return True
    if pair_type == "top_pair" and pair_profile["weak_kicker"]:
        return True
    if pair_type == "top_pair" and board_texture is not None:
        return board_texture["high_card"] >= 14 and pair_profile["kicker_rank"] <= 11
    return False


def board_texture_profile(public_cards):
    info = {
        "wetness": 0.0,
        "flush_pressure": 0.0,
        "straight_pressure": 0.0,
        "paired": False,
        "high_card": 0,
        "dynamic": False,
    }

    if len(public_cards) < 3:
        return info

    board_ranks = [card_number(card) for card in public_cards]
    board_suits = [card_suit(card) for card in public_cards]
    info["high_card"] = max(board_ranks)
    info["paired"] = len(set(board_ranks)) < len(board_ranks)

    suit_counts = {}
    for suit in board_suits:
        suit_counts[suit] = suit_counts.get(suit, 0) + 1
    max_suit = max(suit_counts.values())

    if max_suit >= 4:
        info["flush_pressure"] = 1.0
    elif max_suit == 3:
        info["flush_pressure"] = 0.75
    elif max_suit == 2 and len(public_cards) >= 4:
        info["flush_pressure"] = 0.35

    ranks = set(board_ranks)
    expanded = ranks_with_ace_low(ranks)

    best_straight_pressure = 0.0
    for start in range(1, 11):
        window = set(range(start, start + 5))
        present = len(expanded & window)
        if present >= 4:
            best_straight_pressure = max(best_straight_pressure, 1.0)
        elif present == 3:
            best_straight_pressure = max(best_straight_pressure, 0.65)
        elif present == 2 and max(window & expanded, default=start) - min(window & expanded, default=start) <= 3:
            best_straight_pressure = max(best_straight_pressure, 0.28)

    info["straight_pressure"] = best_straight_pressure

    wetness = 0.18 * info["flush_pressure"]
    wetness += 0.22 * info["straight_pressure"]
    if info["high_card"] >= 12:
        wetness += 0.03
    if len(public_cards) >= 4 and not info["paired"]:
        wetness += 0.04
    if info["paired"]:
        wetness -= 0.06

    info["wetness"] = clamp(wetness, 0.0, 1.0)
    info["dynamic"] = (
        info["flush_pressure"] >= 0.75
        or info["straight_pressure"] >= 0.65
        or info["wetness"] >= 0.45
    )
    return info


def paired_board_outcome_profile(hole_cards, public_cards):
    info = {
        "board_paired": False,
        "board_pair_rank": 0,
        "board_pair_count": 0,
        "hand_class": -1,
        "uses_board_pair": False,
        "board_two_pair": False,
        "trips_vulnerable": False,
        "strengthened": False,
        "weakened": False,
        "fragile_two_pair": False,
        "prefer_check": False,
        "fold_to_raise": False,
        "label": "none",
    }

    if len(public_cards) < 3:
        return info

    board_counts = {}
    for card in public_cards:
        rank = card_number(card)
        board_counts[rank] = board_counts.get(rank, 0) + 1
    paired_ranks = sorted(
        ((rank, count) for rank, count in board_counts.items() if count >= 2),
        reverse=True,
    )
    if not paired_ranks:
        return info

    board_pair_rank, board_pair_count = paired_ranks[0]
    score = evaluate_best(hole_cards + public_cards)
    hand_class = score[0]
    hole_ranks = [card_number(card) for card in hole_cards]
    top_unpaired_board_rank = max(
        (rank for rank in board_counts if rank != board_pair_rank),
        default=0,
    )

    info["board_paired"] = True
    info["board_pair_rank"] = board_pair_rank
    info["board_pair_count"] = board_pair_count
    info["hand_class"] = hand_class

    if hand_class >= 6:
        info["strengthened"] = True
        info["label"] = "trips_plus_on_paired_board"
        return info

    if hand_class == 3:
        trips_rank = score[1]
        if trips_rank == board_pair_rank or hole_ranks.count(trips_rank) == 2:
            info["strengthened"] = True
            info["label"] = "strong_trips_on_paired_board"
        else:
            info["weakened"] = True
            info["prefer_check"] = True
            info["label"] = "fragile_trips_on_paired_board"
        return info

    if hand_class != 2:
        return info

    high_pair = score[1]
    low_pair = score[2]
    uses_board_pair = board_pair_rank in (high_pair, low_pair)
    pocket_pair = hole_ranks[0] == hole_ranks[1]
    info["uses_board_pair"] = uses_board_pair
    info["trips_vulnerable"] = uses_board_pair

    if uses_board_pair and pocket_pair and high_pair == hole_ranks[0] and low_pair == board_pair_rank:
        info["board_two_pair"] = True
        info["fold_to_raise"] = True
        info["label"] = "overpair_two_pair_on_paired_board"
        return info

    if uses_board_pair:
        other_pair = low_pair if high_pair == board_pair_rank else high_pair
        if high_pair == board_pair_rank and other_pair <= 6:
            info["weakened"] = True
            info["fragile_two_pair"] = True
            info["label"] = "low_two_pair_on_paired_board"
        elif high_pair == board_pair_rank and top_unpaired_board_rank > other_pair:
            info["weakened"] = True
            info["fragile_two_pair"] = True
            info["label"] = "dominated_two_pair_on_paired_board"
        elif low_pair == board_pair_rank and high_pair < top_unpaired_board_rank:
            info["weakened"] = True
            info["label"] = "under_top_two_pair_on_paired_board"
        elif low_pair == board_pair_rank and high_pair < 11:
            info["weakened"] = True
            info["fragile_two_pair"] = True
            info["label"] = "thin_two_pair_on_paired_board"
        else:
            info["label"] = "top_two_pair_with_board_pair"
    else:
        if high_pair == top_unpaired_board_rank and high_pair >= 11:
            info["strengthened"] = True
            info["label"] = "top_two_pair_above_board_pair"
        else:
            info["weakened"] = True
            info["label"] = "disconnected_two_pair_on_paired_board"
            if high_pair < 12:
                info["fragile_two_pair"] = True

    if info["weakened"] and not info["strengthened"]:
        info["prefer_check"] = True
    if info["fragile_two_pair"]:
        info["prefer_check"] = True
        info["fold_to_raise"] = True
    elif info["label"] in ("under_top_two_pair_on_paired_board", "disconnected_two_pair_on_paired_board"):
        info["fold_to_raise"] = True

    return info


def bet_size_bucket(last_raise_pot_ratio):
    if last_raise_pot_ratio <= 0.30:
        return "small"
    if last_raise_pot_ratio <= 0.75:
        return "medium"
    return "large"


def value_hand_tier(hole_cards, public_cards, pair_profile=None, board_texture=None, paired_board_profile=None):
    info = {
        "tier": "none",
        "is_value": False,
        "size_bonus": 0.0,
    }

    if len(public_cards) < 3:
        return info

    if pair_profile is None:
        pair_profile = pair_board_profile(hole_cards, public_cards)
    if board_texture is None:
        board_texture = board_texture_profile(public_cards)
    if paired_board_profile is None:
        paired_board_profile = paired_board_outcome_profile(hole_cards, public_cards)

    score = evaluate_best(hole_cards + public_cards)
    hand_class = score[0]
    wetness = board_texture["wetness"]
    hole_ranks = [card_number(card) for card in hole_cards]
    size_bonus = 0.0
    tier = "none"

    if hand_class >= 6:
        tier = "nut"
        size_bonus = 0.22 + 0.08 * wetness
    elif hand_class == 5:
        flush_profile = made_flush_profile(hole_cards, public_cards, board_texture)
        if flush_profile["nut_like"]:
            tier = "nut"
            size_bonus = 0.18 + 0.06 * wetness
        elif flush_profile["high_hole_rank"] >= 12 and flush_profile["better_unseen_ranks"] <= 1:
            tier = "strong"
            size_bonus = 0.15 + 0.06 * wetness
        elif flush_profile["high_hole_rank"] >= 10:
            tier = "strong"
            size_bonus = 0.12 + 0.04 * wetness
        else:
            tier = "thin"
            size_bonus = 0.05 + 0.02 * wetness
    elif hand_class == 4:
        tier = "strong"
        size_bonus = 0.12 + 0.05 * wetness
    elif hand_class == 3:
        set_made = hole_ranks.count(score[1]) == 2
        tier = "nut" if set_made and board_texture["dynamic"] else "strong"
        size_bonus = 0.20 if tier == "nut" else 0.13 + 0.05 * wetness
    elif hand_class == 2:
        if paired_board_profile["board_paired"]:
            if paired_board_profile["board_two_pair"]:
                tier = "strong"
                size_bonus = 0.02 - 0.02 * wetness
            elif paired_board_profile["fragile_two_pair"]:
                tier = "thin"
                size_bonus = -0.01 - 0.03 * wetness
            elif paired_board_profile["weakened"]:
                tier = "thin"
                size_bonus = 0.02 - 0.03 * wetness
            elif paired_board_profile["label"] == "top_two_pair_above_board_pair":
                tier = "strong"
                size_bonus = 0.07 + 0.03 * wetness
            else:
                tier = "strong"
                size_bonus = 0.07 + 0.04 * wetness
        else:
            tier = "strong"
            size_bonus = 0.10 + 0.06 * wetness
    elif hand_class == 1 and pair_profile["made_class"] == 1:
        pair_type = pair_profile["pair_type"]
        if pair_type == "overpair":
            tier = "strong"
            size_bonus = 0.13 + 0.07 * wetness
        elif pair_type == "top_pair":
            if pair_profile["weak_kicker"]:
                tier = "thin"
                size_bonus = 0.01 - 0.03 * wetness
            else:
                tier = "strong" if pair_profile["pair_rank"] >= 11 else "thin"
                size_bonus = 0.09 + 0.03 * wetness if tier == "strong" else 0.03 - 0.02 * wetness
        elif pair_type == "pocket_pair":
            tier = "thin"
            size_bonus = 0.00 - 0.03 * wetness

    info["tier"] = tier
    info["is_value"] = tier != "none"
    info["size_bonus"] = clamp(size_bonus, -0.04, 0.24)
    return info


def value_bet_plan(value_profile, board_texture, paired_board_profile, pair_profile, nutted_risk, round_idx, pot):
    plan = {
        "size_delta": 0.0,
        "induce": False,
        "protect": False,
        "thin_control": False,
        "label": "normal",
    }

    if value_profile is None or board_texture is None or round_idx <= 0:
        return plan

    tier = value_profile.get("tier", "none")
    if tier == "none":
        return plan

    wetness = board_texture["wetness"]
    dynamic_board = board_texture["dynamic"]
    draw_heavy = board_texture["flush_pressure"] >= 0.75 or board_texture["straight_pressure"] >= 0.65
    paired_warning = (
        paired_board_profile is not None
        and paired_board_profile["board_paired"]
        and paired_board_profile["prefer_check"]
    )
    risk = nutted_risk.get("risk", 0.0) if nutted_risk is not None else 0.0

    if tier == "nut":
        if risk > 0.03:
            plan["size_delta"] -= min(0.08, 0.80 * risk)
            plan["label"] = "nutted_risk_control"
            return plan
        if dynamic_board:
            plan["protect"] = True
            plan["size_delta"] += 0.03 + 0.04 * wetness
            plan["label"] = "nut_value_dynamic"
        else:
            plan["induce"] = pot < 2600
            plan["size_delta"] -= 0.12 if round_idx < 3 else 0.16
            plan["label"] = "nut_value_induce"
        return plan

    if tier == "strong":
        vulnerable_pair = (
            pair_profile is not None
            and pair_profile["made_class"] == 1
            and pair_profile["pair_type"] in ("overpair", "top_pair")
        )
        if dynamic_board or draw_heavy:
            plan["protect"] = True
            plan["size_delta"] += 0.07 + 0.05 * wetness
            if round_idx == 2:
                plan["size_delta"] += 0.02
            plan["label"] = "strong_value_protect"
        elif vulnerable_pair:
            plan["size_delta"] -= 0.03
            plan["label"] = "strong_pair_static_control"

    if tier == "thin":
        plan["thin_control"] = True
        plan["size_delta"] -= 0.04 + 0.04 * wetness
        plan["label"] = "thin_value_control"

    if paired_warning and tier != "nut":
        plan["thin_control"] = True
        plan["size_delta"] -= 0.06
        plan["label"] = "paired_board_control"

    if risk > 0.0 and tier != "nut":
        plan["size_delta"] -= min(0.08, 0.45 * risk)

    plan["size_delta"] = clamp(plan["size_delta"], -0.18, 0.16)
    return plan


def straight_draw_value(cards):
    ranks = {card_number(card) for card in cards}
    expanded = ranks_with_ace_low(ranks)

    best = 0.0
    for start in range(1, 11):
        straight = set(range(start, start + 5))
        present = len(expanded & straight)
        if present != 4:
            continue
        missing = next(iter(straight - expanded))
        if missing in (start, start + 4):
            best = max(best, 0.17)
        else:
            best = max(best, 0.09)
    return best


def empty_draw_profile():
    return {
        "quality": 0.0,
        "type": "none",
        "flush_draw": False,
        "nut_flush_draw": False,
        "near_nut_flush_draw": False,
        "high_flush_draw": False,
        "flush_draw_rank": 0,
        "better_flush_draw_ranks": 0,
        "straight_draw": "none",
        "combo_draw": False,
        "overcards": 0,
        "semi_bluff": False,
        "fold_threshold_delta": 0.0,
        "size_bonus": 0.0,
    }


def draw_profile(hole_cards, public_cards, board_texture=None):
    info = empty_draw_profile()
    if len(public_cards) < 3 or len(public_cards) >= 5:
        return info

    if board_texture is None:
        board_texture = board_texture_profile(public_cards)

    cards = hole_cards + public_cards
    hole_ranks = [card_number(card) for card in hole_cards]
    board_high = max(card_number(card) for card in public_cards)
    info["overcards"] = sum(1 for rank in hole_ranks if rank > board_high)

    suit_counts = {}
    for card in cards:
        suit = card_suit(card)
        suit_counts[suit] = suit_counts.get(suit, 0) + 1

    flush_quality = 0.0
    best_flush_rank = 0
    best_better_flush_ranks = 0
    for suit, count in suit_counts.items():
        if count != 4:
            continue
        hole_flush_ranks = sorted(
            (card_number(card) for card in hole_cards if card_suit(card) == suit),
            reverse=True,
        )
        if not hole_flush_ranks:
            continue

        board_flush_ranks = [card_number(card) for card in public_cards if card_suit(card) == suit]
        high_flush_rank = max(hole_flush_ranks)
        seen_flush_ranks = set(hole_flush_ranks + board_flush_ranks)
        better_flush_ranks = len([rank for rank in range(high_flush_rank + 1, 15) if rank not in seen_flush_ranks])
        nut_draw = better_flush_ranks == 0
        info["flush_draw"] = True
        info["nut_flush_draw"] = info["nut_flush_draw"] or nut_draw

        candidate = 0.21 if nut_draw else 0.16
        if not nut_draw and high_flush_rank >= 12 and better_flush_ranks <= 1:
            candidate = max(candidate, 0.185)
        elif not nut_draw and high_flush_rank >= 11:
            candidate = max(candidate, 0.170)
        if high_flush_rank <= 9:
            candidate -= 0.025
        if board_texture["paired"] and not nut_draw:
            candidate -= 0.020
        if candidate > flush_quality:
            best_flush_rank = high_flush_rank
            best_better_flush_ranks = better_flush_ranks
        flush_quality = max(flush_quality, candidate)

    if info["flush_draw"]:
        info["flush_draw_rank"] = best_flush_rank
        info["better_flush_draw_ranks"] = best_better_flush_ranks
        info["near_nut_flush_draw"] = best_flush_rank >= 12 and best_better_flush_ranks <= 1
        info["high_flush_draw"] = best_flush_rank >= 11 and best_better_flush_ranks <= 3

    ranks = {card_number(card) for card in cards}
    expanded = ranks_with_ace_low(ranks)
    hole_expanded = ranks_with_ace_low(hole_ranks)

    straight_quality = 0.0
    gutshot_count = 0
    has_open_ended = False
    has_gutshot = False
    for start in range(1, 11):
        window = set(range(start, start + 5))
        present = expanded & window
        if len(present) != 4 or not (hole_expanded & present):
            continue
        missing = next(iter(window - present))
        if missing in (start, start + 4):
            has_open_ended = True
            straight_quality = max(straight_quality, 0.17)
        else:
            has_gutshot = True
            gutshot_count += 1
            straight_quality = max(straight_quality, 0.10)

    if has_open_ended:
        info["straight_draw"] = "open_ended"
    elif gutshot_count >= 2:
        info["straight_draw"] = "double_gutshot"
        straight_quality = max(straight_quality, 0.13)
    elif has_gutshot:
        info["straight_draw"] = "gutshot"

    info["combo_draw"] = info["flush_draw"] and info["straight_draw"] != "none"
    quality = max(flush_quality, straight_quality)
    if info["flush_draw"] and info["straight_draw"] != "none":
        quality = max(quality, flush_quality + straight_quality + 0.04)
    if len(public_cards) == 3:
        quality += 0.025 * info["overcards"]
    elif info["overcards"] >= 2:
        quality += 0.015

    if info["combo_draw"]:
        info["type"] = "combo_draw"
        info["fold_threshold_delta"] = 0.07
        info["size_bonus"] = 0.06
    elif info["nut_flush_draw"]:
        info["type"] = "nut_flush_draw"
        info["fold_threshold_delta"] = 0.05
        info["size_bonus"] = 0.04
    elif info["flush_draw"]:
        info["type"] = "flush_draw"
        if info["near_nut_flush_draw"]:
            info["fold_threshold_delta"] = 0.04
            info["size_bonus"] = 0.035
        elif info["high_flush_draw"]:
            info["fold_threshold_delta"] = 0.03
            info["size_bonus"] = 0.025
        else:
            info["fold_threshold_delta"] = 0.01 if info["flush_draw_rank"] >= 10 else 0.0
            info["size_bonus"] = 0.015
    elif info["straight_draw"] == "open_ended":
        info["type"] = "open_ended_straight_draw"
        info["fold_threshold_delta"] = 0.03
        info["size_bonus"] = 0.02
    elif info["straight_draw"] == "double_gutshot":
        info["type"] = "double_gutshot"
        info["fold_threshold_delta"] = 0.02
        info["size_bonus"] = 0.01
    elif info["straight_draw"] == "gutshot":
        info["type"] = "gutshot"
        info["fold_threshold_delta"] = -0.03
        info["size_bonus"] = -0.02

    info["quality"] = clamp(quality, 0.0, 0.35)
    info["semi_bluff"] = (
        info["combo_draw"]
        or info["nut_flush_draw"]
        or info["straight_draw"] in ("open_ended", "double_gutshot")
        or (info["flush_draw"] and info["quality"] >= 0.16)
        or (info["straight_draw"] == "gutshot" and info["overcards"] >= 1 and info["quality"] >= 0.13)
    )
    return info


def draw_potential(hole_cards, public_cards):
    return draw_profile(hole_cards, public_cards)["quality"]


def draw_call_margin(draw_info, board_texture, round_idx, spot_info):
    if draw_info is None or draw_info["type"] == "none":
        return 0.0

    margin = 0.0
    draw_type = draw_info["type"]
    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])

    if draw_type == "combo_draw":
        margin -= 0.035
    elif draw_type == "nut_flush_draw":
        margin -= 0.025
    elif draw_type == "open_ended_straight_draw":
        margin -= 0.012
    elif draw_type == "double_gutshot":
        margin -= 0.006
    elif draw_type == "gutshot":
        margin += 0.040
    elif draw_type == "flush_draw" and not draw_info["nut_flush_draw"]:
        if draw_info.get("near_nut_flush_draw", False):
            margin -= 0.010
        elif draw_info.get("high_flush_draw", False):
            margin += 0.004
        else:
            margin += 0.020

    if (
        draw_type == "flush_draw"
        and draw_info.get("high_flush_draw", False)
        and size_bucket in ("small", "medium")
        and spot_info.get("has_position", False)
        and (board_texture is None or not board_texture["paired"])
    ):
        margin -= 0.006

    if board_texture is not None:
        if board_texture["paired"] and draw_info["flush_draw"] and not draw_info["nut_flush_draw"]:
            margin += 0.030
        if board_texture["flush_pressure"] >= 0.75 and draw_type == "open_ended_straight_draw":
            margin += 0.008

    if round_idx == 2:
        if draw_type == "gutshot":
            margin += 0.020
        elif draw_type == "flush_draw":
            if draw_info.get("near_nut_flush_draw", False) and size_bucket != "large" and (board_texture is None or not board_texture["paired"]):
                margin += 0.000
            elif draw_info.get("high_flush_draw", False) and size_bucket == "small" and (board_texture is None or not board_texture["paired"]):
                margin += 0.006
            else:
                margin += 0.020
    elif round_idx == 3:
        margin += 0.050

    if size_bucket == "large":
        if draw_type == "gutshot":
            margin += 0.018
        elif draw_type == "flush_draw":
            if draw_info.get("near_nut_flush_draw", False):
                margin += 0.006
            elif draw_info.get("high_flush_draw", False):
                margin += 0.012
            else:
                margin += 0.018

    return clamp(margin, -0.04, 0.08)


def made_flush_profile(hole_cards, public_cards, board_texture=None):
    info = {
        "is_flush": False,
        "flush_suit": None,
        "hole_flush_ranks": [],
        "board_flush_ranks": [],
        "high_hole_rank": 0,
        "better_unseen_ranks": 0,
        "nut_like": False,
        "repressure_continue": False,
    }

    if len(public_cards) < 3:
        return info

    if board_texture is None:
        board_texture = board_texture_profile(public_cards)

    score = evaluate_best(hole_cards + public_cards)
    if score[0] != 5:
        return info

    suit_counts = {}
    for card in hole_cards + public_cards:
        suit = card_suit(card)
        suit_counts[suit] = suit_counts.get(suit, 0) + 1

    flush_suits = [suit for suit, count in suit_counts.items() if count >= 5]
    if not flush_suits:
        return info

    flush_suit = max(
        flush_suits,
        key=lambda suit: sorted(
            (card_number(card) for card in hole_cards + public_cards if card_suit(card) == suit),
            reverse=True,
        )[:5],
    )
    hole_flush_ranks = sorted(
        (card_number(card) for card in hole_cards if card_suit(card) == flush_suit),
        reverse=True,
    )
    board_flush_ranks = sorted(
        (card_number(card) for card in public_cards if card_suit(card) == flush_suit),
        reverse=True,
    )

    if not hole_flush_ranks:
        return info

    high_hole = hole_flush_ranks[0]
    seen_flush_ranks = set(hole_flush_ranks + board_flush_ranks)
    better_unseen = [rank for rank in range(high_hole + 1, 15) if rank not in seen_flush_ranks]

    info["is_flush"] = True
    info["flush_suit"] = flush_suit
    info["hole_flush_ranks"] = hole_flush_ranks
    info["board_flush_ranks"] = board_flush_ranks
    info["high_hole_rank"] = high_hole
    info["better_unseen_ranks"] = len(better_unseen)
    info["nut_like"] = len(better_unseen) == 0

    three_flush_board = len(board_flush_ranks) == 3
    high_private_flush = high_hole >= 11 and len(better_unseen) <= 2
    info["repressure_continue"] = (
        not board_texture["paired"]
        and three_flush_board
        and high_private_flush
    )
    return info


def blocker_bluff_profile(hole_cards, public_cards, pair_profile=None, board_texture=None):
    info = {
        "eligible": False,
        "score": 0.0,
        "type": "none",
    }

    if len(public_cards) < 3:
        return info

    if pair_profile is None:
        pair_profile = pair_board_profile(hole_cards, public_cards)
    if board_texture is None:
        board_texture = board_texture_profile(public_cards)

    score = evaluate_best(hole_cards + public_cards)
    if score[0] >= 1 and pair_profile["pair_type"] != "board_pair":
        return info

    board_suits = [card_suit(card) for card in public_cards]
    suit_counts = {}
    for suit in board_suits:
        suit_counts[suit] = suit_counts.get(suit, 0) + 1
    target_suit = max(suit_counts, key=suit_counts.get)
    max_board_suit = suit_counts[target_suit]

    blocker_score = 0.0
    bluff_type = "none"
    suited_hole_ranks = sorted(
        (card_number(card) for card in hole_cards if card_suit(card) == target_suit),
        reverse=True,
    )
    if max_board_suit >= 3 and suited_hole_ranks:
        high_blocker = suited_hole_ranks[0]
        if high_blocker == 14:
            blocker_score += 0.24
            bluff_type = "flush_ace_blocker"
        elif high_blocker == 13:
            blocker_score += 0.18
            bluff_type = "flush_king_blocker"
        elif high_blocker == 12:
            blocker_score += 0.11
            bluff_type = "flush_queen_blocker"

    hole_ranks = [card_number(card) for card in hole_cards]
    if board_texture["paired"] and max(hole_ranks) >= 13:
        blocker_score += 0.05
        if bluff_type == "none":
            bluff_type = "paired_board_blocker"
    if board_texture["high_card"] >= 12 and 14 in hole_ranks:
        blocker_score += 0.04
        if bluff_type == "none":
            bluff_type = "ace_high_blocker"
    if board_texture["straight_pressure"] >= 0.65 and max(hole_ranks) >= max(10, board_texture["high_card"] - 1):
        blocker_score += 0.04
        if bluff_type == "none":
            bluff_type = "straight_pressure_blocker"

    info["score"] = blocker_score
    info["type"] = bluff_type
    info["eligible"] = blocker_score >= 0.14
    return info


def allow_low_frequency_blocker_bluff(req, hole_cards, public_cards, blocker_profile, round_idx):
    if not blocker_profile["eligible"]:
        return False

    hand_idx = get_hand_index(req) or 0
    token = (sum(hole_cards) * 7 + sum(public_cards) * 11 + hand_idx * 13 + round_idx * 17) % 100
    threshold = int(clamp(blocker_profile["score"] * 35.0, 5.0, 18.0))
    return token < threshold


def nutted_risk_profile(hole_cards, public_cards, pair_profile=None, board_texture=None, value_profile=None, paired_board_profile=None):
    info = {
        "risk": 0.0,
        "label": "none",
        "vulnerable": False,
    }

    if len(public_cards) < 3:
        return info

    if pair_profile is None:
        pair_profile = pair_board_profile(hole_cards, public_cards)
    if board_texture is None:
        board_texture = board_texture_profile(public_cards)
    if paired_board_profile is None:
        paired_board_profile = paired_board_outcome_profile(hole_cards, public_cards)
    if value_profile is None:
        value_profile = value_hand_tier(hole_cards, public_cards, pair_profile, board_texture, paired_board_profile)

    score = evaluate_best(hole_cards + public_cards)
    hand_class = score[0]
    board_ranks = [card_number(card) for card in public_cards]
    hole_ranks = [card_number(card) for card in hole_cards]

    risk = 0.0
    label = "none"

    if hand_class == 6:
        if len(set(board_ranks)) <= 3 and score[1] < max(board_ranks):
            risk += 0.045
            label = "low_full_house"
    elif hand_class == 5:
        flush_profile = made_flush_profile(hole_cards, public_cards, board_texture)
        if flush_profile["nut_like"]:
            label = "nut_like_flush"
        elif flush_profile["high_hole_rank"] >= 12 and flush_profile["better_unseen_ranks"] <= 1:
            risk += 0.010
            label = "near_nut_flush"
        elif flush_profile["high_hole_rank"] >= 10:
            risk += 0.025
            label = "medium_flush"
        else:
            risk += 0.050
            label = "weak_flush"
        if board_texture["paired"]:
            risk += 0.03
            label += "_paired_board"
    elif hand_class == 4:
        if board_texture["flush_pressure"] >= 0.75:
            risk += 0.04
            label = "straight_on_flush_board"
        if board_texture["paired"]:
            risk += 0.03
            label = "straight_on_paired_board"
    elif hand_class == 3:
        if board_texture["paired"]:
            if paired_board_profile["strengthened"]:
                risk += 0.02
                label = paired_board_profile["label"]
            else:
                risk += 0.04
                label = "trips_on_paired_board"
        if score[1] < max(board_ranks):
            risk += 0.02
    elif hand_class == 2 and board_texture["paired"]:
        if paired_board_profile["board_two_pair"]:
            risk += 0.07
            label = paired_board_profile["label"]
        elif paired_board_profile["fragile_two_pair"]:
            risk += 0.08
            label = paired_board_profile["label"]
        elif paired_board_profile["weakened"]:
            risk += 0.06
            label = paired_board_profile["label"]
        elif paired_board_profile["strengthened"]:
            risk += 0.03
            label = paired_board_profile["label"]
        else:
            risk += 0.05
            label = "two_pair_on_paired_board"
    elif hand_class == 1 and value_profile["tier"] in ("strong", "thin"):
        if board_texture["flush_pressure"] >= 1.0:
            risk += 0.03
            label = "pair_on_four_flush"
        if board_texture["straight_pressure"] >= 1.0:
            risk += 0.02
            label = "pair_on_four_straight"

    info["risk"] = clamp(risk, 0.0, 0.14)
    info["label"] = label
    info["vulnerable"] = info["risk"] >= 0.04
    return info


def combo_range_weight(combo, public_cards, state, opponent_model, spot_info):
    preflop = max(0.05, estimate_preflop_strength(list(combo)))
    confidence = opponent_model["confidence"]

    weight = 0.15 + preflop
    if spot_info["opp_preflop_raises"] > 0:
        pressure = 0.95 + 0.35 * spot_info["opp_preflop_raises"] + 0.20 * spot_info["last_raise_bb"]
        pressure += confidence * max(0.0, 0.42 - opponent_model["pfr"]) * 1.8
        pressure -= confidence * max(0.0, opponent_model["pfr"] - 0.38) * 0.9
        pressure -= confidence * opponent_model["allin_rate"] * 0.8
        weight *= preflop ** clamp(pressure, 0.70, 2.80)
    else:
        flatten = 0.80 - confidence * max(0.0, opponent_model["vpip"] - 0.55) * 0.5
        weight *= preflop ** clamp(flatten, 0.55, 1.10)

    if len(public_cards) >= 3:
        made = made_hand_metric(list(combo), public_cards)
        draw = draw_potential(list(combo), public_cards)
        post_metric = max(made, 0.16 + draw)

        if spot_info["facing_postflop_aggression"] or (spot_info["facing_allin"] and state["round"] > 0):
            pressure = 0.95 + 0.25 * spot_info["opp_round_raises"] + 0.35 * spot_info["last_raise_pot_ratio"]
            pressure += confidence * max(0.0, 0.34 - opponent_model["postflop_aggr"]) * 1.6
            pressure -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.46) * 0.8
            weight *= max(0.08, post_metric) ** clamp(pressure, 0.75, 2.80)
        else:
            loose_bonus = confidence * max(0.0, opponent_model["vpip"] - 0.50) * 0.35
            weight *= 0.40 + post_metric + loose_bonus * 0.10

    if spot_info["facing_allin"] and state["round"] == 0:
        jam_pressure = 0.90 + confidence * max(0.0, 0.06 - opponent_model["allin_rate"]) * 4.0
        weight *= preflop ** clamp(jam_pressure, 0.90, 2.80)

    return max(weight, 1e-6)


def build_opponent_range(my_cards, public_cards, state, opponent_model, spot_info):
    used = set(my_cards + public_cards)
    deck = [card for card in range(52) if card not in used]
    combos = []
    weights = []
    for first, second in itertools.combinations(deck, 2):
        combo = (first, second)
        combos.append(combo)
        weights.append(combo_range_weight(combo, public_cards, state, opponent_model, spot_info))
    return combos, weights


def build_cumulative_weights(weights):
    cumulative = []
    total = 0.0
    for weight in weights:
        total += weight
        cumulative.append(total)
    return cumulative, total


def weighted_choice_index(cumulative, total_weight):
    target = random.random() * total_weight
    return bisect.bisect_left(cumulative, target)


def exact_weighted_river_equity(my_cards, public_cards, combos, weights):
    my_score = evaluate_7(my_cards + public_cards)
    wins = 0.0
    total = 0.0

    for combo, weight in zip(combos, weights):
        opponent_score = evaluate_7(list(combo) + public_cards)
        total += weight
        if my_score > opponent_score:
            wins += weight
        elif my_score == opponent_score:
            wins += 0.5 * weight

    return 0.5 if total <= 0 else wins / total


def monte_carlo_weighted_equity(
    my_cards,
    public_cards,
    combos,
    cumulative,
    total_weight,
    iterations,
    deadline=None,
    stats=None,
    stop_standard_error=None,
):
    if total_weight <= 0 or not combos:
        if stats is not None:
            stats.update({
                "equity_mode": "empty",
                "equity_samples": 0,
                "equity_standard_error": None,
            })
        return 0.5

    used = set(my_cards + public_cards)
    deck = [card for card in range(52) if card not in used]
    need_public = 5 - len(public_cards)

    wins = 0.0
    total = 0
    target_iterations = max(0, int(iterations))
    batch_size = MONTE_CARLO_BATCH_SIZE if deadline is not None else max(1, target_iterations)
    standard_error = None
    if stop_standard_error is None:
        stop_standard_error = MONTE_CARLO_STOP_STANDARD_ERROR

    while total < target_iterations or (
        deadline is not None
        and time.monotonic() + MONTE_CARLO_DEADLINE_MARGIN_SECONDS < deadline
    ):
        if deadline is not None and time.monotonic() + MONTE_CARLO_DEADLINE_MARGIN_SECONDS >= deadline:
            if total >= target_iterations:
                break

        if total < target_iterations:
            current_batch = min(batch_size, target_iterations - total)
        else:
            current_batch = batch_size

        for _ in range(current_batch):
            combo = combos[weighted_choice_index(cumulative, total_weight)]
            combo_used = set(combo)
            rest_public_pool = [card for card in deck if card not in combo_used]
            rest_public = random.sample(rest_public_pool, need_public)
            board = public_cards + rest_public
            my_score = evaluate_7(my_cards + board)
            opponent_score = evaluate_7(list(combo) + board)
            if my_score > opponent_score:
                wins += 1.0
            elif my_score == opponent_score:
                wins += 0.5
        total += current_batch

        if deadline is not None and total >= max(5000, target_iterations):
            p = wins / max(1, total)
            standard_error = (p * (1.0 - p) / total) ** 0.5
            if standard_error < stop_standard_error:
                break

    win_rate = wins / max(1, total)
    if standard_error is None and total > 0:
        standard_error = (win_rate * (1.0 - win_rate) / total) ** 0.5
    if stats is not None:
        stats.update({
            "equity_mode": "monte_carlo_timed" if deadline is not None else "monte_carlo_fixed",
            "equity_samples": total,
            "equity_standard_error": standard_error,
        })
    return win_rate


def exact_weighted_turn_equity(my_cards, public_cards, combos, weights, deadline=None, stats=None):
    used = set(my_cards + public_cards)
    deck = [card for card in range(52) if card not in used]
    wins = 0.0
    total = 0.0
    samples = 0
    partial = False

    for combo, weight in zip(combos, weights):
        combo_used = set(combo)
        for river_card in deck:
            if river_card in combo_used:
                continue
            board = public_cards + [river_card]
            my_score = evaluate_7(my_cards + board)
            opponent_score = evaluate_7(list(combo) + board)
            total += weight
            samples += 1
            if my_score > opponent_score:
                wins += weight
            elif my_score == opponent_score:
                wins += 0.5 * weight

        if (
            deadline is not None
            and samples > 0
            and time.monotonic() + MONTE_CARLO_DEADLINE_MARGIN_SECONDS >= deadline
        ):
            partial = True
            break

    if stats is not None:
        stats.update({
            "equity_mode": "turn_exact_partial" if partial else "turn_exact",
            "equity_samples": samples,
            "equity_standard_error": None,
        })
    return 0.5 if total <= 0 else wins / total


def is_critical_equity_spot(state, pot, remaining_hands):
    to_call = state["to_call"]
    return (
        state["opponent_allin"]
        or to_call >= 4 * BIG_BLIND
        or (to_call > 0 and to_call / max(1, pot) >= 0.25)
        or pot >= 3000
        or (remaining_hands is not None and remaining_hands <= 8)
    )


def remaining_equity_time(decision_deadline):
    if decision_deadline is None:
        return 0.0
    return decision_deadline - time.monotonic() - GUOSAI_DECISION_TAIL_SAFETY_SECONDS


def equity_deadline_for_decision(public_count, state, pot, remaining_hands, decision_deadline):
    if decision_deadline is None or public_count >= 5:
        return None

    available = remaining_equity_time(decision_deadline)
    if available <= 0:
        return None

    critical = is_critical_equity_spot(state, pot, remaining_hands)
    if public_count == 0:
        cap = 1.5 if critical else 0.6
    elif public_count == 3:
        cap = 40.0 if (state["opponent_allin"] or pot >= 3000) else 20.0 if critical else 5.0
    elif public_count == 4:
        cap = 40.0 if (state["opponent_allin"] or pot >= 3000) else 30.0 if critical else 5.0
    else:
        cap = 2.0

    return time.monotonic() + min(available, cap)


def equity_refinement_plan(public_count, state, pot, remaining_hands, win_rate, pot_odds, decision_deadline, stats):
    if decision_deadline is None or public_count == 0 or public_count >= 5:
        return None

    stats = stats or {}
    if stats.get("equity_mode") == "turn_exact":
        return None

    available = remaining_equity_time(decision_deadline)
    if available < EQUITY_REFINEMENT_MIN_SECONDS:
        return None

    if state["to_call"] <= 0:
        return None

    gap = abs(win_rate - pot_odds)
    critical = is_critical_equity_spot(state, pot, remaining_hands)
    if gap <= EQUITY_GAP_EXTREME:
        cap = 50.0 if critical else 35.0
        target = MONTE_CARLO_CRITICAL_STANDARD_ERROR
        reason = "pot_odds_extreme_close"
    elif gap <= EQUITY_GAP_CLOSE:
        cap = 40.0 if critical else 22.0
        target = MONTE_CARLO_CRITICAL_STANDARD_ERROR
        reason = "pot_odds_close"
    elif critical and gap <= EQUITY_GAP_NEAR:
        cap = 24.0
        target = MONTE_CARLO_CLOSE_STANDARD_ERROR
        reason = "critical_pot_odds_near"
    else:
        return None

    now = time.monotonic()
    return {
        "deadline": now + min(available, cap),
        "gap": gap,
        "reason": reason,
        "target_standard_error": target,
    }


def merge_refined_equity(initial_rate, initial_stats, refined_rate, refined_stats):
    initial_mode = initial_stats.get("equity_mode") or ""
    refined_mode = refined_stats.get("equity_mode") or ""
    initial_samples = int(initial_stats.get("equity_samples") or 0)
    refined_samples = int(refined_stats.get("equity_samples") or 0)
    if (
        initial_mode.startswith("monte_carlo")
        and refined_mode.startswith("monte_carlo")
        and initial_samples > 0
        and refined_samples > 0
    ):
        total_samples = initial_samples + refined_samples
        win_rate = (
            initial_rate * initial_samples + refined_rate * refined_samples
        ) / total_samples
        standard_error = (win_rate * (1.0 - win_rate) / total_samples) ** 0.5
        merged_stats = dict(refined_stats)
        merged_stats.update({
            "equity_mode": "monte_carlo_refined",
            "equity_samples": total_samples,
            "equity_standard_error": standard_error,
        })
        return win_rate, merged_stats

    return refined_rate, refined_stats


def estimate_weighted_win_rate(
    my_cards,
    public_cards,
    combos,
    weights,
    iterations,
    deadline=None,
    stats=None,
    stop_standard_error=None,
):
    if len(public_cards) == 5:
        if stats is not None:
            stats.update({
                "equity_mode": "river_exact",
                "equity_samples": len(combos),
                "equity_standard_error": None,
            })
        return exact_weighted_river_equity(my_cards, public_cards, combos, weights)

    if len(public_cards) == 4 and deadline is not None:
        return exact_weighted_turn_equity(my_cards, public_cards, combos, weights, deadline, stats)

    cumulative, total_weight = build_cumulative_weights(weights)
    return monte_carlo_weighted_equity(
        my_cards,
        public_cards,
        combos,
        cumulative,
        total_weight,
        iterations,
        deadline=deadline,
        stats=stats,
        stop_standard_error=stop_standard_error,
    )


def match_risk_adjustment(req, my_id, remaining_hands):
    total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
    if len(total_win_chips) <= my_id:
        return 0.0
    if remaining_hands is None or remaining_hands <= 0:
        return 0.0

    lead = total_win_chips[my_id]
    scale = max(1.0, remaining_hands * BIG_BLIND * 5.0)
    if lead >= 0:
        cap = 0.05
        if remaining_hands >= 45:
            cap = 0.025
        elif remaining_hands >= 35:
            cap = 0.035
        return min(cap, lead / scale)
    return -min(0.05, (-lead) / (scale * 0.85))


def match_pressure_profile(req, my_id, remaining_hands):
    profile = {
        "protect": 0.0,
        "chase": 0.0,
        "threshold_delta": 0.0,
        "sizing_delta": 0.0,
        "open_delta": 0.0,
        "bluff_delta": 0.0,
    }

    total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
    if len(total_win_chips) <= my_id or remaining_hands is None or remaining_hands <= 0:
        return profile

    try:
        lead = int(total_win_chips[my_id])
    except (TypeError, ValueError):
        return profile

    hands = max(1, int(remaining_hands))
    late_factor = clamp((12 - hands) / 10.0, 0.0, 1.0)
    if late_factor <= 0.0:
        return profile

    behind_per_hand = max(0.0, -lead) / hands
    ahead_per_hand = max(0.0, lead) / hands

    chase = clamp((behind_per_hand - 0.8 * BIG_BLIND) / (3.5 * BIG_BLIND), 0.0, 1.0)
    protect = clamp((ahead_per_hand - 0.8 * BIG_BLIND) / (3.0 * BIG_BLIND), 0.0, 1.0)
    chase *= late_factor
    protect *= late_factor
    if not (hands <= 8 and lead < -8 * BIG_BLIND):
        chase = min(chase, 0.25)

    profile["protect"] = protect
    profile["chase"] = chase
    profile["threshold_delta"] = 0.055 * protect - 0.055 * chase
    profile["sizing_delta"] = -0.10 * protect + 0.16 * chase
    profile["open_delta"] = 0.020 * protect - 0.030 * chase
    profile["bluff_delta"] = -0.08 * protect + 0.10 * chase
    return profile


def apply_anti_lock_pressure(match_profile):
    match_profile["protect"] = 0.0
    match_profile["chase"] = max(match_profile["chase"], 0.90)
    match_profile["threshold_delta"] = min(match_profile["threshold_delta"], -0.075)
    match_profile["sizing_delta"] = max(match_profile["sizing_delta"], 0.18)
    match_profile["open_delta"] = min(match_profile["open_delta"], -0.045)
    match_profile["bluff_delta"] = max(match_profile["bluff_delta"], 0.13)
    return match_profile


def opponent_pressure_adjustment(opponent_model, spot_info, round_idx):
    confidence = opponent_model["confidence"]
    adjustment = 0.0

    if spot_info["facing_raise"] or spot_info["facing_allin"]:
        adjustment += confidence * max(0.0, 0.44 - opponent_model["pfr"]) * 0.07
        if round_idx > 0:
            adjustment += confidence * max(0.0, 0.36 - opponent_model["postflop_aggr"]) * 0.06
        adjustment -= confidence * max(0.0, opponent_model["allin_rate"] - 0.08) * 0.08
        adjustment -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.48) * 0.05
        adjustment += min(0.04, spot_info["last_raise_pot_ratio"] * 0.04)

    return clamp(adjustment, -0.05, 0.07)


def aggressive_line_strength(spot_info, board_texture):
    strength = 0.0
    if spot_info.get("opp_postflop_bet_count", 0) >= 2:
        strength += 0.04
    if spot_info.get("opp_current_round_bet_count", 0) >= 2:
        strength += 0.08 if board_texture is not None and board_texture["paired"] else 0.05
    if spot_info.get("opp_current_round_bet_count", 0) >= 3:
        strength += 0.03
    return clamp(strength, 0.0, 0.15)


def check_probe_resistance_margin(spot_info, opponent_model, round_idx):
    if round_idx <= 0 or not spot_info["facing_postflop_aggression"]:
        return 0.0

    margin = 0.0
    same_street_check_raise = (
        spot_info.get("opp_current_round_check_count", 0) > 0
        and spot_info.get("opp_current_round_bet_count", 0) > 0
    )
    delayed_resistance = (
        spot_info.get("opp_prior_postflop_check_count", 0) >= 2
        and spot_info.get("opp_current_round_bet_count", 0) > 0
    )

    if same_street_check_raise:
        margin += 0.035
    if delayed_resistance:
        margin += 0.018

    confidence = opponent_model.get("confidence", 0.0)
    if opponent_model.get("postflop_check_rate", 0.42) >= 0.52:
        margin += confidence * 0.018

    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])
    if size_bucket == "large":
        margin += 0.020
    elif size_bucket == "medium":
        margin += 0.010

    return clamp(margin, 0.0, 0.085)


def must_continue_vs_raise(value_profile, made_strength, pot_odds, nutted_risk, board_texture):
    tier = value_profile.get("tier", "none") if value_profile is not None else "none"
    risk = nutted_risk.get("risk", 0.0) if nutted_risk is not None else 0.0
    extreme_texture = (
        board_texture is not None
        and (board_texture["flush_pressure"] >= 1.0 or board_texture["straight_pressure"] >= 1.0)
    )

    if tier == "nut":
        return True
    if made_strength >= 0.58 and pot_odds <= 0.42 and risk <= 0.07:
        return not (extreme_texture and risk >= 0.04)
    if tier == "strong" and pot_odds <= 0.36 and risk <= 0.05:
        return True
    return False


def anti_lock_continue_floor(pot_odds, round_idx, value_profile, draw_info, made_strength):
    discount = 0.07 + 0.025 * max(0, round_idx)

    if value_profile is not None:
        if value_profile.get("tier") == "nut":
            discount += 0.12
        elif value_profile.get("tier") == "strong":
            discount += 0.08
        elif value_profile.get("tier") == "thin":
            discount += 0.035

    if draw_info is not None:
        if draw_info.get("type") in ("combo_draw", "nut_flush_draw"):
            discount += 0.05
        elif draw_info.get("semi_bluff"):
            discount += 0.03

    if made_strength < 0.18 and (draw_info is None or draw_info.get("quality", 0.0) < 0.08):
        discount -= 0.04

    return max(0.08, pot_odds - discount)


def anti_lock_can_continue(anti_lock_pressure, win_rate, pot_odds, round_idx, value_profile, draw_info, made_strength):
    if not anti_lock_pressure:
        return False
    return win_rate >= anti_lock_continue_floor(pot_odds, round_idx, value_profile, draw_info, made_strength)


def choose_anti_lock_pressure_action(
    state,
    my_chips,
    to_call,
    pot,
    round_idx,
    win_rate,
    opponent_model,
    remaining_hands,
    preflop_strength=None,
    value_profile=None,
    draw_info=None,
    blocker_profile=None,
    board_texture=None,
):
    if state["opponent_allin"] or my_chips <= 1:
        return None
    if to_call >= my_chips:
        return -2

    hands_left = remaining_hands if remaining_hands is not None else TOTAL_HANDS
    pot_after_call = pot + to_call
    fold_to_raise = opponent_model.get("fold_to_raise", 0.44)
    confidence = opponent_model.get("confidence", 0.0)

    tier = value_profile.get("tier", "none") if value_profile is not None else "none"
    draw_quality = draw_info.get("quality", 0.0) if draw_info is not None else 0.0
    has_draw = draw_info.get("semi_bluff", False) if draw_info is not None else False
    has_blocker = blocker_profile is not None and blocker_profile.get("eligible", False)

    weak_showdown = tier in ("none", "thin") and draw_quality < 0.14 and win_rate < 0.45
    high_fold_pressure = confidence < 0.20 or fold_to_raise >= 0.42
    emergency_jam = (
        hands_left <= 3
        or (to_call > 0 and to_call / max(1, pot) >= 0.35)
        or (weak_showdown and high_fold_pressure and hands_left <= 6)
        or (win_rate < 0.18 and hands_left <= 5)
    )
    if tier in ("strong", "nut") or has_draw:
        emergency_jam = emergency_jam and hands_left <= 3

    if emergency_jam:
        return -2

    min_raise_action = state.get("min_raise_action", state["round_raise"])

    if round_idx == 0:
        ratio = 2.20 if to_call == 0 else 2.60
        target = int(to_call + pot_after_call * ratio)
        strength = preflop_strength if preflop_strength is not None else win_rate
        target = max(target, int((5.5 + max(0.0, strength - 0.50) * 3.0) * BIG_BLIND) - state["my_round_bet"])
    elif round_idx == 1:
        target = int(to_call + pot_after_call * 1.15)
    elif round_idx == 2:
        target = int(to_call + pot_after_call * 1.35)
    else:
        target = int(to_call + pot_after_call * 1.55)

    if board_texture is not None and board_texture.get("dynamic", False):
        target = int(target * 1.08)
    if has_blocker or has_draw:
        target = int(target * 1.06)
    if weak_showdown:
        target = int(target * 1.12)

    amount = max(min_raise_action, target)
    if amount >= my_chips * 0.72:
        return -2
    amount = min(amount, my_chips - 1)
    if amount <= to_call or amount < min_raise_action:
        return -2 if hands_left <= 4 else None
    return amount


def paired_board_stackoff_profile(pair_profile, paired_board_profile, board_texture, spot_info, round_idx):
    info = {
        "active": False,
        "severe": False,
        "line_strength": 0.0,
        "size_bucket": "small",
    }

    if round_idx <= 0 or board_texture is None or not board_texture["paired"]:
        return info

    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])
    line_strength = 0.0
    active = False

    if paired_board_profile is not None and paired_board_profile["board_two_pair"]:
        active = True
        line_strength += 0.05
    elif pair_profile is not None and pair_profile["pair_type"] == "overpair":
        active = True
        line_strength += 0.04

    if not active:
        return info

    if spot_info["facing_postflop_aggression"]:
        line_strength += 0.03
    if spot_info.get("opp_current_round_bet_count", 0) >= 2:
        line_strength += 0.08
    elif size_bucket in ("medium", "large"):
        line_strength += 0.04
    if round_idx >= 2:
        line_strength += 0.02

    info["active"] = True
    info["severe"] = (
        spot_info["facing_postflop_aggression"]
        and spot_info.get("opp_current_round_bet_count", 0) >= 2
        and size_bucket in ("medium", "large")
    )
    info["line_strength"] = clamp(line_strength, 0.0, 0.18)
    info["size_bucket"] = size_bucket
    return info


def postflop_call_margin(spot_info, opponent_model, made_strength, draw_strength, round_idx, has_position):
    if round_idx <= 0:
        return 0.0

    margin = 0.0
    air_hand = made_strength < 0.18 and draw_strength < 0.08
    weak_showdown = made_strength < 0.22
    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])

    if weak_showdown:
        margin += 0.012
    if air_hand:
        margin += 0.018

    if spot_info["facing_postflop_aggression"]:
        margin += 0.008
        if size_bucket == "small":
            margin += 0.020
        elif size_bucket == "medium":
            margin += 0.010
        else:
            margin += 0.024

        if spot_info.get("opp_postflop_bet_count", 0) >= 2:
            margin += 0.024 if size_bucket == "small" else 0.014
        if round_idx >= 2 and air_hand:
            margin += 0.010
        if round_idx == 3 and size_bucket == "large":
            margin += 0.020

    if not has_position:
        margin += 0.008

    confidence = opponent_model["confidence"]
    if air_hand:
        margin -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.50) * 0.015
    else:
        margin -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.50) * 0.008

    return clamp(margin, 0.0, 0.08)


def realized_postflop_equity(
    win_rate,
    made_strength,
    draw_strength,
    round_idx,
    has_position,
    spot_info,
    pair_profile=None,
):
    air_hand = made_strength < 0.18 and draw_strength < 0.08
    if round_idx <= 0:
        return win_rate

    eqr = 1.0

    if air_hand:
        eqr = 0.72 if has_position else 0.62

        if spot_info.get("opp_postflop_bet_count", 0) >= 2:
            eqr -= 0.10
        if round_idx == 2:
            eqr -= 0.05
        elif round_idx == 3:
            eqr -= 0.12

        eqr = clamp(eqr, 0.45, 0.85)
        return win_rate * eqr

    if pair_profile is not None and pair_profile["made_class"] == 1:
        pair_type = pair_profile["pair_type"]

        if pair_type in ("middle_pair", "bottom_pair", "underpair", "board_pair"):
            eqr = 0.86 if has_position else 0.78

            if pair_profile["weak_kicker"]:
                eqr -= 0.05
            if spot_info.get("opp_postflop_bet_count", 0) >= 2:
                eqr -= 0.06
            if round_idx == 3:
                eqr -= 0.06

            eqr = clamp(eqr, 0.65, 0.92)
            return win_rate * eqr

        if pair_type == "top_pair" and pair_profile["weak_kicker"]:
            eqr = 0.92 if has_position else 0.86
            if spot_info.get("opp_postflop_bet_count", 0) >= 2:
                eqr -= 0.04
            eqr = clamp(eqr, 0.75, 0.95)
            return win_rate * eqr

    return win_rate


def choose_raise(
    min_raise,
    my_chips,
    my_round_bet,
    to_call,
    pot,
    win_rate,
    round_idx,
    spot_name,
    preflop_strength,
    has_position,
    opponent_model,
    semi_bluff=False,
    value_profile=None,
    value_plan=None,
    board_texture=None,
    draw_info=None,
    blocker_bluff=False,
    probe_mode=False,
    pressure_line=False,
    induce_mode=False,
    nutted_risk_score=0.0,
    match_sizing_delta=0.0,
):
    if my_chips <= max(min_raise, to_call) + 1:
        return None

    pot_after_call = pot + to_call
    confidence = opponent_model["confidence"]
    fold_to_raise = opponent_model["fold_to_raise"]
    if value_profile is None:
        value_profile = {"tier": "none", "size_bonus": 0.0}
    if value_plan is None:
        value_plan = {"size_delta": 0.0, "induce": False, "protect": False, "thin_control": False}
    if board_texture is None:
        board_texture = {"wetness": 0.0, "dynamic": False}
    if draw_info is None:
        draw_info = empty_draw_profile()
    wetness = board_texture["wetness"]

    if round_idx == 0:
        ratio = 0.55 if to_call == 0 else 0.75
    elif round_idx == 1:
        ratio = 0.60
    elif round_idx == 2:
        ratio = 0.70
    else:
        ratio = 0.85

    ratio += max(0.0, win_rate - 0.55) * (0.90 + 0.20 * round_idx)
    ratio += -0.05 if has_position else 0.05
    ratio += confidence * max(0.0, fold_to_raise - 0.52) * (0.20 if semi_bluff else 0.10)
    ratio += value_profile.get("size_bonus", 0.0)
    ratio += value_plan.get("size_delta", 0.0)
    ratio += match_sizing_delta
    if round_idx > 0 and value_profile.get("tier") == "strong" and not semi_bluff and not pressure_line:
        if not board_texture["dynamic"]:
            ratio -= 0.05
        if wetness <= 0.20:
            ratio -= 0.02
    if board_texture["dynamic"]:
        if value_profile.get("tier") in ("strong", "nut"):
            ratio += 0.05 * wetness
        elif value_profile.get("tier") == "thin":
            ratio -= 0.04 * wetness
    if semi_bluff:
        ratio -= 0.08
        ratio += 0.02 * wetness
        ratio += draw_info.get("size_bonus", 0.0)
        if draw_info.get("type") == "gutshot":
            ratio -= 0.04
    if pressure_line:
        ratio += 0.05 + 0.04 * wetness
    if nutted_risk_score > 0.0 and value_profile.get("tier") != "nut":
        ratio -= min(0.10, nutted_risk_score * 0.55)
    if blocker_bluff:
        ratio = min(ratio, 0.54 + 0.18 * wetness + 0.08 * max(0, round_idx - 1))
        ratio += confidence * max(0.0, fold_to_raise - 0.58) * 0.22
    inducing_value = (induce_mode or value_plan.get("induce", False)) and to_call == 0 and value_profile.get("tier") == "nut"
    if inducing_value:
        induce_cap = 0.29 + 0.05 * round_idx + 0.05 * wetness
        ratio = min(ratio, induce_cap)
    if probe_mode:
        probe_ratio = 0.25 + 0.08 * wetness
        if value_profile.get("tier") == "thin":
            probe_ratio += 0.08
        if blocker_bluff and round_idx == 3:
            probe_ratio = max(probe_ratio, 0.34 + 0.08 * wetness)
        elif round_idx == 3:
            probe_ratio += 0.05
        ratio = min(ratio, probe_ratio)
    thin_cap = None
    if value_plan.get("thin_control", False) and value_profile.get("tier") != "nut":
        thin_cap = 0.30 if round_idx <= 2 else 0.38
        ratio = min(ratio, thin_cap)
    low_ratio = 0.28 if inducing_value else 0.22 if probe_mode or (blocker_bluff and to_call == 0) else 0.40
    if thin_cap is not None:
        low_ratio = min(low_ratio, thin_cap)
    ratio = clamp(ratio, low_ratio, 1.45)

    amount = int(to_call + pot_after_call * ratio)

    if round_idx == 0 and preflop_strength is not None:
        if spot_name == "sb_open":
            desired_total = int((2.5 + max(0.0, preflop_strength - 0.58) * 1.8) * BIG_BLIND)
            amount = max(amount, desired_total - my_round_bet)
        elif spot_name == "bb_vs_limp":
            desired_total = int((3.2 + max(0.0, preflop_strength - 0.60) * 1.8) * BIG_BLIND)
            amount = max(amount, desired_total - my_round_bet)

    amount = max(min_raise, amount)
    if semi_bluff and fold_to_raise < 0.45:
        amount = min(amount, max(min_raise, int(to_call + pot_after_call * 0.60)))
    if blocker_bluff:
        bluff_cap = max(min_raise, int(to_call + pot_after_call * (0.45 if round_idx == 3 and to_call == 0 else 0.56 + 0.16 * wetness)))
        amount = min(amount, bluff_cap)
    amount = min(amount, my_chips - 1)

    if amount <= to_call or amount < min_raise or amount >= my_chips:
        return None
    return amount


def choose_preflop_spot_action(req, state, spot_info, opponent_model, preflop_strength, win_rate, match_profile):
    my_chips = req["my_chips"]
    to_call = state["to_call"]
    remaining_hands = get_remaining_hands(req)
    match_adjust = match_risk_adjustment(req, req["my_id"], remaining_hands)
    confidence = opponent_model["confidence"]
    loose_bonus = confidence * max(0.0, opponent_model["vpip"] - 0.55) * 0.03
    trash_hand = is_preflop_trash_hand(req["my_cards"], preflop_strength)

    if spot_info["preflop_spot"] == "sb_open":
        early_open_bonus = -0.015 if remaining_hands is not None and remaining_hands >= 40 else 0.0
        open_threshold = 0.49 + match_adjust + 0.02 + match_profile["open_delta"] + early_open_bonus
        limp_threshold = 0.36 + match_adjust + 0.005
        raise_amount = choose_raise(
            state["min_raise_action"],
            my_chips,
            state["my_round_bet"],
            to_call,
            state["pot"],
            max(win_rate, preflop_strength),
            0,
            spot_info["preflop_spot"],
            preflop_strength,
            spot_info["has_position"],
            opponent_model,
            match_sizing_delta=match_profile["sizing_delta"],
        )
        if not trash_hand and preflop_strength >= open_threshold and raise_amount is not None:
            return raise_amount
        if preflop_strength <= limp_threshold - loose_bonus:
            return -1
        return 0

    if spot_info["preflop_spot"] == "bb_vs_limp":
        iso_threshold = 0.57 + match_adjust - loose_bonus + match_profile["open_delta"]
        iso_threshold -= confidence * max(0.0, opponent_model["vpip"] - 0.58) * 0.08
        iso_threshold -= confidence * max(0.0, opponent_model["fold_to_raise"] - 0.52) * 0.05
        raise_amount = choose_raise(
            state["min_raise_action"],
            my_chips,
            state["my_round_bet"],
            to_call,
            state["pot"],
            max(win_rate, preflop_strength),
            0,
            spot_info["preflop_spot"],
            preflop_strength,
            spot_info["has_position"],
            opponent_model,
            match_sizing_delta=match_profile["sizing_delta"],
        )
        if not trash_hand and preflop_strength >= iso_threshold and raise_amount is not None:
            return raise_amount
        return 0

    return None


def get_action(req, requests, trace=None, decision_deadline=None):
    my_id = req["my_id"]
    my_chips = req["my_chips"]
    my_cards = req["my_cards"]
    public_cards = req["public_cards"]

    state = reconstruct_state(req)
    if trace is not None:
        trace.update({
            "my_id": my_id,
            "my_chips": my_chips,
            "my_cards": list(my_cards),
            "public_cards": list(public_cards),
            "round": state["round"],
            "to_call": state["to_call"],
            "pot": state["pot"],
            "my_round_bet": state["my_round_bet"],
            "round_bet": state["round_bet"],
            "round_raise": state["round_raise"],
            "opponent_allin": state["opponent_allin"],
        })
    if should_lock_win(req, state, my_id):
        if trace is not None:
            trace["decision_branch"] = "lock_win_fold"
        return -1

    opponent_model = build_opponent_model(requests, my_id)
    spot_info = analyze_current_spot(req, state)
    round_idx = state["round"]
    to_call = state["to_call"]
    pot = max(1, state["pot"])
    pot_odds = to_call / (pot + to_call) if to_call > 0 else 0.0
    remaining_hands = get_remaining_hands(req)
    match_profile = match_pressure_profile(req, my_id, remaining_hands)
    anti_lock_pressure = fold_gives_opponent_lock(req, state, my_id)
    if anti_lock_pressure:
        match_profile = apply_anti_lock_pressure(match_profile)
    if trace is not None:
        trace.update({
            "remaining_hands": remaining_hands,
            "anti_lock_pressure": anti_lock_pressure,
            "match_chase": trace_float(match_profile["chase"]),
            "match_protect": trace_float(match_profile["protect"]),
            "pot_odds": trace_float(pot_odds),
            "preflop_spot": spot_info.get("preflop_spot"),
            "has_position": spot_info.get("has_position"),
            "last_opp_action_type": spot_info.get("last_opp_action_type"),
            "opponent_confidence": trace_float(opponent_model.get("confidence")),
            "opponent_aggression": trace_float(opponent_model.get("aggression")),
            "opponent_fold_to_raise": trace_float(opponent_model.get("fold_to_raise")),
        })

    preflop_strength = estimate_preflop_strength(my_cards) if not public_cards else None
    preflop_3bet_candidate = is_preflop_3bet_candidate(my_cards) if preflop_strength is not None else False
    critical_spot = is_critical_equity_spot(state, pot, remaining_hands)
    simulations = 0
    extra = 0
    equity_stats = {}
    equity_refined = False
    equity_refine_reason = None
    equity_refine_gap = None
    equity_refine_target_se = None
    equity_initial_mode = None
    equity_initial_samples = None
    if preflop_strength is not None:
        win_rate = preflop_strength
        equity_stats.update({
            "equity_mode": "preflop_lookup",
            "equity_samples": 0,
            "equity_standard_error": None,
        })
    else:
        combos, weights = build_opponent_range(my_cards, public_cards, state, opponent_model, spot_info)
        simulations = SIMULATIONS_BY_PUBLIC_COUNT.get(len(public_cards), 700)
        equity_deadline = equity_deadline_for_decision(
            len(public_cards),
            state,
            pot,
            remaining_hands,
            decision_deadline,
        )
        win_rate = estimate_weighted_win_rate(
            my_cards,
            public_cards,
            combos,
            weights,
            simulations,
            deadline=equity_deadline,
            stats=equity_stats,
        )

        extra = EXTRA_SIMULATIONS_BY_PUBLIC_COUNT.get(len(public_cards), 0)
        if decision_deadline is None and critical_spot and extra > 0:
            refined = estimate_weighted_win_rate(my_cards, public_cards, combos, weights, extra)
            win_rate = (win_rate * simulations + refined * extra) / (simulations + extra)

        refine_plan = equity_refinement_plan(
            len(public_cards),
            state,
            pot,
            remaining_hands,
            win_rate,
            pot_odds,
            decision_deadline,
            equity_stats,
        )
        if refine_plan is not None:
            initial_rate = win_rate
            initial_stats = dict(equity_stats)
            refine_stats = {}
            refined = estimate_weighted_win_rate(
                my_cards,
                public_cards,
                combos,
                weights,
                simulations,
                deadline=refine_plan["deadline"],
                stats=refine_stats,
                stop_standard_error=refine_plan["target_standard_error"],
            )
            win_rate, equity_stats = merge_refined_equity(
                initial_rate,
                initial_stats,
                refined,
                refine_stats,
            )
            equity_refined = True
            equity_refine_reason = refine_plan["reason"]
            equity_refine_gap = refine_plan["gap"]
            equity_refine_target_se = refine_plan["target_standard_error"]
            equity_initial_mode = initial_stats.get("equity_mode")
            equity_initial_samples = initial_stats.get("equity_samples")
    if trace is not None:
        trace.update({
            "win_rate": trace_float(win_rate),
            "simulations": simulations,
            "extra_simulations": extra if decision_deadline is None and critical_spot else 0,
            "critical_spot": critical_spot,
            "equity_mode": equity_stats.get("equity_mode"),
            "equity_samples": equity_stats.get("equity_samples"),
            "equity_standard_error": trace_float(equity_stats.get("equity_standard_error")),
            "equity_gap": trace_float(equity_refine_gap),
            "equity_refined": equity_refined,
            "equity_refine_reason": equity_refine_reason,
            "equity_refine_target_se": trace_float(equity_refine_target_se),
            "equity_initial_mode": equity_initial_mode,
            "equity_initial_samples": equity_initial_samples,
            "preflop_strength": trace_float(preflop_strength),
            "preflop_3bet_candidate": preflop_3bet_candidate,
        })

    if round_idx == 0 and preflop_strength is not None:
        spot_action = choose_preflop_spot_action(
            req,
            state,
            spot_info,
            opponent_model,
            preflop_strength,
            win_rate,
            match_profile,
        )
        if spot_action is not None:
            if trace is not None:
                trace["decision_branch"] = "preflop_spot_action"
            if anti_lock_pressure and spot_action <= 0:
                anti_lock_attack = choose_anti_lock_pressure_action(
                    state,
                    my_chips,
                    to_call,
                    pot,
                    round_idx,
                    win_rate,
                    opponent_model,
                    remaining_hands,
                    preflop_strength=preflop_strength,
                )
                if anti_lock_attack is not None:
                    if trace is not None:
                        trace["decision_branch"] = "anti_lock_preflop_attack"
                    return anti_lock_attack
                if spot_action == -1 and to_call < my_chips:
                    if trace is not None:
                        trace["decision_branch"] = "anti_lock_preflop_call_override"
                    return 0
            return spot_action

    made_strength = made_hand_metric(my_cards, public_cards) if len(public_cards) >= 3 else 0.0
    pair_profile = pair_board_profile(my_cards, public_cards) if len(public_cards) >= 3 else None
    board_texture = board_texture_profile(public_cards) if len(public_cards) >= 3 else None
    draw_info = draw_profile(my_cards, public_cards, board_texture) if len(public_cards) >= 3 else empty_draw_profile()
    draw_strength = draw_info["quality"]
    marginal_pair = marginal_pair_under_pressure(pair_profile, board_texture) if len(public_cards) >= 3 else False
    paired_board_profile = paired_board_outcome_profile(my_cards, public_cards) if len(public_cards) >= 3 else None
    value_profile = value_hand_tier(my_cards, public_cards, pair_profile, board_texture, paired_board_profile) if len(public_cards) >= 3 else None
    flush_profile = made_flush_profile(my_cards, public_cards, board_texture) if len(public_cards) >= 3 else None
    blocker_profile = blocker_bluff_profile(my_cards, public_cards, pair_profile, board_texture) if len(public_cards) >= 3 else None
    nutted_risk = (
        nutted_risk_profile(my_cards, public_cards, pair_profile, board_texture, value_profile, paired_board_profile)
        if len(public_cards) >= 3
        else {"risk": 0.0, "label": "none", "vulnerable": False}
    )
    value_plan = (
        value_bet_plan(value_profile, board_texture, paired_board_profile, pair_profile, nutted_risk, round_idx, pot)
        if len(public_cards) >= 3
        else {"size_delta": 0.0, "induce": False, "protect": False, "thin_control": False}
    )
    if trace is not None:
        trace.update({
            "pot_odds": trace_float(pot_odds),
            "made_strength": trace_float(made_strength),
            "draw_strength": trace_float(draw_strength),
            "value_tier": value_profile["tier"] if value_profile else None,
            "board_wetness": trace_float(board_texture["wetness"]) if board_texture else None,
            "board_dynamic": board_texture["dynamic"] if board_texture else None,
            "nutted_risk": trace_float(nutted_risk["risk"]),
        })
    line_strength = aggressive_line_strength(spot_info, board_texture) if len(public_cards) >= 3 else 0.0
    check_resistance = check_probe_resistance_margin(spot_info, opponent_model, round_idx) if len(public_cards) >= 3 else 0.0
    paired_board_stackoff = (
        paired_board_stackoff_profile(pair_profile, paired_board_profile, board_texture, spot_info, round_idx)
        if len(public_cards) >= 3
        else {"active": False, "severe": False, "line_strength": 0.0, "size_bucket": "small"}
    )
    repeated_raise_trap = (
        round_idx > 0
        and spot_info["facing_postflop_aggression"]
        and spot_info.get("opp_current_round_bet_count", 0) >= 2
    )
    strong_flush_repressure_continue = (
        flush_profile is not None
        and (
            flush_profile["repressure_continue"]
            or flush_profile["nut_like"]
            or (
                board_texture is not None
                and not board_texture["paired"]
                and flush_profile["high_hole_rank"] >= 12
                and flush_profile["better_unseen_ranks"] <= 1
            )
        )
    )
    hard_repressure_fold = (
        repeated_raise_trap
        and not strong_flush_repressure_continue
        and (value_profile is None or value_profile["tier"] != "nut")
        and (
            (board_texture is not None and board_texture["paired"])
            or bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large")
        )
    )

    strong = 0.69 if round_idx == 0 else 0.65 if round_idx == 1 else 0.61 if round_idx == 2 else 0.59
    medium = 0.54 if round_idx == 0 else 0.50 if round_idx == 1 else 0.48

    if spot_info["has_position"]:
        strong -= 0.015
        medium -= 0.01
    else:
        strong += 0.02
        medium += 0.015

    if preflop_strength is not None:
        if preflop_strength >= 0.72:
            strong -= 0.03
            medium -= 0.02
        elif preflop_strength <= 0.40:
            strong += 0.04
            medium += 0.03

    match_adjust = match_risk_adjustment(req, my_id, remaining_hands)
    pressure_adjust = opponent_pressure_adjustment(opponent_model, spot_info, round_idx)
    strong += match_adjust + pressure_adjust + match_profile["threshold_delta"]
    medium += match_adjust + pressure_adjust * 0.8 + 0.75 * match_profile["threshold_delta"]
    strong += 0.30 * line_strength + 0.45 * paired_board_stackoff["line_strength"]
    medium += 0.18 * line_strength + 0.22 * paired_board_stackoff["line_strength"]
    strong += 0.30 * check_resistance
    medium += 0.20 * check_resistance
    if value_profile is not None:
        if value_profile["tier"] == "nut":
            strong -= 0.07
            medium -= 0.04
        elif value_profile["tier"] == "strong":
            strong -= 0.04
            medium -= 0.02
        elif value_profile["tier"] == "thin":
            medium -= 0.01
    strong += 0.45 * nutted_risk["risk"]
    medium += 0.30 * nutted_risk["risk"]

    if state["opponent_allin"]:
        jam_cost = max(state["allin_call_amount"], to_call)
        jam_odds = jam_cost / (pot + jam_cost) if jam_cost > 0 else 0.0
        jam_buffer = 0.02 + max(0.0, strong - 0.65) * 0.2
        if value_profile is not None and value_profile["tier"] == "thin":
            jam_buffer += 0.04
        jam_buffer += nutted_risk["risk"]
        jam_buffer += 0.04 * match_profile["protect"]
        jam_buffer += line_strength + paired_board_stackoff["line_strength"]
        jam_buffer += check_resistance
        if remaining_hands == 1:
            total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
            if len(total_win_chips) > my_id and total_win_chips[my_id] < 0:
                jam_buffer -= 0.03
        if preflop_strength is not None and preflop_strength < 0.42:
            jam_buffer += 0.02
        if anti_lock_pressure:
            jam_buffer -= 0.10
        anti_lock_jam_continue = anti_lock_can_continue(
            anti_lock_pressure,
            win_rate,
            jam_odds,
            round_idx,
            value_profile,
            draw_info,
            made_strength,
        )
        if hard_repressure_fold or paired_board_stackoff["severe"]:
            if not anti_lock_jam_continue:
                return -1
        jam_buffer = clamp(jam_buffer, -0.05 if anti_lock_pressure else 0.0, 0.14)
        return -2 if win_rate >= jam_odds + jam_buffer or anti_lock_jam_continue else -1

    if to_call >= my_chips:
        shove_odds = my_chips / (pot + my_chips)
        shove_buffer = 0.01 + max(0.0, strong - 0.64) * 0.2
        if value_profile is not None and value_profile["tier"] == "thin":
            shove_buffer += 0.04
        shove_buffer += nutted_risk["risk"]
        shove_buffer += 0.04 * match_profile["protect"]
        shove_buffer += line_strength + paired_board_stackoff["line_strength"]
        shove_buffer += check_resistance
        if anti_lock_pressure:
            shove_buffer -= 0.10
        anti_lock_shove_continue = anti_lock_can_continue(
            anti_lock_pressure,
            win_rate,
            shove_odds,
            round_idx,
            value_profile,
            draw_info,
            made_strength,
        )
        if hard_repressure_fold or paired_board_stackoff["severe"]:
            if not anti_lock_shove_continue:
                return -1
        shove_buffer = clamp(shove_buffer, -0.05 if anti_lock_pressure else 0.0, 0.14)
        return -2 if win_rate >= shove_odds + shove_buffer or anti_lock_shove_continue else -1

    if to_call > 0:
        if round_idx == 0:
            call_margin = 0.005 + (0.010 if not spot_info["has_position"] else 0.0)
            if preflop_strength is not None and preflop_strength <= 0.40:
                call_margin += 0.015
            realized_rate = win_rate
        else:
            call_margin = postflop_call_margin(
                spot_info,
                opponent_model,
                made_strength,
                draw_strength,
                round_idx,
                spot_info["has_position"],
            )
            call_margin += pair_domination_margin(
                pair_profile,
                spot_info,
                round_idx,
            )
            call_margin += draw_call_margin(
                draw_info,
                board_texture,
                round_idx,
                spot_info,
            )
            if (
                round_idx == 2
                and spot_info["facing_postflop_aggression"]
                and pair_profile is not None
                and pair_profile["made_class"] == 1
                and pair_profile["pair_type"] in ("middle_pair", "bottom_pair", "underpair")
            ):
                call_margin += 0.035
            call_margin += line_strength + paired_board_stackoff["line_strength"]
            call_margin += check_resistance
            call_margin += 0.50 * nutted_risk["risk"]
            if round_idx == 3 and made_strength < 0.40 and not (blocker_profile and blocker_profile["eligible"]):
                call_margin += 0.04
            if round_idx == 3 and paired_board_profile is not None and paired_board_profile["fold_to_raise"]:
                call_margin += 0.05
            realized_rate = realized_postflop_equity(
                win_rate,
                made_strength,
                draw_strength,
                round_idx,
                spot_info["has_position"],
                spot_info,
                pair_profile,
            )
        if anti_lock_pressure:
            call_margin -= 0.07
        anti_lock_call_continue = anti_lock_can_continue(
            anti_lock_pressure,
            win_rate,
            pot_odds,
            round_idx,
            value_profile,
            draw_info,
            made_strength,
        )
        strong_made_continue = must_continue_vs_raise(
            value_profile,
            made_strength,
            pot_odds,
            nutted_risk,
            board_texture,
        )
        anti_lock_attack = None
        if anti_lock_pressure:
            anti_lock_attack = choose_anti_lock_pressure_action(
                state,
                my_chips,
                to_call,
                pot,
                round_idx,
                win_rate,
                opponent_model,
                remaining_hands,
                preflop_strength=preflop_strength,
                value_profile=value_profile,
                draw_info=draw_info,
                blocker_profile=blocker_profile,
                board_texture=board_texture,
            )
        fragile_river_raise_fold = (
            round_idx == 3
            and spot_info["facing_postflop_aggression"]
            and bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large")
            and paired_board_profile is not None
            and paired_board_profile["fold_to_raise"]
            and paired_board_profile["hand_class"] == 2
            and (value_profile is None or value_profile["tier"] != "nut")
        )
        fragile_pair_raise_fold = (
            round_idx > 0
            and spot_info["facing_postflop_aggression"]
            and marginal_pair
            and draw_strength < 0.14
            and bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large")
            and (value_profile is None or value_profile["tier"] not in ("strong", "nut"))
        )
        if anti_lock_attack is not None:
            return anti_lock_attack
        if fragile_river_raise_fold:
            if not anti_lock_call_continue:
                return -1
        if fragile_pair_raise_fold:
            if not anti_lock_call_continue:
                return -1
        if hard_repressure_fold or paired_board_stackoff["severe"]:
            if not anti_lock_call_continue and not strong_made_continue:
                return -1
        if realized_rate < pot_odds + call_margin:
            if not anti_lock_call_continue and not strong_made_continue:
                return -1
        if repeated_raise_trap and (value_profile is None or value_profile["tier"] != "nut"):
            return 0

        raise_fold_threshold = 0.56 - 0.30 * match_profile["bluff_delta"]
        blocker_raise_threshold = 0.55 - 0.32 * match_profile["bluff_delta"]
        draw_raise_threshold = clamp(raise_fold_threshold - draw_info["fold_threshold_delta"], 0.46, 0.68)
        draw_equity_slack = 0.05 if draw_info["type"] in ("combo_draw", "nut_flush_draw") else 0.03
        semi_bluff = (
            round_idx > 0
            and draw_info["semi_bluff"]
            and draw_strength >= 0.12
            and opponent_model["confidence"] >= 0.25
            and opponent_model["fold_to_raise"] > draw_raise_threshold
            and win_rate >= pot_odds - draw_equity_slack
        )
        blocker_raise = (
            round_idx == 1
            and spot_info["facing_postflop_aggression"]
            and opponent_model["confidence"] >= 0.25
            and opponent_model["fold_to_raise"] > blocker_raise_threshold
            and blocker_profile is not None
            and blocker_profile["eligible"]
            and made_strength < 0.18
            and draw_strength < 0.12
            and allow_low_frequency_blocker_bluff(req, my_cards, public_cards, blocker_profile, round_idx)
        )
        trap_nut_slowplay = (
            round_idx in (1, 2)
            and value_profile is not None
            and value_profile["tier"] == "nut"
            and board_texture is not None
            and not board_texture["dynamic"]
            and spot_info["facing_postflop_aggression"]
            and bet_size_bucket(spot_info["last_raise_pot_ratio"]) != "large"
            and pot < 1400
            and nutted_risk["risk"] <= 0.02
            and match_profile["chase"] <= 0.45
            and opponent_model["confidence"] >= 0.20
            and (
                opponent_model["postflop_aggr"] >= 0.38
                or opponent_model["aggression"] >= 0.34
                or opponent_model["fold_to_raise"] < 0.46
            )
        )
        flop_checkraise_exploit = (
            round_idx == 1
            and spot_info["facing_postflop_aggression"]
            and opponent_model["confidence"] >= 0.25
            and opponent_model["fold_to_raise"] > blocker_raise_threshold
            and (
                (value_profile and value_profile["tier"] in ("strong", "nut"))
                or (draw_info["semi_bluff"] and draw_strength >= 0.15)
                or blocker_raise
            )
        )

        if trap_nut_slowplay:
            return 0
        preflop_defensive_only = (
            round_idx == 0
            and to_call > 0
            and not preflop_3bet_candidate
        )
        if not preflop_defensive_only and (win_rate >= max(strong, pot_odds + 0.12) or semi_bluff or flop_checkraise_exploit):
            raise_amount = choose_raise(
                state["min_raise_action"],
                my_chips,
                state["my_round_bet"],
                to_call,
                pot,
                win_rate,
                round_idx,
                spot_info["preflop_spot"],
                preflop_strength,
                spot_info["has_position"],
                opponent_model,
                semi_bluff=semi_bluff or (flop_checkraise_exploit and draw_info["semi_bluff"] and draw_strength >= 0.15),
                value_profile=value_profile,
                value_plan=value_plan,
                board_texture=board_texture,
                draw_info=draw_info,
                blocker_bluff=blocker_raise,
                pressure_line=flop_checkraise_exploit,
                nutted_risk_score=nutted_risk["risk"],
                match_sizing_delta=match_profile["sizing_delta"],
            )
            if raise_amount is not None and raise_amount > to_call:
                return raise_amount
        return 0

    weak_pair_river = (
        round_idx == 3
        and pair_profile is not None
        and pair_profile["made_class"] == 1
        and pair_profile["pair_type"] in ("middle_pair", "bottom_pair", "underpair", "board_pair")
    )
    opp_double_barrel_then_river_check = (
        round_idx == 3
        and to_call == 0
        and spot_info.get("opp_postflop_bet_count", 0) >= 2
        and spot_info["last_opp_action_type"] == "check"
    )
    bad_river_bluff_candidate = (
        round_idx == 3
        and to_call == 0
        and made_strength >= 0.18
        and made_strength < 0.40
        and not (blocker_profile and blocker_profile["eligible"])
        and not (value_profile and value_profile["tier"] in ("strong", "nut"))
    )
    weak_bottom_pair_barrel = (
        round_idx >= 2
        and to_call == 0
        and pair_profile is not None
        and pair_profile["made_class"] == 1
        and pair_profile["pair_type"] in ("bottom_pair", "underpair", "board_pair")
        and made_strength < 0.40
        and draw_strength < 0.12
    )
    weak_pair_after_raise_barrel = (
        round_idx >= 2
        and to_call == 0
        and marginal_pair
        and draw_strength < 0.14
        and (value_profile is None or value_profile["tier"] not in ("strong", "nut"))
        and (
            spot_info.get("opp_previous_round_raise_count", 0) > 0
            or spot_info.get("opp_prior_postflop_raise_count", 0) > 0
        )
    )
    bad_river_value_bet = (
        round_idx == 3
        and to_call == 0
        and paired_board_profile is not None
        and paired_board_profile["board_paired"]
        and paired_board_profile["prefer_check"]
        and paired_board_profile["hand_class"] == 2
        and nutted_risk["risk"] >= 0.05
        and (value_profile is None or value_profile["tier"] != "nut")
    )
    bad_stackoff_overpair = (
        round_idx > 0
        and to_call == 0
        and paired_board_stackoff["active"]
        and pot > 3000
        and (value_profile is None or value_profile["tier"] != "nut")
    )
    big_pot_threshold = int(clamp(1500 - 350 * match_profile["protect"] + 250 * match_profile["chase"], 1100, 1800))
    big_pot = pot >= big_pot_threshold
    induce_nut_value = (
        round_idx > 0
        and to_call == 0
        and value_profile is not None
        and value_profile["tier"] == "nut"
        and board_texture is not None
        and not board_texture["dynamic"]
        and not big_pot
        and match_profile["chase"] <= 0.55
        and opponent_model["confidence"] >= 0.20
        and (
            opponent_model["postflop_aggr"] >= 0.38
            or opponent_model["aggression"] >= 0.34
            or opponent_model["fold_to_raise"] < 0.46
        )
    )
    anti_lock_attack = None
    if anti_lock_pressure:
        anti_lock_attack = choose_anti_lock_pressure_action(
            state,
            my_chips,
            to_call,
            pot,
            round_idx,
            win_rate,
            opponent_model,
            remaining_hands,
            preflop_strength=preflop_strength,
            value_profile=value_profile,
            draw_info=draw_info,
            blocker_profile=blocker_profile,
            board_texture=board_texture,
        )
        if anti_lock_attack is not None:
            return anti_lock_attack

    if opp_double_barrel_then_river_check and weak_pair_river:
        return 0
    if bad_river_bluff_candidate:
        return 0
    if weak_bottom_pair_barrel:
        return 0
    if weak_pair_after_raise_barrel:
        return 0
    if bad_river_value_bet:
        return 0
    if bad_stackoff_overpair:
        return 0
    if big_pot and round_idx == 3 and (value_profile is None or value_profile["tier"] not in ("strong", "nut")):
        if blocker_profile is None or not blocker_profile["eligible"]:
            return 0
    thin_static_showdown_control = (
        round_idx >= 2
        and value_profile is not None
        and value_profile["tier"] == "thin"
        and board_texture is not None
        and not board_texture["dynamic"]
        and draw_strength < 0.12
        and not anti_lock_pressure
    )
    if thin_static_showdown_control:
        return 0

    river_bluff_threshold = 0.62 - 0.28 * match_profile["bluff_delta"]
    probe_fold_threshold = 0.56 - 0.32 * match_profile["bluff_delta"]
    semi_bluff_threshold = 0.58 - 0.28 * match_profile["bluff_delta"]
    draw_bet_threshold = clamp(semi_bluff_threshold - draw_info["fold_threshold_delta"], 0.46, 0.70)
    check_probe_signal = (
        spot_info["last_opp_action_type"] == "check"
        and (
            spot_info.get("opp_postflop_check_count", 0) >= 2
            or (
                opponent_model["confidence"] >= 0.20
                and opponent_model.get("postflop_check_rate", 0.42) >= 0.52
            )
        )
    )
    river_blocker_bluff = (
        round_idx == 3
        and made_strength < 0.16
        and draw_strength < 0.08
        and opponent_model["confidence"] >= 0.35
        and opponent_model["fold_to_raise"] > river_bluff_threshold
        and blocker_profile is not None
        and blocker_profile["eligible"]
        and allow_low_frequency_blocker_bluff(req, my_cards, public_cards, blocker_profile, round_idx)
    )
    small_probe = (
        round_idx > 0
        and opponent_model["confidence"] >= 0.25
        and opponent_model["fold_to_raise"] > probe_fold_threshold
        and made_strength < 0.62
        and draw_strength < 0.16
        and board_texture is not None
        and board_texture["wetness"] <= 0.32
        and not (value_profile and value_profile["tier"] in ("strong", "nut"))
    )
    check_probe = (
        round_idx > 0
        and check_probe_signal
        and board_texture is not None
        and board_texture["wetness"] <= 0.55
        and made_strength < 0.58
        and draw_strength < 0.20
        and not (value_profile and value_profile["tier"] in ("strong", "nut"))
        and not (round_idx == 3 and made_strength >= 0.18 and not (blocker_profile and blocker_profile["eligible"]))
    )
    blocker_bluff = (
        river_blocker_bluff
    )
    semi_bluff = (
        round_idx > 0
        and draw_info["semi_bluff"]
        and draw_strength >= 0.12
        and opponent_model["confidence"] >= 0.25
        and opponent_model["fold_to_raise"] > draw_bet_threshold
    )
    if trace is not None:
        trace.update({
            "check_probe_signal": check_probe_signal,
            "small_probe": small_probe,
            "check_probe": check_probe,
            "semi_bluff": semi_bluff,
            "blocker_bluff": blocker_bluff,
            "river_blocker_bluff": river_blocker_bluff,
            "probe_fold_threshold": trace_float(probe_fold_threshold),
            "river_bluff_threshold": trace_float(river_bluff_threshold),
            "draw_bet_threshold": trace_float(draw_bet_threshold),
        })
    if win_rate >= medium or semi_bluff or blocker_bluff or small_probe or check_probe or made_strength >= 0.62 or (value_profile and value_profile["tier"] in ("strong", "nut")):
        raise_amount = choose_raise(
            state["min_raise_action"],
            my_chips,
            state["my_round_bet"],
            to_call,
            pot,
            win_rate,
            round_idx,
            spot_info["preflop_spot"],
            preflop_strength,
            spot_info["has_position"],
            opponent_model,
            semi_bluff=semi_bluff and win_rate < medium,
            value_profile=value_profile,
            value_plan=value_plan,
            board_texture=board_texture,
            draw_info=draw_info,
            blocker_bluff=blocker_bluff and win_rate < medium and not semi_bluff,
            probe_mode=check_probe or small_probe or (value_profile and value_profile["tier"] == "thin" and board_texture and not board_texture["dynamic"]),
            induce_mode=induce_nut_value or value_plan.get("induce", False),
            nutted_risk_score=nutted_risk["risk"],
            match_sizing_delta=match_profile["sizing_delta"],
        )
        if raise_amount is not None:
            return raise_amount
    return 0


def sanitize_action(action, state, my_chips):
    if state["opponent_allin"]:
        return action if action in (-1, -2) else -1

    if state["to_call"] >= my_chips:
        return -2 if action == -2 else -1

    if action > 0:
        min_raise = state.get("min_raise_action", state["round_raise"])
        if action >= my_chips:
            return -2
        if action < min_raise or action <= state["to_call"]:
            return 0

    if action == 0 and state["to_call"] > 0:
        return 0

    return action


def final_legal_command(adapter, command, state):
    command = (command or "").strip().lower()
    if not command or command.startswith("bet"):
        return adapter.passive_continue_command(state)

    command = adapter.sanitize_command(command, state)

    if adapter.opponent_allin or state["opponent_allin"]:
        return "fold" if command == "fold" else "call"

    if command.startswith("bet"):
        return adapter.passive_continue_command(state)

    if command.startswith("raise "):
        try:
            target = int(command.split()[1])
        except (IndexError, ValueError):
            return adapter.passive_continue_command(state)

        target = max(target, adapter.min_raise_total())
        allin_total = adapter.round_bets[adapter.my_id] + adapter.chips[adapter.my_id]
        if target >= allin_total:
            return "allin"
        if target <= adapter.current_max_bet():
            return adapter.passive_continue_command(state)
        return "raise {}".format(target)

    if command in ("call", "check", "fold", "allin"):
        return command
    return adapter.passive_continue_command(state)


GUOSAI_CARD_RE = re.compile(r"<\s*(\d+)\s*,\s*(\d+)\s*>")
GUOSAI_ROUND_INDEX = {
    "preflop": 0,
    "flop": 1,
    "turn": 2,
    "river": 3,
}


def guosai_card_to_internal(suit, rank_index):
    return int(rank_index) * 4 + int(suit)


def parse_guosai_cards(message):
    cards = []
    seen = set()
    for suit_text, rank_text in GUOSAI_CARD_RE.findall(message):
        suit = int(suit_text)
        rank = int(rank_text)
        if suit < 0 or suit > 3 or rank < 0 or rank > 12:
            return None
        card = guosai_card_to_internal(suit, rank)
        if card in seen:
            return None
        seen.add(card)
        cards.append(card)
    return cards


def decode_guosai_chunk(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gbk", errors="ignore")


def guosai_debug_enabled():
    return os.environ.get("GUOSAI_DEBUG", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def format_guosai_log_text(value):
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    if len(text) > GUOSAI_LOG_MAX_CHARS:
        return text[:GUOSAI_LOG_MAX_CHARS] + "...<truncated>"
    return text


def guosai_log(event, detail=""):
    if not guosai_debug_enabled():
        return
    timestamp = time.strftime("%H:%M:%S")
    if detail:
        print(f"[{timestamp}][guosai][{event}] {format_guosai_log_text(detail)}", flush=True)
    else:
        print(f"[{timestamp}][guosai][{event}]", flush=True)


def normalize_guosai_message(message):
    return message.replace("\x00", "").strip()


GUOSAI_MESSAGE_START_RE = re.compile(
    r"(?i)name|preflop\||flop\||turn\||river\||oppo_hands\||"
    r"earnchips\s|raise\s|bet\s|allin|fold|call|check"
)
GUOSAI_MESSAGE_START_TOKENS = (
    "name",
    "preflop|",
    "flop|",
    "turn|",
    "river|",
    "oppo_hands|",
    "earnchips ",
    "raise ",
    "bet ",
    "allin",
    "fold",
    "call",
    "check",
)


def trailing_message_start_prefix(text):
    lower = text.lower()
    for length in range(min(len(lower), max(len(token) for token in GUOSAI_MESSAGE_START_TOKENS)), 0, -1):
        suffix = lower[-length:]
        if any(token.startswith(suffix) for token in GUOSAI_MESSAGE_START_TOKENS):
            return text[-length:]
    return ""


def is_complete_guosai_message(message):
    lower = message.lower()
    if lower in ("name", "allin", "fold", "call", "check"):
        return True
    if re.fullmatch(r"(?:raise|bet)\s+\d+", lower):
        return True
    if re.fullmatch(r"earnchips\s+[+-]?\d+", lower):
        return True

    card_count = len(GUOSAI_CARD_RE.findall(message))
    if lower.startswith("preflop|"):
        parts = message.split("|", 2)
        return (
            len(parts) == 3
            and parts[1].strip().upper() in ("SMALLBLIND", "BIGBLIND")
            and card_count == 2
        )
    if lower.startswith("flop|"):
        return card_count == 3
    if lower.startswith(("turn|", "river|")):
        return card_count == 1
    if lower.startswith("oppo_hands|"):
        return card_count == 2
    return False


def split_guosai_messages(data):
    text = data.replace("\x00", "").replace("\r", "").replace("\n", "").strip()
    if not text:
        return [], ""

    starts = [match.start() for match in GUOSAI_MESSAGE_START_RE.finditer(text)]
    if not starts:
        return [], trailing_message_start_prefix(text)

    messages = []
    remainder = ""
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        candidate = normalize_guosai_message(text[start:end])
        if not candidate:
            continue
        if is_complete_guosai_message(candidate):
            messages.append(candidate)
        elif index + 1 == len(starts):
            remainder += candidate

    return messages, remainder


class GuosaiProtocolAdapter:
    def __init__(self):
        self.my_id = 0
        self.opponent_id = 1
        self.hand = -1
        self.total_win = 0
        self.role = None
        self.dealer_id = 0
        self.my_cards = []
        self.public_cards = []
        self.history = []
        self.requests = []
        self.round_idx = 0
        self.chips = [INITIAL_CHIPS, INITIAL_CHIPS]
        self.round_bets = [0, 0]
        self.last_raise_total = BIG_BLIND
        self.raise_count = 0
        self.opponent_allin = False
        self.last_decision_trace = {}

    def start_hand(self, role, cards):
        self.hand += 1
        self.role = role.upper()
        self.my_cards = cards
        self.public_cards = []
        self.history = []
        self.round_idx = 0
        self.chips = [INITIAL_CHIPS, INITIAL_CHIPS]
        self.opponent_allin = False
        self.raise_count = 0
        self.last_raise_total = BIG_BLIND

        if self.role == "SMALLBLIND":
            self.dealer_id = 1
            self.round_bets = [SMALL_BLIND, BIG_BLIND]
            self.chips[self.my_id] -= SMALL_BLIND
            self.chips[self.opponent_id] -= BIG_BLIND
        else:
            self.dealer_id = 0
            self.round_bets = [BIG_BLIND, SMALL_BLIND]
            self.chips[self.my_id] -= BIG_BLIND
            self.chips[self.opponent_id] -= SMALL_BLIND

    def start_street(self, round_name, cards):
        self.round_idx = GUOSAI_ROUND_INDEX[round_name]
        if round_name == "flop":
            self.public_cards = cards[:]
        else:
            self.public_cards.extend(cards)
        self.round_bets = [0, 0]
        self.last_raise_total = 0
        self.raise_count = 0

    def make_request(self):
        return {
            "num_players": N_PLAYERS,
            "dealer_id": self.dealer_id,
            "my_id": self.my_id,
            "my_chips": max(0, int(self.chips[self.my_id])),
            "my_cards": self.my_cards[:],
            "public_cards": self.public_cards[:],
            "history": [dict(record) for record in self.history],
            "hand": max(0, self.hand),
            "max_hand": TOTAL_HANDS,
            "total_win_chips": [int(self.total_win), -int(self.total_win)],
            "total_win_games": [0, 0],
        }

    def current_max_bet(self):
        return max(self.round_bets)

    def record_action(self, pid, action_type, amount=None):
        action = 0
        if action_type == "raise":
            target_total = max(0, int(amount))
            previous_total = self.round_bets[pid]
            delta = max(0, target_total - previous_total)
            delta = min(delta, self.chips[pid])
            self.chips[pid] -= delta
            self.round_bets[pid] += delta
            self.last_raise_total = max(self.last_raise_total, self.round_bets[pid])
            self.raise_count += 1
            action = delta
        elif action_type == "call":
            need = max(0, self.current_max_bet() - self.round_bets[pid])
            delta = min(need, self.chips[pid])
            self.chips[pid] -= delta
            self.round_bets[pid] += delta
            action = 0
        elif action_type == "check":
            action = 0
        elif action_type == "fold":
            action = -1
        elif action_type == "allin":
            delta = self.chips[pid]
            self.chips[pid] = 0
            self.round_bets[pid] += delta
            action = -2
            if pid == self.opponent_id:
                self.opponent_allin = True
        else:
            return

        record = {
            "round": self.round_idx,
            "player_id": pid,
            "action": action,
            "action_type": action_type,
        }
        if action_type == "raise":
            record["raise_total"] = target_total
        self.history.append(record)

    def my_acted_this_street(self):
        return any(
            record["player_id"] == self.my_id and record["round"] == self.round_idx
            for record in self.history
        )

    def current_street_actions(self):
        return [
            record for record in self.history
            if record["round"] == self.round_idx
        ]

    def opponent_to_call(self):
        return max(0, self.current_max_bet() - self.round_bets[self.opponent_id])

    def should_act_after_opponent(self, action_type):
        if action_type in ("fold",):
            return False
        if action_type in ("raise", "allin"):
            return True
        if self.round_idx == 0 and action_type == "call":
            return self.role == "BIGBLIND" and not self.my_acted_this_street()
        if action_type == "check":
            return not self.my_acted_this_street()
        return False

    def should_act_after_street_message(self):
        if self.round_idx == 0:
            return self.role == "SMALLBLIND"
        return self.role == "BIGBLIND"

    def min_raise_total(self):
        if self.round_idx == 0:
            if self.raise_count == 0:
                return 2 * BIG_BLIND
            return 2 * self.last_raise_total
        if self.raise_count == 0:
            return BIG_BLIND
        return 2 * self.last_raise_total

    def passive_continue_command(self, state):
        if state["to_call"] > 0:
            return "call"
        if self.round_idx > 0 and self.current_street_actions():
            return "call"
        return "check"

    def command_from_internal_action(self, action, state):
        if self.opponent_allin or state["opponent_allin"]:
            return "fold" if action == -1 else "call"

        if action == -1:
            return "fold"
        if action == -2:
            return "allin"
        if action <= 0:
            return self.passive_continue_command(state)

        target_total = int(state["my_round_bet"] + action)
        target_total = max(target_total, self.min_raise_total())
        if target_total <= self.current_max_bet():
            return self.passive_continue_command(state)
        allin_total = self.round_bets[self.my_id] + self.chips[self.my_id]
        if target_total >= allin_total:
            return "allin"
        return "raise {}".format(target_total)

    def sanitize_command(self, command, state):
        command = command.strip().lower()
        if self.opponent_allin or state["opponent_allin"]:
            return "fold" if command == "fold" else "call"

        if command in ("fold", "check", "call", "allin"):
            if command == "check" and state["to_call"] > 0:
                return "call" if state["to_call"] < self.chips[self.my_id] else "allin"
            if command == "call" and state["to_call"] <= 0:
                return self.passive_continue_command(state)
            if command == "check" and state["to_call"] <= 0:
                return self.passive_continue_command(state)
            return command

        if not command.startswith("raise "):
            return self.passive_continue_command(state)

        try:
            target_total = int(command.split()[1])
        except (IndexError, ValueError):
            return self.passive_continue_command(state)

        target_total = max(target_total, self.min_raise_total())
        if target_total <= self.current_max_bet():
            return self.passive_continue_command(state)
        allin_total = self.round_bets[self.my_id] + self.chips[self.my_id]
        if target_total >= allin_total:
            return "allin"
        return "raise {}".format(target_total)

    def decide_command(self, decision_deadline=None):
        trace = {"guosai": True} if hl_trace_enabled() else None
        req = self.make_request()
        requests = self.requests + [req]
        state = reconstruct_state(req)
        fallback_command = self.passive_continue_command(state)
        try:
            raw_action = get_action(req, requests, trace, decision_deadline=decision_deadline)
            if state["opponent_allin"]:
                action = -1 if raw_action == -1 else -2
            else:
                action = sanitize_action(raw_action, state, req["my_chips"])
            command = self.command_from_internal_action(action, state)
        except Exception as exc:
            guosai_log("decision-error", f"{type(exc).__name__}: {exc}")
            raw_action = None
            action = None
            command = fallback_command

        command = final_legal_command(self, command or fallback_command, state)
        if trace is not None:
            trace["raw_action"] = raw_action
            trace["sanitized_action"] = action
            trace["final_command"] = command
        self.last_decision_trace = trace or {}
        self.requests.append(req)
        self.record_own_command(command)
        return command

    def timed_decide(self):
        start = time.monotonic()
        deadline = start + guosai_decision_time_limit()
        command = self.decide_command(decision_deadline=deadline)
        elapsed = time.monotonic() - start
        trace = self.last_decision_trace or {}
        detail = "elapsed={:.3f}s command={}".format(elapsed, command)
        if trace:
            extra = []
            for key in (
                "win_rate",
                "pot_odds",
                "equity_mode",
                "equity_samples",
                "equity_standard_error",
                "equity_refined",
                "equity_refine_reason",
                "equity_gap",
                "equity_initial_samples",
                "decision_branch",
            ):
                if trace.get(key) is not None:
                    extra.append("{}={}".format(key, trace.get(key)))
            if extra:
                detail += " " + " ".join(extra)
        guosai_log("decision", detail)
        return command

    def record_own_command(self, command):
        if command.startswith("raise "):
            self.record_action(self.my_id, "raise", int(command.split()[1]))
        elif command == "allin":
            self.record_action(self.my_id, "allin")
        elif command == "call":
            self.record_action(self.my_id, "call")
        elif command == "check":
            self.record_action(self.my_id, "check")
        elif command == "fold":
            self.record_action(self.my_id, "fold")

    def handle_opponent_action(self, message):
        if self.hand < 0 or len(self.my_cards) != 2:
            guosai_log("ignore-opponent-action", f"no active hand: {message}")
            return False
        lower = message.lower()
        if lower.startswith("raise ") or lower.startswith("bet "):
            try:
                target_total = int(lower.split()[1])
            except (IndexError, ValueError):
                return False
            if target_total >= self.round_bets[self.opponent_id] + self.chips[self.opponent_id]:
                self.record_action(self.opponent_id, "allin")
                return self.should_act_after_opponent("allin")
            if target_total < self.min_raise_total() or target_total <= self.current_max_bet():
                guosai_log("ignore-opponent-action", f"illegal raise target: {message}")
                return False
            self.record_action(self.opponent_id, "raise", target_total)
            return self.should_act_after_opponent("raise")
        if lower == "allin":
            if self.opponent_allin or self.chips[self.opponent_id] <= 0:
                guosai_log("ignore-opponent-action", f"duplicate allin: {message}")
                return False
            self.record_action(self.opponent_id, "allin")
            return self.should_act_after_opponent("allin")
        if lower == "call":
            if self.opponent_to_call() <= 0 and not (
                self.round_idx > 0 and self.current_street_actions()
            ):
                guosai_log("ignore-opponent-action", f"illegal call: {message}")
                return False
            self.record_action(self.opponent_id, "call")
            return self.should_act_after_opponent("call")
        if lower == "check":
            if self.opponent_to_call() > 0 or (
                self.round_idx > 0 and self.current_street_actions()
            ):
                guosai_log("ignore-opponent-action", f"illegal check: {message}")
                return False
            self.record_action(self.opponent_id, "check")
            return self.should_act_after_opponent("check")
        if lower == "fold":
            self.record_action(self.opponent_id, "fold")
            return False
        return False

    def handle_message(self, message):
        message = normalize_guosai_message(message)
        if not message:
            return None

        lower = message.lower()
        if lower.startswith("earnchips"):
            parts = message.split()
            if len(parts) >= 2:
                try:
                    self.total_win += int(parts[1])
                except ValueError:
                    pass
            return None
        if lower.startswith("oppo_hands"):
            return None

        if lower.startswith("preflop"):
            parts = message.split("|")
            role = parts[1].strip() if len(parts) > 1 else "SMALLBLIND"
            if role.upper() not in ("SMALLBLIND", "BIGBLIND"):
                guosai_log("ignore-message", f"invalid role: {message}")
                return None
            cards = parse_guosai_cards(message)
            if cards is None or len(cards) != 2:
                guosai_log("ignore-message", f"invalid preflop cards: {message}")
                return None
            self.start_hand(role, cards)
            return self.timed_decide() if self.should_act_after_street_message() else None

        for round_name in ("flop", "turn", "river"):
            if lower.startswith(round_name):
                if self.hand < 0 or len(self.my_cards) != 2:
                    guosai_log("ignore-message", f"street before hand: {message}")
                    return None
                cards = parse_guosai_cards(message)
                expected_cards = 3 if round_name == "flop" else 1
                known_cards = set(self.my_cards + self.public_cards)
                if (
                    cards is None
                    or len(cards) != expected_cards
                    or any(card in known_cards for card in cards)
                ):
                    guosai_log("ignore-message", f"invalid {round_name} cards: {message}")
                    return None
                self.start_street(round_name, cards)
                return self.timed_decide() if self.should_act_after_street_message() else None

        if self.handle_opponent_action(message):
            return self.timed_decide()
        return None


def send_guosai(sock, message):
    payload = message.encode("utf-8")
    guosai_log("send", f"{message} ({len(payload)} bytes)")
    try:
        sock.sendall(payload)
    except OSError as exc:
        guosai_log("send-error", f"{type(exc).__name__}: {exc}")
        raise
    guosai_log("send-ok", f"{message} ({len(payload)} bytes)")
    # The legacy platform treats each recv() result as one complete command.
    time.sleep(GUOSAI_SEND_GAP_SECONDS)


def guosai_socket_main():
    seed_hl_random()
    host = os.environ.get("GUOSAI_HOST", "127.0.0.1")
    port = int(os.environ.get("GUOSAI_PORT", "10001"))
    name = os.environ.get("GUOSAI_NAME", "HDU_poker1")

    args = [arg for arg in sys.argv[1:] if arg != "--guosai"]
    if args:
        host = args[0]
    if len(args) >= 2:
        port = int(args[1])
    if len(args) >= 3:
        name = args[2]

    adapter = GuosaiProtocolAdapter()
    guosai_log("connect", f"{host}:{port} name={name}")
    try:
        with socket.create_connection((host, port), timeout=20) as sock:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(None)
            guosai_log("connected", f"local={sock.getsockname()} remote={sock.getpeername()}")
            buffer = ""
            while True:
                guosai_log("recv-wait")
                data = sock.recv(4096)
                if not data:
                    guosai_log("recv-closed")
                    break
                decoded = decode_guosai_chunk(data)
                guosai_log("recv-raw", f"{decoded} ({len(data)} bytes)")
                buffer += decoded
                messages, buffer = split_guosai_messages(buffer)
                if messages:
                    guosai_log("recv-split", f"{len(messages)} message(s)")
                if buffer:
                    guosai_log("recv-buffer", buffer)

                for message in messages:
                    message = normalize_guosai_message(message)
                    if not message:
                        continue
                    guosai_log("recv-msg", message)
                    if message.lower() == "name":
                        send_guosai(sock, name)
                        continue
                    response = adapter.handle_message(message)
                    if response:
                        send_guosai(sock, response)
                    else:
                        guosai_log("no-send", f"handled {message}")
    except OSError as exc:
        guosai_log("socket-error", f"{type(exc).__name__}: {exc}")
        raise


def botzone_json_main():
    hl_seed = seed_hl_random()
    trace = {"hl_seed": hl_seed} if hl_trace_enabled() else None
    payload = json.loads(input())
    requests = payload["requests"]
    req = dict(requests[-1])
    if "remaining_hands" not in req:
        req["remaining_hands"] = infer_remaining_hands_from_requests(requests)
    action = get_action(req, requests, trace)
    raw_action = action
    state = reconstruct_state(req)
    action = sanitize_action(action, state, req["my_chips"])
    output = {"response": int(action)}
    if trace is not None:
        trace["raw_action"] = int(raw_action)
        trace["final_action"] = int(action)
        trace["sanitized"] = int(raw_action) != int(action)
        trace["decision_branch"] = infer_trace_branch(trace, int(action))
        output["hl_trace"] = trace
    print(json.dumps(output, separators=(',', ':')))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        botzone_json_main()
    else:
        guosai_socket_main()


if __name__ == "__main__":
    main()
