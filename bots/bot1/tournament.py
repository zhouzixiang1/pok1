from constants import N_PLAYERS, BIG_BLIND, LOCK_WIN_MARGIN
from card_utils import clamp
from state import reconstruct_state, get_remaining_hands, forced_fold_loss_bound


def should_lock_win(req, state, my_id):
    total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
    if len(total_win_chips) <= my_id:
        return False

    try:
        lead = int(total_win_chips[my_id])
    except (TypeError, ValueError):
        return False

    remaining_hands = get_remaining_hands(req)
    if remaining_hands is None:
        return lead >= LOCK_WIN_MARGIN

    max_forced_loss = forced_fold_loss_bound(req, state, my_id, remaining_hands)
    if max_forced_loss is None:
        return lead >= LOCK_WIN_MARGIN
    return lead > max_forced_loss


def opponent_can_lock_win(req, my_id):
    opponent_id = (my_id + 1) % N_PLAYERS
    total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
    if len(total_win_chips) <= opponent_id:
        return False

    try:
        lead = int(total_win_chips[opponent_id])
    except (TypeError, ValueError):
        return False

    remaining_hands = get_remaining_hands(req)
    if remaining_hands is None:
        return lead >= LOCK_WIN_MARGIN

    state = reconstruct_state(req)
    max_forced_loss = forced_fold_loss_bound(req, state, opponent_id, remaining_hands)
    if max_forced_loss is None:
        return lead >= LOCK_WIN_MARGIN
    return lead > max_forced_loss + BIG_BLIND


def fold_gives_opponent_lock(req, state, my_id):
    opponent_id = (my_id + 1) % N_PLAYERS
    total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
    if len(total_win_chips) <= opponent_id:
        return False

    remaining_hands = get_remaining_hands(req)
    if remaining_hands is None or remaining_hands <= 1:
        return False

    try:
        opponent_lead = int(total_win_chips[opponent_id])
    except (TypeError, ValueError):
        return False

    opponent_lead_after_fold = opponent_lead + state["committed"][my_id]
    max_forced_loss = forced_fold_loss_bound(req, state, opponent_id, remaining_hands)
    if max_forced_loss is None:
        return opponent_lead_after_fold >= LOCK_WIN_MARGIN

    future_forced_loss = max(0, max_forced_loss - state["committed"][opponent_id])
    return opponent_lead_after_fold > future_forced_loss


def match_risk_adjustment(req, my_id, remaining_hands):
    total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
    if len(total_win_chips) <= my_id:
        return 0.0
    if remaining_hands is None or remaining_hands <= 0:
        return 0.0

    lead = total_win_chips[my_id]
    scale = max(1.0, remaining_hands * BIG_BLIND * 5.0)
    if lead >= 0:
        return min(0.05, lead / scale)
    return -min(0.05, (-lead) / (scale * 0.85))


def match_pressure_profile(req, my_id, remaining_hands):
    profile = {
        "protect": 0.0,
        "chase": 0.0,
        "threshold_delta": 0.0,
        "sizing_delta": 0.0,
        "open_delta": 0.0,
        "bluff_delta": 0.0,
    }

    total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
    if len(total_win_chips) <= my_id or remaining_hands is None or remaining_hands <= 0:
        return profile

    try:
        lead = int(total_win_chips[my_id])
    except (TypeError, ValueError):
        return profile

    hands = max(1, int(remaining_hands))
    late_factor = clamp((12 - hands) / 10.0, 0.0, 1.0)
    if late_factor <= 0.0:
        return profile

    behind_per_hand = max(0.0, -lead) / hands
    ahead_per_hand = max(0.0, lead) / hands

    chase = clamp((behind_per_hand - 0.8 * BIG_BLIND) / (3.5 * BIG_BLIND), 0.0, 1.0)
    protect = clamp((ahead_per_hand - 0.8 * BIG_BLIND) / (3.0 * BIG_BLIND), 0.0, 1.0)
    chase *= late_factor
    protect *= late_factor
    if not (hands <= 8 and lead < -8 * BIG_BLIND):
        chase = min(chase, 0.25)

    profile["protect"] = protect
    profile["chase"] = chase
    profile["threshold_delta"] = 0.055 * protect - 0.055 * chase
    profile["sizing_delta"] = -0.10 * protect + 0.16 * chase
    profile["open_delta"] = 0.020 * protect - 0.030 * chase
    profile["bluff_delta"] = -0.08 * protect + 0.10 * chase
    return profile


def apply_anti_lock_pressure(match_profile):
    match_profile["protect"] = 0.0
    match_profile["chase"] = max(match_profile["chase"], 0.90)
    match_profile["threshold_delta"] = min(match_profile["threshold_delta"], -0.075)
    match_profile["sizing_delta"] = max(match_profile["sizing_delta"], 0.18)
    match_profile["open_delta"] = min(match_profile["open_delta"], -0.045)
    match_profile["bluff_delta"] = max(match_profile["bluff_delta"], 0.13)
    return match_profile


def anti_lock_continue_floor(pot_odds, round_idx, value_profile, draw_info, made_strength):
    discount = 0.07 + 0.025 * max(0, round_idx)

    if value_profile is not None:
        if value_profile.get("tier") == "nut":
            discount += 0.12
        elif value_profile.get("tier") == "strong":
            discount += 0.08
        elif value_profile.get("tier") == "thin":
            discount += 0.035

    if draw_info is not None:
        if draw_info.get("type") in ("combo_draw", "nut_flush_draw"):
            discount += 0.05
        elif draw_info.get("semi_bluff"):
            discount += 0.03

    if made_strength < 0.18 and (draw_info is None or draw_info.get("quality", 0.0) < 0.08):
        discount -= 0.04

    return max(0.08, pot_odds - discount)


def anti_lock_can_continue(anti_lock_pressure, win_rate, pot_odds, round_idx, value_profile, draw_info, made_strength):
    if not anti_lock_pressure:
        return False
    return win_rate >= anti_lock_continue_floor(pot_odds, round_idx, value_profile, draw_info, made_strength)
