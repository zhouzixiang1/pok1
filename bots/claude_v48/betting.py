from card_utils import clamp
from constants import BIG_BLIND, TOTAL_HANDS


def bet_size_bucket(last_raise_pot_ratio):
    if last_raise_pot_ratio <= 0.30:
        return "small"
    if last_raise_pot_ratio <= 0.75:
        return "medium"
    return "large"


def opponent_pressure_adjustment(opponent_model, spot_info, round_idx):
    confidence = opponent_model["confidence"]
    adjustment = 0.0

    if spot_info["facing_raise"] or spot_info["facing_allin"]:
        adjustment += confidence * max(0.0, 0.44 - opponent_model["pfr"]) * 0.07
        if round_idx > 0:
            adjustment += confidence * max(0.0, 0.36 - opponent_model["postflop_aggr"]) * 0.06
        adjustment -= confidence * max(0.0, opponent_model["allin_rate"] - 0.08) * 0.08
        adjustment -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.48) * 0.05
        adjustment += min(0.04, spot_info["last_raise_pot_ratio"] * 0.04)

    return clamp(adjustment, -0.05, 0.07)


def aggressive_line_strength(spot_info, board_texture):
    strength = 0.0
    if spot_info.get("opp_postflop_bet_count", 0) >= 2:
        strength += 0.04
    if spot_info.get("opp_current_round_bet_count", 0) >= 2:
        strength += 0.08 if board_texture is not None and board_texture["paired"] else 0.05
    if spot_info.get("opp_current_round_bet_count", 0) >= 3:
        strength += 0.03
    return clamp(strength, 0.0, 0.15)


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


def paired_board_stackoff_profile(pair_profile, paired_board_profile, board_texture, spot_info, round_idx):
    info = {
        "active": False,
        "severe": False,
        "line_strength": 0.0,
        "size_bucket": "small",
    }

    if round_idx <= 0 or board_texture is None or not board_texture["paired"]:
        return info

    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])
    line_strength = 0.0
    active = False

    if paired_board_profile is not None and paired_board_profile["board_two_pair"]:
        active = True
        line_strength += 0.05
    elif pair_profile is not None and pair_profile["pair_type"] == "overpair":
        active = True
        line_strength += 0.04

    if not active:
        return info

    if spot_info["facing_postflop_aggression"]:
        line_strength += 0.03
    if spot_info.get("opp_current_round_bet_count", 0) >= 2:
        line_strength += 0.08
    elif size_bucket in ("medium", "large"):
        line_strength += 0.04
    if round_idx >= 2:
        line_strength += 0.02

    info["active"] = True
    info["severe"] = (
        spot_info["facing_postflop_aggression"]
        and spot_info.get("opp_current_round_bet_count", 0) >= 2
        and size_bucket in ("medium", "large")
    )
    info["line_strength"] = clamp(line_strength, 0.0, 0.18)
    info["size_bucket"] = size_bucket
    return info


def postflop_call_margin(spot_info, opponent_model, made_strength, draw_strength, round_idx, has_position):
    if round_idx <= 0:
        return 0.0

    margin = 0.0
    air_hand = made_strength < 0.18 and draw_strength < 0.08
    weak_showdown = made_strength < 0.22
    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])

    if weak_showdown:
        margin += 0.012
    if air_hand:
        margin += 0.020

    if spot_info["facing_postflop_aggression"]:
        margin += 0.008
        if size_bucket == "small":
            margin += 0.015
        elif size_bucket == "medium":
            margin += 0.010
        else:
            margin += 0.025

        if spot_info.get("opp_postflop_bet_count", 0) >= 2:
            margin += 0.024 if size_bucket == "small" else 0.014
        if round_idx >= 2 and air_hand:
            margin += 0.010
        if round_idx == 3 and size_bucket == "large":
            margin += 0.022
        if round_idx == 3 and weak_showdown and size_bucket == "medium":
            margin += 0.010

    if not has_position:
        margin += 0.008

    confidence = opponent_model["confidence"]
    if air_hand:
        margin -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.50) * 0.015
    else:
        margin -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.50) * 0.008

    return clamp(margin, 0.0, 0.055)


def realized_postflop_equity(
    win_rate,
    made_strength,
    draw_strength,
    round_idx,
    has_position,
    spot_info,
    pair_profile=None,
):
    air_hand = made_strength < 0.18 and draw_strength < 0.08
    if round_idx <= 0:
        return win_rate

    eqr = 1.0

    if air_hand:
        eqr = 0.50 if has_position else 0.38

        if spot_info.get("opp_postflop_bet_count", 0) >= 2:
            eqr -= 0.18
        if round_idx == 2:
            eqr -= 0.10
        elif round_idx == 3:
            eqr -= 0.22

        eqr = clamp(eqr, 0.35, 0.72)
        return win_rate * eqr

    if pair_profile is not None and pair_profile["made_class"] == 1:
        pair_type = pair_profile["pair_type"]

        if pair_type in ("middle_pair", "bottom_pair", "underpair", "board_pair"):
            eqr = 0.80 if has_position else 0.70

            if pair_profile["weak_kicker"]:
                eqr -= 0.09
            if spot_info.get("opp_postflop_bet_count", 0) >= 2:
                eqr -= 0.12
            if round_idx == 3:
                eqr -= 0.15

            eqr = clamp(eqr, 0.50, 0.85)
            return win_rate * eqr

        if pair_type == "top_pair" and pair_profile["weak_kicker"]:
            eqr = 0.89 if has_position else 0.82
            if spot_info.get("opp_postflop_bet_count", 0) >= 2:
                eqr -= 0.04
            eqr = clamp(eqr, 0.75, 0.95)
            return win_rate * eqr

    return win_rate


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
    """River overbet bluff with strong blockers on dry-ish boards."""
    # Only when we can bet (not facing a bet already)
    if to_call > 0:
        return None
    # Need sufficient pot size to make overbet worthwhile
    if pot < 300:
        return None
    # Need strong blockers (nut flush blocker, etc.)
    if blocker_profile is None or not blocker_profile.get("eligible", False):
        return None
    # Wet boards have too many value combos opponent can call with
    if board_texture is not None and board_texture["wetness"] > 0.40:
        return None
    # Paired boards are risky for overbet bluffs
    if board_texture is not None and board_texture["paired"]:
        return None
    # Opponent must be fold-prone enough to make this +EV
    confidence = opponent_model.get("confidence", 0.0)
    fold_to_raise = opponent_model.get("fold_to_raise", 0.44)
    if confidence < 0.20:
        return None
    if fold_to_raise < 0.44:
        return None
    # Blocker quality — only bluff with high-quality blockers
    blocker_score = blocker_profile.get("score", 0.0)
    if blocker_score < 0.10:
        return None

    # Size: 1.2-1.6x pot overbet, scaled by fold tendency
    ratio = 1.2 + 0.3 * confidence * max(0.0, fold_to_raise - 0.42)
    if not spot_info.get("has_position", False):
        ratio = max(1.1, ratio - 0.15)
    ratio = min(ratio, 1.6)
    amount = int(pot * ratio)

    if amount >= my_chips:
        return -2
    amount = min(amount, my_chips - 1)
    if amount < min_raise:
        return None
    return amount


def must_continue_vs_raise(value_profile, made_strength, pot_odds, nutted_risk, board_texture):
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
    return False


def _adaptive_aggression_delta(opponent_model):
    """Compute fold threshold adjustment based on opponent postflop aggression.

    Returns a delta applied to fold thresholds:
    - Positive delta: fold MORE readily (vs aggressive opponents who barrel wide)
    - Negative delta: fold LESS readily (vs passive opponents, call more liberally)

    Scaled by model confidence to avoid adjusting with insufficient data.
    Uses graduated scaling: the further postflop_aggr is from the 0.4-0.6 neutral
    band, the larger the adjustment.
    """
    if opponent_model is None:
        return 0.0
    confidence = opponent_model.get("confidence", 0.0)
    if confidence < 0.15:
        return 0.0
    postflop_aggr = opponent_model.get("postflop_aggr", 0.36)
    if postflop_aggr > 0.6:
        # Aggressive opponent → fold more: raise fold thresholds
        return clamp(confidence * (postflop_aggr - 0.6) * 1.5, 0.0, 0.10)
    elif postflop_aggr < 0.4:
        # Passive opponent → call more: lower fold thresholds
        return clamp(-confidence * (0.4 - postflop_aggr) * 1.0, -0.06, 0.0)
    return 0.0


def should_fold_postflop(
    made_strength,
    draw_strength,
    round_idx,
    spot_info,
    board_texture,
    pair_profile,
    value_profile,
    pot_odds,
    blocker_profile=None,
    opponent_model=None,
):
    """Board-texture-aware postflop fold detection layer with adaptive thresholds.

    Identifies categories of holdings that should fold to postflop bets.
    ALL fold categories use ADAPTIVE thresholds based on opponent postflop aggression:
    - Aggressive opponents (postflop_aggr > 0.6): raise fold thresholds (fold more)
      Scales with both confidence and aggression degree. Aggressive opponents barrel
      wide ranges, so our weak holdings are unprofitable calling stations.
    - Passive opponents (postflop_aggr < 0.4): lower fold thresholds (call more)
      Passive opponents rarely apply pressure, so marginal hands retain more value.

    The adaptive delta is computed via _adaptive_aggression_delta() and applied
    consistently across all categories, converting static fold decisions into
    dynamic exploitative ones.

    Returns True if the hand should fold, False otherwise.
    """
    if round_idx <= 0:
        return False

    # --- Adaptive fold threshold based on opponent aggression ---
    aggr_delta = _adaptive_aggression_delta(opponent_model)

    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])
    air_hand = made_strength < 0.18 and draw_strength < 0.08
    has_blocker = blocker_profile is not None and blocker_profile.get("eligible", False)

    # --- Category 0: Air hands on flop facing bets ---
    # Flop air with no draw and no blocker should fold to medium/large bets.
    # Also fold air to small bets on wet flops (board_texture wetness >= 0.35).
    # Adaptive: vs aggressive, also fold air to small bets on drier flops.
    if round_idx == 1 and air_hand and not has_blocker:
        if size_bucket in ("medium", "large"):
            return True
        if size_bucket == "small" and board_texture is not None:
            # Lower the wetness threshold vs aggressive opponents
            effective_wetness = max(0.15, 0.35 - aggr_delta * 5.0)
            if board_texture["wetness"] >= effective_wetness:
                return True

    # --- Category 1: Air hands on turn/river facing medium+ bets ---
    # Air hands have no showdown value and no meaningful draws.
    # Calling is a pure donation unless we have blockers for bluff-raising.
    # Also fold air to small bets on river when no draw and no blocker.
    if air_hand and round_idx >= 2 and not has_blocker:
        if size_bucket in ("medium", "large"):
            return True
        if round_idx == 3 and size_bucket == "small":
            return True

    # --- Category 2: Weak made hands on turn/river facing bets ---
    # Bottom pair, underpair, board pair are dominated when opponent shows
    # aggression with meaningful sizing.
    # Adaptive: adjust draw equity threshold and EV threshold by aggr_delta.
    if round_idx >= 2 and pair_profile is not None and pair_profile["made_class"] == 1:
        pair_type = pair_profile["pair_type"]
        is_weak_pair = pair_type in ("bottom_pair", "underpair", "board_pair")
        is_weak_middle = pair_type == "middle_pair" and pair_profile["weak_kicker"]

        if (is_weak_pair or is_weak_middle) and size_bucket in ("medium", "large"):
            # Higher draw threshold vs aggressive (fold even with slightly more draw equity)
            effective_draw_threshold = 0.16 + aggr_delta * 0.5
            if draw_strength < effective_draw_threshold:
                call_equity = max(0.10, made_strength + draw_strength)
                ev = call_equity - pot_odds
                # Raise EV threshold vs aggressive (fold more easily)
                ev_threshold = -0.01 + aggr_delta * 0.5
                if ev < ev_threshold:
                    return True

    # --- Category 3: Extreme multi-street aggression on wet boards ---
    # When opponent barrels multiple streets on wet/dynamic boards with large sizing,
    # any non-nut holding faces severe risk of being value-owned.
    # Adaptive: require more draw equity to continue vs aggressive; less vs passive.
    if round_idx >= 2 and spot_info.get("opp_current_round_bet_count", 0) >= 2:
        if board_texture is not None and board_texture["dynamic"] and size_bucket == "large":
            has_nut = value_profile is not None and value_profile["tier"] == "nut"
            effective_draw_threshold = 0.16 + aggr_delta * 0.3
            has_strong_draw = draw_strength >= effective_draw_threshold
            if not has_nut and not has_strong_draw:
                effective_made_threshold = 0.58 + aggr_delta * 0.5
                if made_strength < effective_made_threshold:
                    return True

    # --- Category 4: River weak hands ---
    # On river facing any bet, very weak pairs (bottom/under/board pair)
    # with no draw equity should fold.
    # Adaptive: expand made_strength threshold vs aggressive opponents.
    effective_cat4_made = 0.30 + aggr_delta * 0.5
    if round_idx == 3 and made_strength < effective_cat4_made and draw_strength < 0.08:
        if pair_profile is not None and pair_profile["pair_type"] in ("bottom_pair", "underpair", "board_pair"):
            return True
    # --- Category 5: Middle pair on river ---
    # Counter-aggression guard: if opponent is barrel-heavy (≥3 postflop bets),
    # raise threshold to avoid over-folding to pressure.
    # Adaptive: shift threshold by aggr_delta.
    _cat5_made_threshold = 0.45 if spot_info.get("opp_postflop_bet_count", 0) >= 3 else 0.38
    _cat5_made_threshold += aggr_delta * 0.5
    if (round_idx == 3 and pair_profile and pair_profile["made_class"] == 1
            and pair_profile["pair_type"] == "middle_pair"
            and size_bucket in ("medium", "large") and draw_strength < 0.10
            and not has_blocker and made_strength < _cat5_made_threshold
            and (not value_profile or value_profile["tier"] not in ("strong", "nut"))):
        return True
    # --- Category 6: Near-air on turn ---
    # Counter-aggression guard: if opponent is barrel-heavy (≥3 postflop bets),
    # skip this fold — opponent may be over-barreling with wide range.
    # Adaptive: expand upper bound vs aggressive opponents.
    effective_cat6_upper = 0.26 + aggr_delta * 0.5
    if (round_idx == 2 and 0.18 <= made_strength < effective_cat6_upper and draw_strength < 0.10
            and size_bucket == "large" and not has_blocker
            and (not value_profile or value_profile["tier"] == "none")
            and spot_info.get("opp_postflop_bet_count", 0) < 3):
        return True
    # --- Category 7: Adaptive exploitation fold ---
    # Against highly aggressive opponents (postflop_aggr > 0.6), weak pairs on
    # the river that barely escape the static categories should still fold.
    # These hands are unprofitable calling stations vs wide-barreling opponents.
    if aggr_delta > 0.01 and round_idx == 3 and not has_blocker:
        if (pair_profile is not None and pair_profile["made_class"] == 1
                and pair_profile["pair_type"] in ("bottom_pair", "underpair", "board_pair")
                and draw_strength < 0.08
                and made_strength < 0.32 + aggr_delta * 0.5
                and (value_profile is None or value_profile["tier"] not in ("strong", "nut"))):
            return True
    return False


def check_probe_resistance_margin(spot_info, opponent_model, round_idx):
    if round_idx <= 0 or not spot_info["facing_postflop_aggression"]:
        return 0.0

    margin = 0.0
    same_street_check_raise = (
        spot_info.get("opp_current_round_check_count", 0) > 0
        and spot_info.get("opp_current_round_bet_count", 0) > 0
    )
    delayed_resistance = (
        spot_info.get("opp_prior_postflop_check_count", 0) >= 2
        and spot_info.get("opp_current_round_bet_count", 0) > 0
    )

    if same_street_check_raise:
        margin += 0.035
    if delayed_resistance:
        margin += 0.018

    confidence = opponent_model.get("confidence", 0.0)
    if opponent_model.get("postflop_check_rate", 0.42) >= 0.52:
        margin += confidence * 0.018

    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])
    if size_bucket == "large":
        margin += 0.020
    elif size_bucket == "medium":
        margin += 0.010

    return clamp(margin, 0.0, 0.085)
