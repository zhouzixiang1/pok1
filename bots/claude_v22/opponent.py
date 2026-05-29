"""
Opponent modeling and spot analysis.
"""
from constants import N_PLAYERS, BIG_BLIND
from card_utils import clamp, next_player
from state import (
    get_hand_index,
    collect_latest_requests_by_hand,
)
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

    cbet_opportunities = 0
    cbet_count = 0
    fold_to_cbet_opportunities = 0
    fold_to_cbet_count = 0

    hand_vpip_flags = []
    hand_pfr_flags = []
    hand_postflop_aggr_counts = []
    hand_postflop_action_counts = []

    for req in hand_requests:
        if opponent_can_lock_win(req, my_id):
            continue

        history = req.get("history", [])
        if not history:
            continue

        saw_opponent_preflop_action = False
        pending_my_pressure = False

        opp_raised_preflop_this_hand = False
        first_flop_action_seen = False
        facing_cbet = False
        hand_opp_vpip = False
        hand_opp_pfr = False
        hand_postflop_aggr = 0
        hand_postflop_total = 0

        for record in history:
            pid = record["player_id"]
            action_type = record["action_type"]
            action = record["action"]
            round_idx = record["round"]

            if round_idx == 1 and not first_flop_action_seen:
                first_flop_action_seen = True
                if pid == opponent_id and opp_raised_preflop_this_hand:
                    cbet_opportunities += 1
                    if action_type in ("raise", "allin"):
                        cbet_count += 1
                        facing_cbet = True

            if pid == my_id and facing_cbet:
                fold_to_cbet_opportunities += 1
                if action_type == "fold":
                    fold_to_cbet_count += 1
                facing_cbet = False

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

            if round_idx == 0:
                if action_type in ("raise", "allin"):
                    opp_raised_preflop_this_hand = True
                if not saw_opponent_preflop_action:
                    saw_opponent_preflop_action = True
                    preflop_opportunities += 1
                    if action_type in ("call", "raise", "allin"):
                        voluntary_preflop += 1
                        hand_opp_vpip = True
                    if action_type in ("raise", "allin"):
                        preflop_raise += 1
                        hand_opp_pfr = True

            if round_idx > 0:
                postflop_actions += 1
                hand_postflop_total += 1
                if action_type in ("raise", "allin"):
                    postflop_aggressive += 1
                    hand_postflop_aggr += 1
                if action_type == "check":
                    postflop_checks += 1

            if action_type == "raise":
                raise_sizes.append(action / BIG_BLIND)

            if pending_my_pressure:
                fold_to_raise_opportunities += 1
                if action_type == "fold":
                    fold_to_raise += 1
                pending_my_pressure = False

        if saw_opponent_preflop_action:
            hand_vpip_flags.append(1 if hand_opp_vpip else 0)
            hand_pfr_flags.append(1 if hand_opp_pfr else 0)
            hand_postflop_aggr_counts.append(hand_postflop_aggr)
            hand_postflop_action_counts.append(hand_postflop_total)

    confidence = clamp((total_actions - 5) / 35.0, 0.0, 1.0)
    avg_raise_bb = sum(raise_sizes) / len(raise_sizes) if raise_sizes else 2.6

    result = {
        "confidence": confidence,
        "vpip": smooth_rate(voluntary_preflop, preflop_opportunities, 0.52, 4.0),
        "pfr": smooth_rate(preflop_raise, preflop_opportunities, 0.24, 4.0),
        "allin_rate": smooth_rate(allin_actions, total_actions, 0.05, 8.0),
        "postflop_aggr": smooth_rate(postflop_aggressive, postflop_actions, 0.36, 5.0),
        "postflop_check_rate": smooth_rate(postflop_checks, postflop_actions, 0.42, 5.0),
        "fold_to_raise": smooth_rate(fold_to_raise, fold_to_raise_opportunities, 0.44, 4.0),
        "aggression": smooth_rate(aggressive_actions, total_actions, 0.30, 6.0),
        "avg_raise_bb": avg_raise_bb,
        "cbet_rate": smooth_rate(cbet_count, cbet_opportunities, 0.55, 4.0),
        "fold_to_cbet": smooth_rate(fold_to_cbet_count, fold_to_cbet_opportunities, 0.40, 3.0),
        "drift_detected": False,
    }

    if len(hand_vpip_flags) >= 12:
        recent_count = min(10, len(hand_vpip_flags))
        recent_vpip = sum(hand_vpip_flags[-10:]) / recent_count
        all_time_vpip = sum(hand_vpip_flags) / len(hand_vpip_flags)
        recent_pfr = sum(hand_pfr_flags[-10:]) / recent_count
        all_time_pfr = sum(hand_pfr_flags) / len(hand_pfr_flags)

        if abs(recent_vpip - all_time_vpip) > 0.15 or abs(recent_pfr - all_time_pfr) > 0.12:
            result["drift_detected"] = True
            result["vpip"] = recent_vpip
            result["pfr"] = recent_pfr
            recent_postflop_aggr_actions = sum(hand_postflop_aggr_counts[-10:])
            recent_postflop_total_actions = sum(hand_postflop_action_counts[-10:])
            result["postflop_aggr"] = smooth_rate(recent_postflop_aggr_actions, recent_postflop_total_actions, 0.36, 5.0)
            result["confidence"] = max(0.25, confidence * 0.6)

    return result


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


def detect_bot4_profile(opponent_model, n_hands_played):
    """Detect if opponent exhibits bot_4's characteristic stats."""
    confidence = opponent_model["confidence"]
    if confidence < 0.10:
        return False, 0.0

    score = 0.0
    vpip = opponent_model["vpip"]
    pfr = opponent_model["pfr"]
    aggr = opponent_model["aggression"]
    post_aggr = opponent_model["postflop_aggr"]
    fold_raise = opponent_model["fold_to_raise"]

    if abs(vpip - 0.58) < 0.15:
        score += 0.20
    if abs(pfr - 0.28) < 0.13:
        score += 0.20
    if abs(post_aggr - 0.36) < 0.15:
        score += 0.20
    if abs(fold_raise - 0.44) < 0.15:
        score += 0.15
    if abs(aggr - 0.30) < 0.13:
        score += 0.15

    score *= confidence
    return score >= 0.20, score


def get_anti_bot4_adjustments(bot4_score, board_texture, spot_info, round_idx, value_profile):
    """Return strategy adjustments targeting bot_4's weaknesses."""
    adj = {
        "bluff_freq_bonus": 0.0,
        "raise_size_bonus": 0.0,
        "call_threshold_delta": 0.0,
        "fold_threshold_delta": 0.0,
        "river_overbet_enabled": False,
        "trap_defense_delta": 0.0,
    }

    # Wet board: exploit bot_4 overfold on dynamic boards
    if board_texture and board_texture["dynamic"]:
        adj["bluff_freq_bonus"] += 0.15 * bot4_score
        adj["raise_size_bonus"] += 0.08 * bot4_score

    # Paired board: exploit bot_4 paired board caution
    if board_texture and board_texture["paired"]:
        adj["bluff_freq_bonus"] += 0.10 * bot4_score
        adj["raise_size_bonus"] += 0.05 * bot4_score

    # River check exploit: bot_4 checks too much on river
    if round_idx == 3 and spot_info.get("last_opp_action_type") == "check":
        adj["bluff_freq_bonus"] += 0.12 * bot4_score

    # Preflop 3-Bet wider vs bot_4
    if round_idx == 0 and spot_info.get("preflop_spot") in ("bb_vs_raise", "sb_vs_reraise"):
        adj["call_threshold_delta"] -= 0.05 * bot4_score

    # Anti-trap: more cautious vs check-raise
    if spot_info.get("opp_current_round_check_count", 0) > 0 and spot_info["facing_raise"]:
        adj["trap_defense_delta"] += 0.08 * bot4_score

    # River overbet always enabled with strong hands (not just vs bot_4)
    if round_idx == 3 and value_profile and value_profile["tier"] in ("nut", "strong"):
        adj["river_overbet_enabled"] = True

    return adj


def classify_opponent_style(opp_model):
    """Classify opponent style and return threshold deltas.
    Based on v3's classification but WITHOUT EXP3.
    Returns dict of deltas that default to zero for unknown opponents."""
    deltas = {
        "strong_delta": 0.0,
        "medium_delta": 0.0,
        "bluff_freq_bonus": 0.0,
        "call_aggression_bonus": 0.0,
        "fold_vs_passive_bonus": 0.0,
    }

    confidence = opp_model.get("confidence", 0.0)
    if confidence < 0.15:
        return deltas

    vpip = opp_model.get("vpip", 0.52)
    pfr = opp_model.get("pfr", 0.24)
    fold_to_raise = opp_model.get("fold_to_raise", 0.44)
    postflop_aggr = opp_model.get("postflop_aggr", 0.36)

    # Nit: low VPIP, high fold_to_raise
    if vpip < 0.35 and fold_to_raise > 0.50:
        deltas["strong_delta"] = -0.02
        deltas["medium_delta"] = -0.015
        deltas["bluff_freq_bonus"] = 0.12
    # Maniac: high VPIP, high PFR, high postflop aggression
    elif vpip > 0.65 and pfr > 0.40 and postflop_aggr > 0.45:
        deltas["strong_delta"] = 0.03
        deltas["medium_delta"] = 0.025
        deltas["bluff_freq_bonus"] = -0.08
        deltas["call_aggression_bonus"] = 0.04
    # Calling station: high VPIP, low PFR, low fold_to_raise
    elif vpip > 0.55 and pfr < 0.20 and fold_to_raise < 0.38:
        deltas["strong_delta"] = -0.01
        deltas["medium_delta"] = -0.02
        deltas["bluff_freq_bonus"] = -0.12
    # Fold-heavy: high fold_to_raise
    elif fold_to_raise > 0.52:
        deltas["strong_delta"] = -0.015
        deltas["medium_delta"] = -0.01
        deltas["bluff_freq_bonus"] = 0.10

    return deltas
