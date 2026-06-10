from constants import BIG_BLIND
from card_utils import clamp, next_player
from state import collect_latest_requests_by_hand
from tournament import opponent_can_lock_win


def smooth_rate(successes, total, prior_mean, prior_weight):
    return (successes + prior_mean * prior_weight) / (total + prior_weight)


def classify_opponent_sizing_pattern(requests, my_id):
    opponent_id = next_player(my_id, 1)
    small = medium = large = 0
    postflop_aggr = 0
    postflop_actions = 0
    for req in collect_latest_requests_by_hand(requests):
        for rec in req.get("history", []):
            if rec["player_id"] != opponent_id:
                continue
            if rec["round"] > 0:
                postflop_actions += 1
                if rec["action_type"] in ("raise", "allin"):
                    postflop_aggr += 1
            if rec["action_type"] != "raise":
                continue
            bb = rec["action"] / BIG_BLIND
            if bb < 3.0:
                small += 1
            elif bb <= 8.0:
                medium += 1
            else:
                large += 1
    total = small + medium + large
    if total < 5:
        return {"pattern": "none", "confidence": 0.0, "large_rate": 0.0,
                "medium_rate": 0.0, "small_rate": 0.0, "postflop_aggr": 0.0}
    pa = postflop_aggr / max(1, postflop_actions)
    lr, mr, sr = large / total, medium / total, small / total
    conf = clamp((total - 5) / 15.0, 0.0, 1.0)
    if lr > 0.40 and sr > 0.30 and mr < 0.25:
        pattern = "polarized"
    elif mr > 0.50:
        pattern = "merged"
    elif lr > 0.55 and pa > 0.42:
        pattern = "over_bluff"
    elif sr > 0.55 and pa < 0.30:
        pattern = "under_value"
    else:
        pattern = "none"
    return {"pattern": pattern, "confidence": conf, "large_rate": lr,
            "medium_rate": mr, "small_rate": sr, "postflop_aggr": pa}


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
    opp_small_bet_count = 0
    opp_large_bet_count = 0

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
                if action_type == 'raise':
                    sizing_bb = action / BIG_BLIND
                    if sizing_bb >= 8.0:
                        opp_large_bet_count += 1
                    else:
                        opp_small_bet_count += 1
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

    confidence = clamp((total_actions - 5) / 35.0, 0.0, 1.0)
    avg_raise_bb = sum(raise_sizes) / len(raise_sizes) if raise_sizes else 2.6
    pattern_info = classify_opponent_sizing_pattern(requests, my_id)

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
        "flop_aggr": smooth_rate(flop_bets, flop_acts, 0.36, 5.0),
        "turn_aggr": smooth_rate(turn_bets, turn_acts, 0.32, 5.0),
        "river_aggr": smooth_rate(river_bets, river_acts, 0.28, 5.0),
        "avg_flop_raise_bb": sum(flop_raise_bb)/len(flop_raise_bb) if flop_raise_bb else 3.0,
        "avg_turn_raise_bb": sum(turn_raise_bb)/len(turn_raise_bb) if turn_raise_bb else 4.5,
        "avg_river_raise_bb": sum(river_raise_bb)/len(river_raise_bb) if river_raise_bb else 5.5,
        "barrel_freq": smooth_rate(barrel_continue, barrel_hands, 0.45, 4.0),
        "sizing_aggr": smooth_rate(opp_large_bet_count, opp_small_bet_count + opp_large_bet_count, 0.35, 4.0),
        "sizing_pattern": pattern_info["pattern"],
        "pattern_confidence": pattern_info["confidence"],
        "pattern_large_rate": pattern_info["large_rate"],
        "pattern_medium_rate": pattern_info["medium_rate"],
        "pattern_small_rate": pattern_info["small_rate"],
        "pattern_postflop_aggr": pattern_info["postflop_aggr"],
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
                # Detect if SB limped (call) vs raised (v23 fix)
                sb_first_action = None
                for rec in history:
                    if rec["player_id"] == my_id and rec["round"] == 0:
                        sb_first_action = rec["action_type"]
                        break
                if sb_first_action == "call":
                    info["preflop_spot"] = "sb_vs_iso_raise"
                else:
                    info["preflop_spot"] = "sb_vs_reraise"

    return info
