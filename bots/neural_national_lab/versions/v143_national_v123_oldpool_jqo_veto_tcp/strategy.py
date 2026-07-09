import sys
from constants import (
    N_PLAYERS, BIG_BLIND, TOTAL_HANDS, SMALL_BLIND,
    SIMULATIONS_BY_PUBLIC_COUNT, EXTRA_SIMULATIONS_BY_PUBLIC_COUNT,
    CRITICAL_SPOT_RATIO, CRITICAL_SPOT_BB_MULT,
    RAISE_RATIO_PREFLOP_OPEN, RAISE_RATIO_PREFLOP_FACING, RAISE_RATIO_FLOP, RAISE_RATIO_TURN, RAISE_RATIO_RIVER,
    SB_OPEN_BASE_BB, SB_OPEN_STRENGTH_SCALE, SB_OPEN_STRENGTH_OFFSET,
    SB_OPEN_FOLD_SIZE_SCALE, SB_OPEN_FOLD_SIGNAL_RANGE,
    BB_ISO_BASE_BB, BB_ISO_STRENGTH_SCALE, BB_ISO_STRENGTH_OFFSET,
    RAISE_MAX_RATIO, OVERBET_MAX_RATIO,
    BLOCKER_BLUFF_CAP_BASE, BLOCKER_BLUFF_WET_SCALE, BLOCKER_BLUFF_STREET_SCALE,
    PASSIVE_THIN_VALUE_MAX_RATIO,
    PRIOR_POSTFLOP_AGGR, PRIOR_FOLD_TO_RAISE,
    PRIOR_VPIP, PRIOR_PFR, PRIOR_ALLIN_RATE,
    PRIOR_BARREL_FREQ, PRIOR_RIVER_AGGR, PRIOR_FLOP_AGGR,
    CONFIDENCE_OFFSET, CONFIDENCE_SCALE,
    PASSIVE_AGGR_MAX, PASSIVE_VPIP_MIN, PASSIVE_BARREL_MAX, PASSIVE_CONFIDENCE_GATE,
    SB_OPEN_THRESHOLD, SB_OPEN_MATCH_ADJ, SB_LIMP_THRESHOLD,
    BB_ISO_THRESHOLD, BB_VPIP_FOLD_ADJUST_SCALE, BB_FTR_FOLD_ADJUST_SCALE,
    BB_VALUE_3BET_THRESHOLD, BB_BLUFF_3BET_LOW, BB_BLUFF_3BET_HIGH, BB_BLUFF_3BET_FREQ,
    BB_CALL_THRESHOLD,
    SB_PREMIUM_4BET_THRESHOLD, SB_FACING_ALLIN_CALL, SB_VS_RERAISE_CALL,
    SB_VS_RERAISE_POT_SLACK,
    LIGHT_4BET_MIN_CONFIDENCE, LIGHT_4BET_MIN_OPP_PFR, LIGHT_4BET_MAX_OPP_4BET,
    LIGHT_4BET_STRENGTH_LOW, LIGHT_4BET_STRENGTH_HIGH, LIGHT_4BET_FREQ_ROLL_CAP,
    LIGHT_4BET_SIZE_MULT, LIGHT_4BET_STACK_CAP, LIGHT_4BET_HALF_STACK_CAP,
    TRASH_STRENGTH_THRESHOLD,
    ANTI_LOCK_PREFLOP_OPEN_RATIO, ANTI_LOCK_PREFLOP_FACING_RATIO,
    ANTI_LOCK_FLOP_RATIO, ANTI_LOCK_TURN_RATIO, ANTI_LOCK_RIVER_RATIO,
    ANTI_LOCK_DYNAMIC_MULT, ANTI_LOCK_BLOCKER_DRAW_MULT, ANTI_LOCK_WEAK_SHOWDOWN_MULT,
    ANTI_LOCK_JAM_STACK_RATIO,
    STRONG_THRESHOLD, MEDIUM_THRESHOLD,
    THRESHOLD_POS_IP_STRONG, THRESHOLD_POS_IP_MEDIUM,
    THRESHOLD_POS_OOP_STRONG, THRESHOLD_POS_OOP_MEDIUM,
    THRESHOLD_PREMIUM_STRONG, THRESHOLD_PREMIUM_MEDIUM, THRESHOLD_PREMIUM_FLOOR,
    THRESHOLD_WEAK_STRONG, THRESHOLD_WEAK_MEDIUM, THRESHOLD_WEAK_FLOOR,
    JAM_BASE_BUFFER, JAM_STRONG_SCALE, JAM_STRONG_OFFSET, JAM_THIN_BONUS,
    JAM_MATCH_PROTECT_SCALE, JAM_BUFFER_CAP, JAM_ANTI_LOCK_PENALTY,
    SHOVE_BASE_BUFFER, SHOVE_STRONG_OFFSET,
    PREFLOP_CALL_BASE, PREFLOP_CALL_OOP_BONUS, PREFLOP_CALL_TRASH_BONUS, PREFLOP_TRASH_STRENGTH,
    RAISE_FOLD_THRESHOLD, BLOCKER_RAISE_THRESHOLD,
    BLUFF_DELTA_RAISE_SCALE, BLUFF_DELTA_BLOCKER_SCALE,
    DRAW_EQUITY_SLACK_PREMIUM, DRAW_EQUITY_SLACK_NORMAL,
    SEMI_BLUFF_MIN_DRAW, SEMI_BLUFF_MIN_CONFIDENCE,
    BB_FLOP_CR_CONF_MIN, BB_FLOP_CR_FOLD_TO_RAISE_MIN,
    BB_FLOP_CR_WETNESS_MAX, BB_FLOP_CR_MARGINAL_MIN,
    NUTTED_RISK_STRONG_MULT, NUTTED_RISK_MEDIUM_MULT, NUTTED_RISK_CALL_MULT,
    TRAP_MIN_CONFIDENCE, TRAP_MIN_AGGR, TRAP_MAX_WETNESS, TRAP_FREQ_CAP,
    TRAP_NUT_MAX_POT,
    BIG_POT_BASE, BIG_POT_PROTECT_SCALE, BIG_POT_CHASE_SCALE, BIG_POT_FLOOR, BIG_POT_CEIL,
)

# Local opponent-model constants (avoid cross-file import race during parallel worker builds)
PRIOR_FOLD_TO_JAM = 0.45
PRIOR_FOLD_TO_JAM_WEIGHT = 6.0
FOLD_TO_JAM_HIGH = 0.60
FOLD_TO_JAM_LOW = 0.25
SPR_COMMITMENT_OPP_ALLIN_FOLD_MADE_MAX = 0.55
SPR_COMMITMENT_OPP_ALLIN_TURN_FOLD_MADE_MAX = 0.50
SPR_COMMITMENT_OPP_ALLIN_THIN_PENALTY = 0.05
from card_utils import clamp, card_number, card_suit
from state import (
    reconstruct_state, get_remaining_hands, estimate_preflop_strength,
    is_preflop_3bet_candidate, is_preflop_trash_hand,
    preflop_hand_profile, preflop_domination_penalty,
)
from tournament import (
    should_lock_win, fold_gives_opponent_lock, match_risk_adjustment,
    match_pressure_profile, apply_anti_lock_pressure, anti_lock_can_continue,
    _is_passive_opponent, _is_fourbet_light_candidate, _should_4bet_light,
    _should_checkraise_trap,
)
from opponent import build_opponent_model, analyze_current_spot, classify_opponent_archetype, _bb_defense_pressure_profile, _sb_open_pressure_profile
from postflop import (
    made_hand_metric, pair_board_profile, pair_domination_margin,
    marginal_pair_under_pressure, board_texture_profile,
    classify_street_texture,
    paired_board_outcome_profile, bet_size_bucket, value_hand_tier,
    value_bet_plan, empty_draw_profile, draw_profile, draw_potential,
    draw_call_margin, made_flush_profile, blocker_bluff_profile,
    allow_low_frequency_blocker_bluff, nutted_risk_profile,
    check_probe_resistance_margin, must_continue_vs_raise,
    disciplined_opp_river_margin,
)
from simulation import (
    build_opponent_range, estimate_weighted_win_rate,
    polarized_jam_equity,
)
from overbet import should_overbet, overbet_sizing
from donk_probe import should_donk_bet, should_probe_bet, donk_probe_sizing
from river_thin_value import river_thin_value_bet
from bet_tendency import river_tendency_hero_call


# Tight-opponent raise suppression (v61 crossover): marginal raises → calls vs tight ranges.
# [v117 mutation] widen suppression band 0.50 -> 0.56 for top-pair-weak-kicker calls.
TIGHT_OPP_PFR_MAX = 0.22
TIGHT_OPP_RAISE_SUPPRESS_CONF = 0.40
TIGHT_OPP_RAISE_SUPPRESS_MS_LOW = 0.48
TIGHT_OPP_RAISE_SUPPRESS_MS_HIGH = 0.56
TIGHT_OPP_RAISE_SUPPRESS_DRAW_CEIL = 0.20

# River / turn SPR-commitment fold thresholds.
SPR_COMMITMENT_FOLD_MADE_MAX = 0.55
SPR_TURN_COMMIT_THRESHOLD = 4.0
SPR_TURN_FOLD_MADE_MAX = 0.50
SPR_TURN_COMMIT_BUFFER = 0.03


# BB Defense-Depth Matrix keyed to SB opener raise-to-total.
_BB_DEFENSE_SMALL_MAX_BB = 2.5
_BB_DEFENSE_STANDARD_MAX_BB = 3.5
_BB_DEFENSE_LARGE_MAX_BB = 5.0

_BB_DEFENSE_DEPTH_MATRIX = {
    'small':    {'call_delta': -0.03, 'value_3bet_delta': -0.02,
                 'bluff_low_delta': -0.03, 'bluff_high_delta': +0.03, 'bluff_freq_delta': +0.06},
    'standard': {'call_delta': 0.0,  'value_3bet_delta': 0.0,
                 'bluff_low_delta': 0.0,  'bluff_high_delta': 0.0,  'bluff_freq_delta': 0.0},
    'large':    {'call_delta': +0.03, 'value_3bet_delta': +0.01,
                 'bluff_low_delta': +0.03, 'bluff_high_delta': -0.03, 'bluff_freq_delta': -0.08},
    'xl':       {'call_delta': +0.06, 'value_3bet_delta': +0.02,
                 'bluff_low_delta': +0.05, 'bluff_high_delta': -0.06, 'bluff_freq_delta': -0.20},
}


def _tight_opp_raise_suppress(opponent_model, made_strength, draw_strength, round_idx, value_profile):
    """Suppress marginal postflop raises vs confirmed tight opponents (OFFENSE-side only)."""
    confidence = opponent_model.get('confidence', 0.0)
    pfr = opponent_model.get('pfr', PRIOR_PFR)
    tier = value_profile.get('tier', 'none') if value_profile else 'none'

    fired = (
        round_idx >= 1
        and confidence >= TIGHT_OPP_RAISE_SUPPRESS_CONF
        and pfr <= TIGHT_OPP_PFR_MAX
        and tier not in ('strong', 'nut')
        and TIGHT_OPP_RAISE_SUPPRESS_MS_LOW <= made_strength <= TIGHT_OPP_RAISE_SUPPRESS_MS_HIGH
        and draw_strength < TIGHT_OPP_RAISE_SUPPRESS_DRAW_CEIL
    )
    # Telemetry: print UNCONDITIONALLY so fire-rate is honest (experience_pool
    # PARAMETER_TUNING: gate direction is load-bearing; verify fire >=5% @>=30g).
    sys.stderr.write(
        f'OPP_TIGHT_RAISE_SUPPRESS fired={1 if fired else 0} '
        f'pfr={pfr:.3f} conf={confidence:.2f} ms={made_strength:.2f} '
        f'ds={draw_strength:.2f} round={round_idx} tier={tier}\n'
    )
    return fired


# Preflop jam defense: pot-odds comparison with wider slack for wide jammers.
def _preflop_jam_call_decision(win_rate, pot_odds, opponent_allin_rate, confidence):
    """Return call/fold/defer for preflop jam spots using pot-odds-equity."""
    if confidence >= 0.15 and opponent_allin_rate >= 0.20:
        slack = 0.05
        reason = 'wide_jammer'
    else:
        slack = 0.01
        reason = 'standard_jam'
    decision = None
    if win_rate >= pot_odds - slack:
        decision = 0
    elif win_rate < pot_odds - slack - 0.05:
        decision = -1
    sys.stderr.write(
        f"PREFLOP_JAM_DEFENSE decision={decision if decision is not None else 'defer'} "
        f"win_rate={win_rate:.3f} pot_odds={pot_odds:.3f} slack={slack:.2f} "
        f"allin_rate={opponent_allin_rate:.3f} conf={confidence:.2f} reason={reason}\n"
    )
    return decision


def _classify_open_raise_size(raise_to_total):
    """Classify SB open-raise size into defense-depth bucket."""
    raise_bb = raise_to_total / BIG_BLIND
    if raise_bb <= _BB_DEFENSE_SMALL_MAX_BB:
        return 'small'
    elif raise_bb <= _BB_DEFENSE_STANDARD_MAX_BB:
        return 'standard'
    elif raise_bb <= _BB_DEFENSE_LARGE_MAX_BB:
        return 'large'
    return 'xl'


def _bb_defense_depth_adjustments(raise_to_total):
    """Return BB defense threshold deltas keyed to SB open-raise size.
    Emits UNCONDITIONAL stderr telemetry for per-nemesis measurement."""
    bucket = _classify_open_raise_size(raise_to_total)
    adj = _BB_DEFENSE_DEPTH_MATRIX[bucket]
    raise_bb = raise_to_total / BIG_BLIND
    sys.stderr.write(
        f"BB_DEFENSE bucket={bucket} raise_bb={raise_bb:.1f} "
        f"call_d={adj['call_delta']:+.3f} v3b_d={adj['value_3bet_delta']:+.3f} "
        f"bluff_freq_d={adj['bluff_freq_delta']:+.3f}\n"
    )
    return adj


def _bb_flop_checkraise_exploit(round_idx, spot_info, opponent_model, board_texture,
                                value_profile, draw_info, draw_strength, made_strength,
                                blocker_profile, blocker_raise):
    """Broaden BB flop check-raise defense vs opponents who fold to raises.
    Returns True when OOP BB should raise over a flop c-bet.
    Telemetry is unconditional so fire-rate is honest."""
    fired = False
    reason = 'none'
    if round_idx == 1 and spot_info.get('facing_postflop_aggression') and not spot_info.get('has_position'):
        confidence = opponent_model.get('confidence', 0.0)
        fold_to_raise = opponent_model.get('fold_to_raise', PRIOR_FOLD_TO_RAISE)
        wetness = board_texture.get('wetness', 0.0) if board_texture else 0.0
        dynamic = board_texture.get('dynamic', False) if board_texture else False
        if (confidence >= BB_FLOP_CR_CONF_MIN
                and fold_to_raise > BB_FLOP_CR_FOLD_TO_RAISE_MIN
                and wetness <= BB_FLOP_CR_WETNESS_MAX
                and not dynamic):
            if value_profile and value_profile.get('tier') in ('strong', 'nut'):
                fired = True
                reason = 'value'
            elif draw_info.get('semi_bluff') and draw_strength >= SEMI_BLUFF_MIN_DRAW:
                fired = True
                reason = 'semi'
            elif blocker_raise:
                fired = True
                reason = 'blocker'
            elif made_strength >= BB_FLOP_CR_MARGINAL_MIN and blocker_profile and blocker_profile.get('eligible'):
                fired = True
                reason = 'marginal'
    sys.stderr.write(
        f'BB_FLOP_CR fired={int(fired)} reason={reason} '
        f'conf={opponent_model.get("confidence", 0.0):.2f} '
        f'ftr={opponent_model.get("fold_to_raise", PRIOR_FOLD_TO_RAISE):.3f} '
        f'wet={board_texture.get("wetness", 0.0) if board_texture else 0.0:.2f} '
        f'ms={made_strength:.2f} ds={draw_strength:.2f}\n'
    )
    return fired


def _per_street_diverges(opponent_model, per_street_key, per_street_prior, aggregate_key, aggregate_prior):
    per_street_val = opponent_model.get(per_street_key, per_street_prior)
    aggregate_val = opponent_model.get(aggregate_key, aggregate_prior)
    ps_above = per_street_val > per_street_prior
    ag_above = aggregate_val > aggregate_prior
    return ps_above != ag_above


def _aligned_signal_boost(opponent_model, per_street_key, per_street_prior, aggregate_key, aggregate_prior):
    per_street_val = opponent_model.get(per_street_key, per_street_prior)
    aggregate_val = opponent_model.get(aggregate_key, aggregate_prior)
    ps_above = per_street_val > per_street_prior
    ag_above = aggregate_val > aggregate_prior
    if ps_above != ag_above:
        return 0.0
    ps_dev = abs(per_street_val - per_street_prior) / per_street_prior
    ag_dev = abs(aggregate_val - aggregate_prior) / aggregate_prior
    return (ps_dev * ag_dev) ** 0.5


def opponent_pressure_adjustment(opponent_model, spot_info, round_idx):
    confidence = opponent_model["confidence"]
    adjustment = 0.0

    if spot_info["facing_raise"] or spot_info["facing_allin"]:
        adjustment += confidence * max(0.0, PRIOR_FOLD_TO_RAISE - opponent_model["pfr"]) * 0.07
        if round_idx > 0:
            adjustment += confidence * max(0.0, PRIOR_POSTFLOP_AGGR - opponent_model["postflop_aggr"]) * 0.06
        adjustment -= confidence * max(0.0, opponent_model["allin_rate"] - PRIOR_ALLIN_RATE) * 0.08
        adjustment -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.48) * 0.05
        adjustment += min(0.04, spot_info["last_raise_pot_ratio"] * 0.04)

    # v18 per-street profiling adjustments
    if confidence >= 0.15:
        if round_idx == 2:
            barrel = opponent_model.get('barrel_freq', PRIOR_BARREL_FREQ)
            if barrel >= 0.60:
                adjustment -= confidence * (barrel - 0.50) * 0.100
            elif barrel <= 0.30:
                adjustment += confidence * (0.40 - barrel) * 0.060
        elif round_idx == 3:
            river_bb = opponent_model.get('avg_river_raise_bb', 5.5)
            river_aggr = opponent_model.get('river_aggr', PRIOR_RIVER_AGGR)
            if river_bb >= 8.0 and river_aggr >= 0.32:
                adjustment += confidence * 0.060
            elif river_bb <= 3.0 and river_aggr <= 0.22:
                adjustment -= confidence * 0.050

    # v18 aligned-signal boost
    if confidence >= 0.15:
        if round_idx == 2:
            barrel = opponent_model.get('barrel_freq', PRIOR_BARREL_FREQ)
            alignment = _aligned_signal_boost(opponent_model, 'barrel_freq', PRIOR_BARREL_FREQ, 'postflop_aggr', PRIOR_POSTFLOP_AGGR)
            if alignment > 0:
                if barrel >= 0.60:
                    adjustment -= confidence * alignment * barrel * 1.5
                elif barrel <= 0.30:
                    adjustment += confidence * alignment * (1.0 - barrel) * 1.5
        elif round_idx == 3:
            river_aggr = opponent_model.get('river_aggr', PRIOR_RIVER_AGGR)
            alignment = _aligned_signal_boost(opponent_model, 'river_aggr', PRIOR_RIVER_AGGR, 'postflop_aggr', PRIOR_POSTFLOP_AGGR)
            if alignment > 0:
                if river_aggr >= 0.32:
                    adjustment += confidence * alignment * 1.5
                elif river_aggr <= 0.22:
                    adjustment -= confidence * alignment * 1.5

    # v13 tighter clamp — prevents over-adjustment noise
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
    fold_to_raise = opponent_model.get("fold_to_raise", PRIOR_FOLD_TO_RAISE)
    confidence = opponent_model.get("confidence", 0.0)
    fold_to_jam = opponent_model.get('fold_to_jam_rate', PRIOR_FOLD_TO_JAM)
    jam_samples = opponent_model.get('fold_to_jam_samples', 0)
    jam_confidence = confidence * min(1.0, jam_samples / 8.0)
    jam_threshold_delta = 0
    jam_reason = 'neutral'
    if jam_confidence >= 0.20:
        if fold_to_jam >= FOLD_TO_JAM_HIGH:
            jam_threshold_delta = -1
            jam_reason = 'high_folder'
        elif fold_to_jam <= FOLD_TO_JAM_LOW:
            jam_threshold_delta = 1
            jam_reason = 'low_folder'
    if jam_threshold_delta == -1:      # high folder -> jam more
        jam_stack_delta = -0.025
        jam_stack_reason = 'high_folder'
    elif jam_threshold_delta == 1:     # low folder -> jam less
        jam_stack_delta = +0.025
        jam_stack_reason = 'low_folder'
    else:
        jam_stack_delta = 0.0
        jam_stack_reason = 'neutral'
    sys.stderr.write(f'FOLD_TO_JAM_ADAPT delta={jam_threshold_delta} reason={jam_reason} ftj={fold_to_jam:.3f} samples={jam_samples} jam_conf={jam_confidence:.2f}\n')

    tier = value_profile.get("tier", "none") if value_profile is not None else "none"
    draw_quality = draw_info.get("quality", 0.0) if draw_info is not None else 0.0
    has_draw = draw_info.get("semi_bluff", False) if draw_info is not None else False
    has_blocker = blocker_profile is not None and blocker_profile.get("eligible", False)

    weak_showdown = tier in ("none", "thin") and draw_quality < 0.14 and win_rate < 0.45
    high_fold_pressure = confidence < 0.20 or fold_to_raise >= 0.42
    emergency_jam = (
        hands_left <= 3
        or (to_call > 0 and to_call / max(1, pot) >= 0.35)
        or (weak_showdown and high_fold_pressure and hands_left <= 6 + jam_threshold_delta)
        or (win_rate < 0.18 and hands_left <= 5 + jam_threshold_delta)
    )
    if tier in ("strong", "nut") or has_draw:
        emergency_jam = emergency_jam and hands_left <= 3

    if emergency_jam:
        return -2

    min_raise_action = state.get("min_raise_action", state["round_raise"])

    if round_idx == 0:
        ratio = ANTI_LOCK_PREFLOP_OPEN_RATIO if to_call == 0 else ANTI_LOCK_PREFLOP_FACING_RATIO
        target = int(to_call + pot_after_call * ratio)
        strength = preflop_strength if preflop_strength is not None else win_rate
        target = max(target, int((5.5 + max(0.0, strength - 0.35) * 3.0) * BIG_BLIND) - state["my_round_bet"])
    elif round_idx == 1:
        target = int(to_call + pot_after_call * ANTI_LOCK_FLOP_RATIO)
    elif round_idx == 2:
        target = int(to_call + pot_after_call * ANTI_LOCK_TURN_RATIO)
    else:
        target = int(to_call + pot_after_call * ANTI_LOCK_RIVER_RATIO)

    if board_texture is not None and board_texture.get("dynamic", False):
        target = int(target * ANTI_LOCK_DYNAMIC_MULT)
    if has_blocker or has_draw:
        target = int(target * ANTI_LOCK_BLOCKER_DRAW_MULT)
    if weak_showdown:
        target = int(target * ANTI_LOCK_WEAK_SHOWDOWN_MULT)

    amount = max(min_raise_action, target)
    adjusted_jam_ratio = clamp(ANTI_LOCK_JAM_STACK_RATIO + jam_stack_delta, 0.35, 0.85)
    if amount >= my_chips * adjusted_jam_ratio:
        sys.stderr.write(f'FOLD_TO_JAM_STACK ratio={adjusted_jam_ratio:.3f} delta={jam_stack_delta:+.3f} reason={jam_stack_reason} ftj={fold_to_jam:.3f}\n')
        return -2
    amount = min(amount, my_chips - 1)
    if amount <= to_call or amount < min_raise_action:
        return -2 if hands_left <= max(1, 4 + jam_threshold_delta) else None
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


def postflop_call_margin(spot_info, opponent_model, made_strength, draw_strength, round_idx, has_position, texture_class="none"):
    if round_idx <= 0:
        return 0.0

    margin = 0.0
    air_hand = made_strength < 0.18 and draw_strength < 0.08
    weak_showdown = made_strength < 0.22
    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])

    if weak_showdown:
        margin += 0.020
    if air_hand:
        margin += 0.028

    if spot_info["facing_postflop_aggression"]:
        margin += 0.008
        if size_bucket == "small":
            margin += 0.032
        elif size_bucket == "medium":
            margin += 0.010
        else:
            margin += 0.024

        if spot_info.get("opp_postflop_bet_count", 0) >= 2:
            margin += 0.024 if size_bucket == "small" else 0.014
        if round_idx >= 2 and air_hand:
            margin += 0.020
        if round_idx == 3 and size_bucket == "large":
            margin += 0.032

    if not has_position:
        margin += 0.008

    confidence = opponent_model["confidence"]
    if air_hand:
        margin -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.50) * 0.015
    else:
        margin -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.50) * 0.008

    if texture_class == "dry":
        # call_threshold = base + margin; more negative => wider calling.
        # Dry boards: defending wider is correct; -0.040 reduces over-fold tendency.
        margin -= 0.040
    elif texture_class in ("draw_heavy", "monotone"):
        margin += 0.020

    # Betsize polarity + shove defense adjustment.
    shove_rate = opponent_model.get("shove_rate", 0.08)
    opp_conf = opponent_model.get("confidence", 0.0)
    polarity = opponent_model.get("betsize_polarity", 0.0)
    # Prefer per-street polarity when available (Worker 1); fallback to aggregate
    if round_idx == 3:
        street_polarity = opponent_model.get("river_polarity", polarity)
    elif round_idx == 2:
        street_polarity = opponent_model.get("turn_polarity", polarity)
    else:
        street_polarity = opponent_model.get("flop_polarity", polarity)

    polarity_delta = 0.0
    if size_bucket == "large":
        # Polarized large bets from polarized opponents skew value-heavy
        polarity_delta = 0.020 * street_polarity * opp_conf
    elif size_bucket == "small":
        # Small bets from polarized opponents skew bluff-heavy
        polarity_delta = -0.014 * street_polarity * opp_conf
    else:  # medium
        polarity_delta = 0.006 * street_polarity * opp_conf
    margin += polarity_delta

    # Shove defense: scale call margin by opponent shove tendency.
    SHOVE_DEFENSE_SCALE = 0.10
    SHOVE_NEUTRAL = 0.08
    shove_signal = clamp((shove_rate - SHOVE_NEUTRAL) / 0.20, -1.0, 1.0)
    # Large bets from shove-happy opponents are more dangerous (more nutted)
    if size_bucket == "large":
        shove_delta = SHOVE_DEFENSE_SCALE * shove_signal * opp_conf * 0.22
    elif size_bucket == "medium":
        shove_delta = SHOVE_DEFENSE_SCALE * shove_signal * opp_conf * 0.12
    else:
        shove_delta = SHOVE_DEFENSE_SCALE * shove_signal * opp_conf * 0.04
    margin += shove_delta

    # Telemetry for call-margin components.
    sys.stderr.write(
        f"POLARITY_MARGIN final_milli={margin*1000:+.0f} "
        f"polarity_delta_milli={polarity_delta*1000:+.0f} "
        f"shove_delta_milli={shove_delta*1000:+.0f} "
        f"street_pol={street_polarity:+.3f} shove_rate={shove_rate:.3f} "
        f"opp_conf={opp_conf:.2f} size_bucket={size_bucket} round={round_idx}\n"
    )

    return clamp(margin, 0.0, 0.08)


def _opp_bet_size_adaptation(spot_info, opponent_model, round_idx):
    """Adjust call margin by opponent's current bet size vs their street average."""
    opp_conf = opponent_model.get('confidence', 0.0)
    if opp_conf < 0.15 or round_idx <= 0:
        return 0.0

    street_avg_key = {1: 'avg_flop_raise_bb', 2: 'avg_turn_raise_bb', 3: 'avg_river_raise_bb'}.get(round_idx)
    if not street_avg_key:
        return 0.0
    avg_bb = opponent_model.get(street_avg_key, 3.0)
    current_bet_bb = spot_info.get('last_raise_bb', 0.0)

    if current_bet_bb <= 0 or avg_bb <= 0:
        return 0.0

    # Continuous deviation: how far current bet is from opponent's avg.
    # Normalized by avg_bb so signal is scale-invariant per opponent.
    deviation = (current_bet_bb - avg_bb) / max(avg_bb, 1.0)

    # Continuous delta — NO deadzone (M5 compliant).
    # Positive deviation -> tighter calls (positive margin).
    # Scale: max ±0.025 * confidence. At conf=0.5, deviation=+50% -> +0.0125 margin.
    delta = 0.025 * clamp(deviation / 0.50, -1.0, 1.0) * opp_conf

    sys.stderr.write(
        f'BET_SIZE_ADAPT delta_milli={delta*1000:+.0f} '
        f'deviation={deviation:+.3f} current_bb={current_bet_bb:.1f} '
        f'avg_bb={avg_bb:.1f} conf={opp_conf:.2f} round={round_idx}\n'
    )

    return clamp(delta, -0.025, 0.025)


def realized_postflop_equity(
    win_rate,
    made_strength,
    draw_strength,
    round_idx,
    has_position,
    spot_info,
    pair_profile=None,
    opponent_model=None,
):
    air_hand = made_strength < 0.18 and draw_strength < 0.08
    if round_idx <= 0:
        return win_rate

    eqr = 1.0

    if air_hand:
        eqr = 0.65 if has_position else 0.55

        if spot_info.get("opp_postflop_bet_count", 0) >= 2:
            eqr -= 0.10
        if round_idx == 2:
            eqr -= 0.05
        elif round_idx == 3:
            eqr -= 0.12

        eqr = clamp(eqr, 0.45, 0.85)
        return win_rate * eqr

    if pair_profile is not None and pair_profile["made_class"] == 1:
        pair_type = pair_profile["pair_type"]

        if pair_type in ("middle_pair", "bottom_pair", "underpair", "board_pair"):
            eqr = 0.82 if has_position else 0.72

            if pair_profile["weak_kicker"]:
                eqr -= 0.05
            if spot_info.get("opp_postflop_bet_count", 0) >= 2:
                eqr -= 0.06
            if round_idx == 3:
                eqr -= 0.06

            eqr = clamp(eqr, 0.65, 0.92)
            return win_rate * eqr

        if pair_type == "top_pair" and pair_profile["weak_kicker"]:
            eqr = 0.88 if has_position else 0.80
            if spot_info.get("opp_postflop_bet_count", 0) >= 2:
                eqr -= 0.04
            eqr = clamp(eqr, 0.75, 0.95)
            return win_rate * eqr

    return win_rate


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
    passive_thin_value=False,
    overbet_mode=False,
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
        ratio = RAISE_RATIO_PREFLOP_OPEN if to_call == 0 else RAISE_RATIO_PREFLOP_FACING
    elif round_idx == 1:
        ratio = RAISE_RATIO_FLOP
    elif round_idx == 2:
        ratio = RAISE_RATIO_TURN
    else:
        ratio = RAISE_RATIO_RIVER

    ratio += max(0.0, win_rate - 0.55) * (0.90 + 0.20 * round_idx)
    ratio += -0.05 if has_position else 0.05
    ratio += confidence * max(0.0, fold_to_raise - 0.52) * (0.20 if semi_bluff else 0.10)
    ratio += value_profile.get("size_bonus", 0.0)
    ratio += value_plan.get("size_delta", 0.0)
    ratio += match_sizing_delta
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
        ratio = min(ratio, BLOCKER_BLUFF_CAP_BASE + BLOCKER_BLUFF_WET_SCALE * wetness + BLOCKER_BLUFF_STREET_SCALE * max(0, round_idx - 1))
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

    # ── Mutation: passive-exploit thin value sizing ── smaller sizing vs passives.
    if passive_thin_value:
        ratio = min(ratio, PASSIVE_THIN_VALUE_MAX_RATIO)

    low_ratio = 0.28 if inducing_value else 0.22 if probe_mode or (blocker_bluff and to_call == 0) else 0.40
    if thin_cap is not None:
        low_ratio = min(low_ratio, thin_cap)
    # Overbet mode bypasses the 1.45 cap to allow 1.3x-1.8x pot sizing
    max_ratio = OVERBET_MAX_RATIO if overbet_mode else RAISE_MAX_RATIO
    ratio = clamp(ratio, low_ratio, max_ratio)

    amount = int(to_call + pot_after_call * ratio)

    if round_idx == 0 and preflop_strength is not None:
        if spot_name == "sb_open":
            fold_signal = clamp((fold_to_raise - PRIOR_FOLD_TO_RAISE) / SB_OPEN_FOLD_SIGNAL_RANGE, -1.0, 1.0)
            opp_bb_delta = -SB_OPEN_FOLD_SIZE_SCALE * fold_signal * confidence
            base_bb = SB_OPEN_BASE_BB + opp_bb_delta
            desired_total = int((base_bb + max(0.0, preflop_strength - SB_OPEN_STRENGTH_OFFSET) * SB_OPEN_STRENGTH_SCALE) * BIG_BLIND)
            amount = max(amount, desired_total - my_round_bet)
            sys.stderr.write(
                f"SB_OPEN_SIZE base_bb={base_bb:.2f} opp_bb_delta={opp_bb_delta:+.2f} "
                f"fold_signal={fold_signal:+.2f} ftr={fold_to_raise:.3f} conf={confidence:.2f}\n"
            )
        elif spot_name == "bb_vs_limp":
            desired_total = int((BB_ISO_BASE_BB + max(0.0, preflop_strength - BB_ISO_STRENGTH_OFFSET) * BB_ISO_STRENGTH_SCALE) * BIG_BLIND)
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


def choose_preflop_spot_action(req, state, spot_info, opponent_model, preflop_strength, win_rate, match_profile, opp_archetype='unknown'):
    my_chips = req["my_chips"]
    to_call = state["to_call"]
    match_adjust = match_risk_adjustment(req, req["my_id"], get_remaining_hands(req))
    confidence = opponent_model["confidence"]
    loose_bonus = confidence * max(0.0, opponent_model["vpip"] - PRIOR_VPIP) * 0.03
    trash_hand = is_preflop_trash_hand(req["my_cards"], preflop_strength)

    if spot_info["preflop_spot"] == "sb_open":
        _sb_pressure = _sb_open_pressure_profile(opponent_model)
        open_threshold = SB_OPEN_THRESHOLD + match_adjust + SB_OPEN_MATCH_ADJ + match_profile["open_delta"] + _sb_pressure['open_delta']
        limp_threshold = SB_LIMP_THRESHOLD + match_adjust
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
        iso_threshold = BB_ISO_THRESHOLD + match_adjust - loose_bonus + match_profile["open_delta"]
        iso_threshold -= confidence * max(0.0, opponent_model["vpip"] - 0.58) * BB_VPIP_FOLD_ADJUST_SCALE
        iso_threshold -= confidence * max(0.0, opponent_model["fold_to_raise"] - 0.52) * BB_FTR_FOLD_ADJUST_SCALE
        # [v117 crossover from national_v71] iso delta from BB defense pressure profile
        iso_threshold += _bb_defense_pressure_profile(opponent_model)['iso_delta']
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
        # Preflop jam defense routes jam decisions through pot-odds-equity.
        raise_to_total_jam = to_call + state['my_round_bet']
        eff_stack_jam = my_chips + state['my_round_bet']
        if state.get('opponent_allin', False) or (eff_stack_jam > 0 and raise_to_total_jam >= 0.50 * eff_stack_jam):
            jam_decision = _preflop_jam_call_decision(
                win_rate, pot_odds_pf,
                opponent_model.get('allin_rate', PRIOR_ALLIN_RATE),
                opponent_model['confidence'],
            )
            if jam_decision is not None:
                return jam_decision
        # Adjust BB thresholds by SB open size and opponent pressure profile.
        raise_to_total = to_call + state['my_round_bet']
        _def_adj = _bb_defense_depth_adjustments(raise_to_total)
        _bb_pressure = _bb_defense_pressure_profile(opponent_model)
        eff_value_3bet = BB_VALUE_3BET_THRESHOLD + _def_adj['value_3bet_delta']
        eff_bluff_low = BB_BLUFF_3BET_LOW + _def_adj['bluff_low_delta']
        eff_bluff_high = BB_BLUFF_3BET_HIGH + _def_adj['bluff_high_delta']
        eff_bluff_freq = BB_BLUFF_3BET_FREQ + _def_adj['bluff_freq_delta'] + _bb_pressure['bluff_freq_delta']
        eff_call_threshold = BB_CALL_THRESHOLD + _def_adj['call_delta'] + _bb_pressure['call_delta']
        if preflop_strength >= eff_value_3bet and not trash_hand:
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
        # Bluff 3bet vs non-calling-station opponents.
        can_bluff_3bet = opp_archetype != 'calling_station'
        if can_bluff_3bet and eff_bluff_low <= preflop_strength <= eff_bluff_high and not trash_hand:
            hand_idx = req.get("hand", 0)
            bluff_roll = ((hash(tuple(req['my_cards'])) * 31 + hash((hand_idx, my_chips))) % 100) / 100.0
            if bluff_roll < eff_bluff_freq:
                raise_amount = choose_raise(
                    state['min_raise_action'], my_chips, state['my_round_bet'],
                    to_call, state['pot'], max(win_rate, preflop_strength),
                    0, 'bb_vs_raise', preflop_strength,
                    True, opponent_model,
                    match_sizing_delta=match_profile['sizing_delta'],
                )
                if raise_amount is not None:
                    return raise_amount
        # Crossover (v110): upstream range pruning for dominated offsuit broadway.
        _dom_penalty = preflop_domination_penalty(req['my_cards'])
        _eff_call_strength = preflop_strength - _dom_penalty
        if _dom_penalty != 0.0:
            sys.stderr.write(
                f'PREFLOP_DOM_PENALTY spot=bb_vs_raise penalty={_dom_penalty:.3f} '
                f'raw={preflop_strength:.3f} eff={_eff_call_strength:.3f} '
                f'thresh={eff_call_threshold:.3f} folded={1 if _eff_call_strength < eff_call_threshold else 0}\n'
            )
        if _eff_call_strength >= eff_call_threshold or win_rate >= pot_odds_pf - 0.02:
            return 0
        return -1

    elif spot_info['preflop_spot'] == 'sb_vs_reraise':
        pot_odds_sbr = to_call / (to_call + state['pot']) if to_call > 0 else 0.0
        # Preflop jam defense before premium-4bet logic.
        raise_to_total_sbr = to_call + state['my_round_bet']
        eff_stack_sbr = my_chips + state['my_round_bet']
        if state.get('opponent_allin', False) or (eff_stack_sbr > 0 and raise_to_total_sbr >= 0.50 * eff_stack_sbr):
            jam_decision = _preflop_jam_call_decision(
                win_rate, pot_odds_sbr,
                opponent_model.get('allin_rate', PRIOR_ALLIN_RATE),
                opponent_model['confidence'],
            )
            if jam_decision is not None:
                return jam_decision
        if preflop_strength >= SB_PREMIUM_4BET_THRESHOLD:
            raise_amount = choose_raise(
                state['min_raise_action'], my_chips, state['my_round_bet'],
                to_call, state['pot'], max(win_rate, preflop_strength),
                0, 'sb_vs_reraise', preflop_strength,
                False, opponent_model,
                match_sizing_delta=match_profile['sizing_delta'],
            )
            if raise_amount is not None:
                return raise_amount
            return 0
        if state.get("opponent_allin", False) and preflop_strength >= SB_FACING_ALLIN_CALL:
            return 0
        # Non-all-in: call with strong hands if pot odds are reasonable.
        # Crossover (v110): upstream range pruning for dominated offsuit broadway.
        _dom_penalty_sbr = preflop_domination_penalty(req['my_cards'])
        _eff_call_strength_sbr = preflop_strength - _dom_penalty_sbr
        if _dom_penalty_sbr != 0.0:
            sys.stderr.write(
                f'PREFLOP_DOM_PENALTY spot=sb_vs_reraise penalty={_dom_penalty_sbr:.3f} '
                f'raw={preflop_strength:.3f} eff={_eff_call_strength_sbr:.3f} '
                f'folded={1 if _eff_call_strength_sbr < SB_VS_RERAISE_CALL else 0}\n'
            )
        if _eff_call_strength_sbr >= SB_VS_RERAISE_CALL and win_rate >= pot_odds_sbr - SB_VS_RERAISE_POT_SLACK:
            return 0
        # Call wider vs confirmed LAG 3-bettors.
        if opp_archetype == 'lag' and preflop_strength >= 0.30:
            if win_rate >= pot_odds_sbr - 0.05:
                return 0
        light_4bet = _should_4bet_light(req["my_cards"], preflop_strength, opponent_model, state, my_chips)
        if light_4bet > 0:
            return light_4bet
        return -1

    return None


# Postflop fold gate using pot-odds-equity with EQR adjustments.
def should_fold_postflop(round_idx, made_strength, draw_strength, value_profile, spot_info, texture_class="none", opponent_model=None, spr=999.0, pot_odds=0.0, win_rate=0.0, has_position=True):
    if round_idx <= 0:
        return False
    tier = value_profile.get("tier", "none") if value_profile else "none"
    if tier in ("strong", "nut"):
        return False
    has_draw = draw_strength >= 0.14
    if not spot_info["facing_postflop_aggression"]:
        return False

    # Estimate effective equity from made strength and simulation
    equity = max(made_strength, win_rate * 0.6)
    if has_draw:
        equity = max(equity, made_strength + draw_strength * 0.25)
    equity = min(equity, 0.95)

    # EQR adjustments: reduce equity estimate for unfavorable conditions
    eqr = 1.0
    if not has_position:
        eqr -= 0.06
    if round_idx == 3:
        eqr -= 0.08
    elif round_idx == 2:
        eqr -= 0.03
    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])
    if size_bucket == "large":
        eqr -= 0.05
    elif size_bucket == "medium":
        eqr -= 0.02
    if texture_class == "dry":
        eqr -= 0.04
    # Opponent aggression: frequent barrels mean stronger range
    if opponent_model is not None and opponent_model.get("confidence", 0) >= 0.15:
        barrel = opponent_model.get("barrel_freq", PRIOR_BARREL_FREQ)
        if barrel >= 0.55 and round_idx >= 2:
            eqr -= 0.04
    eqr = max(eqr, 0.55)
    realized_equity = equity * eqr

    # Fold when realized equity is insufficient vs pot odds
    # Safety margin increases on later streets (less implied odds)
    safety = 0.04 * round_idx
    if not has_draw:
        safety += 0.03
    if spot_info.get("opp_current_round_bet_count", 0) >= 2:
        safety += 0.03
    return realized_equity < pot_odds + safety


def _spr_commitment_gate(round_idx, value_profile, made_strength, win_rate, pot_odds, spr=999.0):
    """Fold thin/none-tier hands when equity is below pot-odds commitment threshold."""
    tier = value_profile.get('tier', 'none') if value_profile else 'none'
    if tier not in ('thin', 'none'):
        return False

    if round_idx == 3:
        if made_strength >= SPR_COMMITMENT_FOLD_MADE_MAX:
            return False
        fold = win_rate < pot_odds
        sys.stderr.write(
            f'SPR_FOLD fired={1 if fold else 0} reason={"equity_below_potodds" if fold else "pass"} '
            f'tier={tier} made={made_strength:.3f} win_rate={win_rate:.3f} pot_odds={pot_odds:.3f}\n'
        )
        return fold

    if round_idx == 2 and spr < SPR_TURN_COMMIT_THRESHOLD:
        if made_strength >= SPR_TURN_FOLD_MADE_MAX:
            return False
        fold = win_rate < pot_odds + SPR_TURN_COMMIT_BUFFER
        sys.stderr.write(
            f'SPR_TURN_FOLD fired={1 if fold else 0} reason={"turn_low_spr_equity" if fold else "pass"} '
            f'tier={tier} made={made_strength:.3f} win_rate={win_rate:.3f} '
            f'pot_odds={pot_odds:.3f} spr={spr:.2f}\n'
        )
        return fold

    return False


def pot_odds_call_threshold(pot_odds, has_position, round_idx, draw_info, spr):
    """Minimum equity to call, adjusted for position, draws, SPR, and street."""
    threshold = pot_odds

    # Position adjustment
    if has_position:
        threshold -= 0.02

    # Draw implied odds
    if draw_info is not None:
        if draw_info.get("type") == "combo_draw":
            threshold -= 0.06
        elif draw_info.get("nut_flush_draw"):
            threshold -= 0.04
        elif draw_info.get("type") == "open_ended_straight_draw":
            threshold -= 0.03

    # SPR commitment
    if spr < 3:
        threshold -= 0.03
    elif spr < 6:
        threshold -= 0.01

    # Turn/river: less future action = less implied odds
    if round_idx == 3:
        threshold += 0.02
    elif round_idx == 2:
        threshold += 0.01

    return max(0.05, threshold)


def _river_thin_value_construct(
    round_idx, to_call, value_profile, made_strength, draw_strength,
    opponent_model, opp_archetype, board_texture, nutted_risk,
    paired_board_stackoff, pot, my_chips, match_profile, anti_lock_pressure,
    state, preflop_strength, spot_info,
):
    """River thin-value raise vs calling-prone opponents."""
    # Guard: river only, we are aggressor or checked-to (to_call==0)
    if round_idx != 3 or to_call > 0:
        sys.stderr.write("RIVER_THIN_VALUE reason=skip_not_river_to0\n")
        return None
    tier = value_profile.get("tier", "none") if value_profile else "none"
    if tier != "thin":
        sys.stderr.write(f"RIVER_THIN_VALUE reason=skip_tier={tier}\n")
        return None
    # Real made hand floor; if a real draw is present, defer to semi_bluff path
    if made_strength < 0.45 or draw_strength > 0.18:
        sys.stderr.write(f"RIVER_THIN_VALUE reason=skip_made={made_strength:.2f}_draw={draw_strength:.2f}\n")
        return None
    # Board safety
    if nutted_risk.get("vulnerable", False) or paired_board_stackoff.get("severe", False):
        sys.stderr.write("RIVER_THIN_VALUE reason=skip_vulnerable\n")
        return None
    if anti_lock_pressure:
        sys.stderr.write("RIVER_THIN_VALUE reason=skip_anti_lock\n")
        return None
    # Calling-prone gate (CRITICAL: only fire vs opponents who call worse)
    vpip = opponent_model.get("vpip", 0.58)
    ftr = opponent_model.get("fold_to_raise", 0.44)
    is_calling_prone = (
        opp_archetype == "calling_station"
        or (vpip > 0.52 and ftr < 0.45)
    )
    if not is_calling_prone:
        sys.stderr.write(
            f"RIVER_THIN_VALUE reason=skip_not_calling vpip={vpip:.2f} "
            f"ftr={ftr:.2f} arch={opp_archetype}\n"
        )
        return None
    if my_chips < pot * 0.4 or my_chips <= 1:
        sys.stderr.write("RIVER_THIN_VALUE reason=skip_low_chips\n")
        return None
    # Sizing: 0.50 at floor, scales up with made_strength, capped 0.62
    ratio = 0.50 + 0.04 * (made_strength - 0.45)
    ratio = max(0.46, min(ratio, 0.62))
    ratio += match_profile.get("sizing_delta", 0.0) * 0.5
    ratio = max(0.42, min(ratio, 0.65))
    amount = int(pot * ratio)  # to_call==0 so pot_after_call == pot
    min_raise = state.get("min_raise_action", BIG_BLIND)
    my_round_bet = state.get("my_round_bet", 0)
    if amount < min_raise:
        amount = max(min_raise, BIG_BLIND)
    raise_to_total = my_round_bet + amount
    # Never all-in for thin value (sanity; sanitize_action will also catch)
    if amount >= my_chips:
        sys.stderr.write("RIVER_THIN_VALUE reason=skip_allin_boundary\n")
        return None
    sys.stderr.write(
        f"RIVER_THIN_VALUE reason=fired made={made_strength:.2f} "
        f"vpip={vpip:.2f} ftr={ftr:.2f} arch={opp_archetype} "
        f"ratio={ratio:.2f} amount={amount} raise_to={raise_to_total}\n"
    )
    return raise_to_total


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
    opp_archetype = classify_opponent_archetype(opponent_model)
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
        to_call / pot >= CRITICAL_SPOT_RATIO or to_call >= BIG_BLIND * CRITICAL_SPOT_BB_MULT or spot_info["facing_allin"]
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
            opp_archetype=opp_archetype,
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
    street_texture = classify_street_texture(public_cards) if len(public_cards) >= 3 else {"class": "none", "dry_score": 0.5, "bluff_combos": 0.5}
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

    # Passive-exploit thin value detection.
    passive_opp = _is_passive_opponent(opponent_model)
    passive_thin_value = (
        passive_opp
        and to_call == 0
        and round_idx >= 2
        and 0.40 <= made_strength < 0.65
        and draw_strength < 0.12
        and not anti_lock_pressure
        and (value_profile is not None and value_profile["tier"] in ("thin", "strong"))
        and (nutted_risk["risk"] <= 0.05)
    )

    strong = STRONG_THRESHOLD[round_idx]
    medium = MEDIUM_THRESHOLD[round_idx]

    if spot_info["has_position"]:
        strong += THRESHOLD_POS_IP_STRONG
        medium += THRESHOLD_POS_IP_MEDIUM
    else:
        strong += THRESHOLD_POS_OOP_STRONG
        medium += THRESHOLD_POS_OOP_MEDIUM

    if preflop_strength is not None:
        if preflop_strength >= THRESHOLD_PREMIUM_FLOOR:
            strong += THRESHOLD_PREMIUM_STRONG
            medium += THRESHOLD_PREMIUM_MEDIUM
        elif preflop_strength <= THRESHOLD_WEAK_FLOOR:
            strong += THRESHOLD_WEAK_STRONG
            medium += THRESHOLD_WEAK_MEDIUM

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
    strong += NUTTED_RISK_STRONG_MULT * nutted_risk["risk"]
    medium += NUTTED_RISK_MEDIUM_MULT * nutted_risk["risk"]

    if state["opponent_allin"]:
        jam_cost = max(state["allin_call_amount"], to_call)
        jam_odds = jam_cost / (pot + jam_cost) if jam_cost > 0 else 0.0
        jam_buffer = JAM_BASE_BUFFER + max(0.0, strong - JAM_STRONG_OFFSET) * JAM_STRONG_SCALE
        if value_profile is not None and value_profile["tier"] == "thin":
            jam_buffer += JAM_THIN_BONUS
        jam_buffer += nutted_risk["risk"]
        jam_buffer += JAM_MATCH_PROTECT_SCALE * match_profile["protect"]
        jam_buffer += line_strength + paired_board_stackoff["line_strength"]
        jam_buffer += check_resistance
        if remaining_hands == 1:
            total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
            if len(total_win_chips) > my_id and total_win_chips[my_id] < 0:
                jam_buffer -= 0.03
        # v68-repair: restore 0.42 preflop-strength gate to avoid dominated calls vs all-ins.
        if preflop_strength is not None and preflop_strength < 0.42:
            jam_buffer += 0.02
        if anti_lock_pressure:
            jam_buffer += JAM_ANTI_LOCK_PENALTY
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
        jam_buffer = clamp(jam_buffer, -0.05 if anti_lock_pressure else 0.0, JAM_BUFFER_CAP)
        eff_threshold = _opp_allin_spr_threshold(
            round_idx, value_profile, made_strength, jam_odds, jam_buffer,
            anti_lock_pressure, anti_lock_jam_continue,
        )
        # Inline polarized-range equity decision at the moment a turn/river
        # all-in jam is faced with a marginal hand. Structural trigger only
        # (facing a jam + marginal tier/made band) — NOT opponent-archetype
        # labels, which need sample-starved shove_rate thresholds that rarely
        # fire within 70-hand matches. Direct pot-odds math vs a {value-jam,
        # bluff-jam} range is the fresh axis (direction audit option A).
        if round_idx >= 2 and not anti_lock_pressure:
            _pj_tier = value_profile.get('tier', 'none') if value_profile else 'none'
            if _pj_tier in ('thin', 'none') and 0.20 <= made_strength < 0.55:
                _pj_eq = polarized_jam_equity(
                    my_cards, public_cards, combos, weights,
                    opponent_model, iterations=200,
                )
                _pj_call = _pj_eq >= jam_odds
                sys.stderr.write(
                    f'POLARIZED_JAM_INLINE fired=1 round={round_idx} '
                    f'tier={_pj_tier} made={made_strength:.3f} '
                    f'polarized_eq={_pj_eq:.3f} jam_odds={jam_odds:.3f} '
                    f'generic_wr={win_rate:.3f} eff_thr={eff_threshold:.3f} '
                    f'decision={"call" if _pj_call else "fold"}\n'
                )
                return -2 if _pj_call else -1
        return -2 if win_rate >= eff_threshold or anti_lock_jam_continue else -1

    # River/turn SPR-commitment fold gate (reachable before shove path).
    if to_call > 0:
        _spr_val = my_chips / max(1, pot)
        if _spr_commitment_gate(round_idx, value_profile, made_strength, win_rate, pot_odds, _spr_val):
            if not anti_lock_pressure:
                return -1

    if to_call >= my_chips:
        shove_odds = my_chips / (pot + my_chips)
        shove_buffer = SHOVE_BASE_BUFFER + max(0.0, strong - SHOVE_STRONG_OFFSET) * JAM_STRONG_SCALE
        if value_profile is not None and value_profile["tier"] == "thin":
            shove_buffer += 0.04
        shove_buffer += nutted_risk["risk"]
        shove_buffer += JAM_MATCH_PROTECT_SCALE * match_profile["protect"]
        shove_buffer += line_strength + paired_board_stackoff["line_strength"]
        shove_buffer += check_resistance
        if anti_lock_pressure:
            shove_buffer += JAM_ANTI_LOCK_PENALTY
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
        shove_buffer = clamp(shove_buffer, -0.05 if anti_lock_pressure else 0.0, JAM_BUFFER_CAP)
        return -2 if win_rate >= shove_odds + shove_buffer or anti_lock_shove_continue else -1

    if to_call > 0:
        if round_idx == 0:
            call_margin = PREFLOP_CALL_BASE + (PREFLOP_CALL_OOP_BONUS if not spot_info["has_position"] else 0.0)
            if preflop_strength is not None and preflop_strength <= PREFLOP_TRASH_STRENGTH:
                call_margin += PREFLOP_CALL_TRASH_BONUS
            realized_rate = win_rate
            call_threshold = pot_odds + call_margin
        else:
            spr = my_chips / max(1, pot)
            base_threshold = pot_odds_call_threshold(pot_odds, spot_info["has_position"], round_idx, draw_info, spr)
            call_margin = postflop_call_margin(
                spot_info,
                opponent_model,
                made_strength,
                draw_strength,
                round_idx,
                spot_info["has_position"],
                texture_class=street_texture["class"],
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
            call_margin += NUTTED_RISK_CALL_MULT * nutted_risk["risk"]
            if round_idx == 3 and made_strength < 0.40 and not (blocker_profile and blocker_profile["eligible"]):
                call_margin += 0.04
            if round_idx == 3 and paired_board_profile is not None and paired_board_profile["fold_to_raise"]:
                call_margin += 0.05
            # River overcall penalty vs tight-disciplined opponents.
            call_margin += disciplined_opp_river_margin(
                opponent_model, value_profile, round_idx,
            )
            # Bet-size deviation adaptation (confidence-gated, no deadzone).
            call_margin += _opp_bet_size_adaptation(spot_info, opponent_model, round_idx)
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
            call_threshold = base_threshold + call_margin
        if anti_lock_pressure:
            call_threshold -= 0.07
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
            if not anti_lock_call_continue and not strong_made_continue:
                return -1
        if fragile_pair_raise_fold:
            if not anti_lock_call_continue and not strong_made_continue:
                return -1
        if river_tendency_hero_call(
            opponent_model, made_strength, value_profile, spot_info,
            pot_odds, win_rate, round_idx
        ):
            return 0
        _spr = my_chips / max(1, pot)
        if should_fold_postflop(round_idx, made_strength, draw_strength, value_profile, spot_info, texture_class=street_texture["class"], opponent_model=opponent_model, spr=_spr, pot_odds=pot_odds, win_rate=win_rate, has_position=spot_info["has_position"]):
            if not anti_lock_call_continue and not strong_made_continue:
                return -1
        if hard_repressure_fold or paired_board_stackoff["severe"]:
            if not anti_lock_call_continue and not strong_made_continue:
                return -1
        if realized_rate < call_threshold:
            if not anti_lock_call_continue and not strong_made_continue:
                return -1
        # [v20 crossover] Smarter repeated-raise-trap: fold weak hands vs aggression
        if repeated_raise_trap and (value_profile is None or value_profile["tier"] != "nut"):
            trap_size = bet_size_bucket(spot_info["last_raise_pot_ratio"])
            if made_strength < 0.25 and draw_strength < 0.14 and trap_size in ("medium", "large"):
                return -1
            return 0

        raise_fold_threshold = RAISE_FOLD_THRESHOLD - BLUFF_DELTA_RAISE_SCALE * match_profile["bluff_delta"]
        blocker_raise_threshold = BLOCKER_RAISE_THRESHOLD - BLUFF_DELTA_BLOCKER_SCALE * match_profile["bluff_delta"]
        draw_raise_threshold = clamp(raise_fold_threshold - draw_info["fold_threshold_delta"], 0.46, 0.68)
        draw_equity_slack = DRAW_EQUITY_SLACK_PREMIUM if draw_info["type"] in ("combo_draw", "nut_flush_draw") else DRAW_EQUITY_SLACK_NORMAL
        semi_bluff = (
            round_idx > 0
            and draw_info["semi_bluff"]
            and draw_strength >= SEMI_BLUFF_MIN_DRAW
            and opponent_model["confidence"] >= SEMI_BLUFF_MIN_CONFIDENCE
            and opponent_model["fold_to_raise"] > draw_raise_threshold
            and win_rate >= pot_odds - draw_equity_slack
        )
        blocker_raise = (
            round_idx == 1
            and spot_info["facing_postflop_aggression"]
            and opponent_model["confidence"] >= SEMI_BLUFF_MIN_CONFIDENCE
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
            and pot < TRAP_NUT_MAX_POT
            and nutted_risk["risk"] <= 0.02
            and match_profile["chase"] <= 0.45
            and opponent_model["confidence"] >= 0.20
            and (
                opponent_model["postflop_aggr"] >= 0.38
                or opponent_model["aggression"] >= 0.34
                or opponent_model["fold_to_raise"] < 0.46
            )
        )
        flop_checkraise_exploit = _bb_flop_checkraise_exploit(
            round_idx, spot_info, opponent_model, board_texture,
            value_profile, draw_info, draw_strength, made_strength,
            blocker_profile, blocker_raise,
        )

        if trap_nut_slowplay:
            return 0
        preflop_defensive_only = (
            round_idx == 0
            and to_call > 0
            and not preflop_3bet_candidate
        )
        # [v61 crossover from national_v59] tight-opponent raise suppression.
        # Fired before the offense raise path; converts marginal one-pair raises
        # to calls vs confirmed tight opponents (see _tight_opp_raise_suppress).
        _suppress_raise = _tight_opp_raise_suppress(
            opponent_model, made_strength, draw_strength, round_idx, value_profile
        )
        if not preflop_defensive_only and not _suppress_raise and (win_rate >= max(strong, pot_odds + 0.12) or semi_bluff or flop_checkraise_exploit):
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
    big_pot_threshold = int(clamp(BIG_POT_BASE - BIG_POT_PROTECT_SCALE * match_profile["protect"] + BIG_POT_CHASE_SCALE * match_profile["chase"], BIG_POT_FLOOR, BIG_POT_CEIL))
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
            if round_idx == 3 and anti_lock_attack == -2:
                tier = value_profile.get("tier", "none") if value_profile else "none"
                if tier != "nut":
                    pot_raise = int(pot * 1.0)
                    pot_raise = max(pot_raise, state["min_raise_action"])
                    pot_raise = min(pot_raise, my_chips - 1)
                    if pot_raise > 0 and pot_raise < my_chips:
                        anti_lock_attack = pot_raise
            return anti_lock_attack

    # Passive-exploit thin value bet bypass.
    if passive_thin_value and not thin_static_showdown_control_check(
        round_idx, value_profile, board_texture, draw_strength, anti_lock_pressure
    ):
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
            value_profile=value_profile,
            value_plan=value_plan,
            board_texture=board_texture,
            passive_thin_value=True,
            nutted_risk_score=nutted_risk["risk"],
            match_sizing_delta=match_profile["sizing_delta"],
        )
        if raise_amount is not None:
            return raise_amount

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
    # River thin-value extraction vs call-wide / check-back opponents
    if round_idx == 3 and to_call == 0 and not anti_lock_pressure:
        thin_bet = river_thin_value_bet(
            round_idx, to_call, value_profile, made_strength, draw_strength,
            opponent_model, board_texture, nutted_risk, paired_board_profile,
            pot, my_chips, state, spot_info,
        )
        if thin_bet is not None and thin_bet > 0:
            return thin_bet

    # Ultra-dry thin value: bet for value+protection on the driest boards
    # instead of checking passively. On ultra-dry textures equity is maximally
    # stable (few scare cards), so checking surrenders value and initiative.
    _ultra_dry_thin = (
        round_idx >= 2
        and to_call == 0
        and value_profile is not None
        and value_profile["tier"] == "thin"
        and board_texture is not None
        and not board_texture["dynamic"]
        and board_texture["wetness"] <= 0.15
        and made_strength >= 0.28
        and draw_strength < 0.10
        and not anti_lock_pressure
        and nutted_risk["risk"] < 0.05
    )
    if _ultra_dry_thin:
        _thin_bet = max(state["min_raise_action"], int(pot * 0.45))
        _thin_bet = min(_thin_bet, my_chips - 1)
        if 0 < _thin_bet < my_chips:
            sys.stderr.write(
                f'ULTRA_DRY_THIN_VALUE round={round_idx} '
                f'made={made_strength:.3f} wet={board_texture["wetness"]:.2f} '
                f'pot={pot} bet={_thin_bet}\n'
            )
            return _thin_bet

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

    # Overbet evaluation.
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

    # Donk bet evaluation.
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

    # Probe bet evaluation.
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
    thin_value_raise = _river_thin_value_construct(
        round_idx, to_call, value_profile, made_strength, draw_strength,
        opponent_model, opp_archetype, board_texture, nutted_risk,
        paired_board_stackoff, pot, my_chips, match_profile, anti_lock_pressure,
        state, preflop_strength, spot_info,
    )
    if thin_value_raise is not None and thin_value_raise > 0:
        return thin_value_raise
    if win_rate >= medium or semi_bluff or blocker_bluff or small_probe or check_probe or made_strength >= 0.62 or (value_profile and value_profile["tier"] in ("strong", "nut")):
        # Check-raise trap: check with strong/nut hands on dry flop vs aggressive opponents
        # Trap line: check flop -> call opponent bet -> raise turn for max value
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
        )
        if raise_amount is not None:
            if round_idx == 3 and raise_amount >= my_chips * 0.60:
                tier = value_profile.get("tier", "none") if value_profile else "none"
                if tier != "nut":
                    cap_ratio = 1.0 if tier == "strong" else 0.75
                    capped = int(to_call + (pot + to_call) * cap_ratio)
                    capped = max(capped, state["min_raise_action"])
                    capped = min(capped, my_chips - 1)
                    if capped > to_call and capped < my_chips:
                        raise_amount = capped
            return raise_amount
    return 0

def thin_static_showdown_control_check(round_idx, value_profile, board_texture, draw_strength, anti_lock_pressure):
    """Check whether thin made hands should take a showdown control line."""
    return (
        round_idx >= 2
        and value_profile is not None
        and value_profile["tier"] == "thin"
        and board_texture is not None
        and not board_texture["dynamic"]
        and draw_strength < 0.12
        and not anti_lock_pressure
    )

def _opp_allin_spr_threshold(round_idx, value_profile, made_strength, jam_odds, jam_buffer, anti_lock_pressure, anti_lock_jam_continue):
    """Return the effective all-in calling threshold, adding a thin-hand SPR penalty on turn/river."""
    base = jam_odds + jam_buffer
    penalty = 0.0
    reason = "none"
    if (
        not anti_lock_pressure
        and round_idx >= 2
        and not anti_lock_jam_continue
    ):
        tier = value_profile.get("tier", "none") if value_profile else "none"
        made_max = (
            SPR_COMMITMENT_OPP_ALLIN_FOLD_MADE_MAX
            if round_idx == 3
            else SPR_COMMITMENT_OPP_ALLIN_TURN_FOLD_MADE_MAX
        )
        if tier in ("thin", "none") and made_strength < made_max:
            penalty = SPR_COMMITMENT_OPP_ALLIN_THIN_PENALTY
            reason = f"{tier}_penalty"
    total = base + penalty
    sys.stderr.write(
        f"OPP_ALLIN_SPR_THRESH round={round_idx} base={base:.3f} "
        f"penalty={penalty:.3f} total={total:.3f} reason={reason}\n"
    )
    return total
