"""
Preflop strategy: spot-specific decision logic for preflop situations.
"""
from card_utils import clamp
from constants import BIG_BLIND
from state import get_hand_index, is_preflop_trash_hand
from betting import choose_raise


def choose_preflop_spot_action(req, state, spot_info, opponent_model, preflop_strength, win_rate, match_profile):
    from tournament import match_risk_adjustment
    from state import get_remaining_hands

    my_chips = req["my_chips"]
    to_call = state["to_call"]
    remaining_hands = get_remaining_hands(req)
    match_adjust = match_risk_adjustment(req, req["my_id"], remaining_hands)
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
