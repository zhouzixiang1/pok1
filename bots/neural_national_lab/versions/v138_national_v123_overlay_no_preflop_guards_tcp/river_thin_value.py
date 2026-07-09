import sys
from constants import BIG_BLIND
from card_utils import clamp


def _call_wide_score(opponent_model):
    """Continuous [0,1] score: how wide / check-back prone the opponent is on the river."""
    confidence = opponent_model.get("confidence", 0.0)
    if confidence <= 0.0:
        return 0.0
    river_overcall = opponent_model.get("river_overcall_freq", 0.55)
    vpip = opponent_model.get("vpip", 0.58)
    ftr = opponent_model.get("fold_to_raise", 0.44)
    postflop_check = opponent_model.get("postflop_check_rate", 0.42)
    base = 0.35
    overcall_dev = clamp((river_overcall - 0.55) / 0.20, -1.0, 1.0)
    vpip_dev = clamp((vpip - 0.58) / 0.16, -1.0, 1.0)
    ftr_dev = clamp((0.44 - ftr) / 0.16, -1.0, 1.0)
    check_dev = clamp((postflop_check - 0.42) / 0.16, -1.0, 1.0)
    score = base + 0.25 * overcall_dev + 0.20 * vpip_dev + 0.15 * ftr_dev + 0.10 * check_dev
    return clamp(score, 0.0, 1.0) * confidence


def _hand_extractability(value_profile, made_strength, draw_strength):
    if value_profile is None:
        return 0.0
    tier = value_profile.get("tier", "none")
    if tier == "none" or draw_strength >= 0.12:
        return 0.0
    if tier == "strong":
        return clamp(0.55 + 0.45 * (made_strength - 0.45) / 0.30, 0.0, 1.0)
    if tier == "thin":
        return clamp(0.40 + 0.60 * (made_strength - 0.35) / 0.25, 0.0, 1.0)
    return 0.0


def _board_safety(board_texture, nutted_risk, paired_board_profile):
    if board_texture is None:
        return 0.0
    risk = nutted_risk.get("risk", 0.0) if nutted_risk else 0.0
    if risk >= 0.08:
        return 0.0
    if paired_board_profile and paired_board_profile.get("severe", False):
        return 0.0
    wet = board_texture.get("wetness", 0.5)
    dynamic = 1.0 if board_texture.get("dynamic", False) else 0.0
    return clamp(1.0 - 0.50 * wet - 0.40 * dynamic - 2.0 * risk, 0.0, 1.0)


def river_thin_value_bet(
    round_idx, to_call, value_profile, made_strength, draw_strength,
    opponent_model, board_texture, nutted_risk, paired_board_profile,
    pot, my_chips, state, spot_info,
):
    """Return raise-to-total for a river thin-value bet, or None."""
    reason = "early_guard"
    composite = 0.0
    bet = None
    if round_idx != 3 or to_call != 0:
        reason = "not_river_or_facing_bet"
    elif value_profile is None or value_profile.get("tier", "none") not in ("thin", "strong"):
        reason = "hand_not_extractable"
    elif made_strength < 0.35 or draw_strength >= 0.12:
        reason = "strength_or_draw_guard"
    elif my_chips < max(1, int(pot * 0.35)):
        reason = "low_chips"
    else:
        call_wide = _call_wide_score(opponent_model)
        hand_score = _hand_extractability(value_profile, made_strength, draw_strength)
        safety = _board_safety(board_texture, nutted_risk, paired_board_profile)
        composite = call_wide * hand_score * safety
        if composite >= 0.06:
            ratio = clamp(0.35 + 0.10 * hand_score + 0.05 * safety, 0.35, 0.55)
            amount = int(pot * ratio)
            min_raise = state.get("min_raise_action", BIG_BLIND)
            my_round_bet = state.get("my_round_bet", 0)
            if amount < min_raise:
                amount = max(min_raise, BIG_BLIND)
            raise_to_total = my_round_bet + amount
            if amount >= my_chips:
                reason = "allin_boundary"
            else:
                bet = raise_to_total
                reason = "fired"
        else:
            reason = "below_threshold"
    sys.stderr.write(
        f"RIVER_THIN_VALUE_FINAL reason={reason} "
        f"composite={composite:.3f} bet={bet}\n"
    )
    return bet


if __name__ == "__main__":
    opp = {
        "confidence": 0.5,
        "river_overcall_freq": 0.55,
        "vpip": 0.58,
        "fold_to_raise": 0.44,
        "postflop_check_rate": 0.42,
    }
    vp = {"tier": "thin"}
    texture = {"wetness": 0.2, "dynamic": False}
    state = {"min_raise_action": 100, "my_round_bet": 0}
    bet = river_thin_value_bet(
        3, 0, vp, 0.45, 0.0, opp, texture, {"risk": 0.0}, {"severe": False},
        2000, 18000, state, {},
    )
    assert bet is not None and bet > 0, f"expected default bet, got {bet}"
    print(f"RIVER_THIN_VALUE_SELF_TEST OK bet={bet}")
