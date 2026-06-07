from constants import BIG_BLIND, N_PLAYERS
from card_utils import clamp, next_player
from state import collect_latest_requests_by_hand
from tournament import opponent_can_lock_win


def smooth_rate(successes, total, prior_mean, prior_weight):
    return (successes + prior_mean * prior_weight) / (total + prior_weight)


def _track_hand_stats(req, my_id, opponent_id):
    """Track per-hand statistics including CBet metrics."""
    stats = {
        "preflop_opportunities": 0, "voluntary_preflop": 0, "preflop_raise": 0,
        "total_actions": 0, "aggressive_actions": 0, "allin_actions": 0,
        "postflop_actions": 0, "postflop_aggressive": 0, "postflop_checks": 0,
        "fold_to_raise_opportunities": 0, "fold_to_raise": 0,
        "raise_sizes": [],
        "cbet_opportunities": 0, "cbet_count": 0,
        "fold_to_cbet_opportunities": 0, "fold_to_cbet_count": 0,
    }
    history = req.get("history", [])
    if not history:
        return stats

    saw_opponent_preflop_action = False
    pending_my_pressure = False
    opp_preflop_raised = False
    saw_flop = False
    opp_first_on_flop = True

    for record in history:
        pid = record["player_id"]
        action_type = record["action_type"]
        round_idx = record["round"]

        if round_idx == 1 and not saw_flop:
            saw_flop = True

        if pid == my_id:
            if action_type in ("raise", "allin"):
                pending_my_pressure = True
            if saw_flop and round_idx == 1:
                # Track if I fold to opponent's CBet
                pass
            continue

        if pid != opponent_id:
            continue

        stats["total_actions"] += 1
        if action_type in ("raise", "allin"):
            stats["aggressive_actions"] += 1
        if action_type == "allin":
            stats["allin_actions"] += 1

        if round_idx == 0 and not saw_opponent_preflop_action:
            saw_opponent_preflop_action = True
            stats["preflop_opportunities"] += 1
            if action_type in ("call", "raise", "allin"):
                stats["voluntary_preflop"] += 1
            if action_type in ("raise", "allin"):
                stats["preflop_raise"] += 1
                opp_preflop_raised = True

        if round_idx > 0:
            stats["postflop_actions"] += 1
            if action_type in ("raise", "allin"):
                stats["postflop_aggressive"] += 1
            if action_type == "check":
                stats["postflop_checks"] += 1

        # CBet tracking: opponent raised preflop, acts first on flop
        if round_idx == 1 and opp_first_on_flop and opp_preflop_raised:
            opp_first_on_flop = False
            stats["cbet_opportunities"] += 1
            if action_type in ("raise", "allin"):
                stats["cbet_count"] += 1
        elif round_idx == 1 and opp_first_on_flop:
            opp_first_on_flop = False

        if action_type == "raise":
            stats["raise_sizes"].append(record["action"] / BIG_BLIND)

        if pending_my_pressure:
            stats["fold_to_raise_opportunities"] += 1
            if action_type == "fold":
                stats["fold_to_raise"] += 1
            pending_my_pressure = False

    return stats


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

    for req in hand_requests:
        if opponent_can_lock_win(req, my_id):
            continue

        hs = _track_hand_stats(req, my_id, opponent_id)
        preflop_opportunities += hs["preflop_opportunities"]
        voluntary_preflop += hs["voluntary_preflop"]
        preflop_raise += hs["preflop_raise"]
        total_actions += hs["total_actions"]
        aggressive_actions += hs["aggressive_actions"]
        allin_actions += hs["allin_actions"]
        postflop_actions += hs["postflop_actions"]
        postflop_aggressive += hs["postflop_aggressive"]
        postflop_checks += hs["postflop_checks"]
        fold_to_raise_opportunities += hs["fold_to_raise_opportunities"]
        fold_to_raise += hs["fold_to_raise"]
        raise_sizes.extend(hs["raise_sizes"])
        cbet_opportunities += hs["cbet_opportunities"]
        cbet_count += hs["cbet_count"]
        fold_to_cbet_opportunities += hs["fold_to_cbet_opportunities"]
        fold_to_cbet_count += hs["fold_to_cbet_count"]

    confidence = clamp((total_actions - 5) / 35.0, 0.0, 1.0)
    avg_raise_bb = sum(raise_sizes) / len(raise_sizes) if raise_sizes else 2.6

    cbet_rate = smooth_rate(cbet_count, cbet_opportunities, 0.55, 4.0)
    fold_to_cbet = smooth_rate(fold_to_cbet_count, fold_to_cbet_opportunities, 0.40, 3.0)

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
        "cbet_rate": cbet_rate,
        "fold_to_cbet": fold_to_cbet,
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
