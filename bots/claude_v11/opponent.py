from constants import BIG_BLIND, N_PLAYERS
from card_utils import clamp, next_player
from state import collect_latest_requests_by_hand
from tournament import opponent_can_lock_win


def smooth_rate(successes, total, prior_mean, prior_weight):
    return (successes + prior_mean * prior_weight) / (total + prior_weight)


def _classify_sizing_tendency(sizes_bb):
    """Classify opponent's per-street sizing pattern."""
    if len(sizes_bb) < 3:
        return {"type": "unknown", "diversity": 0.0, "large_ratio": 0.0, "medium_ratio": 0.0, "small_ratio": 0.0}
    small = sum(1 for s in sizes_bb if s < 4.0)
    medium = sum(1 for s in sizes_bb if 4.0 <= s < 8.0)
    large = sum(1 for s in sizes_bb if s >= 8.0)
    n = len(sizes_bb)
    large_r = large / n
    medium_r = medium / n
    small_r = small / n
    diversity = 1.0 - max(small_r, medium_r, large_r)
    if diversity >= 0.30 and large_r >= 0.20 and small_r >= 0.15:
        t = "polarized"
    elif medium_r >= 0.50:
        t = "merged"
    elif large_r >= 0.50:
        t = "heavy"
    else:
        t = "mixed"
    return {"type": t, "diversity": diversity, "large_ratio": large_r, "medium_ratio": medium_r, "small_ratio": small_r}


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
    # Per-street raise sizing tracking for tendency classification
    flop_raise_bb = []
    turn_raise_bb = []
    river_raise_bb = []
    # Per-street tracking: separate bet/raise counts for flop, turn, river
    street_bet_opportunities = {1: 0, 2: 0, 3: 0}
    street_bets = {1: 0, 2: 0, 3: 0}
    # Per-street fold-to-raise tracking
    street_ftr_opportunities = {1: 0, 2: 0, 3: 0}
    street_ftr = {1: 0, 2: 0, 3: 0}
    # Turn/river continuation tracking (bet next street after betting current)
    flop_bet_hands = 0
    turn_after_flop_bet = 0
    turn_bet_hands = 0
    river_after_turn_bet = 0
    # Sizing aggression: large vs small bet classification
    opp_small_bet_count = 0
    opp_large_bet_count = 0

    for req in hand_requests:
        if opponent_can_lock_win(req, my_id):
            continue

        history = req.get("history", [])
        if not history:
            continue

        saw_opponent_preflop_action = False
        pending_my_pressure_round = None

        for record in history:
            pid = record["player_id"]
            action_type = record["action_type"]
            action = record["action"]
            round_idx = record["round"]

            if pid == my_id and action_type in ("raise", "allin"):
                pending_my_pressure_round = round_idx
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

                # Per-street tracking: count bet opportunities and actual bets
                if round_idx in street_bet_opportunities:
                    street_bet_opportunities[round_idx] += 1
                    if action_type in ("raise", "allin"):
                        street_bets[round_idx] += 1

                # Sizing aggression classification for exploit targeting
                if action_type == "raise":
                    sizing_bb = action / BIG_BLIND
                    if sizing_bb >= 8.0:
                        opp_large_bet_count += 1
                    else:
                        opp_small_bet_count += 1

            if action_type == "raise":
                raise_sizes.append(action / BIG_BLIND)
                if round_idx == 1:
                    flop_raise_bb.append(action / BIG_BLIND)
                elif round_idx == 2:
                    turn_raise_bb.append(action / BIG_BLIND)
                elif round_idx == 3:
                    river_raise_bb.append(action / BIG_BLIND)

            if pending_my_pressure_round is not None:
                fold_to_raise_opportunities += 1
                if action_type == "fold":
                    fold_to_raise += 1
                    if pending_my_pressure_round in street_ftr:
                        street_ftr[pending_my_pressure_round] += 1
                if pending_my_pressure_round in street_ftr_opportunities:
                    street_ftr_opportunities[pending_my_pressure_round] += 1
                pending_my_pressure_round = None

        # Per-hand continuation tracking
        opp_streets_with_bets = set()
        for record in history:
            if record["player_id"] == opponent_id and record["round"] > 0 and record["action_type"] in ("raise", "allin"):
                opp_streets_with_bets.add(record["round"])
        if 1 in opp_streets_with_bets:
            flop_bet_hands += 1
            if 2 in opp_streets_with_bets:
                turn_after_flop_bet += 1
        if 2 in opp_streets_with_bets:
            turn_bet_hands += 1
            if 3 in opp_streets_with_bets:
                river_after_turn_bet += 1

    confidence = clamp((total_actions - 5) / 35.0, 0.0, 1.0)
    avg_raise_bb = sum(raise_sizes) / len(raise_sizes) if raise_sizes else 2.6

    flop_tendency = _classify_sizing_tendency(flop_raise_bb)
    turn_tendency = _classify_sizing_tendency(turn_raise_bb)
    river_tendency = _classify_sizing_tendency(river_raise_bb)

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
        "flop_aggr": smooth_rate(street_bets[1], street_bet_opportunities[1], 0.36, 5.0),
        "turn_aggr": smooth_rate(street_bets[2], street_bet_opportunities[2], 0.30, 5.0),
        "river_aggr": smooth_rate(street_bets[3], street_bet_opportunities[3], 0.25, 5.0),
        "flop_fold_to_raise": smooth_rate(street_ftr[1], street_ftr_opportunities[1], 0.44, 4.0),
        "turn_fold_to_raise": smooth_rate(street_ftr[2], street_ftr_opportunities[2], 0.40, 4.0),
        "river_fold_to_raise": smooth_rate(street_ftr[3], street_ftr_opportunities[3], 0.35, 4.0),
        "turn_continuation": smooth_rate(turn_after_flop_bet, flop_bet_hands, 0.45, 3.0),
        "river_continuation": smooth_rate(river_after_turn_bet, turn_bet_hands, 0.40, 3.0),
        "flop_sizing_tendency": flop_tendency,
        "turn_sizing_tendency": turn_tendency,
        "river_sizing_tendency": river_tendency,
        "avg_flop_raise_bb": sum(flop_raise_bb) / len(flop_raise_bb) if flop_raise_bb else 3.0,
        "avg_turn_raise_bb": sum(turn_raise_bb) / len(turn_raise_bb) if turn_raise_bb else 4.5,
        "avg_river_raise_bb": sum(river_raise_bb) / len(river_raise_bb) if river_raise_bb else 5.5,
        "sizing_aggr": smooth_rate(opp_large_bet_count, opp_small_bet_count + opp_large_bet_count, 0.35, 4.0),
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
