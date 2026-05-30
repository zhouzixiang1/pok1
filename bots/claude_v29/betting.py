"""Betting decision functions extracted from strategy.py for modularity."""
from constants import BIG_BLIND, TOTAL_HANDS
from card_utils import clamp
from postflop import empty_draw_profile
from state import (
    get_remaining_hands,
    is_preflop_trash_hand,
    classify_preflop_tier,
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
        ratio = 0.65 if to_call == 0 else 0.85
    elif round_idx == 1:
        ratio = 0.68
    elif round_idx == 2:
        ratio = 0.75
    else:
        ratio = 0.90

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
    low_ratio = 0.28 if inducing_value else 0.22 if probe_mode or (blocker_bluff and to_call == 0) else 0.48
    if thin_cap is not None:
        low_ratio = min(low_ratio, thin_cap)
    max_ratio = 2.2 if (allow_river_overbet and round_idx == 3 and value_profile.get("tier") == "nut") else 1.45
    ratio = clamp(ratio, low_ratio, max_ratio)

    amount = int(to_call + pot_after_call * ratio)

    if round_idx == 0 and preflop_strength is not None:
        if spot_name == "sb_open":
            desired_total = int((3.5 + max(0.0, preflop_strength - 0.58) * 2.0) * BIG_BLIND)
            amount = max(amount, desired_total - my_round_bet)
        elif spot_name == "bb_vs_limp":
            desired_total = int((4.0 + max(0.0, preflop_strength - 0.60) * 2.0) * BIG_BLIND)
            amount = max(amount, desired_total - my_round_bet)

    amount = max(min_raise, amount)
    if semi_bluff and fold_to_raise < 0.45:
        amount = min(amount, max(min_raise, int(to_call + pot_after_call * 0.70)))
    if blocker_bluff:
        bluff_cap = max(min_raise, int(to_call + pot_after_call * (0.45 if round_idx == 3 and to_call == 0 else 0.56 + 0.16 * wetness)))
        amount = min(amount, bluff_cap)
    amount = min(amount, my_chips - 1)

    if amount <= to_call or amount < min_raise or amount >= my_chips:
        return None
    return amount


def _preflop_tier_raise_size(tier, preflop_strength, my_round_bet, min_raise, pot, my_chips, has_position):
    """Compute a raise size based on preflop tier.
    Returns raise amount or None if can't raise."""
    if tier == 1:
        desired_total = int((3.0 + max(0.0, preflop_strength - 0.60) * 2.0) * BIG_BLIND)
    elif tier == 2:
        desired_total = int((2.8 + max(0.0, preflop_strength - 0.55) * 1.5) * BIG_BLIND)
    else:
        desired_total = int((2.5 + max(0.0, preflop_strength - 0.50) * 1.2) * BIG_BLIND)
    
    amount = max(min_raise, desired_total - my_round_bet)
    amount = min(amount, my_chips - 1)
    if amount < min_raise or amount >= my_chips:
        return None
    return amount


def choose_preflop_spot_action(req, state, spot_info, opponent_model, preflop_strength, win_rate, match_profile):
    """Tier-based preflop action selection.
    
    BTN (SB open): Raise T1-3, limp/call T4, fold only T5.
    BB (vs limp): Raise T1-2, call T3-4, fold only T5.
    BB (vs raise): Call T1-3, call T4 with good odds, fold T5.
    """
    my_chips = req["my_chips"]
    my_cards = req["my_cards"]
    to_call = state["to_call"]
    pot = max(1, state["pot"])
    match_adjust = match_risk_adjustment(req, req["my_id"], get_remaining_hands(req))
    confidence = opponent_model["confidence"]
    loose_bonus = confidence * max(0.0, opponent_model["vpip"] - 0.55) * 0.03
    tier = classify_preflop_tier(my_cards)
    min_raise_action = state.get("min_raise_action", state["round_raise"])
    
    # If we're facing a raise (not a limp) in BB, we use the existing flow
    # to let the main get_action() handle the call/raise/fold logic with proper
    # equity estimation and opponent modeling. This function only handles
    # the open/limp/iso spots where our tier system makes the decision.
    
    if spot_info["preflop_spot"] == "sb_open":
        # BTN: Raise T1-3, call T4, fold T5
        if tier <= 3:
            raise_amount = _preflop_tier_raise_size(
                tier, preflop_strength, state["my_round_bet"],
                min_raise_action, pot, my_chips, spot_info["has_position"],
            )
            if raise_amount is not None:
                return raise_amount
            # If raise too expensive, just call (limp)
            return 0
        elif tier == 4:
            # Limp/call with decent but weak hands
            return 0
        else:
            # Tier 5: garbage, but limp if match pressure demands it
            if match_profile.get("chase", 0) > 0.55 or match_adjust < -0.03:
                return 0
            return -1
    
    if spot_info["preflop_spot"] == "bb_vs_limp":
        # BB vs limp: Iso-raise T1-2, call T3-4, fold T5
        if tier <= 2:
            raise_amount = _preflop_tier_raise_size(
                tier, preflop_strength, state["my_round_bet"],
                min_raise_action, pot, my_chips, spot_info["has_position"],
            )
            if raise_amount is not None:
                return raise_amount
            return 0
        elif tier <= 4:
            # Call with T3-4
            return 0
        else:
            # T5 garbage: check back (free play)
            return 0
    
    # Unknown spot: fall back to old logic
    trash_hand = is_preflop_trash_hand(my_cards, preflop_strength)
    if spot_info["preflop_spot"] == "sb_open":
        open_threshold = 0.49 + match_adjust + 0.02 + match_profile["open_delta"]
        limp_threshold = 0.36 + match_adjust
        raise_amount = choose_raise(
            min_raise_action, my_chips, state["my_round_bet"],
            to_call, pot, max(win_rate, preflop_strength),
            0, spot_info["preflop_spot"], preflop_strength,
            spot_info["has_position"], opponent_model,
            match_sizing_delta=match_profile["sizing_delta"],
        )
        if not trash_hand and preflop_strength >= open_threshold and raise_amount is not None:
            return raise_amount
        if preflop_strength <= limp_threshold - loose_bonus:
            return -1
        return 0
    
    return None


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
    """Return True if the situation is too risky for aggressive play with marginal hands."""
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

def classify_river_hand(made_strength, value_profile, pair_profile, draw_info, board_texture, win_rate, public_cards):
    """Classify river hand into 3 branches: STRONG, MEDIUM, WEAK.
    
    STRONG: Two pair+, strong top pair (good kicker), overpairs, sets, flushes, straights, full house+
    MEDIUM: Weak top pair, middle pair, decent pair with draw backup
    WEAK: Missed draws, bottom pair, underpair, nothing
    """
    if len(public_cards) < 5:
        return "none"
    
    # If no value profile or pair profile, use made_strength heuristic
    if value_profile is None:
        if made_strength >= 0.55:
            return "strong"
        elif made_strength >= 0.25:
            return "medium"
        return "weak"
    
    tier = value_profile.get("tier", "none")
    
    # NUT and STRONG value tiers are always STRONG branch
    if tier in ("nut", "strong"):
        return "strong"
    
    # Thin value: in HU NLHE, thin value hands should often bet river
    if tier == "thin":
        if made_strength >= 0.30:
            return "strong"
        return "medium"

    # Check pair profile for top pair strength
    if pair_profile is not None and pair_profile.get("made_class") == 1:
        pair_type = pair_profile.get("pair_type", "none")
        if pair_type == "top_pair" and not pair_profile.get("weak_kicker", True):
            return "strong"
        # In HU NLHE, top pair with weak kicker is still value-bettable on river
        if pair_type == "top_pair" and pair_profile.get("weak_kicker", True):
            if made_strength >= 0.32:
                return "strong"
            return "medium"
        if pair_type == "overpair":
            return "strong"
        if pair_type in ("middle_pair", "pocket_pair"):
            if made_strength >= 0.28:
                return "medium"
            # Weak middle/pocket pair → likely behind, treat as weak
            return "weak"
        if pair_type in ("bottom_pair", "underpair", "board_pair"):
            return "weak"

    # Made strength fallback
    if made_strength >= 0.42:
        return "strong"
    elif made_strength >= 0.20:
        return "medium"

    return "weak"


def river_3branch_decision(
    state, my_chips, to_call, pot, win_rate, made_strength,
    value_profile, pair_profile, draw_info, board_texture,
    blocker_profile, spot_info, opponent_model, anti_bot4,
    match_profile, public_cards, paired_board_profile, nutted_risk,
):
    """3-branch river decision: STRONG→raise/bet, MEDIUM→call/check, WEAK→fold/bluff.
    
    Returns: action (int) or None if the existing flow should handle it.
    """
    if len(public_cards) < 5:
        return None
    
    branch = classify_river_hand(made_strength, value_profile, pair_profile, draw_info, board_texture, win_rate, public_cards)
    if branch == "none":
        return None
    
    min_raise_action = state.get("min_raise_action", state["round_raise"])
    pot_ratio = to_call / pot if pot > 0 else 0.0
    confidence = opponent_model.get("confidence", 0.0)
    fold_to_raise = opponent_model.get("fold_to_raise", 0.44)
    has_position = spot_info.get("has_position", False)
    
    # ========== STRONG BRANCH ==========
    if branch == "strong":
        if to_call > 0:
            # Facing a bet: raise for value (65-80% pot)
            # In HU NLHE, strong hands should almost always raise river
            risk = nutted_risk.get("risk", 0.0) if nutted_risk is not None else 0.0

            # Only call (not raise) if extreme nutted risk AND thin value
            is_thin = value_profile is not None and value_profile.get("tier") == "thin"
            if risk >= 0.15 and is_thin:
                return 0

            # Raise sizing: 65-80% pot
            raise_ratio = 0.65 + 0.15 * max(0.0, made_strength - 0.45)
            raise_ratio = min(raise_ratio, 0.80)
            raise_amount = int(to_call + (pot + to_call) * raise_ratio)
            raise_amount = max(min_raise_action, raise_amount)
            raise_amount = min(raise_amount, my_chips - 1)

            if raise_amount >= my_chips:
                return -2
            if raise_amount <= to_call or raise_amount < min_raise_action:
                return 0  # Can't raise, just call

            return raise_amount
        else:
            # Checked to us: bet for value (60-75% pot)
            bet_ratio = 0.60 + 0.15 * max(0.0, made_strength - 0.45)
            bet_ratio = min(bet_ratio, 0.75)
            bet_amount = int(pot * bet_ratio)
            bet_amount = max(min_raise_action, bet_amount)
            bet_amount = min(bet_amount, my_chips - 1)

            if bet_amount >= my_chips:
                return -2
            if bet_amount < min_raise_action:
                return 0

            return bet_amount
    
    # ========== MEDIUM BRANCH ==========
    if branch == "medium":
        if to_call > 0:
            # Call small bets (<40% pot), fold to large bets (>55% pot)
            if pot_ratio > 0.55:
                # Large bet: need good equity to continue
                if win_rate >= pot_ratio + 0.05:
                    return 0  # Call with good pot odds
                return -1
            elif pot_ratio < 0.35:
                # Small bet: call
                return 0
            else:
                # Medium bet (35-55% pot): call if equity justifies it
                if win_rate >= pot_ratio - 0.08:
                    return 0
                return -1
        else:
            # Checked to us: check it down (medium hands don't bet river)
            return 0
    
    # ========== WEAK BRANCH ==========
    if branch == "weak":
        if to_call > 0:
            # Fold to any bet with weak hands
            # Exception: very small bets in big pots (getting good odds)
            if pot_ratio <= 0.20 and win_rate >= 0.20:
                return 0  # Cheap call
            return -1
        else:
            # Checked to us: bluff if board is scary and we have blockers
            # Otherwise check
            bluff_eligible = False
            
            # Bluff with blocker
            if blocker_profile is not None and blocker_profile.get("eligible", False):
                bluff_eligible = True
            
            # Bluff on scary boards (flush/straight completed)
            if board_texture is not None:
                if board_texture.get("flush_pressure", 0) >= 0.75:
                    bluff_eligible = True
                if board_texture.get("straight_pressure", 0) >= 0.65:
                    bluff_eligible = True
            
            # Bluff with missed draws
            if draw_info is not None and draw_info.get("semi_bluff", False):
                bluff_eligible = True
            
            if not bluff_eligible:
                return 0  # Check
            
            # Need some fold equity
            if confidence < 0.20 or fold_to_raise <= 0.42:
                return 0
            
            # Bluff sizing: 50-60% pot
            bluff_ratio = 0.50 + 0.10 * (1.0 - made_strength / 0.22) if made_strength > 0 else 0.55
            bluff_ratio = clamp(bluff_ratio, 0.45, 0.60)
            bluff_amount = int(pot * bluff_ratio)
            bluff_amount = max(min_raise_action, bluff_amount)
            bluff_amount = min(bluff_amount, my_chips - 1)
            
            if bluff_amount >= my_chips:
                return 0  # Don't jam as a bluff
            if bluff_amount < min_raise_action:
                return 0
            
            # Anti-bot4 bonus: bluff more
            if anti_bot4 and anti_bot4.get("bluff_freq_bonus", 0) > 0.05:
                pass  # Proceed with bluff
            
            return bluff_amount
    
    return None

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
        ratio = 3.00 if to_call == 0 else 3.50
        target = int(to_call + pot_after_call * ratio)
        strength = preflop_strength if preflop_strength is not None else win_rate
        target = max(target, int((7.0 + max(0.0, strength - 0.50) * 3.5) * BIG_BLIND) - state["my_round_bet"])
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
