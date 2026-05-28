"""
Preflop decision logic: spot actions, gift tracking, and exploitation scaling.
"""
from constants import BIG_BLIND, N_PLAYERS, INITIAL_CHIPS
from card_utils import clamp, next_player
from state import (
    estimate_preflop_strength,
    is_preflop_trash_hand,
    is_preflop_3bet_candidate,
    get_remaining_hands,
    get_hand_index,
    collect_latest_requests_by_hand,
)
from tournament import match_risk_adjustment
from betting import choose_raise


def track_opponent_gift(requests, my_id):
    opponent_id = next_player(my_id, 1)
    hand_requests = collect_latest_requests_by_hand(requests)
    gift_balance = 0.0
    for req in hand_requests:
        total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
        if len(total_win_chips) > opponent_id:
            opp_chips = total_win_chips[opponent_id]
            if opp_chips < -200:
                gift_balance += (-opp_chips - 200) / INITIAL_CHIPS
    return gift_balance


def safe_exploitation_lambda(gift_balance, confidence):
    if confidence < 0.25:
        return 0.0
    return clamp(confidence * min(1.0, max(0, gift_balance) / 2.0), 0.0, 0.85)


def choose_preflop_spot_action(req, state, spot_info, opponent_model, preflop_strength, win_rate, match_profile):
    my_chips = req["my_chips"]
    to_call = state["to_call"]
    match_adjust = match_risk_adjustment(req, req["my_id"], get_remaining_hands(req))
    confidence = opponent_model["confidence"]
    loose_bonus = confidence * max(0.0, opponent_model["vpip"] - 0.55) * 0.03
    trash_hand = is_preflop_trash_hand(req["my_cards"], preflop_strength)

    if spot_info["preflop_spot"] == "sb_open":
        open_threshold = 0.49 + match_adjust + 0.02 + match_profile["open_delta"]
        limp_threshold = 0.36 + match_adjust
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

    if spot_info["preflop_spot"] == "bb_vs_raise":
        pot_after_call = state["pot"] + to_call
        fold_to_raise = opponent_model.get("fold_to_raise", 0.44)

        if preflop_strength >= 0.72:
            target = int(to_call + pot_after_call * 0.75)
            target = max(target, state["min_raise_action"])
            if target >= my_chips * 0.5:
                return -2
            return target

        if 0.38 <= preflop_strength <= 0.52 and confidence >= 0.25 and fold_to_raise > 0.45:
            hand_index = get_hand_index(req) or 0
            token = (sum(req["my_cards"]) * 13 + hand_index * 7) % 100
            bluff_freq = clamp((fold_to_raise - 0.45) * 1.2, 0, 0.6)
            if token < int(bluff_freq * 100):
                target = int(to_call + pot_after_call * 0.60)
                target = max(target, state["min_raise_action"])
                if target >= my_chips:
                    return -2
                return target

        call_threshold = 0.42 + match_adjust - loose_bonus
        if preflop_strength >= call_threshold:
            return 0
        if preflop_strength < 0.35 and to_call > BIG_BLIND * 3:
            return -1
        return 0

    if spot_info["preflop_spot"] == "sb_vs_reraise":
        if preflop_strength >= 0.85:
            pot_after_call = state["pot"] + to_call
            target = int(to_call + pot_after_call * 0.70)
            target = max(target, state["min_raise_action"])
            if target >= my_chips * 0.5:
                return -2
            return target

        if preflop_strength >= 0.60 and to_call <= my_chips * 0.15:
            return 0

        return -1

    return None
