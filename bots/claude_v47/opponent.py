from constants import (
    BIG_BLIND, N_PLAYERS,
    PRIOR_VPIP, PRIOR_PFR, PRIOR_ALLIN_RATE, PRIOR_POSTFLOP_AGGR,
    PRIOR_POSTFLOP_CHECK, PRIOR_FOLD_TO_RAISE, PRIOR_AGGRESSION,
    PRIOR_FLOP_AGGR, PRIOR_TURN_AGGR, PRIOR_RIVER_AGGR, PRIOR_BARREL_FREQ,
    PRIOR_VPID_WEIGHT, PRIOR_PFR_WEIGHT, PRIOR_ALLIN_WEIGHT,
    PRIOR_POSTFLOP_AGGR_WEIGHT, PRIOR_POSTFLOP_CHECK_WEIGHT, PRIOR_FTR_WEIGHT, PRIOR_AGGRESSION_WEIGHT,
    PRIOR_FLOP_AGGR_WEIGHT, PRIOR_TURN_AGGR_WEIGHT, PRIOR_RIVER_AGGR_WEIGHT, PRIOR_BARREL_WEIGHT,
    DEFAULT_AVG_RAISE_BB, DEFAULT_FLOP_RAISE_BB, DEFAULT_TURN_RAISE_BB, DEFAULT_RIVER_RAISE_BB,
    CONFIDENCE_OFFSET, CONFIDENCE_SCALE,
)
from card_utils import clamp, next_player
from state import collect_latest_requests_by_hand
from tournament import opponent_can_lock_win


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
    flop_bets = 0; turn_bets = 0; river_bets = 0
    flop_acts = 0; turn_acts = 0; river_acts = 0
    flop_raise_bb = []; turn_raise_bb = []; river_raise_bb = []
    barrel_hands = 0; barrel_continue = 0
    opp_bet_flop = False; opp_bet_turn = False

    for req in hand_requests:
        if opponent_can_lock_win(req, my_id):
            continue

        opp_bet_flop = False
        opp_bet_turn = False

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
                if round_idx == 1:
                    flop_acts += 1
                    if action_type in ('raise', 'allin'):
                        flop_bets += 1
                        opp_bet_flop = True
                    if action_type == 'raise':
                        flop_raise_bb.append(action / BIG_BLIND)
                elif round_idx == 2:
                    turn_acts += 1
                    if action_type in ('raise', 'allin'):
                        turn_bets += 1
                        opp_bet_turn = True
                    if action_type == 'raise':
                        turn_raise_bb.append(action / BIG_BLIND)
                elif round_idx == 3:
                    river_acts += 1
                    if action_type in ('raise', 'allin'):
                        river_bets += 1
                    if action_type == 'raise':
                        river_raise_bb.append(action / BIG_BLIND)

            if action_type == "raise":
                raise_sizes.append(action / BIG_BLIND)

            if pending_my_pressure:
                fold_to_raise_opportunities += 1
                if action_type == "fold":
                    fold_to_raise += 1
                pending_my_pressure = False

        if opp_bet_flop:
            barrel_hands += 1
            if opp_bet_turn:
                barrel_continue += 1

    confidence = clamp((total_actions - CONFIDENCE_OFFSET) / CONFIDENCE_SCALE, 0.0, 1.0)
    avg_raise_bb = sum(raise_sizes) / len(raise_sizes) if raise_sizes else DEFAULT_AVG_RAISE_BB

    return {
        "confidence": confidence,
        "vpip": smooth_rate(voluntary_preflop, preflop_opportunities, PRIOR_VPIP, PRIOR_VPID_WEIGHT),
        "pfr": smooth_rate(preflop_raise, preflop_opportunities, PRIOR_PFR, PRIOR_PFR_WEIGHT),
        "allin_rate": smooth_rate(allin_actions, total_actions, PRIOR_ALLIN_RATE, PRIOR_ALLIN_WEIGHT),
        "postflop_aggr": smooth_rate(postflop_aggressive, postflop_actions, PRIOR_POSTFLOP_AGGR, PRIOR_POSTFLOP_AGGR_WEIGHT),
        "postflop_check_rate": smooth_rate(postflop_checks, postflop_actions, PRIOR_POSTFLOP_CHECK, PRIOR_POSTFLOP_CHECK_WEIGHT),
        "fold_to_raise": smooth_rate(fold_to_raise, fold_to_raise_opportunities, PRIOR_FOLD_TO_RAISE, PRIOR_FTR_WEIGHT),
        "aggression": smooth_rate(aggressive_actions, total_actions, PRIOR_AGGRESSION, PRIOR_AGGRESSION_WEIGHT),
        "avg_raise_bb": avg_raise_bb,
        "flop_aggr": smooth_rate(flop_bets, flop_acts, PRIOR_FLOP_AGGR, PRIOR_FLOP_AGGR_WEIGHT),
        "turn_aggr": smooth_rate(turn_bets, turn_acts, PRIOR_TURN_AGGR, PRIOR_TURN_AGGR_WEIGHT),
        "river_aggr": smooth_rate(river_bets, river_acts, PRIOR_RIVER_AGGR, PRIOR_RIVER_AGGR_WEIGHT),
        "avg_flop_raise_bb": sum(flop_raise_bb)/len(flop_raise_bb) if flop_raise_bb else DEFAULT_FLOP_RAISE_BB,
        "avg_turn_raise_bb": sum(turn_raise_bb)/len(turn_raise_bb) if turn_raise_bb else DEFAULT_TURN_RAISE_BB,
        "avg_river_raise_bb": sum(river_raise_bb)/len(river_raise_bb) if river_raise_bb else DEFAULT_RIVER_RAISE_BB,
        "barrel_freq": smooth_rate(barrel_continue, barrel_hands, PRIOR_BARREL_FREQ, PRIOR_BARREL_WEIGHT),
    }


def build_action_sequence_profile(req, state, my_id):
    """Analyze opponent's betting pattern across streets in the current hand."""
    opponent_id = 1 - my_id
    history = req.get('history', [])
    round_idx = state['round']

    profile = {
        'bet_street_count': 0,      # How many streets opponent bet/raised
        'total_street_count': 0,     # Total postflop streets seen
        'is_triple_barrel': False,   # Bet all 3 postflop streets
        'is_double_barrel': False,   # Bet 2+ consecutive streets
        'river_bet_after_check': False,  # Checked earlier, bet river
        'aggression_intensity': 0.0, # 0.0-1.0 score of aggression
    }

    if round_idx < 1:
        return profile

    # Track which streets opponent bet/raised
    street_bet = {1: False, 2: False, 3: False}
    street_had_action = {1: False, 2: False, 3: False}
    last_opp_round = -1
    consecutive_bets = 0

    for record in history:
        if record['player_id'] != opponent_id or record['round'] == 0:
            continue
        r = record['round']
        if r > round_idx:
            continue
        street_had_action[r] = True
        if record['action_type'] in ('raise', 'allin'):
            street_bet[r] = True
            if last_opp_round == r - 1 or (last_opp_round >= 1 and consecutive_bets > 0):
                consecutive_bets += 1
            else:
                consecutive_bets = 1
            last_opp_round = r
        elif record['action_type'] == 'check':
            consecutive_bets = 0
            last_opp_round = r

    postflop_streets_seen = sum(1 for r in range(1, round_idx + 1) if street_had_action.get(r, False))
    postflop_bets = sum(1 for r in range(1, round_idx + 1) if street_bet.get(r, False))

    profile['bet_street_count'] = postflop_bets
    profile['total_street_count'] = postflop_streets_seen
    profile['is_triple_barrel'] = postflop_bets >= 3
    profile['is_double_barrel'] = postflop_bets >= 2
    profile['river_bet_after_check'] = street_bet.get(3, False) and not street_bet.get(1, False) and not street_bet.get(2, False)
    profile['aggression_intensity'] = postflop_bets / max(1, postflop_streets_seen)

    return profile


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
                info["last_raise_bb"] = last["action"] / BIG_BLIND
                info["last_raise_pot_ratio"] = last["action"] / max(1, state["pot"])
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
