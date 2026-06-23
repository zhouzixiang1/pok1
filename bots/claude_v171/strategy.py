import sys
from constants import N_PLAYERS, BIG_BLIND, TOTAL_HANDS, SIMULATIONS_BY_PUBLIC_COUNT, EXTRA_SIMULATIONS_BY_PUBLIC_COUNT
from card_utils import clamp, next_player
from state import (
    reconstruct_state, get_remaining_hands, estimate_preflop_strength,
    is_preflop_3bet_candidate, is_preflop_trash_hand,
    preflop_hand_profile, classify_preflop_hand,
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
    sizing_exploit_adjustment, bluff_heavy_call_widen,
    _street_fold_exploit_sizing_boost,
    _delayed_calldown_bluff,
    exploit_dispatch, river_value_raise_tier,
    _river_value_extraction_amplifier,
    check_raise_pressure, barrel_pressure_profile,
    turn_second_barrel_planner,
    value_maximizer_overbet,
    _river_stackoff_guard,
    _river_value_ship_guard,
    _river_bet_commit_guard,
    _spr_commitment_gate,
    _river_weak_made_hand_gate,
    _turn_oop_pot_control,
    _vulnerable_made_protection_floor,
    sb_open_opp_sizing_delta,
    bb_vs_limp_opp_sizing_delta,
    bb_vs_raise_opp_sizing_delta,
    _preflop_steal_defense_widen,
    _post_missed_cbet_exploit,
    _turn_probe_sizing,
    _multi_street_calldown_tax,
    _opponent_sizing_call_tighten,
    _river_reraise_tighten,
    _opponent_sizing_raise_boost,
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
    street_fold_boost=0.0,
    turn_probe_delta=0.0,
    preflop_opp_size_bb=None,
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
    # v170 NEW OFFENSE: Turn probe sizing delta. Applied AFTER probe_mode cap
    # so it lifts underbet probe lines from 0.25-0.33x toward 0.55-0.65x pot,
    # denying free cards to draws and extracting value from capped ranges.
    if round_idx == 2 and turn_probe_delta > 0.0 and to_call == 0:
        ratio += turn_probe_delta
    thin_cap = None
    if value_plan.get("thin_control", False) and value_profile.get("tier") != "nut":
        thin_cap = 0.30 if round_idx <= 2 else 0.38
        ratio = min(ratio, thin_cap)
    low_ratio = 0.28 if inducing_value else 0.22 if probe_mode or (blocker_bluff and to_call == 0) else 0.40
    if thin_cap is not None:
        low_ratio = min(low_ratio, thin_cap)
    # v149 NEW OFFENSE: per-street fold-exploit sizing boost. Added AFTER all
    # intermediate caps (thin_cap, probe_ratio, blocker_bluff, induce_cap) so
    # the boost survives small-bet lines that would otherwise swallow it, but
    # is suppressed for intentional small-bet modes (probe/blocker/induce).
    if street_fold_boost > 0.0 and not probe_mode and not blocker_bluff and not inducing_value:
        ratio += street_fold_boost
    ratio = clamp(ratio, low_ratio, 1.45)

    amount = int(to_call + pot_after_call * ratio)

    if round_idx == 0 and preflop_strength is not None:
        if spot_name == "sb_open":
            _opp_bb = preflop_opp_size_bb if preflop_opp_size_bb is not None else 0.0
            _base_bb = max(2.0, 2.5 + _opp_bb)  # v144: never below 2.0BB
            desired_total = int((_base_bb + max(0.0, preflop_strength - 0.58) * 2.2) * BIG_BLIND)
            amount = max(amount, desired_total - my_round_bet)
        elif spot_name == "bb_vs_limp":
            _opp_bb = preflop_opp_size_bb if preflop_opp_size_bb is not None else 0.0
            _base_bb = max(2.5, 3.2 + _opp_bb)  # v145: never below 2.5BB
            desired_total = int((_base_bb + max(0.0, preflop_strength - 0.60) * 2.2) * BIG_BLIND)
            amount = max(amount, desired_total - my_round_bet)
        elif spot_name == "bb_vs_raise":
            _opp_bb = preflop_opp_size_bb if preflop_opp_size_bb is not None else 0.0
            _base_bb = max(2.5, 3.5 + _opp_bb)  # v146: never below 2.5BB
            desired_total = int((_base_bb + max(0.0, preflop_strength - 0.58) * 2.2) * BIG_BLIND)
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


def _sb_open_bucket_action(hand_cat, opponent_model, trash_hand):
    open_conf = opponent_model.get('open_response_confidence', 0.0)
    fold_open = opponent_model.get('fold_to_open_preflop', opponent_model.get('fold_to_raise', 0.42))
    threebet = opponent_model.get('threebet_vs_open', 0.16)
    has_open_read = open_conf >= 0.25

    high_fold_bb = has_open_read and fold_open >= 0.55 and threebet <= 0.18
    pressure_bb = has_open_read and threebet >= 0.26
    sticky_bb = has_open_read and fold_open <= 0.34 and threebet < 0.24

    if hand_cat in ('premium', 'strong_pair', 'mid_pair', 'big_cards'):
        return 'raise'

    implied = hand_cat in ('small_pair', 'suited_ace', 'suited_connector', 'broadway_suited')
    marginal = hand_cat == 'playable'

    if implied:
        if high_fold_bb:
            return 'raise'
        if pressure_bb or sticky_bb:
            return 'call'
        return 'raise'

    if marginal:
        if high_fold_bb:
            return 'raise'
        if pressure_bb or sticky_bb:
            return 'fold'
        return 'call'

    if trash_hand:
        return 'raise' if high_fold_bb else 'fold'
    return 'call'


def _bb_vs_raise_bucket_action(hand_cat, opponent_model, pot_odds, preflop_strength, win_rate, trash_hand):
    confidence = opponent_model.get('confidence', 0.0)
    vpip = opponent_model.get('vpip', 0.58)
    pfr = opponent_model.get('pfr', 0.28)
    fold_to_raise = opponent_model.get('fold_to_raise', 0.44)
    has_read = confidence >= 0.20
    loose_opener = has_read and (vpip >= 0.62 or pfr >= 0.34)
    tight_opener = (not has_read) or (pfr <= 0.22 and vpip <= 0.52)
    high_fold = has_read and fold_to_raise >= 0.52

    # v152 NEW: Steal-defense widen — defend playable/mid pairs/speculative
    # vs unknown/loose openers BEFORE the existing tight_opener early-fold.
    _widen = _preflop_steal_defense_widen(hand_cat, opponent_model, pot_odds,
                                           preflop_strength, win_rate, facing_3bet=False)
    if _widen == 'call' and not trash_hand:
        try:
            sys.stderr.write('PREFLOP_DEFEND_WIDEN spot=bb_vs_raise '
                             'hand_cat=%s pot_odds=%.3f conf=%.2f pfr=%.2f\n'
                             % (hand_cat, pot_odds, confidence, pfr))
        except Exception:
            pass
        return 'call'

    if hand_cat in ('premium', 'big_cards') and not trash_hand:
        return 'value_raise'
    # CROSSOVER (from v109/v108/v89): broadway_suited (KQs/KJs/QJs/QTs/JTs)
    # gets a dedicated call/fold decision with implied-odds reasoning.
    # CROSSOVER v116 (v115 × v111 → v116): REVERT v115's broadway widening
    # (pot_odds 0.40) back to v111's tighter 0.36 gate. H2H evidence: v115 loses
    # vs newer aggressive openers where v111 wins (v89 0.50 vs 0.56; v90 0.50 vs
    # 0.58; v95 0.40 vs 0.52; v96 0.30 vs 0.56; v109 0.40 vs 0.53). The 0.40 gate
    # over-defends suited broadways into tight opens (paying off iso-raises with
    # KJs/QTs that need ~4:1 implied odds against tight ranges). KEEP v115's
    # v102 probe_mode fix (crossover below) — that fix targets value-hand sizing,
    # an orthogonal axis from BB defense range.
    if hand_cat == 'broadway_suited' and not trash_hand:
        if pot_odds <= 0.36 or win_rate >= pot_odds - 0.02:
            return 'call'
        return 'fold'
    if hand_cat in ('suited_connector', 'suited_ace', 'small_pair') and not trash_hand:
        if loose_opener and high_fold:
            return 'bluff_raise'
        if pot_odds <= 0.34 or win_rate >= pot_odds - 0.01:
            return 'call'
        return 'fold'
    if hand_cat in ('strong_pair', 'mid_pair'):
        # v146: 3rd-site thin-value raise vs high-fold openers (mid pairs vs
        # SB openers who over-fold to 3bets). Reuses existing high_fold read.
        if hand_cat == 'mid_pair' and high_fold and preflop_strength >= 0.48:
            return 'thin_value_raise'
        return 'call' if pot_odds <= 0.38 or win_rate >= pot_odds else 'fold'
    if hand_cat == 'playable':
        if tight_opener:
            return 'fold'
        if loose_opener and (pot_odds <= 0.30 or preflop_strength >= 0.44):
            return 'call'
        return 'call' if win_rate >= pot_odds + 0.01 else 'fold'
    return 'fold'


def choose_preflop_spot_action(req, state, spot_info, opponent_model, preflop_strength, win_rate, match_profile):
    my_chips = req["my_chips"]
    to_call = state["to_call"]
    match_adjust = match_risk_adjustment(req, req["my_id"], get_remaining_hands(req))
    confidence = opponent_model["confidence"]
    loose_bonus = confidence * max(0.0, opponent_model["vpip"] - 0.55) * 0.03
    trash_hand = is_preflop_trash_hand(req["my_cards"], preflop_strength)

    if spot_info["preflop_spot"] == "sb_open":
        hand_cat = classify_preflop_hand(req['my_cards'])
        action = _sb_open_bucket_action(hand_cat, opponent_model, trash_hand)
        if action == 'raise':
            _sb_opp_size_bb = sb_open_opp_sizing_delta(opponent_model, preflop_strength)
            if _sb_opp_size_bb != 0.0:
                print(f"SB_OPEN_OPP_SIZE delta={_sb_opp_size_bb:+.2f} "
                      f"fold_open={opponent_model.get('fold_to_open_preflop', 0.42):.2f} "
                      f"3bet={opponent_model.get('threebet_vs_open', 0.16):.2f} "
                      f"conf={opponent_model.get('open_response_confidence', 0.0):.2f}",
                      file=sys.stderr)
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
                preflop_opp_size_bb=_sb_opp_size_bb,
            )
            if raise_amount is not None:
                return raise_amount
            return 0 if not trash_hand else -1
        if action == 'fold':
            return -1
        return 0

    if spot_info["preflop_spot"] == "bb_vs_limp":
        iso_threshold = 0.57 + match_adjust - loose_bonus + match_profile["open_delta"]
        iso_threshold -= confidence * max(0.0, opponent_model["vpip"] - 0.58) * 0.08
        iso_threshold -= confidence * max(0.0, opponent_model["fold_to_raise"] - 0.52) * 0.05
        _bb_limp_opp_size_bb = bb_vs_limp_opp_sizing_delta(opponent_model, preflop_strength)
        if _bb_limp_opp_size_bb != 0.0:
            print(f"BB_VS_LIMP_OPP_SIZE delta={_bb_limp_opp_size_bb:+.2f} "
                  f"fold_to_raise={opponent_model.get('fold_to_raise', 0.44):.2f} "
                  f"limp_rate={max(0.0, opponent_model.get('vpip', 0.58) - opponent_model.get('pfr', 0.28)):.2f} "
                  f"conf={opponent_model.get('confidence', 0.0):.2f}",
                  file=sys.stderr)
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
            preflop_opp_size_bb=_bb_limp_opp_size_bb,
        )
        if not trash_hand and preflop_strength >= iso_threshold and raise_amount is not None:
            return raise_amount
        return 0

    elif spot_info['preflop_spot'] == 'bb_vs_raise':
        pot_odds_pf = to_call / (to_call + state['pot']) if to_call > 0 else 0.0
        hand_cat = classify_preflop_hand(req['my_cards'])
        # v146: Opponent-adaptive 3bet sizing delta (3rd preflop offense axis).
        _bb_raise_opp_size_bb = bb_vs_raise_opp_sizing_delta(opponent_model, preflop_strength)
        if _bb_raise_opp_size_bb != 0.0:
            print(f"BB_VS_RAISE_OPP_SIZE delta={_bb_raise_opp_size_bb:+.2f} "
                  f"fold_to_raise={opponent_model.get('fold_to_raise', 0.44):.2f} "
                  f"3bet={opponent_model.get('threebet_vs_open', 0.16):.2f} "
                  f"conf={opponent_model.get('open_response_confidence', 0.0):.2f}",
                  file=sys.stderr)
        defend_action = _bb_vs_raise_bucket_action(
            hand_cat, opponent_model, pot_odds_pf, preflop_strength, win_rate, trash_hand
        )
        if defend_action == 'value_raise':
            raise_amount = choose_raise(
                state['min_raise_action'], my_chips, state['my_round_bet'],
                to_call, state['pot'], max(win_rate, preflop_strength),
                0, 'bb_vs_raise', preflop_strength,
                True, opponent_model,
                match_sizing_delta=match_profile['sizing_delta'],
                preflop_opp_size_bb=_bb_raise_opp_size_bb,
            )
            return raise_amount if raise_amount is not None else 0
        if defend_action == 'bluff_raise':
            bluff_roll = (hash(tuple(req['my_cards'])) % 100) / 100.0
            if bluff_roll < 0.22:
                raise_amount = choose_raise(
                    state['min_raise_action'], my_chips, state['my_round_bet'],
                    to_call, state['pot'], max(win_rate, preflop_strength),
                    0, 'bb_vs_raise', preflop_strength,
                    True, opponent_model,
                    match_sizing_delta=match_profile['sizing_delta'],
                    preflop_opp_size_bb=_bb_raise_opp_size_bb,
                )
                if raise_amount is not None:
                    return raise_amount
            return 0
        if defend_action == 'thin_value_raise':
            raise_amount = choose_raise(
                state['min_raise_action'], my_chips, state['my_round_bet'],
                to_call, state['pot'], max(win_rate, preflop_strength),
                0, 'bb_vs_raise', preflop_strength,
                True, opponent_model,
                match_sizing_delta=match_profile['sizing_delta'],
                preflop_opp_size_bb=_bb_raise_opp_size_bb,
            )
            return raise_amount if raise_amount is not None else 0
        if defend_action == 'call':
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
        # Call with most limp-range hands, including broadway suited
        # (KQs/KJs/QJs/QTs/JTs) which flop playable draws / two-pair+ often
        # enough to justify the call vs limp.
        _hand_cat_iso = classify_preflop_hand(req['my_cards'])
        if _hand_cat_iso == 'broadway_suited' and not trash_hand:
            return 0
        if preflop_strength >= 0.34 or win_rate >= pot_odds_iso - 0.03:
            return 0
        return -1

    elif spot_info['preflop_spot'] == 'sb_vs_reraise':
        pot_odds_sbr = to_call / (to_call + state['pot']) if to_call > 0 else 0.0
        hand_cat_sbr = classify_preflop_hand(req['my_cards'])
        _prof_sbr = preflop_hand_profile(req['my_cards'])
        is_aks = (hand_cat_sbr == 'big_cards' and _prof_sbr['suited']
                  and _prof_sbr['high'] == 14 and _prof_sbr['low'] == 13)
        # 4bet/jam with TRUE premiums only: QQ+, KK, AA, and AKs.
        # NOTE: the old >=0.78 strength gate 4-bet 88+ because estimate_preflop_strength
        # saturates 88-AA to 1.0. The hand-class gate restricts to QQ+/AKs.
        if hand_cat_sbr == 'premium' or is_aks:
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
        # Facing all-in: call with strong pairs (TT+), big cards (AK/AQ), AKs
        if state.get("opponent_allin", False):
            if hand_cat_sbr in ('premium', 'strong_pair', 'big_cards'):
                return 0
            return -1
        # Non-all-in: call with strong hands (TT+, AK/AQ) if pot odds reasonable
        if hand_cat_sbr in ('premium', 'strong_pair', 'big_cards') and win_rate >= pot_odds_sbr - 0.03:
            return 0
        # 4-bet light: exploit opponents who 3-bet wide with playable hands
        light_4bet = _should_4bet_light(req["my_cards"], preflop_strength, opponent_model, state, my_chips)
        if light_4bet > 0:
            return light_4bet
        # Fold everything else
        # v152 NEW: Implied-odds defense vs frequent 3-bettors
        _implied_call = _preflop_steal_defense_widen(hand_cat_sbr, opponent_model,
                                                      pot_odds_sbr, preflop_strength,
                                                      win_rate, facing_3bet=True)
        if _implied_call == 'call':
            try:
                sys.stderr.write('PREFLOP_DEFEND_WIDEN spot=sb_vs_3bet '
                                 'hand_cat=%s pot_odds=%.3f 3bet=%.2f\n'
                                 % (hand_cat_sbr, pot_odds_sbr,
                                    opponent_model.get('threebet_vs_open', 0.16)))
            except Exception:
                pass
            return 0
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

    # NEW (v137): sizing-tendency defense relaxation vs confirmed polarized
    # overbettors. Targets exploitability probe weakness: 0% WR vs 2x pot bets.
    # Confirmed overbettors (Worker 1's sizing_tendency signal) polarize —
    # their 2x-pot bets include bluffs. Folding every marginal made hand to a
    # "large" bet (ratio > 0.75) surrenders equity vs their polar range.
    #
    # CRITICAL PLACEMENT (fixes project's #1 failure mode: dispatch-order
    # shadow per master_prompt PRIMARY reviewer risk). This block is
    # intentionally placed BEFORE every upstream fold gate (round_idx
    # size-bucket folds, SPR commitment, value-heavy opponent-model fold,
    # river multi-barrel, check_raise_pressure, barrel_pressure_profile).
    # The previous v137 attempt placed the override at the END, which made
    # it a FUNCTIONAL NO-OP: when an upstream gate returns True=fold,
    # control never reaches the trailing override. Placing it first (after
    # the early-return guards and after `size_bucket` is computed, since we
    # depend on `size_bucket == 'large'`) is the only way to relax defense
    # vs a confirmed polarized overbettor.
    #
    # The override fires ONLY when ALL hold:
    #   - opponent_model has sizing_tendency (Worker 1 owns the producer)
    #   - samples >= 8 AND confidence >= 0.30 (statistical floor)
    #   - tendency == 'overbettor'
    #   - current size_bucket == 'large'
    #   - NOT (made_strength < 0.15 AND no draw) — absolute air still folds
    # Otherwise, existing fold logic runs unchanged.
    if opponent_model is not None:
        sizing = opponent_model.get('sizing_tendency')
        # Two-part gate separates opponent-signal validity from hand-strength
        # so the intent is self-documenting: trust the model enough to relax
        # defense, but still release hands with near-zero equity.
        sizing_signal_valid = (
            sizing is not None
            and sizing.get('samples', 0) >= 8
            and sizing.get('confidence', 0.0) >= 0.30
            and sizing.get('tendency') == 'overbettor'
        )
        if sizing_signal_valid and size_bucket == 'large':
            # Pull the per-street overbet fraction for the CURRENT street
            # from Worker 1's sizing_tendency signal. Consumes a previously-
            # dead field (per_street_overbet was produced but never read),
            # surfacing whether the overbet tendency holds on this street
            # rather than only in aggregate.
            street_over_rate = sizing.get(
                'per_street_overbet', {}
            ).get(round_idx, 0.0)
            absolute_air = made_strength < 0.15 and not has_draw
            if not absolute_air:
                # Telemetry: prove this override is reachable and firing
                # during daemon evaluation. Without this log line, a
                # structurally-dead override (unreachable due to dispatch-
                # order shadow or an upstream gate returning first) is
                # indistinguishable from a live defense relaxation. Grep
                # for SIZING_RELAX_FIRE in stderr after daemon runs >=30
                # games vs overbettor opponents to confirm the override is
                # LIVE (project's recurring INERTNESS failure mode —
                # v127/v128/v130 all shipped dead fold gates).
                try:
                    sys.stderr.write(
                        "SIZING_RELAX_FIRE r=%d made=%.2f draw=%.2f ratio=%.2f "
                        "samples=%d conf=%.2f street_over=%.2f\n"
                        % (round_idx, made_strength, draw_strength,
                           spot_info.get('last_raise_pot_ratio', 0.0),
                           sizing.get('samples', 0),
                           sizing.get('confidence', 0.0),
                           street_over_rate)
                    )
                except Exception:
                    pass
                return False  # defense relaxation: do NOT fold to confirmed polarized overbettor

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

    # Crossover from v128: Check-raise pressure fold gate (delivers
    # check_raise_freq mandate). Folds marginal hands to a LIVE opponent
    # check-raise trap (opp_current_round_check_count>=1 AND
    # opp_current_round_bet_count>=1). Targets the 0%-postflop-fold leak by
    # eliminating river CR stack-offs. Threshold 0.42 river catches one-pair+
    # air, preserves two-pair+; 0.30 turn catches air+weak-one-pair.
    if opponent_model is not None:
        cr_active, cr_severity = check_raise_pressure(spot_info, opponent_model)
        if cr_active:
            river_threshold = 0.42 - cr_severity
            turn_threshold = 0.30 - cr_severity
            if round_idx == 3 and made_strength < river_threshold and not has_draw:
                return True
            if round_idx == 2 and made_strength < turn_threshold and not has_draw:
                return True

    # Crossover from v128 + MUTATION (option a): barrel_pressure fold gate.
    # Folds weak/mid-one-pair and air facing single+ barrel from frequent
    # barreler. MUTATION: river_thr 0.32 -> 0.36 (+12.5%) to cover the empty
    # HAND_CLASS_SCORE band (mid one-pair ~0.32-0.36) where v118 over-calls
    # vs modern barrel-aggressive bots (loses v93/v95/v129). Capped by
    # has_draw and not strong-tier upstream; preserves strong one-pair+.
    if opponent_model is not None:
        bp_active, bp_severity = barrel_pressure_profile(spot_info, opponent_model, round_idx)
        if bp_active:
            river_thr = 0.36 - bp_severity  # MUTATION: 0.32 -> 0.36
            turn_thr = 0.24 - bp_severity
            if round_idx == 3 and made_strength < river_thr and not has_draw:
                return True
            if round_idx == 2 and made_strength < turn_thr and not has_draw:
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
        # Crossover from v128: Check-raise + barrel pressure tighten all-in
        # call willingness. jam_buffer = required win_rate margin; higher =
        # tighter call. Fires only on river/turn facing live trap or barrel.
        if opponent_model is not None:
            cr_active, cr_severity = check_raise_pressure(spot_info, opponent_model)
            if cr_active:
                jam_buffer += cr_severity
            bp_active, bp_severity = barrel_pressure_profile(spot_info, opponent_model, round_idx)
            if bp_active:
                jam_buffer += bp_severity
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
        # v154 FIX: SPR commitment gate — RELOCATED from dead `to_call>=my_chips`
        # block (v147-v153: ZERO fires across 153,484 decisions). Now in the
        # ACTIVE `opponent_allin` block. Folds marginal hands (made_strength
        # < 0.55, tier thin/none) facing all-in when pot_odds > win_rate+0.03.
        # Preserves draws per street: flop >=0.12, turn >=0.15, river value_heavy
        # >=0.18. Targets -20k stack-offs (TT one-pair, J2 two-pair, etc.).
        if _spr_commitment_gate(round_idx, my_chips, made_strength, value_profile,
                                to_call, pot, win_rate, anti_lock_pressure,
                                line_profile.get('line_label', 'balanced'),
                                draw_strength):
            print(f"SPR_FOLD round={round_idx} made={made_strength:.2f} "
                  f"tier={value_profile.get('tier','none') if value_profile else 'none'} "
                  f"to_call={to_call} pot={pot} win={win_rate:.3f} "
                  f"pot_odds={to_call/(pot+to_call):.3f}"
                  f" [ACTIVE_PATH]",
                  file=sys.stderr)
            return -1
        # v154 NEW: River weak-made-hand all-in gate. Catches the 0.55-0.60
        # made_strength band that the SPR gate (made < 0.55) misses. Folds when:
        # river + large pot (>= 65% stack) + weak made hand + no live draw.
        if _river_weak_made_hand_gate(round_idx, pot, my_chips, made_strength,
                                      value_profile, draw_strength,
                                      anti_lock_pressure):
            print(f"RIVER_WEAK_FOLD made={made_strength:.2f} "
                  f"tier={value_profile.get('tier','none') if value_profile else 'none'} "
                  f"pot={pot} my_chips={my_chips} win={win_rate:.3f}",
                  file=sys.stderr)
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
        # v147 NEW: SPR commitment gate — closed-form fold for marginal river
        # hands facing stack-covering all-ins. Fixes 7-generation placement
        # shadow: L1084 _river_stackoff_guard is UNREACHABLE when to_call >= my_chips
        # because this block returns at L1076 before that guard ever runs.
        if _spr_commitment_gate(round_idx, my_chips, made_strength, value_profile,
                                to_call, pot, win_rate, anti_lock_pressure,
                                line_profile.get('line_label', 'balanced'),
                                draw_strength):
            print(f"SPR_FOLD round={round_idx} made={made_strength:.2f} "
                  f"tier={value_profile.get('tier','none') if value_profile else 'none'} "
                  f"to_call={to_call} pot={pot} win={win_rate:.3f} "
                  f"pot_odds={to_call/(pot+to_call):.3f}",
                  file=sys.stderr)
            return -1
        shove_buffer = clamp(shove_buffer, -0.05 if anti_lock_pressure else 0.0, 0.14)
        return -2 if win_rate >= shove_odds + shove_buffer or anti_lock_shove_continue else -1

    if to_call > 0:
        # NEW v138: River stack-off guard — fold weak hands facing large bets
        # BEFORE any call_margin/realized_rate computation. Targets -15.5k to
        # -20k river stack-off leak (~76 pairs v14-v137, 0% postflop fold).
        # Persistent fixture: `python strategy_helpers.py` must pass all 10 cases.
        if round_idx == 3:
            if _river_stackoff_guard(made_strength, value_profile, spot_info,
                                     opponent_model, my_chips, pot, to_call):
                return -1

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
            # Bluff-catch widening vs detected bluff_heavy opponents (CALL path, not fold)
            call_margin += bluff_heavy_call_widen(
                line_profile, value_profile, made_strength, draw_strength,
                round_idx, opponent_model,
            )
            if round_idx == 3 and made_strength < 0.40 and not (blocker_profile and blocker_profile["eligible"]):
                call_margin += 0.04
            # v167: River large-bet call_margin boost for marginal hands.
            if round_idx == 3 and made_strength < 0.55 and bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large"):
                call_margin += 0.10
            if round_idx == 3 and paired_board_profile is not None and paired_board_profile["fold_to_raise"]:
                call_margin += 0.05
            # v169: River re-raise polarization tighten for strong-but-not-nut hands.
            if round_idx == 3:
                call_margin += _river_reraise_tighten(
                    state, spot_info, made_strength, value_profile,
                    round_idx, board_texture, pair_profile,
                )
            # Crossover from v128: Check-raise + barrel pressure tighten call
            # threshold (more folds). call_margin = required safety margin;
            # higher = harder to call. Parallel to the jam_buffer gate above.
            if opponent_model is not None:
                cr_active, cr_severity = check_raise_pressure(spot_info, opponent_model)
                if cr_active:
                    call_margin += cr_severity
                bp_active, bp_severity = barrel_pressure_profile(spot_info, opponent_model, round_idx)
                if bp_active:
                    call_margin += bp_severity
            # v157 NEW: Multi-street cumulative call-down tax. Raises the
            # call bar on the LIVE to_call>0 path when opp has bet on >=2
            # postflop streets. Targets the 0%-fold leak (~620 versions).
            # NOT a fold-side gate (EXHAUSTED v135-v154) — a margin additive.
            call_margin += _multi_street_calldown_tax(
                spot_info, made_strength, draw_strength, value_profile, round_idx,
            )
            # v166: Opponent sizing tell — tighten call margin for large-bet-heavy opps
            call_margin += _opponent_sizing_call_tighten(
                spot_info, opponent_model, round_idx, spot_info["has_position"],
            )
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
            # v167: Skip the -0.07 reduction on river with medium or worse hands.
            if not (round_idx == 3 and made_strength < 0.55 and bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large")):
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
        street_fold_boost = _street_fold_exploit_sizing_boost(opponent_model, round_idx)
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
                street_fold_boost=street_fold_boost,
            )
            if raise_amount is not None and raise_amount > to_call:
                # v140 NEW: _river_value_ship_guard — BET-side river ship guard
                # (permitted by direction audit; fold-side _river_stackoff_guard
                # is FORBIDDEN/EXHAUSTED). Downgrades the bot's OWN non-nut
                # river raise committing >=25% stack to a call. Targets
                # G4H45/G6H15 -20k stack-offs.
                if round_idx == 3 and not anti_lock_pressure:
                    if _river_value_ship_guard(round_idx, my_chips, made_strength,
                                                value_profile, board_texture,
                                                pair_profile, raise_amount):
                        return 0
                # v142 NEW: Made-hand protection floor (flop/turn value-raise
                # vs sticky opp). Lifts thin/strong-tier underbet sizing to
                # 0.55-0.70x pot on draw-heavy boards. PARAMETER_TUNING EXEMPT
                # (NEW opponent-signal gating: vm_idx + passivity_score).
                if round_idx in (1, 2) and not anti_lock_pressure:
                    _prot_floor = _vulnerable_made_protection_floor(
                        round_idx, to_call, made_strength, value_profile,
                        board_texture, pair_profile, opponent_model,
                        state['min_raise_action'], state['my_round_bet'],
                        my_chips, pot,
                    )
                    if _prot_floor is not None and _prot_floor > raise_amount:
                        raise_amount = _prot_floor
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

    # v151 NEW: Delayed bluff vs opponents with street-declining call-down.
    # Fires on turn/river to_call==0 with air vs sticky-flop/foldy-later opps.
    # NEW signal: calldown_profile (per-street call-down rate). No prior
    # function reads this. Sited BEFORE bad_river_bluff_candidate to avoid
    # preemption (that check blocks made 0.18-0.40; ours targets <0.28 air).
    if to_call == 0 and round_idx in (2, 3):
        _delayed_bluff = _delayed_calldown_bluff(
            round_idx, to_call, made_strength, draw_strength,
            board_texture, opponent_model, pot, my_chips,
            state['min_raise_action'], state['my_round_bet'],
        )
        if _delayed_bluff is not None:
            return _delayed_bluff

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
    # v139 NEW: Turn OOP pot-control — single computation, 3 dispatch wires.
    # Downsizes any turn bet on deteriorated boards when OOP with marginal hand
    # in mid-SPR. Attacks 0%-fold/stack-off leak UPSTREAM (not at river call).
    _turn_pot_cap = (_turn_oop_pot_control(
        round_idx, to_call, spot_info, made_strength, draw_strength,
        value_profile, board_texture, public_cards, my_chips, pot,
        opponent_model, anti_lock_pressure, state['min_raise_action'],
    ) if round_idx == 2 and to_call == 0 else None)
    if round_idx == 3 and to_call == 0:
        # v155 NEW: Barrel-abandonment exploit on river. When opponent checked
        # river after barreling turn (or gave up range advantage), they are
        # capped. Bet for value / thin-value before falling to vm_overbet path.
        # BARREL_ABANDON stderr telemetry marks this dispatch site.
        _missed_river = _post_missed_cbet_exploit(
            round_idx, spot_info, opponent_model, made_strength,
            draw_strength, value_profile, pot, my_chips,
            state['min_raise_action'], state['my_round_bet'],
        )
        if _missed_river > 0:
            _base_river = 0.50  # larger base on river for value extraction
            _target_r = int(pot * (_base_river + _missed_river))
            _target_r = max(_target_r, state['min_raise_action'])
            if _target_r < my_chips:
                return _target_r
        _vm_overbet = value_maximizer_overbet(
            round_idx, to_call, made_strength, value_profile, board_texture,
            opponent_model, pot, my_chips, state['min_raise_action'],
            state['my_round_bet'], nutted_risk['risk'],
        )
        if _vm_overbet is not None:
            # v143 NEW: _river_bet_commit_guard — extends _river_value_ship_guard
            # to the to_call==0 river bet path. Blocks non-nut big-commit river
            # bets (open-jams) that the v140 guard could not reach. Targets
            # G3H25 (AJ one-pair open-jam -20000) and G2H44 (AQ open-jam -20000).
            _guarded = _river_bet_commit_guard(_vm_overbet, round_idx, my_chips, made_strength,
                                               value_profile, board_texture, pair_profile,
                                               anti_lock_pressure)
            if _guarded is not None and _guarded > 0:
                return _guarded
        # v141 NEW: _river_value_extraction_amplifier — 3rd classify_sizing_tendency
        # wired site (OFFENSIVE). Boosts river value sizing 0.65-0.90x vs
        # moderately sticky opponents with thin-to-strong made hands on static
        # boards. Fills the gap between value_maximizer_overbet (vm_idx>=0.75)
        # and river_value_raise_tier (no opponent gating). Satisfies v140 critic
        # mandate of >=3 sizing_tendency sites (currently 2, both DEFENSIVE;
        # this is the 1st OFFENSIVE site).
        _rva = _river_value_extraction_amplifier(
            round_idx, to_call, made_strength, value_profile, board_texture,
            opponent_model, pot, my_chips, state['min_raise_action'],
            state['my_round_bet'],
        )
        if _rva is not None:
            # v143 NEW: _river_bet_commit_guard — guard the river bet path.
            _guarded = _river_bet_commit_guard(_rva, round_idx, my_chips, made_strength,
                                               value_profile, board_texture, pair_profile,
                                               anti_lock_pressure)
            if _guarded is not None and _guarded > 0:
                return _guarded
        _rvr = river_value_raise_tier(
            round_idx, to_call, made_strength, value_profile, board_texture,
            opponent_model, pot, my_chips, state['min_raise_action'], state['my_round_bet'],
        )
        if _rvr is not None:
            # v143 NEW: _river_bet_commit_guard — guard the river bet path.
            _guarded = _river_bet_commit_guard(_rvr, round_idx, my_chips, made_strength,
                                               value_profile, board_texture, pair_profile,
                                               anti_lock_pressure)
            if _guarded is not None and _guarded > 0:
                return _guarded
    if big_pot and round_idx == 3 and (value_profile is None or value_profile["tier"] not in ("strong", "nut")):
        if blocker_profile is None or not blocker_profile["eligible"]:
            return 0
    # Crossover v130 x v129 OFFENSIVE: re-imported turn_second_barrel_planner
    # from v129. Fires a turn value second-barrel when bot was flop aggressor
    # with top-pair-good-kicker+ on non-deteriorated board. Lifts turn-barrel
    # rate from ~33.5% baseline toward 45-55% in eligible value spots.
    # ORTHOGONAL to v130's defensive CR/BP fold-gates (does NOT subtract from
    # call_margin). Targets v130's losses vs v111(0.45) / v116(0.50) /
    # v121(0.50) where v129 (with this primitive) wins 0.55 / 0.575 / 0.575.
    if round_idx == 2 and to_call == 0:
        if _turn_pot_cap is not None:
            return _turn_pot_cap
        _vm_overbet = value_maximizer_overbet(
            round_idx, to_call, made_strength, value_profile, board_texture,
            opponent_model, pot, my_chips, state['min_raise_action'],
            state['my_round_bet'], nutted_risk['risk'],
        )
        if _vm_overbet is not None:
            return _vm_overbet
        _turn_barrel = turn_second_barrel_planner(
            round_idx, to_call, my_id, req.get('history', []),
            pair_profile, value_profile, made_strength,
            board_texture, opponent_model,
            pot, my_chips, state['min_raise_action'], state['my_round_bet'],
        )
        if _turn_barrel is not None:
            return _turn_barrel
        # v155 NEW: Barrel-abandonment exploit on turn. When opponent checked
        # back as the prior-street aggressor (giving up range advantage), widen
        # our turn barrel range and size up. Targets v130/v139 weak matchups
        # (30% WR @ 10g). BARREL_ABANDON stderr telemetry marks this dispatch.
        _missed_cbet_delta = _post_missed_cbet_exploit(
            round_idx, spot_info, opponent_model, made_strength,
            draw_strength, value_profile, pot, my_chips,
            state['min_raise_action'], state['my_round_bet'],
        )
        if _missed_cbet_delta > 0:
            _base_ratio = 0.40  # base turn bet sizing
            _target = int(pot * (_base_ratio + _missed_cbet_delta))
            _target = max(_target, state['min_raise_action'])
            if _target < my_chips:
                return _target
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

    # v155 NEW: Barrel-abandonment exploit (late dispatch). Catches missed-cbet
    # spots on any street when other bet paths (turn_second_barrel_planner,
    # value_maximizer_overbet, river_value_raise_tier) didn't fire.
    # BARREL_ABANDON stderr telemetry marks this dispatch site.
    if to_call == 0 and round_idx in (2, 3):
        _missed_late = _post_missed_cbet_exploit(
            round_idx, spot_info, opponent_model, made_strength,
            draw_strength, value_profile, pot, my_chips,
            state['min_raise_action'], state['my_round_bet'],
        )
        if _missed_late > 0:
            # v170 NEW OFFENSE: turn probe sizing delta added to barrel-abandon
            # late dispatch line — lifts sizing vs weak/capped opponent ranges.
            _tp_delta_late = _turn_probe_sizing(
                round_idx, to_call, spot_info, opponent_model,
                made_strength, value_profile, board_texture, draw_strength)
            _ratio_late = 0.45 + _missed_late + _tp_delta_late
            _target_late = int(pot * _ratio_late)
            _target_late = max(_target_late, state['min_raise_action'])
            if _target_late < my_chips:
                return _target_late

    # Donk bet: BB leads into PFR on favorable flop textures
    donk = should_donk_bet(
        round_idx, to_call, spot_info, value_profile, board_texture,
        made_strength, draw_strength, draw_info, opponent_model,
        my_cards, public_cards, pot, req.get("history", []), state,
    )
    if donk["eligible"]:
        # v150: 3rd dispatch site for _street_fold_exploit_sizing_boost.
        # Donk path bypasses choose_raise(), so the boost must be applied here
        # directly to the donk_probe_sizing ratio. Closes the gap identified by
        # direction audit v150: previously the boost fired only on the 2
        # choose_raise() call sites, missing donk/probe value-raise lines.
        _donk_fold_boost = _street_fold_exploit_sizing_boost(opponent_model, round_idx)
        _donk_station_delta = _opponent_sizing_raise_boost(opponent_model, round_idx)
        raise_amount = donk_probe_sizing(
            donk["ratio"] + _donk_fold_boost + _donk_station_delta, to_call, pot,
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
        # v150: 4th dispatch site for _street_fold_exploit_sizing_boost.
        # Probe path (like donk) bypasses choose_raise(); apply the boost here
        # so turn/river probe bets also size up vs high-folding opponents.
        _probe_fold_boost = _street_fold_exploit_sizing_boost(opponent_model, round_idx)
        _probe_station_delta = _opponent_sizing_raise_boost(opponent_model, round_idx)
        # v170 NEW OFFENSE: turn probe sizing delta — sizes up probe bets on the
        # turn vs weak/capped opponent ranges (checked-to-us / low flop aggression).
        _tp_delta_probe = _turn_probe_sizing(
            round_idx, to_call, spot_info, opponent_model,
            made_strength, value_profile, board_texture, draw_strength)
        raise_amount = donk_probe_sizing(
            probe["ratio"] + _probe_fold_boost + _tp_delta_probe + _probe_station_delta, to_call, pot,
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
    street_fold_boost = _street_fold_exploit_sizing_boost(opponent_model, round_idx)
    exploit = exploit_dispatch(opponent_model, round_idx, value_profile, made_strength)
    exploit_sizing = exploit['value_boost']
    _station_sizing_delta = _opponent_sizing_raise_boost(opponent_model, round_idx)
    if win_rate >= medium or semi_bluff or blocker_bluff or small_probe or check_probe or made_strength >= 0.62 or (value_profile and value_profile["tier"] in ("strong", "nut")) or exploit['should_barrel']:
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
        # v170 NEW OFFENSE: turn probe sizing delta — computed before the main
        # choose_raise call so it lifts turn bets vs weak/capped opponent ranges.
        _tp_delta_main = _turn_probe_sizing(
            round_idx, to_call, spot_info, opponent_model,
            made_strength, value_profile, board_texture, draw_strength)
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
            # CROSSOVER (v111×v104 → v115): import v104's probe_mode simplification.
            # v111 inherited a v101-era probe_mode thin-value extension from its
            # v100→v111 lineage that the v102 fix REMOVED because it bled value-hand
            # sizing from 0.60-0.85x down to 0.33-0.41x (the probe_ratio cap inside
            # choose_raise). v104's lineage (v101→v102→...→v104) carries the v102
            # fix; v111 was missing it. Removing the thin-value probe extension
            # restores correct value-hand sizing without touching defensive guards.
            probe_mode=check_probe or small_probe,
            induce_mode=induce_nut_value or value_plan.get("induce", False),
            nutted_risk_score=nutted_risk["risk"],
            match_sizing_delta=match_profile["sizing_delta"],
            sizing_exploit_delta=sizing_delta + exploit_sizing + _station_sizing_delta,
            street_fold_boost=street_fold_boost,
            turn_probe_delta=_tp_delta_main,
        )
        if raise_amount is not None:
            # v142 NEW: Made-hand protection floor (flop/turn own-value-bet
            # vs sticky opp). Lifts underbet value sizing on draw-heavy boards
            # when bot is the aggressor. PARAMETER_TUNING EXEMPT (NEW gating).
            if round_idx in (1, 2) and not anti_lock_pressure:
                _prot_floor = _vulnerable_made_protection_floor(
                    round_idx, to_call, made_strength, value_profile,
                    board_texture, pair_profile, opponent_model,
                    state['min_raise_action'], state['my_round_bet'],
                    my_chips, pot,
                )
                if _prot_floor is not None and _prot_floor > raise_amount:
                    raise_amount = _prot_floor
            # v143 NEW: _river_bet_commit_guard — extends _river_value_ship_guard
            # to the to_call==0 river bet path. Called UNCONDITIONALLY so the
            # flop/turn value path (and anti_lock endgame) is NOT regressed: the
            # guard no-ops (returns raise_amount unchanged) on non-river and on
            # anti_lock_pressure, mirroring the unconditional pattern at sites
            # 1-3. On river, if it blocks a non-nut big-commit bet it returns 0
            # and we fall through to `return 0` (check). Fixes the regression
            # where the original `if round_idx==3` gate made flop/turn fall to 0.
            _guarded = _river_bet_commit_guard(raise_amount, round_idx, my_chips, made_strength,
                                               value_profile, board_texture, pair_profile,
                                               anti_lock_pressure)
            if _guarded is not None and _guarded > 0:
                return _guarded
        return 0
    return 0
