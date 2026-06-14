from constants import N_PLAYERS, BIG_BLIND, TOTAL_HANDS, SIMULATIONS_BY_PUBLIC_COUNT, EXTRA_SIMULATIONS_BY_PUBLIC_COUNT
from card_utils import clamp, next_player
from state import (
    reconstruct_state, get_remaining_hands, estimate_preflop_strength,
    is_preflop_3bet_candidate, is_preflop_trash_hand,
    preflop_hand_profile,
)
from tournament import (
    should_lock_win, fold_gives_opponent_lock, match_risk_adjustment,
    match_pressure_profile, apply_anti_lock_pressure, anti_lock_can_continue,
)
from opponent import build_opponent_model, analyze_current_spot
from postflop import (
    made_hand_metric, pair_board_profile, pair_domination_margin,
    marginal_pair_under_pressure, board_texture_profile,
    paired_board_outcome_profile, bet_size_bucket, value_hand_tier,
    value_bet_plan, empty_draw_profile, draw_profile,
    draw_call_margin, made_flush_profile, blocker_bluff_profile,
    allow_low_frequency_blocker_bluff, nutted_risk_profile,
    check_probe_resistance_margin, must_continue_vs_raise,
)
from simulation import build_opponent_range, estimate_weighted_win_rate
# v27 exploits: overbet/donk/probe target passive opponents (v62/v30/v78).
from overbet import should_overbet, overbet_sizing
from donk_probe import should_donk_bet, should_probe_bet, donk_probe_sizing
from passive_exploit import passive_exploit_trigger, passive_exploit_sizing
from line_reading import line_polarization_profile
from strategy_helpers import (
    _per_street_diverges, _aligned_signal_boost,
    opponent_pressure_adjustment, aggressive_line_strength,
    postflop_call_margin, realized_postflop_equity,
    sizing_exploit_adjustment,
)


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
    sizing_exploit_delta=0.0,
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
    ratio += sizing_exploit_delta
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
    ratio = clamp(ratio, low_ratio, 1.45)

    amount = int(to_call + pot_after_call * ratio)

    if round_idx == 0 and preflop_strength is not None:
        if spot_name == "sb_open":
            desired_total = int((2.5 + max(0.0, preflop_strength - 0.58) * 2.2) * BIG_BLIND)
            amount = max(amount, desired_total - my_round_bet)
        elif spot_name == "bb_vs_limp":
            desired_total = int((3.2 + max(0.0, preflop_strength - 0.60) * 2.2) * BIG_BLIND)
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


def _is_fourbet_light_candidate(my_cards):
    """Check if the hand is a good candidate for a light 4-bet.

    Suitable hands have strong postflop playability and blocker value,
    but are not strong enough for a value 4-bet:
    - Small pairs 22-44: set-mine potential, easy to play postflop
    - Suited connectors 45s-JTs: excellent postflop playability
    - Suited one-gappers 46s-9Js: good connectivity and flush potential
    - Suited A2s-A5s: ace blocker + wheel straight draw potential
    """
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
    """Determine whether to make a light 4-bet and return the sizing.

    Returns a raise-to total (int) if a light 4-bet is appropriate, or 0 otherwise.

    Exploits opponents who 3-bet too wide by re-raising with hands that have
    good postflop playability but aren't strong enough to 4-bet for value.
    Sizing is ~2.5x the opponent's 3-bet, capped at 25% of effective stack.
    """
    if state.get("opponent_allin", False):
        return 0

    confidence = opponent_model.get("confidence", 0.0)
    opp_pfr = opponent_model.get("pfr", 0.28)
    opp_fold_to_raise = opponent_model.get("fold_to_raise", 0.45)

    if confidence < 0.15 or opp_pfr < 0.25:
        return 0

    if not _is_fourbet_light_candidate(my_cards):
        return 0

    if preflop_strength < 0.30 or preflop_strength >= 0.55:
        return 0

    # Mutation: increased activation frequency from 60% to 70% (threshold 0.60 → 0.70)
    freq_roll = (hash(tuple(my_cards)) % 100) / 100.0
    if freq_roll >= 0.70:
        return 0

    opp_3bet_total = state["round_bet"]
    fourbet_target = int(opp_3bet_total * 2.5)

    min_raise = state.get("min_raise_action", state.get("round_raise", 0))
    fourbet_target = max(fourbet_target, min_raise)

    if fourbet_target > my_chips * 0.25:
        return 0

    if fourbet_target >= my_chips * 0.50:
        return 0

    return fourbet_target


def choose_preflop_spot_action(req, state, spot_info, opponent_model, preflop_strength, win_rate, match_profile):
    my_chips = req["my_chips"]
    to_call = state["to_call"]
    match_adjust = match_risk_adjustment(req, req["my_id"], get_remaining_hands(req))
    confidence = opponent_model["confidence"]
    loose_bonus = confidence * max(0.0, opponent_model["vpip"] - 0.55) * 0.03
    trash_hand = is_preflop_trash_hand(req["my_cards"], preflop_strength)

    if spot_info["preflop_spot"] == "sb_open":
        open_threshold = 0.46 + match_adjust + 0.02 + match_profile["open_delta"]
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

    elif spot_info['preflop_spot'] == 'bb_vs_raise':
        pot_odds_pf = to_call / (to_call + state['pot']) if to_call > 0 else 0.0
        # Value 3bet: TT+, AK, AQs (strength >= 0.60)
        if preflop_strength >= 0.60 and not trash_hand:
            raise_amount = choose_raise(
                state['min_raise_action'], my_chips, state['my_round_bet'],
                to_call, state['pot'], max(win_rate, preflop_strength),
                0, 'bb_vs_raise', preflop_strength,
                True, opponent_model,
                match_sizing_delta=match_profile['sizing_delta'],
            )
            if raise_amount is not None:
                return raise_amount
            return 0  # Call if 3bet sizing fails
        # Bluff 3bet: medium suited/connected hands, ~25% frequency
        if 0.38 <= preflop_strength <= 0.54 and not trash_hand:
            bluff_roll = (hash(tuple(req['my_cards'])) % 100) / 100.0
            if bluff_roll < 0.25:
                raise_amount = choose_raise(
                    state['min_raise_action'], my_chips, state['my_round_bet'],
                    to_call, state['pot'], max(win_rate, preflop_strength),
                    0, 'bb_vs_raise', preflop_strength,
                    True, opponent_model,
                    match_sizing_delta=match_profile['sizing_delta'],
                )
                if raise_amount is not None:
                    return raise_amount
        # Call with playable hands
        if preflop_strength >= 0.37 or win_rate >= pot_odds_pf - 0.02:
            return 0
        return -1

    elif spot_info['preflop_spot'] == 'sb_vs_iso_raise':
        pot_odds_iso = to_call / (to_call + state['pot']) if to_call > 0 else 0.0
        # Limp-reraise with strong hands
        if preflop_strength >= 0.58 and not trash_hand:
            raise_amount = choose_raise(
                state['min_raise_action'], my_chips, state['my_round_bet'],
                to_call, state['pot'], max(win_rate, preflop_strength),
                0, 'sb_vs_iso_raise', preflop_strength,
                False, opponent_model,
                match_sizing_delta=match_profile['sizing_delta'],
            )
            if raise_amount is not None:
                return raise_amount
        # Call with most limp-range hands
        if preflop_strength >= 0.34 or win_rate >= pot_odds_iso - 0.03:
            return 0
        return -1

    elif spot_info['preflop_spot'] == 'sb_vs_reraise':
        pot_odds_sbr = to_call / (to_call + state['pot']) if to_call > 0 else 0.0
        # 4bet/jam with premiums: AA, KK, QQ, AKs (strength >= 0.78)
        if preflop_strength >= 0.78:
            raise_amount = choose_raise(
                state['min_raise_action'], my_chips, state['my_round_bet'],
                to_call, state['pot'], max(win_rate, preflop_strength),
                0, 'sb_vs_reraise', preflop_strength,
                False, opponent_model,
                match_sizing_delta=match_profile['sizing_delta'],
            )
            if raise_amount is not None:
                return raise_amount
            # If raise sizing fails (e.g. too many chips needed), call with premiums
            return 0
        # Facing all-in: call with any strong hand (AKs, AQs, JJ+)
        if state.get("opponent_allin", False) and preflop_strength >= 0.55:
            return 0
        # Non-all-in: call with strong hands if pot odds are reasonable
        if preflop_strength >= 0.55 and win_rate >= pot_odds_sbr - 0.03:
            return 0
        # 4-bet light: exploit opponents who 3-bet wide with playable hands
        light_4bet = _should_4bet_light(req["my_cards"], preflop_strength, opponent_model, state, my_chips)
        if light_4bet > 0:
            return light_4bet
        # Fold everything else
        return -1

    return None


def should_fold_postflop(round_idx, made_strength, draw_strength, value_profile, spot_info, opponent_model=None, spr=999.0):
    if round_idx <= 0:
        return False
    tier = value_profile.get("tier", "none") if value_profile else "none"
    if tier in ("strong", "nut"):
        return False
    has_draw = draw_strength >= 0.14
    if not spot_info["facing_postflop_aggression"]:
        return False
    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])
    opp_bets = spot_info.get("opp_current_round_bet_count", 0)
    if round_idx == 1:
        if made_strength < 0.20 and not has_draw and size_bucket in ("medium", "large"):
            return True
        if made_strength < 0.22 and not has_draw and opp_bets >= 2:
            return True
    if round_idx == 2:
        if made_strength < 0.25 and not has_draw and size_bucket in ("medium", "large"):
            return True
        if made_strength < 0.28 and not has_draw and opp_bets >= 2:
            return True
    if round_idx == 3:
        if made_strength < 0.35 and not has_draw and size_bucket in ("medium", "large"):
            return True
        if made_strength < 0.40 and not has_draw and opp_bets >= 2:
            return True

    # SPR commitment: fold weak uncommitted hands on late streets
    if spr > 4.0 and not has_draw:
        if round_idx >= 2 and made_strength < 0.28 and size_bucket in ('medium', 'large'):
            return True
        if round_idx == 3 and made_strength < 0.35 and size_bucket == 'large':
            return True

    # Opponent-model-aware fold: value-heavy opponents
    if opponent_model is not None and opponent_model.get('confidence', 0) >= 0.15:
        barrel = opponent_model.get('barrel_freq', 0.45)
        post_aggr = opponent_model.get('postflop_aggr', 0.36)
        opp_value_heavy = barrel >= 0.50 or post_aggr >= 0.42
        if opp_value_heavy:
            if round_idx >= 2 and made_strength < 0.28 and not has_draw and size_bucket in ('medium', 'large'):
                return True
            if round_idx == 3 and opp_bets >= 2 and made_strength < 0.34 and not has_draw:
                return True

    # River multi-barrel fold: very weak hands even vs small bets
    if round_idx == 3 and made_strength < 0.20 and not has_draw and opp_bets >= 2:
        return True

    return False


def _should_checkraise_trap(value_profile, round_idx, board_texture, opponent_model, my_cards, public_cards):
    """Check with a strong hand on a dry flop to trap aggressive opponents.

    Returns True to activate: check flop -> call opponent bet -> raise turn.
    Only fires on the flop with strong/nut hands on dry boards vs aggressive
    opponents. ~40% activation frequency via hand-based seed.
    """
    if round_idx != 1:
        return False

    if value_profile is None or value_profile.get("tier") not in ("strong", "nut"):
        return False

    if board_texture is None:
        return False
    if board_texture.get("dynamic", False):
        return False
    if board_texture.get("wetness", 0.0) > 0.25:
        return False
    if board_texture.get("paired", False):
        return False

    confidence = opponent_model.get("confidence", 0.0)
    if confidence < 0.15:
        return False

    flop_aggr = opponent_model.get("flop_aggr", 0.36)
    postflop_aggr = opponent_model.get("postflop_aggr", 0.36)
    effective_aggr = max(flop_aggr, postflop_aggr)
    if effective_aggr < 0.35:
        return False

    seed = (sum(my_cards) * 7 + sum(public_cards) * 13) % 100
    if seed >= 40:
        return False

    return True


def get_action(req, requests):
    my_id = req["my_id"]
    my_chips = req["my_chips"]
    my_cards = req["my_cards"]
    public_cards = req["public_cards"]

    state = reconstruct_state(req)
    if should_lock_win(req, state, my_id):
        return -1

    opponent_model = build_opponent_model(requests, my_id)
    spot_info = analyze_current_spot(req, state)
    round_idx = state["round"]
    to_call = state["to_call"]
    pot = max(1, state["pot"])
    remaining_hands = get_remaining_hands(req)
    match_profile = match_pressure_profile(req, my_id, remaining_hands)
    anti_lock_pressure = fold_gives_opponent_lock(req, state, my_id)
    if anti_lock_pressure:
        match_profile = apply_anti_lock_pressure(match_profile)

    preflop_strength = estimate_preflop_strength(my_cards) if not public_cards else None
    preflop_3bet_candidate = is_preflop_3bet_candidate(my_cards) if preflop_strength is not None else False
    combos, weights = build_opponent_range(my_cards, public_cards, state, opponent_model, spot_info)

    simulations = SIMULATIONS_BY_PUBLIC_COUNT.get(len(public_cards), 700)

    win_rate = estimate_weighted_win_rate(my_cards, public_cards, combos, weights, simulations)

    critical_spot = to_call > 0 and (
        to_call / pot >= 0.25 or to_call >= BIG_BLIND * 4 or spot_info["facing_allin"]
    )
    extra = EXTRA_SIMULATIONS_BY_PUBLIC_COUNT.get(len(public_cards), 0)
    if critical_spot and extra > 0:
        refined = estimate_weighted_win_rate(my_cards, public_cards, combos, weights, extra)
        win_rate = (win_rate * simulations + refined * extra) / (simulations + extra)

    if round_idx == 0 and preflop_strength is not None:
        spot_action = choose_preflop_spot_action(
            req,
            state,
            spot_info,
            opponent_model,
            preflop_strength,
            win_rate,
            match_profile,
        )
        if spot_action is not None:
            if anti_lock_pressure and spot_action <= 0:
                anti_lock_attack = choose_anti_lock_pressure_action(
                    state,
                    my_chips,
                    to_call,
                    pot,
                    round_idx,
                    win_rate,
                    opponent_model,
                    remaining_hands,
                    preflop_strength=preflop_strength,
                )
                if anti_lock_attack is not None:
                    return anti_lock_attack
                if spot_action == -1 and to_call < my_chips:
                    return 0
            return spot_action

    pot_odds = to_call / (pot + to_call) if to_call > 0 else 0.0
    made_strength = made_hand_metric(my_cards, public_cards) if len(public_cards) >= 3 else 0.0
    pair_profile = pair_board_profile(my_cards, public_cards) if len(public_cards) >= 3 else None
    board_texture = board_texture_profile(public_cards) if len(public_cards) >= 3 else None
    draw_info = draw_profile(my_cards, public_cards, board_texture) if len(public_cards) >= 3 else empty_draw_profile()
    draw_strength = draw_info["quality"]
    marginal_pair = marginal_pair_under_pressure(pair_profile, board_texture) if len(public_cards) >= 3 else False
    paired_board_profile = paired_board_outcome_profile(my_cards, public_cards) if len(public_cards) >= 3 else None
    value_profile = value_hand_tier(my_cards, public_cards, pair_profile, board_texture, paired_board_profile) if len(public_cards) >= 3 else None
    flush_profile = made_flush_profile(my_cards, public_cards, board_texture) if len(public_cards) >= 3 else None
    blocker_profile = blocker_bluff_profile(my_cards, public_cards, pair_profile, board_texture) if len(public_cards) >= 3 else None
    nutted_risk = (
        nutted_risk_profile(my_cards, public_cards, pair_profile, board_texture, value_profile, paired_board_profile)
        if len(public_cards) >= 3
        else {"risk": 0.0, "label": "none", "vulnerable": False}
    )
    value_plan = (
        value_bet_plan(value_profile, board_texture, paired_board_profile, pair_profile, nutted_risk, round_idx, pot)
        if len(public_cards) >= 3
        else {"size_delta": 0.0, "induce": False, "protect": False, "thin_control": False}
    )
    line_profile = line_polarization_profile(
        public_cards, req.get('history', []), state, spot_info, opponent_model, round_idx
    ) if len(public_cards) >= 3 else {'value_pressure': 0.0, 'bluff_opportunity': 0.0, 'line_label': 'balanced'}
    line_strength = aggressive_line_strength(spot_info, board_texture) if len(public_cards) >= 3 else 0.0
    check_resistance = check_probe_resistance_margin(spot_info, opponent_model, round_idx) if len(public_cards) >= 3 else 0.0
    paired_board_stackoff = (
        paired_board_stackoff_profile(pair_profile, paired_board_profile, board_texture, spot_info, round_idx)
        if len(public_cards) >= 3
        else {"active": False, "severe": False, "line_strength": 0.0, "size_bucket": "small"}
    )
    repeated_raise_trap = (
        round_idx > 0
        and spot_info["facing_postflop_aggression"]
        and spot_info.get("opp_current_round_bet_count", 0) >= 2
    )
    strong_flush_repressure_continue = (
        flush_profile is not None
        and (
            flush_profile["repressure_continue"]
            or flush_profile["nut_like"]
            or (
                board_texture is not None
                and not board_texture["paired"]
                and flush_profile["high_hole_rank"] >= 12
                and flush_profile["better_unseen_ranks"] <= 1
            )
        )
    )
    hard_repressure_fold = (
        repeated_raise_trap
        and not strong_flush_repressure_continue
        and (value_profile is None or value_profile["tier"] != "nut")
        and (
            (board_texture is not None and board_texture["paired"])
            or bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large")
        )
    )

    strong = 0.69 if round_idx == 0 else 0.65 if round_idx == 1 else 0.61 if round_idx == 2 else 0.59
    medium = 0.54 if round_idx == 0 else 0.50 if round_idx == 1 else 0.48

    if spot_info["has_position"]:
        strong -= 0.015
        medium -= 0.01
    else:
        strong += 0.02
        medium += 0.015

    if preflop_strength is not None:
        if preflop_strength >= 0.72:
            strong -= 0.03
            medium -= 0.02
        elif preflop_strength <= 0.40:
            strong += 0.04
            medium += 0.03

    match_adjust = match_risk_adjustment(req, my_id, remaining_hands)
    pressure_adjust = opponent_pressure_adjustment(opponent_model, spot_info, round_idx)
    strong += match_adjust + pressure_adjust + match_profile["threshold_delta"]
    medium += match_adjust + pressure_adjust * 0.8 + 0.75 * match_profile["threshold_delta"]
    strong += 0.30 * line_strength + 0.45 * paired_board_stackoff["line_strength"]
    medium += 0.18 * line_strength + 0.22 * paired_board_stackoff["line_strength"]
    strong += 0.30 * check_resistance
    medium += 0.20 * check_resistance
    if value_profile is not None:
        if value_profile["tier"] == "nut":
            strong -= 0.07
            medium -= 0.04
        elif value_profile["tier"] == "strong":
            strong -= 0.04
            medium -= 0.02
        elif value_profile["tier"] == "thin":
            medium -= 0.01
    strong += 0.45 * nutted_risk["risk"]
    medium += 0.30 * nutted_risk["risk"]

    if state["opponent_allin"]:
        jam_cost = max(state["allin_call_amount"], to_call)
        jam_odds = jam_cost / (pot + jam_cost) if jam_cost > 0 else 0.0
        jam_buffer = 0.02 + max(0.0, strong - 0.65) * 0.2
        if value_profile is not None and value_profile["tier"] == "thin":
            jam_buffer += 0.04
        jam_buffer += nutted_risk["risk"]
        jam_buffer += 0.04 * match_profile["protect"]
        jam_buffer += line_strength + paired_board_stackoff["line_strength"]
        jam_buffer += check_resistance
        if remaining_hands == 1:
            total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
            if len(total_win_chips) > my_id and total_win_chips[my_id] < 0:
                jam_buffer -= 0.03
        if preflop_strength is not None and preflop_strength < 0.42:
            jam_buffer += 0.02
        if anti_lock_pressure:
            jam_buffer -= 0.10
        anti_lock_jam_continue = anti_lock_can_continue(
            anti_lock_pressure,
            win_rate,
            jam_odds,
            round_idx,
            value_profile,
            draw_info,
            made_strength,
        )
        if hard_repressure_fold or paired_board_stackoff["severe"]:
            if not anti_lock_jam_continue:
                return -1
        if round_idx >= 2 and line_profile['line_label'] == 'value_heavy':
            if (value_profile is None or value_profile['tier'] not in ('strong', 'nut')) and draw_strength < 0.18 and not anti_lock_jam_continue:
                return -1
        jam_buffer = clamp(jam_buffer, -0.05 if anti_lock_pressure else 0.0, 0.14)
        return -2 if win_rate >= jam_odds + jam_buffer or anti_lock_jam_continue else -1

    if to_call >= my_chips:
        shove_odds = my_chips / (pot + my_chips)
        shove_buffer = 0.01 + max(0.0, strong - 0.64) * 0.2
        if value_profile is not None and value_profile["tier"] == "thin":
            shove_buffer += 0.04
        shove_buffer += nutted_risk["risk"]
        shove_buffer += 0.04 * match_profile["protect"]
        shove_buffer += line_strength + paired_board_stackoff["line_strength"]
        shove_buffer += check_resistance
        if anti_lock_pressure:
            shove_buffer -= 0.10
        anti_lock_shove_continue = anti_lock_can_continue(
            anti_lock_pressure,
            win_rate,
            shove_odds,
            round_idx,
            value_profile,
            draw_info,
            made_strength,
        )
        if hard_repressure_fold or paired_board_stackoff["severe"]:
            if not anti_lock_shove_continue:
                return -1
        if round_idx >= 2 and line_profile['line_label'] == 'value_heavy':
            if (value_profile is None or value_profile['tier'] not in ('strong', 'nut')) and draw_strength < 0.18 and not anti_lock_shove_continue:
                return -1
        shove_buffer = clamp(shove_buffer, -0.05 if anti_lock_pressure else 0.0, 0.14)
        return -2 if win_rate >= shove_odds + shove_buffer or anti_lock_shove_continue else -1

    if to_call > 0:
        if round_idx == 0:
            call_margin = 0.005 + (0.010 if not spot_info["has_position"] else 0.0)
            if preflop_strength is not None and preflop_strength <= 0.40:
                call_margin += 0.015
            realized_rate = win_rate
        else:
            call_margin = postflop_call_margin(
                spot_info,
                opponent_model,
                made_strength,
                draw_strength,
                round_idx,
                spot_info["has_position"],
            )
            call_margin += pair_domination_margin(
                pair_profile,
                spot_info,
                round_idx,
            )
            call_margin += draw_call_margin(
                draw_info,
                board_texture,
                round_idx,
                spot_info,
            )
            if (
                round_idx == 2
                and spot_info["facing_postflop_aggression"]
                and pair_profile is not None
                and pair_profile["made_class"] == 1
                and pair_profile["pair_type"] in ("middle_pair", "bottom_pair", "underpair")
            ):
                call_margin += 0.035
            call_margin += line_strength + paired_board_stackoff["line_strength"]
            call_margin += check_resistance
            call_margin += 0.50 * nutted_risk["risk"]
            if round_idx == 3 and made_strength < 0.40 and not (blocker_profile and blocker_profile["eligible"]):
                call_margin += 0.04
            if round_idx == 3 and paired_board_profile is not None and paired_board_profile["fold_to_raise"]:
                call_margin += 0.05
            realized_rate = realized_postflop_equity(
                win_rate,
                made_strength,
                draw_strength,
                round_idx,
                spot_info["has_position"],
                spot_info,
                pair_profile,
                opponent_model,
            )
        if anti_lock_pressure:
            call_margin -= 0.07
        anti_lock_call_continue = anti_lock_can_continue(
            anti_lock_pressure,
            win_rate,
            pot_odds,
            round_idx,
            value_profile,
            draw_info,
            made_strength,
        )
        strong_made_continue = must_continue_vs_raise(
            value_profile,
            made_strength,
            pot_odds,
            nutted_risk,
            board_texture,
        )
        if round_idx >= 2 and (state.get('opponent_allin') or bet_size_bucket(spot_info['last_raise_pot_ratio']) in ('medium', 'large')):
            if line_profile['line_label'] == 'value_heavy':
                if (value_profile is None or value_profile['tier'] not in ('strong', 'nut')) and draw_strength < 0.18 and not (blocker_profile and blocker_profile['eligible']):
                    if not anti_lock_call_continue and not strong_made_continue:
                        return -1
        anti_lock_attack = None
        if anti_lock_pressure:
            anti_lock_attack = choose_anti_lock_pressure_action(
                state,
                my_chips,
                to_call,
                pot,
                round_idx,
                win_rate,
                opponent_model,
                remaining_hands,
                preflop_strength=preflop_strength,
                value_profile=value_profile,
                draw_info=draw_info,
                blocker_profile=blocker_profile,
                board_texture=board_texture,
            )
        fragile_river_raise_fold = (
            round_idx == 3
            and spot_info["facing_postflop_aggression"]
            and bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large")
            and paired_board_profile is not None
            and paired_board_profile["fold_to_raise"]
            and paired_board_profile["hand_class"] == 2
            and (value_profile is None or value_profile["tier"] != "nut")
        )
        fragile_pair_raise_fold = (
            round_idx > 0
            and spot_info["facing_postflop_aggression"]
            and marginal_pair
            and draw_strength < 0.14
            and bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large")
            and (value_profile is None or value_profile["tier"] not in ("strong", "nut"))
        )
        if anti_lock_attack is not None:
            return anti_lock_attack
        # Crossover from v10: include strong_made_continue guard in fragile fold checks
        # Prevents over-folding genuinely strong hands facing aggression
        if fragile_river_raise_fold:
            if not anti_lock_call_continue and not strong_made_continue:
                return -1
        if fragile_pair_raise_fold:
            if not anti_lock_call_continue and not strong_made_continue:
                return -1
        _spr = my_chips / pot if pot > 0 else 999.0
        if should_fold_postflop(round_idx, made_strength, draw_strength, value_profile, spot_info, opponent_model=opponent_model, spr=_spr):
            if not anti_lock_call_continue and not strong_made_continue:
                return -1
        if hard_repressure_fold or paired_board_stackoff["severe"]:
            if not anti_lock_call_continue and not strong_made_continue:
                return -1
        if realized_rate < pot_odds + call_margin:
            if not anti_lock_call_continue and not strong_made_continue:
                return -1
        if repeated_raise_trap and (value_profile is None or value_profile["tier"] != "nut"):
            trap_size = bet_size_bucket(spot_info["last_raise_pot_ratio"])
            if made_strength < 0.25 and draw_strength < 0.14 and trap_size in ("medium", "large"):
                return -1
            return 0

        raise_fold_threshold = 0.56 - 0.30 * match_profile["bluff_delta"]
        blocker_raise_threshold = 0.55 - 0.32 * match_profile["bluff_delta"]
        draw_raise_threshold = clamp(raise_fold_threshold - draw_info["fold_threshold_delta"], 0.46, 0.68)
        draw_equity_slack = 0.05 if draw_info["type"] in ("combo_draw", "nut_flush_draw") else 0.03
        semi_bluff = (
            round_idx > 0
            and draw_info["semi_bluff"]
            and draw_strength >= 0.12
            and opponent_model["confidence"] >= 0.25
            and opponent_model["fold_to_raise"] > draw_raise_threshold
            and win_rate >= pot_odds - draw_equity_slack
        )
        blocker_raise = (
            round_idx == 1
            and spot_info["facing_postflop_aggression"]
            and opponent_model["confidence"] >= 0.25
            and opponent_model["fold_to_raise"] > blocker_raise_threshold
            and blocker_profile is not None
            and blocker_profile["eligible"]
            and made_strength < 0.18
            and draw_strength < 0.12
            and allow_low_frequency_blocker_bluff(req, my_cards, public_cards, blocker_profile, round_idx)
        )
        trap_nut_slowplay = (
            round_idx in (1, 2)
            and value_profile is not None
            and value_profile["tier"] == "nut"
            and board_texture is not None
            and not board_texture["dynamic"]
            and spot_info["facing_postflop_aggression"]
            and bet_size_bucket(spot_info["last_raise_pot_ratio"]) != "large"
            and pot < 1400
            and nutted_risk["risk"] <= 0.02
            and match_profile["chase"] <= 0.45
            and opponent_model["confidence"] >= 0.20
            and (
                opponent_model["postflop_aggr"] >= 0.38
                or opponent_model["aggression"] >= 0.34
                or opponent_model["fold_to_raise"] < 0.46
            )
        )
        flop_checkraise_exploit = (
            round_idx == 1
            and spot_info["facing_postflop_aggression"]
            and opponent_model["confidence"] >= 0.25
            and opponent_model["fold_to_raise"] > blocker_raise_threshold
            and (
                (value_profile and value_profile["tier"] in ("strong", "nut"))
                or (draw_info["semi_bluff"] and draw_strength >= 0.15)
                or blocker_raise
            )
        )

        if trap_nut_slowplay:
            return 0
        preflop_defensive_only = (
            round_idx == 0
            and to_call > 0
            and not preflop_3bet_candidate
        )
        sizing_delta = sizing_exploit_adjustment(opponent_model, round_idx)
        if not preflop_defensive_only and (win_rate >= max(strong, pot_odds + 0.12) or semi_bluff or flop_checkraise_exploit):
            raise_amount = choose_raise(
                state["min_raise_action"],
                my_chips,
                state["my_round_bet"],
                to_call,
                pot,
                win_rate,
                round_idx,
                spot_info["preflop_spot"],
                preflop_strength,
                spot_info["has_position"],
                opponent_model,
                semi_bluff=semi_bluff or (flop_checkraise_exploit and draw_info["semi_bluff"] and draw_strength >= 0.15),
                value_profile=value_profile,
                value_plan=value_plan,
                board_texture=board_texture,
                draw_info=draw_info,
                blocker_bluff=blocker_raise,
                pressure_line=flop_checkraise_exploit,
                nutted_risk_score=nutted_risk["risk"],
                match_sizing_delta=match_profile["sizing_delta"],
                sizing_exploit_delta=sizing_delta,
            )
            if raise_amount is not None and raise_amount > to_call:
                return raise_amount
        return 0

    weak_pair_river = (
        round_idx == 3
        and pair_profile is not None
        and pair_profile["made_class"] == 1
        and pair_profile["pair_type"] in ("middle_pair", "bottom_pair", "underpair", "board_pair")
    )
    opp_double_barrel_then_river_check = (
        round_idx == 3
        and to_call == 0
        and spot_info.get("opp_postflop_bet_count", 0) >= 2
        and spot_info["last_opp_action_type"] == "check"
    )
    bad_river_bluff_candidate = (
        round_idx == 3
        and to_call == 0
        and made_strength >= 0.18
        and made_strength < 0.40
        and not (blocker_profile and blocker_profile["eligible"])
        and not (value_profile and value_profile["tier"] in ("strong", "nut"))
    )
    weak_bottom_pair_barrel = (
        round_idx >= 2
        and to_call == 0
        and pair_profile is not None
        and pair_profile["made_class"] == 1
        and pair_profile["pair_type"] in ("bottom_pair", "underpair", "board_pair")
        and made_strength < 0.40
        and draw_strength < 0.12
    )
    weak_pair_after_raise_barrel = (
        round_idx >= 2
        and to_call == 0
        and marginal_pair
        and draw_strength < 0.14
        and (value_profile is None or value_profile["tier"] not in ("strong", "nut"))
        and (
            spot_info.get("opp_previous_round_raise_count", 0) > 0
            or spot_info.get("opp_prior_postflop_raise_count", 0) > 0
        )
    )
    bad_river_value_bet = (
        round_idx == 3
        and to_call == 0
        and paired_board_profile is not None
        and paired_board_profile["board_paired"]
        and paired_board_profile["prefer_check"]
        and paired_board_profile["hand_class"] == 2
        and nutted_risk["risk"] >= 0.05
        and (value_profile is None or value_profile["tier"] != "nut")
    )
    bad_stackoff_overpair = (
        round_idx > 0
        and to_call == 0
        and paired_board_stackoff["active"]
        and pot > 3000
        and (value_profile is None or value_profile["tier"] != "nut")
    )
    big_pot_threshold = int(clamp(1500 - 350 * match_profile["protect"] + 250 * match_profile["chase"], 1100, 1800))
    big_pot = pot >= big_pot_threshold
    induce_nut_value = (
        round_idx > 0
        and to_call == 0
        and value_profile is not None
        and value_profile["tier"] == "nut"
        and board_texture is not None
        and not board_texture["dynamic"]
        and not big_pot
        and match_profile["chase"] <= 0.55
        and opponent_model["confidence"] >= 0.20
        and (
            opponent_model["postflop_aggr"] >= 0.38
            or opponent_model["aggression"] >= 0.34
            or opponent_model["fold_to_raise"] < 0.46
        )
    )
    anti_lock_attack = None
    if anti_lock_pressure:
        anti_lock_attack = choose_anti_lock_pressure_action(
            state,
            my_chips,
            to_call,
            pot,
            round_idx,
            win_rate,
            opponent_model,
            remaining_hands,
            preflop_strength=preflop_strength,
            value_profile=value_profile,
            draw_info=draw_info,
            blocker_profile=blocker_profile,
            board_texture=board_texture,
        )
        if anti_lock_attack is not None:
            return anti_lock_attack

    if opp_double_barrel_then_river_check and weak_pair_river:
        return 0
    if bad_river_bluff_candidate:
        return 0
    if weak_bottom_pair_barrel:
        return 0
    if weak_pair_after_raise_barrel:
        return 0
    if bad_river_value_bet:
        return 0
    if bad_stackoff_overpair:
        return 0
    if big_pot and round_idx == 3 and (value_profile is None or value_profile["tier"] not in ("strong", "nut")):
        if blocker_profile is None or not blocker_profile["eligible"]:
            return 0
    thin_static_showdown_control = (
        round_idx >= 2
        and value_profile is not None
        and value_profile["tier"] == "thin"
        and board_texture is not None
        and not board_texture["dynamic"]
        and draw_strength < 0.12
        and not anti_lock_pressure
    )
    if thin_static_showdown_control:
        return 0

    # v27 exploits: overbet/donk/probe take priority before value/bluff path.
    overbet = should_overbet(
        round_idx, to_call, value_profile, board_texture,
        nutted_risk, paired_board_profile, opponent_model,
        my_cards, public_cards, pot, my_chips,
    )
    if overbet["eligible"]:
        raise_amount = overbet_sizing(
            overbet["ratio"], to_call, pot,
            state["min_raise_action"], my_chips, state["my_round_bet"],
        )
        if raise_amount is not None:
            return raise_amount

    # Donk bet: BB leads into PFR on favorable flop textures
    donk = should_donk_bet(
        round_idx, to_call, spot_info, value_profile, board_texture,
        made_strength, draw_strength, draw_info, opponent_model,
        my_cards, public_cards, pot, req.get("history", []), state,
    )
    if donk["eligible"]:
        raise_amount = donk_probe_sizing(
            donk["ratio"], to_call, pot,
            state["min_raise_action"], my_chips, state["my_round_bet"],
        )
        if raise_amount is not None:
            return raise_amount

    # Probe bet: bet after PFR checked the previous street (turn/river)
    probe = should_probe_bet(
        round_idx, to_call, spot_info, value_profile, board_texture,
        made_strength, draw_strength, draw_info, opponent_model,
        my_cards, public_cards, pot, req.get("history", []), state,
    )
    if probe["eligible"]:
        raise_amount = donk_probe_sizing(
            probe["ratio"], to_call, pot,
            state["min_raise_action"], my_chips, state["my_round_bet"],
        )
        if raise_amount is not None:
            return raise_amount

    # Passive-station exploits: delayed c-bet, second barrel, river thin value.
    opponent_id = next_player(my_id, 1)
    passive_exploit = passive_exploit_trigger(
        round_idx, to_call, my_id, opponent_id, spot_info,
        opponent_model, value_profile, made_strength, draw_strength,
        board_texture, req.get("history", []), state,
    )
    if passive_exploit["active"]:
        amount = passive_exploit_sizing(
            passive_exploit["ratio"], to_call, pot,
            state["min_raise_action"], my_chips, state["my_round_bet"],
        )
        if amount is not None:
            return amount

    river_bluff_threshold = 0.62 - 0.28 * match_profile["bluff_delta"]
    probe_fold_threshold = 0.56 - 0.32 * match_profile["bluff_delta"]
    semi_bluff_threshold = 0.58 - 0.28 * match_profile["bluff_delta"]
    draw_bet_threshold = clamp(semi_bluff_threshold - draw_info["fold_threshold_delta"], 0.46, 0.70)
    check_probe_signal = (
        spot_info["last_opp_action_type"] == "check"
        and (
            spot_info.get("opp_postflop_check_count", 0) >= 2
            or (
                opponent_model["confidence"] >= 0.20
                and opponent_model.get("postflop_check_rate", 0.42) >= 0.52
            )
        )
    )
    river_blocker_bluff = (
        round_idx == 3
        and made_strength < 0.16
        and draw_strength < 0.08
        and opponent_model["confidence"] >= 0.35
        and opponent_model["fold_to_raise"] > river_bluff_threshold
        and blocker_profile is not None
        and blocker_profile["eligible"]
        and allow_low_frequency_blocker_bluff(req, my_cards, public_cards, blocker_profile, round_idx)
    )
    small_probe = (
        round_idx > 0
        and opponent_model["confidence"] >= 0.25
        and opponent_model["fold_to_raise"] > probe_fold_threshold
        and made_strength < 0.62
        and draw_strength < 0.16
        and board_texture is not None
        and board_texture["wetness"] <= 0.32
        and not (value_profile and value_profile["tier"] in ("strong", "nut"))
    )
    check_probe = (
        round_idx > 0
        and check_probe_signal
        and board_texture is not None
        and board_texture["wetness"] <= 0.55
        and made_strength < 0.58
        and draw_strength < 0.20
        and not (value_profile and value_profile["tier"] in ("strong", "nut"))
        and not (round_idx == 3 and made_strength >= 0.18 and not (blocker_profile and blocker_profile["eligible"]))
    )
    blocker_bluff = (
        river_blocker_bluff
    )
    semi_bluff = (
        round_idx > 0
        and draw_info["semi_bluff"]
        and draw_strength >= 0.12
        and opponent_model["confidence"] >= 0.25
        and opponent_model["fold_to_raise"] > draw_bet_threshold
    )
    sizing_delta = sizing_exploit_adjustment(opponent_model, round_idx)
    if win_rate >= medium or semi_bluff or blocker_bluff or small_probe or check_probe or made_strength >= 0.62 or (value_profile and value_profile["tier"] in ("strong", "nut")):
        # Check-raise trap: check with strong/nut hands on dry flop vs aggressive opponents
        is_pure_value_raise = (
            value_profile is not None
            and value_profile["tier"] in ("strong", "nut")
            and not semi_bluff
            and not blocker_bluff
            and not small_probe
            and not check_probe
        )
        if is_pure_value_raise and _should_checkraise_trap(value_profile, round_idx, board_texture, opponent_model, my_cards, public_cards):
            return 0  # check (trap) — plan to call flop bet, raise turn
        raise_amount = choose_raise(
            state["min_raise_action"],
            my_chips,
            state["my_round_bet"],
            to_call,
            pot,
            win_rate,
            round_idx,
            spot_info["preflop_spot"],
            preflop_strength,
            spot_info["has_position"],
            opponent_model,
            semi_bluff=semi_bluff and win_rate < medium,
            value_profile=value_profile,
            value_plan=value_plan,
            board_texture=board_texture,
            draw_info=draw_info,
            blocker_bluff=blocker_bluff and win_rate < medium and not semi_bluff,
            probe_mode=check_probe or small_probe or (value_profile and value_profile["tier"] == "thin" and board_texture and not board_texture["dynamic"]),
            induce_mode=induce_nut_value or value_plan.get("induce", False),
            nutted_risk_score=nutted_risk["risk"],
            match_sizing_delta=match_profile["sizing_delta"],
            sizing_exploit_delta=sizing_delta,
        )
        if raise_amount is not None:
            return raise_amount
    return 0
