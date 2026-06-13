from constants import BIG_BLIND, N_PLAYERS
from card_utils import clamp, next_player
from state import collect_latest_requests_by_hand
from tournament import opponent_can_lock_win


# Default priors (hardcoded for archetype classifier independence)
_PRIOR_VPIP = 0.58
_PRIOR_PFR = 0.28
_PRIOR_POSTFLOP_AGGR = 0.36
_PRIOR_FOLD_TO_RAISE = 0.44


def classify_opponent_archetype(opponent_model):
    """Classify opponent into behavioral archetype for structural adjustments.

    Returns one of: 'calling_station', 'nit', 'lag', 'tag', 'unknown'.
    Requires confidence >= 0.12 to avoid noisy misclassification.

    Mutation: confidence gate lowered from 0.15 to 0.12 (~20% reduction) so
    archetype-aware adjustments activate earlier in matches. This synergizes
    with the board_range_filter crossover — better opponent range estimation
    + earlier archetype activation = faster exploitative adjustments.

    Archetype definitions:
    - calling_station: high VPIP, low fold-to-raise, passive postflop
      → don't bluff barrels, don't bluff 3bet preflop
    - nit: low VPIP, high fold-to-raise
      → wider bluff range (they fold), smaller value sizing
    - lag: high VPIP with high postflop aggression
      → respect their raises, tighten call thresholds
    - tag: balanced tight-aggressive
      → play standard balanced strategy
    """
    confidence = opponent_model.get('confidence', 0.0)
    if confidence < 0.12:
        return 'unknown'

    vpip = opponent_model.get('vpip', _PRIOR_VPIP)
    pfr = opponent_model.get('pfr', _PRIOR_PFR)
    postflop_aggr = opponent_model.get('postflop_aggr', _PRIOR_POSTFLOP_AGGR)
    fold_to_raise = opponent_model.get('fold_to_raise', _PRIOR_FOLD_TO_RAISE)

    if vpip > 0.65 and fold_to_raise < 0.35 and postflop_aggr < 0.28:
        return 'calling_station'
    if vpip < 0.40 and fold_to_raise > 0.55:
        return 'nit'
    if vpip > 0.60 and postflop_aggr > 0.45:
        return 'lag'
    if vpip > 0.45 and pfr > 0.25 and postflop_aggr > 0.30:
        return 'tag'

    return 'unknown'


def exploit_dispatch(opponent_model, round_idx):
    """Offensive adjustments based on per-street fold tendencies.

    Returns: {'barrel_freq_boost': float, 'value_sizing_boost': float, 'bluff_suppress': bool}
    - barrel_freq_boost: relaxes bluff barrel fold_to_raise threshold (positive=barrel more)
    - value_sizing_boost: increases value bet sizing ratio (positive=bigger value bets)
    - bluff_suppress: True when opponent rarely folds this street (don't waste bluffs)
    """
    confidence = opponent_model.get('confidence', 0.0)
    result = {'barrel_freq_boost': 0.0, 'value_sizing_boost': 0.0, 'bluff_suppress': False}
    if confidence < 0.12:
        return result

    street_key = {1: 'fold_to_bet_flop', 2: 'fold_to_bet_turn', 3: 'fold_to_bet_river'}.get(round_idx)
    if street_key is None:
        return result
    street_fold = opponent_model.get(street_key, 0.40)

    # High fold-to-bet on this street: boost bluff barrels, smaller value sizing
    if street_fold > 0.50:
        result['barrel_freq_boost'] = confidence * (street_fold - 0.50) * 0.60
    # Low fold-to-bet (calls wide): boost value sizing, suppress bluffs
    elif street_fold < 0.30:
        result['value_sizing_boost'] = confidence * (0.30 - street_fold) * 0.80
        result['bluff_suppress'] = True
    return result


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
    fold_to_bet_flop_opp = 0; fold_to_bet_flop = 0
    fold_to_bet_turn_opp = 0; fold_to_bet_turn = 0
    fold_to_bet_river_opp = 0; fold_to_bet_river = 0
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
                if round_idx == 1:
                    fold_to_bet_flop_opp += 1
                    if action_type == "fold":
                        fold_to_bet_flop += 1
                elif round_idx == 2:
                    fold_to_bet_turn_opp += 1
                    if action_type == "fold":
                        fold_to_bet_turn += 1
                elif round_idx == 3:
                    fold_to_bet_river_opp += 1
                    if action_type == "fold":
                        fold_to_bet_river += 1
                pending_my_pressure = False

        if opp_bet_flop:
            barrel_hands += 1
            if opp_bet_turn:
                barrel_continue += 1

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
        "fold_to_bet_flop": smooth_rate(fold_to_bet_flop, fold_to_bet_flop_opp, 0.44, 3.0),
        "fold_to_bet_turn": smooth_rate(fold_to_bet_turn, fold_to_bet_turn_opp, 0.40, 3.0),
        "fold_to_bet_river": smooth_rate(fold_to_bet_river, fold_to_bet_river_opp, 0.36, 3.0),
        "aggression": smooth_rate(aggressive_actions, total_actions, 0.30, 6.0),
        "avg_raise_bb": avg_raise_bb,
        "flop_aggr": smooth_rate(flop_bets, flop_acts, 0.36, 5.0),
        "turn_aggr": smooth_rate(turn_bets, turn_acts, 0.32, 5.0),
        "river_aggr": smooth_rate(river_bets, river_acts, 0.28, 5.0),
        "avg_flop_raise_bb": sum(flop_raise_bb)/len(flop_raise_bb) if flop_raise_bb else 3.0,
        "avg_turn_raise_bb": sum(turn_raise_bb)/len(turn_raise_bb) if turn_raise_bb else 4.5,
        "avg_river_raise_bb": sum(river_raise_bb)/len(river_raise_bb) if river_raise_bb else 5.5,
        "barrel_freq": smooth_rate(barrel_continue, barrel_hands, 0.45, 4.0),
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
