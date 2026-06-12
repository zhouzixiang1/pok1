from constants import (
    N_PLAYERS, BIG_BLIND, LOCK_WIN_MARGIN,
    LATE_HANDS_THRESHOLD, LATE_HANDS_SCALE,
    CHASE_BB_SCALE, CHASE_BB_OFFSET,
    PROTECT_BB_SCALE, PROTECT_BB_OFFSET,
    CHASE_DESPERATE_HANDS, CHASE_DESPERATE_BB, CHASE_MAX_NORMAL,
    PROTECT_THRESHOLD_DELTA, CHASE_THRESHOLD_DELTA,
    PROTECT_SIZING_DELTA, CHASE_SIZING_DELTA,
    PROTECT_OPEN_DELTA, CHASE_OPEN_DELTA,
    PROTECT_BLUFF_DELTA, CHASE_BLUFF_DELTA,
    ANTI_LOCK_CHASE, ANTI_LOCK_THRESHOLD,
    ANTI_LOCK_SIZING, ANTI_LOCK_OPEN, ANTI_LOCK_BLUFF,
    PASSIVE_AGGR_MAX, PASSIVE_VPIP_MIN, PASSIVE_BARREL_MAX, PASSIVE_CONFIDENCE_GATE,
    PRIOR_POSTFLOP_AGGR, PRIOR_VPIP, PRIOR_BARREL_FREQ, PRIOR_FLOP_AGGR,
    LIGHT_4BET_MIN_CONFIDENCE, LIGHT_4BET_MIN_OPP_PFR, LIGHT_4BET_MAX_OPP_4BET,
    LIGHT_4BET_STRENGTH_LOW, LIGHT_4BET_STRENGTH_HIGH, LIGHT_4BET_FREQ_ROLL_CAP,
    LIGHT_4BET_SIZE_MULT, LIGHT_4BET_STACK_CAP, LIGHT_4BET_HALF_STACK_CAP,
    TRAP_MIN_CONFIDENCE, TRAP_MIN_AGGR, TRAP_MAX_WETNESS, TRAP_FREQ_CAP,
)
from card_utils import clamp, next_player
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
    opponent_id = next_player(my_id, 1)
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
    opponent_id = next_player(my_id, 1)
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
    late_factor = clamp((LATE_HANDS_THRESHOLD - hands) / LATE_HANDS_SCALE, 0.0, 1.0)
    if late_factor <= 0.0:
        return profile

    behind_per_hand = max(0.0, -lead) / hands
    ahead_per_hand = max(0.0, lead) / hands

    chase = clamp((behind_per_hand - CHASE_BB_OFFSET * BIG_BLIND) / (CHASE_BB_SCALE * BIG_BLIND), 0.0, 1.0)
    protect = clamp((ahead_per_hand - PROTECT_BB_OFFSET * BIG_BLIND) / (PROTECT_BB_SCALE * BIG_BLIND), 0.0, 1.0)
    chase *= late_factor
    protect *= late_factor
    if not (hands <= CHASE_DESPERATE_HANDS and lead < -CHASE_DESPERATE_BB * BIG_BLIND):
        chase = min(chase, CHASE_MAX_NORMAL)

    profile["protect"] = protect
    profile["chase"] = chase
    profile["threshold_delta"] = PROTECT_THRESHOLD_DELTA * protect - CHASE_THRESHOLD_DELTA * chase
    profile["sizing_delta"] = PROTECT_SIZING_DELTA * protect + CHASE_SIZING_DELTA * chase
    profile["open_delta"] = PROTECT_OPEN_DELTA * protect + CHASE_OPEN_DELTA * chase
    profile["bluff_delta"] = PROTECT_BLUFF_DELTA * protect + CHASE_BLUFF_DELTA * chase
    return profile


def apply_anti_lock_pressure(match_profile):
    match_profile["protect"] = 0.0
    match_profile["chase"] = max(match_profile["chase"], ANTI_LOCK_CHASE)
    match_profile["threshold_delta"] = min(match_profile["threshold_delta"], ANTI_LOCK_THRESHOLD)
    match_profile["sizing_delta"] = max(match_profile["sizing_delta"], ANTI_LOCK_SIZING)
    match_profile["open_delta"] = min(match_profile["open_delta"], ANTI_LOCK_OPEN)
    match_profile["bluff_delta"] = max(match_profile["bluff_delta"], ANTI_LOCK_BLUFF)
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


def _is_passive_opponent(opponent_model, confidence_gate=PASSIVE_CONFIDENCE_GATE):
    if opponent_model["confidence"] < confidence_gate:
        return False
    post_aggr = opponent_model.get("postflop_aggr", PRIOR_POSTFLOP_AGGR)
    vpip = opponent_model.get("vpip", PRIOR_VPIP)
    barrel = opponent_model.get("barrel_freq", PRIOR_BARREL_FREQ)
    return post_aggr <= PASSIVE_AGGR_MAX and vpip >= PASSIVE_VPIP_MIN and barrel <= PASSIVE_BARREL_MAX


def _is_fourbet_light_candidate(my_cards):
    from state import preflop_hand_profile
    profile = preflop_hand_profile(my_cards)
    high = profile["high"]
    low = profile["low"]
    suited = profile["suited"]
    pair = profile["pair"]
    gap = high - low

    if pair and high <= 4:
        return True
    if suited and gap == 1 and low >= 4 and high <= 11:
        return True
    if suited and gap == 2 and low >= 4 and high <= 11:
        return True
    if suited and high == 14 and low >= 2 and low <= 5:
        return True

    return False


def _should_4bet_light(my_cards, preflop_strength, opponent_model, state, my_chips):
    if state.get("opponent_allin", False):
        return 0

    confidence = opponent_model.get("confidence", 0.0)
    opp_pfr = opponent_model.get("pfr", 0.28)

    if confidence < LIGHT_4BET_MIN_CONFIDENCE or opp_pfr < LIGHT_4BET_MIN_OPP_PFR:
        return 0

    opp_4bet = opponent_model.get("four_bet_freq", 0.0)
    if opp_4bet >= LIGHT_4BET_MAX_OPP_4BET:
        return 0

    if not _is_fourbet_light_candidate(my_cards):
        return 0

    if preflop_strength < LIGHT_4BET_STRENGTH_LOW or preflop_strength >= LIGHT_4BET_STRENGTH_HIGH:
        return 0

    # [v44 mutation] Game-state entropy instead of deterministic hash
    freq_roll = ((hash(tuple(my_cards)) * 31 + hash(my_chips)) % 100) / 100.0
    if freq_roll >= LIGHT_4BET_FREQ_ROLL_CAP:
        return 0

    opp_3bet_total = state["round_bet"]
    fourbet_target = int(opp_3bet_total * LIGHT_4BET_SIZE_MULT)

    min_raise = state.get("min_raise_action", state.get("round_raise", 0))
    fourbet_target = max(fourbet_target, min_raise)

    if fourbet_target > my_chips * LIGHT_4BET_STACK_CAP:
        return 0

    if fourbet_target >= my_chips * LIGHT_4BET_HALF_STACK_CAP:
        return 0

    return fourbet_target


def _should_checkraise_trap(value_profile, round_idx, board_texture, opponent_model, my_cards, public_cards):
    if round_idx != 1:
        return False

    if value_profile is None or value_profile.get("tier") not in ("strong", "nut"):
        return False

    if board_texture is None:
        return False
    if board_texture.get("dynamic", False):
        return False
    if board_texture.get("wetness", 0.0) > TRAP_MAX_WETNESS:
        return False
    if board_texture.get("paired", False):
        return False

    confidence = opponent_model.get("confidence", 0.0)
    if confidence < TRAP_MIN_CONFIDENCE:
        return False

    flop_aggr = opponent_model.get("flop_aggr", PRIOR_FLOP_AGGR)
    postflop_aggr = opponent_model.get("postflop_aggr", PRIOR_POSTFLOP_AGGR)
    effective_aggr = max(flop_aggr, postflop_aggr)
    if effective_aggr < TRAP_MIN_AGGR:
        return False

    seed = (sum(my_cards) * 7 + sum(public_cards) * 13) % 100
    if seed >= TRAP_FREQ_CAP:
        return False

    return True
