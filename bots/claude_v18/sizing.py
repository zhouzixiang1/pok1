"""
Betting size and action selection: raise sizing, anti-lock pressure, preflop spots, exploitation.
"""
from constants import N_PLAYERS, INITIAL_CHIPS, BIG_BLIND, TOTAL_HANDS
from card_utils import clamp, next_player
from state import (
    get_remaining_hands,
    get_hand_index,
    is_preflop_trash_hand,
    collect_latest_requests_by_hand,
)
from tournament import match_risk_adjustment
from postflop import empty_draw_profile


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
    anti_bot4_bonus=0.0,
    allow_river_overbet=False,
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
    ratio += anti_bot4_bonus
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
    if value_plan.get("thin_control", False) and value_profile.get("tier") != "nut" and to_call == 0:
        thin_cap = 0.46 + 0.08 * wetness + 0.05 * max(0, round_idx - 1)
        ratio = min(ratio, thin_cap)
    low_ratio = 0.28 if inducing_value else 0.22 if probe_mode or (blocker_bluff and to_call == 0) else 0.40
    if thin_cap is not None:
        low_ratio = min(low_ratio, thin_cap)
    max_ratio = 2.2 if (allow_river_overbet and round_idx == 3 and value_profile.get("tier") == "nut") else 1.45
    ratio = clamp(ratio, low_ratio, max_ratio)

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
        if opponent_model["fold_to_raise"] > 0.52:
            iso_threshold -= 0.03
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
        pot_after_call = state["pot"] + to_call
        fold_to_raise = opponent_model.get("fold_to_raise", 0.44)

        if preflop_strength >= 0.85:
            target = int(to_call + pot_after_call * 0.70)
            target = max(target, state["min_raise_action"])
            if target >= my_chips * 0.5:
                return -2
            return target

        # 3-bet bluff: medium-strength hands with blockers vs fold-prone opponents
        if 0.40 <= preflop_strength <= 0.55 and fold_to_raise > 0.48 and confidence >= 0.25:
            from state import preflop_hand_profile
            profile = preflop_hand_profile(req["my_cards"])
            if profile["high"] >= 12:
                hand_index = get_hand_index(req) or 0
                token = (sum(req["my_cards"]) * 17 + hand_index * 11) % 100
                bluff_freq = clamp((fold_to_raise - 0.48) * 1.5, 0, 0.5)
                if token < int(bluff_freq * 100):
                    target = int(to_call + pot_after_call * 0.58)
                    target = max(target, state["min_raise_action"])
                    if target >= my_chips:
                        return -2
                    return target

        if preflop_strength >= 0.60 and to_call <= my_chips * 0.15:
            return 0

        return -1

    return None


def choose_overbet_river(min_raise, my_chips, to_call, pot, win_rate, value_profile, board_texture, spot_info):
    """River overbet: 1.5-2.2x pot with nut hands; 1.3-1.5x pot with strong on dry boards."""
    if value_profile is None or value_profile["tier"] not in ("nut", "strong"):
        return None
    if pot < 400:
        return None

    tier = value_profile["tier"]
    wetness = board_texture["wetness"] if board_texture else 0.0

    if tier == "nut":
        if wetness > 0.35:
            return None
        ratio = 1.5 + 0.3 * max(0.0, win_rate - 0.70)
        if not spot_info.get("has_position", False):
            ratio = max(1.3, ratio - 0.2)
        ratio = min(ratio, 2.2)
    else:
        if board_texture is not None and (wetness > 0.20 or board_texture.get("dynamic", False)):
            return None
        ratio = 1.30 + 0.20 * max(0.0, win_rate - 0.60)
        ratio = min(ratio, 1.50)

    pot_after_call = pot + to_call
    amount = int(to_call + pot_after_call * ratio)

    if amount >= my_chips:
        return -2
    amount = min(amount, my_chips - 1)
    if amount <= to_call or amount < min_raise:
        return None
    return amount


def big_pot_safety_guard(pot, my_chips, value_profile, made_strength, round_idx, to_call, draw_strength):
    """Return True if the situation is too risky for aggressive play with marginal hands.
    Prevents catastrophic stack-offs with thin value in big pots."""
    if round_idx < 2:
        return False
    if to_call > 0:
        return False
    if value_profile is None:
        return False
    tier = value_profile.get("tier", "none")
    if tier in ("nut", "strong"):
        return False
    if made_strength >= 0.65:
        return False
    if pot < 7000:
        return False
    if tier == "thin" and draw_strength < 0.15:
        return True
    if 0.30 <= made_strength <= 0.50 and pot >= 10000 and draw_strength < 0.15:
        return True
    return False
