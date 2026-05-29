"""
Strategy decision functions.
"""
from constants import N_PLAYERS, INITIAL_CHIPS, BIG_BLIND, TOTAL_HANDS, SIMULATIONS_BY_PUBLIC_COUNT, EXTRA_SIMULATIONS_BY_PUBLIC_COUNT
from card_utils import clamp, next_player
from state import (
    estimate_preflop_strength,
    is_preflop_3bet_candidate,
    is_preflop_trash_hand,
    get_remaining_hands,
    get_hand_index,
    collect_latest_requests_by_hand,
    reconstruct_state,
)
from tournament import (
    should_lock_win,
    fold_gives_opponent_lock,
    match_risk_adjustment,
    match_pressure_profile,
    apply_anti_lock_pressure,
    anti_lock_can_continue,
)
from opponent import build_opponent_model, analyze_current_spot
from postflop import (
    made_hand_metric,
    pair_board_profile,
    pair_domination_margin,
    marginal_pair_under_pressure,
    board_texture_profile,
    paired_board_outcome_profile,
    bet_size_bucket,
    value_hand_tier,
    value_bet_plan,
    empty_draw_profile,
    draw_profile,
    draw_call_margin,
    made_flush_profile,
    blocker_bluff_profile,
    allow_low_frequency_blocker_bluff,
    nutted_risk_profile,
    river_thin_value_profile,
    river_bluff_ev,
    donk_bet_profile,
    turn_barrel_profile,
    spr_profile,
)
from simulation import build_opponent_range, estimate_weighted_win_rate


from strategy_helpers import *

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
    preflop_trash_hand = is_preflop_trash_hand(my_cards, preflop_strength) if preflop_strength is not None else False
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
                if not preflop_trash_hand:
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
    line_strength = aggressive_line_strength(spot_info, board_texture) if len(public_cards) >= 3 else 0.0
    check_resistance = check_probe_resistance_margin(spot_info, opponent_model, round_idx) if len(public_cards) >= 3 else 0.0
    paired_board_stackoff = (
        paired_board_stackoff_profile(pair_profile, paired_board_profile, board_texture, spot_info, round_idx)
        if len(public_cards) >= 3
        else {"active": False, "severe": False, "line_strength": 0.0, "size_bucket": "small"}
    )
    spr_info = spr_profile(my_chips, pot)
    donk_info = (
        donk_bet_profile(my_cards, public_cards, pair_profile, board_texture, value_profile, draw_info, spot_info, opponent_model)
        if len(public_cards) >= 3
        else {"eligible": False, "sizing_ratio": 0.0, "reason": "none"}
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

    gift_balance = track_opponent_gift(requests, my_id)
    exploit_lambda = safe_exploitation_lambda(gift_balance, opponent_model["confidence"])

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
    gto_strong = strong + match_adjust + match_profile["threshold_delta"]
    gto_medium = medium + match_adjust + 0.75 * match_profile["threshold_delta"]
    pressure_adjust = opponent_pressure_adjustment(opponent_model, spot_info, round_idx)
    strong += match_adjust + pressure_adjust + match_profile["threshold_delta"]
    medium += match_adjust + pressure_adjust * 0.8 + 0.75 * match_profile["threshold_delta"]
    strong += 0.30 * line_strength + 0.45 * paired_board_stackoff["line_strength"]
    medium += 0.18 * line_strength + 0.22 * paired_board_stackoff["line_strength"]
    strong += 0.30 * check_resistance
    medium += 0.20 * check_resistance
    # SPR-based adjustments: shallower stacks -> lower commitment thresholds
    spr_commitment = spr_info["commitment_level"]
    if spr_commitment == "shallow":
        strong -= 0.02
        medium -= 0.015
    elif spr_commitment == "commit":
        strong -= 0.04
        medium -= 0.03
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

    if exploit_lambda > 0.0:
        strong = (1 - exploit_lambda) * gto_strong + exploit_lambda * strong
        medium = (1 - exploit_lambda) * gto_medium + exploit_lambda * medium

    if state["opponent_allin"]:
        # Premium hand protection: never fold AKs+ (preflop_strength >= 0.72) vs preflop all-in
        if round_idx == 0 and preflop_strength is not None and preflop_strength >= 0.72:
            return -2
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
                pot,
            )
            cbet_rate = opponent_model.get("cbet_rate", 0.55)
            if round_idx == 1 and spot_info["facing_postflop_aggression"] and cbet_rate > 0.60:
                call_margin -= 0.025
            if round_idx == 1 and spot_info["facing_postflop_aggression"] and cbet_rate < 0.35:
                call_margin += 0.025
            if round_idx == 2 and spot_info["facing_postflop_aggression"] and cbet_rate > 0.65:
                call_margin -= 0.018
            if round_idx == 2 and spot_info["facing_postflop_aggression"] and cbet_rate < 0.35:
                call_margin += 0.018
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
        anti_lock_attack = None
        if anti_lock_pressure:
            if round_idx > 0 or preflop_3bet_candidate:
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
            if not anti_lock_call_continue and not strong_made_continue:
                return -1
        if realized_rate < pot_odds + call_margin:
            if not anti_lock_call_continue and not strong_made_continue:
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

    # Enhanced river thin value betting
    if round_idx == 3 and to_call == 0 and win_rate >= 0.52:
        thin_value = river_thin_value_profile(
            my_cards, public_cards, pair_profile, board_texture, value_profile, nutted_risk
        )
        if thin_value["eligible"]:
            sizing_tier = thin_value["sizing_tier"]
            if sizing_tier == "small":
                thin_sizing = pot * 0.40
            elif sizing_tier == "medium":
                thin_sizing = pot * 0.55
            else:
                thin_sizing = pot * 0.70
            thin_amount = max(state["min_raise_action"], int(thin_sizing))
            if thin_amount > to_call and thin_amount < my_chips:
                return thin_amount

    # Donk betting: lead out OOP on favorable boards
    if round_idx > 0 and to_call == 0 and donk_info["eligible"]:
        donk_sizing = int(pot * donk_info["sizing_ratio"] * spr_info["sizing_scale"])
        donk_amount = max(state["min_raise_action"], donk_sizing)
        if donk_amount > to_call and donk_amount < my_chips:
            return donk_amount

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
        and river_bluff_ev(my_cards, public_cards, blocker_profile, pot, opponent_model)["recommended"]
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
    barrel_info = turn_barrel_profile(
        my_cards, public_cards, value_profile, board_texture, draw_info, spot_info, round_idx
    )
    barrel_raise = barrel_info["barrel_eligible"] and win_rate >= medium + 0.08
    barrel_sizing_delta = barrel_info["barrel_sizing_delta"] if barrel_raise else 0.0
    if win_rate >= medium or semi_bluff or blocker_bluff or small_probe or check_probe or made_strength >= 0.62 or (value_profile and value_profile["tier"] in ("strong", "nut")) or barrel_raise:
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
            match_sizing_delta=match_profile["sizing_delta"] + barrel_sizing_delta,
        )
        if raise_amount is not None:
            return raise_amount
    return 0
