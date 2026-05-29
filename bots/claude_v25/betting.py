"""Betting decision functions extracted from strategy.py for modularity."""
from constants import BIG_BLIND
from card_utils import clamp
from postflop import empty_draw_profile
from state import (
    get_remaining_hands,
    classify_preflop_hand_tier,
)
from tournament import match_risk_adjustment


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
    if value_plan.get("thin_control", False) and value_profile.get("tier") != "nut":
        thin_cap = 0.30 if round_idx <= 2 else 0.38
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


def _tier_preflop_raise_amount(tier, min_raise, my_chips, my_round_bet, to_call, pot, preflop_strength, spot_name):
    """Compute tier-based preflop raise sizing. Returns raise amount or None."""
    if my_chips <= max(min_raise, to_call) + 1:
        return None
    if tier == 1:
        desired_total = int(3.5 * BIG_BLIND + max(0.0, preflop_strength - 0.80) * 2.0 * BIG_BLIND)
    elif tier == 2:
        desired_total = int(2.8 * BIG_BLIND + max(0.0, preflop_strength - 0.65) * 2.0 * BIG_BLIND)
    else:
        desired_total = int(2.2 * BIG_BLIND + max(0.0, preflop_strength - 0.50) * 1.5 * BIG_BLIND)
    amount = max(min_raise, desired_total - my_round_bet)
    if spot_name == "bb_vs_limp":
        amount = max(amount, int(3.0 * BIG_BLIND - my_round_bet))
    amount = min(amount, my_chips - 1)
    if amount <= to_call or amount < min_raise or amount >= my_chips:
        return None
    return amount


def choose_preflop_spot_action(req, state, spot_info, opponent_model, preflop_strength, win_rate, match_profile):
    my_chips = req["my_chips"]
    to_call = state["to_call"]
    min_raise = state.get("min_raise_action", state["round_raise"])
    tier = classify_preflop_hand_tier(req["my_cards"])
    match_adjust = match_risk_adjustment(req, req["my_id"], get_remaining_hands(req))
    confidence = opponent_model["confidence"]
    loose_bonus = confidence * max(0.0, opponent_model["vpip"] - 0.55) * 0.03

    if spot_info["preflop_spot"] == "sb_open":
        if tier <= 3:
            raise_amount = _tier_preflop_raise_amount(
                tier, min_raise, my_chips, state["my_round_bet"],
                to_call, state["pot"], preflop_strength, "sb_open")
            if raise_amount is not None:
                return raise_amount
            return 0
        elif tier == 4:
            return 0  # complete SB
        else:
            return -1  # fold trash

    if spot_info["preflop_spot"] == "bb_vs_limp":
        if tier <= 3:
            raise_amount = _tier_preflop_raise_amount(
                tier, min_raise, my_chips, state["my_round_bet"],
                to_call, state["pot"], preflop_strength, "bb_vs_limp")
            if raise_amount is not None:
                return raise_amount
            return 0
        return 0  # check BB (free)

    if spot_info["preflop_spot"] == "bb_vs_raise":
        if tier <= 1:
            raise_amount = _tier_preflop_raise_amount(
                tier, min_raise, my_chips, state["my_round_bet"],
                to_call, state["pot"], preflop_strength, "bb_vs_raise")
            if raise_amount is not None:
                return raise_amount
            return 0
        elif tier <= 3:
            return 0  # call
        elif tier == 4:
            if to_call <= state["pot"] * 0.5 or to_call <= 3 * BIG_BLIND:
                return 0
            return -1
        else:
            return -1

    if spot_info["preflop_spot"] == "sb_vs_reraise":
        if tier <= 1:
            raise_amount = _tier_preflop_raise_amount(
                tier, min_raise, my_chips, state["my_round_bet"],
                to_call, state["pot"], preflop_strength, "sb_vs_reraise")
            if raise_amount is not None:
                return raise_amount
            return 0
        elif tier <= 2:
            return 0
        elif tier == 3:
            if to_call <= state["pot"] * 0.4 or to_call <= 3 * BIG_BLIND:
                return 0
            return -1
        else:
            return -1

    return None


def should_fold_postflop(made_strength, value_profile, board_texture, spot_info,
                          draw_info, pot_odds, to_call, pot, round_idx):
    """Structural postflop fold decision for weak hands facing aggression.
    Returns True if we should fold. Target: Flop 15-25%, Turn 20-30%, River 10-15%."""
    # Never fold nut or strong hands
    if value_profile is not None and value_profile["tier"] in ("nut", "strong"):
        return False
    # Never fold with decent draws
    if draw_info is not None and draw_info.get("quality", 0) >= 0.12:
        return False
    # Never fold thin value with decent made hand
    if value_profile is not None and value_profile["tier"] == "thin" and made_strength >= 0.35:
        return False
    # Don't fold for free
    if to_call <= 0:
        return False
    # Don't fold very small bets
    if pot > 0 and to_call / pot < 0.25:
        return False
    # Decent made hand threshold
    if made_strength >= 0.45:
        return False

    weak_hand = made_strength < 0.25
    medium_hand = 0.25 <= made_strength < 0.45
    bet_size = to_call / pot if pot > 0 else 0

    # Board threat assessment
    board_scary = False
    if board_texture is not None:
        threats = 0
        if board_texture["flush_pressure"] >= 0.75:
            threats += 1
        if board_texture["straight_pressure"] >= 0.65:
            threats += 1
        if board_texture["paired"]:
            threats += 1
        board_scary = threats >= 2

    # Multi-street aggression count
    opp_bets = spot_info.get("opp_postflop_bet_count", 0)

    # Fold criteria:
    # 1. Large bet (>75% pot) vs weak hand
    if bet_size > 0.75 and weak_hand:
        return True
    # 2. Large bet vs medium hand on turn/river with scary board
    if bet_size > 0.75 and medium_hand and round_idx >= 2 and board_scary:
        return True
    # 3. Multi-street aggression (2+ bets) vs weak hand
    if opp_bets >= 2 and weak_hand:
        return True
    # 4. Multi-street aggression vs medium hand on turn/river + scary board
    if opp_bets >= 2 and medium_hand and round_idx >= 2 and board_scary:
        return True
    # 5. Turn/river: medium bet (>50% pot) vs weak hand
    if round_idx >= 2 and bet_size > 0.50 and weak_hand:
        return True

    return False


def choose_overbet_river(
    min_raise, my_chips, my_round_bet, to_call, pot,
    win_rate, value_profile, board_texture, spot_info, opponent_model
):
    """River overbet: 1.5-2.2x pot with NUT hands only."""
    if value_profile is None or value_profile["tier"] != "nut":
        return None
    if board_texture is not None and board_texture["wetness"] > 0.35:
        return None
    if pot < 400:
        return None

    pot_after_call = pot + to_call
    ratio = 1.5 + 0.3 * max(0.0, win_rate - 0.70)
    if not spot_info.get("has_position", False):
        ratio = max(1.3, ratio - 0.2)
    ratio = min(ratio, 2.2)
    amount = int(to_call + pot_after_call * ratio)

    if amount >= my_chips:
        return -2
    amount = min(amount, my_chips - 1)
    if amount <= to_call or amount < min_raise:
        return None
    return amount


def choose_overbet_bluff_river(
    min_raise, my_chips, my_round_bet, to_call, pot,
    blocker_profile, board_texture, spot_info, opponent_model
):
    """River overbet bluff with strong blockers on dry boards."""
    if to_call != 0:
        return None
    if blocker_profile is None or not blocker_profile.get("eligible", False):
        return None
    if blocker_profile["score"] < 0.35:
        return None
    if board_texture is not None and board_texture["wetness"] >= 0.25:
        return None
    if board_texture is not None and board_texture.get("paired", False):
        return None
    if pot < 400:
        return None
    if opponent_model.get("fold_to_raise", 0) <= 0.48:
        return None
    ratio = 1.3 + 0.2 * blocker_profile["score"]
    if not spot_info.get("has_position", False):
        ratio -= 0.15
    ratio = min(ratio, 1.6)
    amount = int(to_call + (pot + to_call) * ratio)
    if amount >= my_chips:
        return -2
    amount = min(amount, my_chips - 1)
    if amount < min_raise:
        return None
    return amount


def big_pot_safety_guard(pot, my_chips, value_profile, made_strength, round_idx, to_call, draw_strength):
    """Return True if situation is too risky for aggressive play with marginal hands."""
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


def must_continue_vs_raise(value_profile, made_strength, pot_odds, nutted_risk, board_texture, draw_strength=0.0):
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
    if draw_strength >= 0.20 and pot_odds <= 0.38:
        if not extreme_texture:
            return True
    return False
