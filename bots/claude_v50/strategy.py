from constants import N_PLAYERS, INITIAL_CHIPS, BIG_BLIND, TOTAL_HANDS, SIMULATIONS_BY_PUBLIC_COUNT, EXTRA_SIMULATIONS_BY_PUBLIC_COUNT
from card_utils import clamp, next_player
from state import (
    reconstruct_state, get_remaining_hands,
    estimate_preflop_strength, is_preflop_3bet_candidate, is_preflop_trash_hand,
    get_hand_index, collect_latest_requests_by_hand, preflop_hand_profile,
)
from tournament import (
    should_lock_win, fold_gives_opponent_lock,
    match_risk_adjustment, match_pressure_profile, apply_anti_lock_pressure,
    anti_lock_can_continue,
)
from opponent import build_opponent_model, analyze_current_spot, detect_bot4_profile, get_anti_bot4_adjustments, classify_opponent_style, build_opponent_line_profile
from postflop import (
    made_hand_metric, pair_board_profile, pair_domination_margin,
    marginal_pair_under_pressure, board_texture_profile,
    paired_board_outcome_profile,
    value_hand_tier, value_bet_plan,
    draw_profile, draw_potential, draw_call_margin,
    empty_draw_profile, made_flush_profile,
    blocker_bluff_profile, allow_low_frequency_blocker_bluff,
    nutted_risk_profile,
)
from betting import (
    bet_size_bucket,
    opponent_pressure_adjustment,
    aggressive_line_strength,
    choose_anti_lock_pressure_action,
    paired_board_stackoff_profile,
    postflop_call_margin,
    realized_postflop_equity,
    choose_overbet_river,
    choose_overbet_bluff_river,
    must_continue_vs_raise,
    should_fold_postflop,
    check_probe_resistance_margin,
)
from simulation import build_opponent_range, estimate_weighted_win_rate

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
        ratio = 0.65 if to_call == 0 else 0.80
    elif round_idx == 1:
        ratio = 0.82
    elif round_idx == 2:
        ratio = 0.92
    else:
        ratio = 1.02

    ratio += max(0.0, win_rate - 0.55) * (0.90 + 0.20 * round_idx)
    ratio += -0.05 if has_position else 0.05
    ratio += confidence * max(0.0, fold_to_raise - 0.52) * (0.20 if semi_bluff else 0.10)
    ratio += value_profile.get("size_bonus", 0.0)
    ratio += value_plan.get("size_delta", 0.0)
    ratio += match_sizing_delta
    ratio += anti_bot4_bonus
    if round_idx > 0 and value_profile.get("tier") == "strong" and not semi_bluff and not pressure_line:
        if not board_texture["dynamic"]:
            ratio -= 0.02
        if wetness <= 0.20:
            ratio -= 0.01
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
        induce_cap = 0.42 + 0.06 * round_idx + 0.04 * wetness
        ratio = min(ratio, induce_cap)
    if probe_mode:
        probe_ratio = 0.35 + 0.08 * wetness
        if value_profile.get("tier") == "thin":
            probe_ratio += 0.10
        if blocker_bluff and round_idx == 3:
            probe_ratio = max(probe_ratio, 0.42 + 0.08 * wetness)
        elif round_idx == 3:
            probe_ratio += 0.05
        ratio = min(ratio, probe_ratio)
    thin_cap = None
    if value_plan.get("thin_control", False) and value_profile.get("tier") != "nut":
        thin_cap = 0.42 if round_idx <= 2 else 0.52
        ratio = min(ratio, thin_cap)
    low_ratio = 0.28 if inducing_value else 0.22 if probe_mode or (blocker_bluff and to_call == 0) else 0.45
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

    # min_raise 现在是 raise-to-total 语义（最小输出总额）
    raise_to_total = my_round_bet + amount
    raise_to_total = max(min_raise, raise_to_total)
    if semi_bluff and fold_to_raise < 0.45:
        cap = max(min_raise, my_round_bet + int(to_call + pot_after_call * 0.60))
        raise_to_total = min(raise_to_total, cap)
    if blocker_bluff:
        bluff_cap = max(min_raise, my_round_bet + int(to_call + pot_after_call * (0.45 if round_idx == 3 and to_call == 0 else 0.56 + 0.16 * wetness)))
        raise_to_total = min(raise_to_total, bluff_cap)
    raise_to_total = min(raise_to_total, my_round_bet + my_chips - 1)
    if raise_to_total <= my_round_bet + to_call or raise_to_total < min_raise or raise_to_total >= my_round_bet + my_chips:
        return None
    return raise_to_total

def choose_preflop_spot_action(req, state, spot_info, opponent_model, preflop_strength, win_rate, match_profile):
    my_chips = req["my_chips"]
    to_call = state["to_call"]
    match_adjust = match_risk_adjustment(req, req["my_id"], get_remaining_hands(req))
    confidence = opponent_model["confidence"]
    loose_bonus = confidence * max(0.0, opponent_model["vpip"] - 0.55) * 0.03
    trash_hand = is_preflop_trash_hand(req["my_cards"], preflop_strength)
    early_game_bonus = 0.03 if confidence < 0.10 else 0.0  # early-game wider defense

    if spot_info["preflop_spot"] == "sb_open":
        # Lower SB open/limp thresholds to open ~75% and limp ~10% more.
        open_threshold = 0.30 + match_adjust + match_profile["open_delta"] - early_game_bonus
        limp_threshold = 0.20 + match_adjust - early_game_bonus
        raise_amount = choose_raise(
            state.get("min_raise_action", state["round_raise"]),
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
            state.get("min_raise_action", state["round_raise"]),
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

    if spot_info["preflop_spot"] == "bb_vs_raise":  # BB vs preflop raise — defend wide in HU
        pot = max(1, state["pot"])
        pot_odds = to_call / (pot + to_call) if to_call > 0 else 0.0
        # Premium hand guard: return -2 vs all-in because sanitize_action converts 0→-1.
        if preflop_strength >= 0.60:
            if to_call >= my_chips or state["opponent_allin"]:
                return -2
            # 3bet with strong hands when not facing all-in
            if preflop_strength >= 0.62 and not trash_hand:
                raise_amount = choose_raise(
                    state.get("min_raise_action", state["round_raise"]),
                    my_chips,
                    state["my_round_bet"],
                    to_call,
                    pot,
                    max(win_rate, preflop_strength),
                    0,
                    spot_info["preflop_spot"],
                    preflop_strength,
                    spot_info["has_position"],
                    opponent_model,
                    match_sizing_delta=match_profile["sizing_delta"],
                )
                if raise_amount is not None:
                    return raise_amount
            return 0
        if preflop_strength >= 0.38 - early_game_bonus:  # Widen BB defense: call 0.38+
            return 0
        # Fold the very weakest hands
        if preflop_strength < 0.22 - early_game_bonus:
            return -1
        if pot_odds < 0.50 and not trash_hand:  # 0.26-0.38 gap: call with good pot odds
            return 0
        return -1

    # SB facing BB 3bet (reraise) — selective defense
    if spot_info["preflop_spot"] == "sb_vs_reraise":
        pot = max(1, state["pot"])
        # Premium hand guard: return -2 vs all-in because sanitize_action converts 0→-1.
        if preflop_strength >= 0.60:
            if to_call >= my_chips or state["opponent_allin"]:
                return -2
            # 4bet with strong hands when not facing all-in
            if not trash_hand:
                raise_amount = choose_raise(
                    state.get("min_raise_action", state["round_raise"]),
                    my_chips,
                    state["my_round_bet"],
                    to_call,
                    pot,
                    max(win_rate, preflop_strength),
                    0,
                    spot_info["preflop_spot"],
                    preflop_strength,
                    spot_info["has_position"],
                    opponent_model,
                    match_sizing_delta=match_profile["sizing_delta"],
                )
                if raise_amount is not None:
                    return raise_amount
            return 0
        # Change 3: Defend wider vs 3bets — call with 0.40+ (was 0.46).
        if preflop_strength >= 0.40 - early_game_bonus:
            return 0
        # Fold weak hands
        return -1

    return None

def get_action(req, requests):
    my_id = req["my_id"]
    my_chips = req["my_chips"]
    my_cards = req["my_cards"]
    public_cards = req["public_cards"]

    state = reconstruct_state(req)
    if should_lock_win(req, state, my_id):
        return -1

    opponent_model = build_opponent_model(requests, my_id)
    style_deltas = classify_opponent_style(opponent_model)
    spot_info = analyze_current_spot(req, state)
    round_idx = state["round"]
    _lp = build_opponent_line_profile(requests, my_id, state) if round_idx > 0 else {"strength_signal": 0.0, "passive_count": 0}
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

    # Anti-bot_4 detection and adjustments
    n_hands = len(collect_latest_requests_by_hand(requests))
    is_bot4, bot4_score = detect_bot4_profile(opponent_model, n_hands)
    anti_bot4 = None

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
    river_strict_fold = (
        round_idx == 3
        and spot_info["facing_postflop_aggression"]
        and bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large")
        and value_profile is not None
        and value_profile["tier"] != "nut"
    )
    line_strength = aggressive_line_strength(spot_info, board_texture) if len(public_cards) >= 3 else 0.0
    check_resistance = check_probe_resistance_margin(spot_info, opponent_model, round_idx) if len(public_cards) >= 3 else 0.0
    paired_board_stackoff = (
        paired_board_stackoff_profile(pair_profile, paired_board_profile, board_texture, spot_info, round_idx)
        if len(public_cards) >= 3
        else {"active": False, "severe": False, "line_strength": 0.0, "size_bucket": "small"}
    )

    # Postflop fold detection layer (v40): break 0% fold rate, only when facing bet and NOT under match-ending pressure.
    if (
        round_idx > 0
        and to_call > 0
        and len(public_cards) >= 3
        and not anti_lock_pressure
        and to_call < my_chips
    ):
        if should_fold_postflop(
            made_strength,
            draw_strength,
            round_idx,
            spot_info,
            board_texture,
            pair_profile,
            value_profile,
            pot_odds,
            blocker_profile,
            opponent_model,
        ):
            return -1

    # Anti-bot_4 adjustments
    if board_texture is not None:
        anti_bot4 = get_anti_bot4_adjustments(bot4_score, board_texture, spot_info, round_idx, value_profile)
    else:
        anti_bot4 = {
            "bluff_freq_bonus": 0.0, "raise_size_bonus": 0.0,
            "call_threshold_delta": 0.0, "fold_threshold_delta": 0.0,
            "river_overbet_enabled": False, "trap_defense_delta": 0.0,
        }

    # River overbet with nut hands (always enabled, not just vs bot_4)
    if round_idx == 3 and to_call == 0 and len(public_cards) >= 3 and value_profile is not None and value_profile["tier"] == "nut":
        overbet = choose_overbet_river(
            state.get("min_raise_action", state["round_raise"]), my_chips, state["my_round_bet"],
            to_call, pot, win_rate, value_profile, board_texture, spot_info, opponent_model
        )
        if overbet is not None:
            return overbet

    # River overbet bluff
    if round_idx == 3 and to_call == 0 and len(public_cards) >= 3 and made_strength < 0.18:
        overbet_bluff = choose_overbet_bluff_river(
            state.get("min_raise_action", state["round_raise"]), my_chips, state["my_round_bet"],
            to_call, pot, blocker_profile, board_texture, spot_info, opponent_model
        )
        if overbet_bluff is not None:
            return overbet_bluff

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

    strong += style_deltas["strong_delta"] + _lp.get("strength_signal", 0.0)
    medium += style_deltas["medium_delta"] + _lp.get("strength_signal", 0.0) * 0.75

    # Apply anti-bot_4 adjustments
    strong -= anti_bot4["call_threshold_delta"]
    medium -= anti_bot4["call_threshold_delta"] * 0.8
    strong += anti_bot4["trap_defense_delta"]

    if state["opponent_allin"]:
        # Unconditional guard: never fold premium hands (AKo+, JJ+) to preflop all-in.
        if round_idx == 0 and preflop_strength is not None and preflop_strength >= 0.68:
            _hprof = preflop_hand_profile(my_cards)
            if (_hprof["pair"] and _hprof["high"] >= 11) or (_hprof["high"] == 14 and _hprof["low"] == 13):
                return -2
        jam_cost = max(state["allin_call_amount"], to_call)
        jam_odds = jam_cost / (pot + jam_cost) if jam_cost > 0 else 0.0
        jam_buffer = 0.02 + max(0.0, strong - 0.65) * 0.2
        if value_profile is not None and value_profile["tier"] == "thin":
            jam_buffer += 0.04
        jam_buffer += nutted_risk["risk"]
        jam_buffer += 0.04 * match_profile["protect"] + line_strength + paired_board_stackoff["line_strength"] + check_resistance
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
        jam_buffer = clamp(jam_buffer, -0.05 if anti_lock_pressure else 0.0, 0.14)
        return -2 if win_rate >= jam_odds + jam_buffer or anti_lock_jam_continue else -1

    if to_call >= my_chips:
        shove_odds = my_chips / (pot + my_chips)
        shove_buffer = 0.01 + max(0.0, strong - 0.64) * 0.2
        if value_profile is not None and value_profile["tier"] == "thin":
            shove_buffer += 0.04
        shove_buffer += nutted_risk["risk"]
        shove_buffer += 0.04 * match_profile["protect"] + line_strength + paired_board_stackoff["line_strength"] + check_resistance
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
        shove_buffer = clamp(shove_buffer, -0.05 if anti_lock_pressure else 0.0, 0.14)
        return -2 if win_rate >= shove_odds + shove_buffer or anti_lock_shove_continue else -1

    if to_call > 0:
        if round_idx == 0:
            call_margin = -0.025 + (0.010 if not spot_info["has_position"] else 0.0)
            if preflop_strength is not None and preflop_strength <= 0.40:
                call_margin += 0.005
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
            call_margin += line_strength + paired_board_stackoff["line_strength"] + check_resistance + 0.50 * nutted_risk["risk"]
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
            )
        if anti_lock_pressure:
            call_margin -= 0.07
        # Against aggressive styles (maniac), reduce call margin to call lighter
        if spot_info["facing_raise"] or spot_info["facing_allin"]:
            call_margin -= style_deltas["call_aggression_bonus"]
            call_margin += _lp.get("strength_signal", 0.0) * 0.5
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
        if fragile_river_raise_fold:
            if not anti_lock_call_continue:
                return -1
        if fragile_pair_raise_fold:
            if not anti_lock_call_continue:
                return -1
        if hard_repressure_fold or paired_board_stackoff["severe"]:
            if not anti_lock_call_continue and (not strong_made_continue or river_strict_fold):
                return -1
        if realized_rate < pot_odds + call_margin:
            if not anti_lock_call_continue and (not strong_made_continue or river_strict_fold):
                return -1
        if repeated_raise_trap and (value_profile is None or value_profile["tier"] != "nut"):
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
            and allow_low_frequency_blocker_bluff(req, my_cards, public_cards, blocker_profile, round_idx, anti_bot4["bluff_freq_bonus"])
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
        if not preflop_defensive_only and (win_rate >= max(strong, pot_odds + 0.12) or semi_bluff or flop_checkraise_exploit):
            raise_amount = choose_raise(
                state.get("min_raise_action", state["round_raise"]),
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
                anti_bot4_bonus=anti_bot4["raise_size_bonus"],
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
        and anti_bot4["bluff_freq_bonus"] < 0.05
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
        and anti_bot4["bluff_freq_bonus"] < 0.05
    )
    if thin_static_showdown_control:
        return 0

    river_bluff_threshold = 0.62 - 0.28 * match_profile["bluff_delta"] - anti_bot4["bluff_freq_bonus"] - style_deltas["bluff_freq_bonus"] - (0.03 if _lp.get("passive_count", 0) >= 2 else 0.0)
    probe_fold_threshold = 0.56 - 0.32 * match_profile["bluff_delta"] - anti_bot4["bluff_freq_bonus"] - style_deltas["bluff_freq_bonus"] - (0.03 if _lp.get("passive_count", 0) >= 2 else 0.0)
    semi_bluff_threshold = 0.58 - 0.28 * match_profile["bluff_delta"] - anti_bot4["bluff_freq_bonus"] - style_deltas["bluff_freq_bonus"]
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
        and allow_low_frequency_blocker_bluff(req, my_cards, public_cards, blocker_profile, round_idx, anti_bot4["bluff_freq_bonus"])
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
    if win_rate >= medium or semi_bluff or blocker_bluff or small_probe or check_probe or made_strength >= 0.62 or (value_profile and value_profile["tier"] in ("strong", "nut")):
        raise_amount = choose_raise(
            state.get("min_raise_action", state["round_raise"]),
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
            anti_bot4_bonus=anti_bot4["raise_size_bonus"],
            allow_river_overbet=anti_bot4["river_overbet_enabled"],
        )
        if raise_amount is not None:
            return raise_amount
    return 0
