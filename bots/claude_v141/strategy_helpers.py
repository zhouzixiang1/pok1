import sys
from card_utils import clamp, card_number
from postflop import bet_size_bucket, board_texture_profile


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
        adjustment += confidence * max(0.0, 0.44 - opponent_model["pfr"]) * 0.07
        if round_idx > 0:
            adjustment += confidence * max(0.0, 0.36 - opponent_model["postflop_aggr"]) * 0.06
        adjustment -= confidence * max(0.0, opponent_model["allin_rate"] - 0.08) * 0.08
        adjustment -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.48) * 0.05
        adjustment += min(0.04, spot_info["last_raise_pot_ratio"] * 0.04)

    # Per-street opponent profiling — unconditional adjustments
    if confidence >= 0.15:
        if round_idx == 2:
            barrel = opponent_model.get('barrel_freq', 0.45)
            if barrel >= 0.60:
                adjustment -= confidence * (barrel - 0.50) * 0.100
            elif barrel <= 0.30:
                adjustment += confidence * (0.40 - barrel) * 0.060
        elif round_idx == 3:
            river_bb = opponent_model.get('avg_river_raise_bb', 5.5)
            river_aggr = opponent_model.get('river_aggr', 0.28)
            if river_bb >= 8.0 and river_aggr >= 0.32:
                adjustment += confidence * 0.060
            elif river_bb <= 3.0 and river_aggr <= 0.22:
                adjustment -= confidence * 0.050
    # Aligned-signal boost
    if confidence >= 0.15:
        if round_idx == 2:
            barrel = opponent_model.get('barrel_freq', 0.45)
            alignment = _aligned_signal_boost(opponent_model, 'barrel_freq', 0.45, 'postflop_aggr', 0.36)
            if alignment > 0:
                if barrel >= 0.60:
                    adjustment -= confidence * alignment * barrel * 1.5
                elif barrel <= 0.30:
                    adjustment += confidence * alignment * (1.0 - barrel) * 1.5
        elif round_idx == 3:
            river_aggr = opponent_model.get('river_aggr', 0.28)
            alignment = _aligned_signal_boost(opponent_model, 'river_aggr', 0.28, 'postflop_aggr', 0.36)
            if alignment > 0:
                if river_aggr >= 0.32:
                    adjustment += confidence * alignment * 1.5
                elif river_aggr <= 0.22:
                    adjustment -= confidence * alignment * 1.5
    return clamp(adjustment, -0.09, 0.11)


def aggressive_line_strength(spot_info, board_texture):
    strength = 0.0
    if spot_info.get("opp_postflop_bet_count", 0) >= 2:
        strength += 0.04
    if spot_info.get("opp_current_round_bet_count", 0) >= 2:
        strength += 0.08 if board_texture is not None and board_texture["paired"] else 0.05
    if spot_info.get("opp_current_round_bet_count", 0) >= 3:
        strength += 0.03
    return clamp(strength, 0.0, 0.15)


def postflop_call_margin(spot_info, opponent_model, made_strength, draw_strength, round_idx, has_position):
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

    # NEW (v137): sizing-tendency defense adjustment (consumes Worker 1's
    # sizing_tendency signal from opponent.py). Targets exploitability probe
    # weaknesses: 28% WR vs min bets and 0% WR vs 2x pot bets.
    #   - Underbettor (>=40% of bets <=0.30x pot): their small bets are
    #     value-heavy. TIGHTEN call margin (fold more weak hands) — their
    #     min-bets are traps, not weakness.
    #   - Overbettor (>=35% of bets >=1.0x pot): polarized range. When facing
    #     a LARGE bet from them, LOOSEN call margin (their overbet range
    #     includes bluffs).
    # Local constants 0.030 / 0.025 are NEW inside this block; no existing
    # constants touched. Clamped by `return clamp(margin, 0.0, 0.08)` below.
    sizing = opponent_model.get('sizing_tendency') if opponent_model else None
    if sizing is not None and sizing.get('samples', 0) >= 8 \
            and sizing.get('confidence', 0.0) >= 0.30:
        tendency = sizing.get('tendency', 'unknown')
        if tendency == 'underbettor' and size_bucket == 'small':
            # Their small bets are value-heavy; raise the bar to call.
            margin += 0.030
        elif tendency == 'overbettor' and size_bucket == 'large':
            # Their large bets are polarized; lower the bar to call.
            margin -= 0.025
        # Telemetry: prove the sizing-tendency adjustment is reachable and
        # firing during daemon evaluation. Without this log, we cannot
        # distinguish a LIVE call-margin adjustment from an inert dead branch
        # (project's recurring INERTNESS failure mode — v127/v128/v130 all
        # shipped dead fold gates). Grep SIZING_MARGIN_ADJ after daemon runs
        # >=30 games to confirm the path is LIVE.
        try:
            adj_milli = 30 if (tendency == 'underbettor' and size_bucket == 'small') else \
                        (-25 if (tendency == 'overbettor' and size_bucket == 'large') else 0)
            sys.stderr.write(
                "SIZING_MARGIN_ADJ tend=%s bucket=%s delta_adj=%+d samples=%d\n"
                % (tendency, size_bucket, adj_milli, sizing.get('samples', 0))
            )
        except Exception:
            pass

    return clamp(margin, 0.0, 0.08)


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

    # Opponent-model-aware EQR adjustment for unclassified hands on late streets
    if opponent_model is not None and round_idx >= 2:
        opp_conf = opponent_model.get('confidence', 0.0)
        if opp_conf >= 0.15:
            barrel = opponent_model.get('barrel_freq', 0.45)
            if barrel >= 0.60:
                eqr -= 0.06
            if round_idx == 3:
                river_bb = opponent_model.get('avg_river_raise_bb', 5.5)
                if river_bb <= 3.0:
                    eqr -= 0.08
            # Aligned-signal boost (per-street AND aggregate must agree)
            barrel_align = _aligned_signal_boost(opponent_model, 'barrel_freq', 0.45, 'postflop_aggr', 0.36)
            if barrel_align > 0:
                if barrel >= 0.60:
                    eqr -= barrel_align * opp_conf * 1.5
                elif barrel <= 0.30:
                    eqr += barrel_align * opp_conf * 1.5
            if round_idx == 3:
                river_aggr = opponent_model.get('river_aggr', 0.28)
                river_align = _aligned_signal_boost(opponent_model, 'river_aggr', 0.28, 'postflop_aggr', 0.36)
                if river_align > 0:
                    if river_aggr >= 0.32:
                        eqr -= river_align * opp_conf * 1.5
                    elif river_aggr <= 0.22:
                        eqr += river_align * opp_conf * 1.5
        eqr = clamp(eqr, 0.45, 0.85)
        return win_rate * eqr

    return win_rate


def sizing_exploit_adjustment(opponent_model, round_idx):
    """Adjust raise sizing based on opponent bet-size patterns."""
    confidence = opponent_model.get('confidence', 0.0)
    if confidence < 0.15:
        return 0.0
    sizing_aggr = opponent_model.get('sizing_aggr', 0.35)
    if sizing_aggr >= 0.55:
        return -0.03 * confidence  # Over-bettors: size down our raises
    elif sizing_aggr <= 0.20:
        return 0.04 * confidence   # Under-bettors: size up for value
    return 0.0


def exploit_dispatch(opponent_model, round_idx, value_profile, made_strength):
    result = {'value_boost': 0.0, 'should_barrel': False}
    confidence = opponent_model.get('confidence', 0.0)
    if confidence < 0.20:
        return result
    tier = value_profile.get('tier', 'none') if value_profile else 'none'
    is_value = tier in ('thin', 'strong', 'nut') or made_strength >= 0.50
    if round_idx >= 2 and is_value:
        call_down_ft = opponent_model.get('call_down_flop_turn', 0.35)
        fold_turn = opponent_model.get('fold_to_bet_turn', 0.44)
        if call_down_ft >= 0.55 and fold_turn <= 0.30:
            result['value_boost'] = 0.08 * confidence
    if round_idx == 3 and is_value:
        call_down_tr = opponent_model.get('call_down_turn_river', 0.35)
        fold_river = opponent_model.get('fold_to_bet_river', 0.44)
        if call_down_tr >= 0.50 and fold_river <= 0.30:
            result['value_boost'] = max(result['value_boost'], 0.10 * confidence)
    if round_idx == 1:
        fold_flop = opponent_model.get('fold_to_bet_flop', 0.44)
        if fold_flop >= 0.55:
            result['should_barrel'] = True
    result['value_boost'] = clamp(result['value_boost'], 0.0, 0.12)
    return result


def bluff_heavy_call_widen(line_profile, value_profile, made_strength, draw_strength, round_idx, opponent_model):
    """Call-widening vs detected bluff_heavy opponents.
    Returns a positive call_margin boost (float) to add to the call decision,
    or 0.0 if not applicable. This WIDENS the call range — it never folds.
    Only fires on turn/river vs bluff_heavy opponents with marginal made hands."""
    if line_profile is None or line_profile.get('line_label') != 'bluff_heavy':
        return 0.0
    if round_idx < 2:
        return 0.0
    if value_profile is not None and value_profile.get('tier') in ('strong', 'nut'):
        return 0.0
    confidence = opponent_model.get('confidence', 0.0)
    if confidence < 0.15:
        return 0.0
    if made_strength < 0.20 or made_strength > 0.45:
        return 0.0
    if draw_strength >= 0.18:
        return 0.0
    bluff_opp = line_profile.get('bluff_opportunity', 0.0)
    boost = 0.03 + 0.05 * max(0.0, bluff_opp - 0.55)
    boost *= confidence
    return clamp(boost, 0.0, 0.08)


def river_value_raise_tier(round_idx, to_call, made_strength, value_profile, board_texture, opponent_model, pot, my_chips, min_raise, my_round_bet):
    """Graduated river value raise (0.45-0.80x pot) for thin-to-strong hands.

    Fires on static river boards when facing no bet, filling the gap between
    checking thin hands and polarizing strong hands. Only activates when we
    have enough opponent data and the board is not dynamic (wet/scary).
    Returns the raise-to-total increment amount, or None if not applicable.

    MUTATION v118 (option a — threshold ~10% lower): widen lower made_strength
    bound 0.50 -> 0.45 to extract thin value on static rivers from sub-top-pair
    holdings (e.g., second pair / weak top pair on dry boards) that v116 was
    leaving as silent checks. The 0.45-0.50 band uses the smallest sizing
    (0.45-0.45x extrapolated below) and remains gated by confidence>=0.10,
    static board, and tier!='nut'. Aligned with experience pool guidance:
    OFFENSIVE primitive on existing detector (NOT a defensive-axis tweak),
    same upper-tier slope so polarized strong hands keep 0.80x sizing.
    """
    if round_idx != 3 or to_call != 0:
        return None
    if made_strength < 0.45 or made_strength > 0.82:
        return None
    if board_texture is None or board_texture.get('dynamic', True):
        return None
    tier = value_profile.get('tier', 'none') if value_profile else 'none'
    if tier == 'nut':
        return None
    confidence = opponent_model.get('confidence', 0.0)
    if confidence < 0.10:
        return None
    # Slope anchored at 0.45-strength → 0.45x sizing; preserves upper end
    # 0.82-strength → ~0.80x identical to v116 behavior on the strong tail.
    t = (made_strength - 0.45) / 0.37
    ratio = 0.45 + 0.35 * t
    fold_river = opponent_model.get('fold_to_bet_river', 0.44)
    if confidence >= 0.20 and fold_river <= 0.30:
        ratio += 0.08 * confidence
    if confidence >= 0.20 and fold_river >= 0.55:
        ratio -= 0.05 * confidence
    ratio = max(0.45, min(ratio, 0.85))
    target = int(pot * ratio)
    amount = max(min_raise, target - my_round_bet)
    amount = min(amount, my_chips - 1)
    if amount < min_raise or amount >= my_chips:
        return None
    return amount


def _river_value_extraction_amplifier(
    round_idx, to_call, made_strength, value_profile, board_texture,
    opponent_model, pot, my_chips, min_raise, my_round_bet,
):
    """v141 NEW: River value-extraction amplifier — 3rd classify_sizing_tendency
    wired site (OFFENSIVE, distinct from the 2 existing DEFENSIVE sites).
    Boosts river value sizing from the 0.45-0.80x baseline (river_value_raise_tier)
    toward 0.65-0.90x when facing a moderately sticky opponent with thin-to-strong
    made hands on static boards. Fills the gap between river_value_raise_tier
    (no opponent gating) and value_maximizer_overbet (requires vm_index>=0.75).
    Returns raise-to-total amount (int) or None.
    """
    if round_idx != 3 or to_call != 0:
        return None
    if made_strength < 0.50 or made_strength > 0.82:
        return None
    if board_texture is None or board_texture.get('dynamic', True):
        return None
    tier = value_profile.get('tier', 'none') if value_profile else 'none'
    if tier == 'nut':
        return None
    confidence = opponent_model.get('confidence', 0.0)
    if confidence < 0.20:
        return None

    sizing = opponent_model.get('sizing_tendency')
    vm_idx = opponent_model.get('value_maximizer_index', 0.5)
    passivity = opponent_model.get('passivity_score', 0.5)
    sizing_valid = (sizing is not None and sizing.get('samples', 0) >= 8
                    and sizing.get('confidence', 0.0) >= 0.30)
    is_underbettor = sizing_valid and sizing.get('tendency') == 'underbettor'
    is_standard_sticky = (sizing_valid and sizing.get('tendency') == 'standard'
                          and vm_idx >= 0.55 and passivity >= 0.55)
    is_unknown_sticky = (not sizing_valid and vm_idx >= 0.60 and passivity >= 0.60
                         and confidence >= 0.30)
    if not (is_underbettor or is_standard_sticky or is_unknown_sticky):
        return None

    t = clamp((made_strength - 0.50) / 0.32, 0.0, 1.0)
    ratio = 0.65 + 0.25 * t
    if is_underbettor and vm_idx >= 0.65:
        ratio += 0.05
    ratio = clamp(ratio, 0.65, 0.90)
    try:
        sys.stderr.write(
            "RIVER_VALUE_AMP made=%.2f tier=%s vm=%.2f pass=%.2f ratio=%.2f path=%s\n"
            % (made_strength, tier, vm_idx, passivity, ratio,
               'under' if is_underbettor else ('std' if is_standard_sticky else 'unk'))
        )
    except Exception:
        pass
    target = int(pot * ratio)
    amount = max(min_raise, target - my_round_bet)
    amount = min(amount, my_chips - 1)
    if amount < min_raise or amount >= my_chips:
        return None
    return amount


# === Crossover from v128 (defensive fold gates) ===
# H2H evidence: v128 beats v129 (0.60 vs v118 0.40), v93 (0.65 vs 0.54),
# v95 (0.57 vs 0.50), v79 (0.70 vs 0.58), v102 (0.60 vs 0.55) — v118's
# losses concentrate against modern barrel-aggressive bots. v128's fold
# gates directly address the 0%-postflop-fold leak.


def check_raise_pressure(spot_info, opponent_model):
    """LIVE check-raise detector: fires when opponent checked AND bet/raised
    in the SAME round (trap line). Strong indicator opponent has a premium
    hand (set / two-pair / strong top-pair) that beats one-pair / air.
    Returns (active: bool, severity: float in [0.04, 0.16]).

    Imports the check_raise_freq directive (undelivered across v118 lineage)
    via a NEW opponent-line signal: opp_current_round_check_count AND
    opp_current_round_bet_count both >= 1, not used elsewhere in the bot.
    Targets the 0%-postflop-fold leak by folding marginal hands to trap lines.
    """
    check_count = spot_info.get('opp_current_round_check_count', 0)
    bet_count = spot_info.get('opp_current_round_bet_count', 0)
    if check_count < 1 or bet_count < 1:
        return False, 0.0
    size_ratio = spot_info.get('last_raise_pot_ratio', 0.0)
    if size_ratio >= 1.0:
        severity = 0.10
    elif size_ratio >= 0.5:
        severity = 0.07
    else:
        severity = 0.04
    confidence = opponent_model.get('confidence', 0.0)
    if confidence >= 0.15:
        post_aggr = opponent_model.get('postflop_aggr', 0.36)
        if post_aggr <= 0.30:
            severity += 0.04
    return True, min(severity, 0.16)


def barrel_pressure_profile(spot_info, opponent_model, round_idx):
    """NEW opponent-line signal: detects REGULAR opponent postflop barrels
    (not check-raise traps). Fires on ANY single+ barrel on turn/river with
    confidence-gated barrel_freq/postflop_aggr evidence.

    Distinct from check_raise_pressure (which requires same-round check+bet
    trap pattern). barrel_pressure captures the dominant leak path: single
    and multi-barrel lines from frequent barrelers.

    Returns (active: bool, severity: float in [0.0, 0.14]).
    """
    if round_idx < 2:
        return False, 0.0
    if not spot_info.get('facing_postflop_aggression', False):
        return False, 0.0
    confidence = opponent_model.get('confidence', 0.0)
    if confidence < 0.15:
        return False, 0.0
    barrel_freq = opponent_model.get('barrel_freq', 0.45)
    post_aggr = opponent_model.get('postflop_aggr', 0.36)
    if barrel_freq < 0.45 and post_aggr < 0.38:
        return False, 0.0
    size_ratio = spot_info.get('last_raise_pot_ratio', 0.0)
    opp_bets = spot_info.get('opp_current_round_bet_count', 0)
    if size_ratio >= 1.0:
        severity = 0.10
    elif size_ratio >= 0.5:
        severity = 0.07
    elif size_ratio >= 0.25:
        severity = 0.04
    else:
        severity = 0.0
    if severity == 0.0:
        return False, 0.0
    if barrel_freq >= 0.55:
        severity += 0.03
    if opp_bets >= 2:
        severity += 0.02
    return True, clamp(severity, 0.0, 0.14)


def turn_second_barrel_planner(
    round_idx, to_call, my_id, history,
    pair_profile, value_profile, made_strength,
    board_texture, opponent_model,
    pot, my_chips, min_raise, my_round_bet,
):
    """OFFENSIVE v129 (re-imported via v130 x v129 crossover): fire a turn
    value second-barrel when bot was the flop aggressor with top-pair-good-
    kicker+ on a non-deteriorated board.

    Targets the turn-under-aggression leak (turn raise ~33.5% baseline, opp
    fold-to-turn-bet ~99%). Returns raise-to-total amount (int) or None.
    Orthogonal to v130's defensive fold-gates (does NOT subtract from
    call_margin / jam_buffer). H2H evidence (v129, 900g, r=1513 rd=79):
    wins 5/6 modern-field matchups >=30g (v96/v95/v93/v110/v113).

    MUTATION (option a, crossover v130 x v129): widened value_eligible
    strength floor 0.50 -> 0.45 (-10%) for top_pair_good_kicker / overpair.
    Targets the empty mid-top-pair band (made_strength 0.45-0.50) where v130
    under-barrels vs modern opponents it loses to (v111 0.45 / v116 0.50 /
    v121 0.50). Two-pair+ branch is unchanged (still auto-eligible); preserves
    all v129 board-texture guards + sizing math.
    """
    if round_idx != 2 or to_call != 0:
        return None
    if pair_profile is None or board_texture is None:
        return None

    # 1. Bot was flop aggressor (raised or all-in on round 1).
    my_flop_aggressor = any(
        rec.get("player_id") == my_id
        and rec.get("round") == 1
        and rec.get("action_type") in ("raise", "allin")
        for rec in history
    )
    if not my_flop_aggressor:
        return None

    # 2. Value hand: two-pair+ OR (top-pair-good-kicker / overpair with strength).
    made_class = pair_profile.get("made_class", -1)
    pair_type = pair_profile.get("pair_type", "none")
    weak_kicker = pair_profile.get("weak_kicker", True)
    if made_class >= 2:
        value_eligible = True
    elif made_class == 1:
        # MUTATION: 0.50 -> 0.45 (widens top-pair-good-kicker eligibility -10%).
        value_eligible = (
            (pair_type == "top_pair" and not weak_kicker)
            or pair_type == "overpair"
        ) and made_strength >= 0.45
    else:
        value_eligible = False
    if not value_eligible:
        return None

    # 3. Non-deteriorated board: no 3-flush / 4-straight completion, not dynamic.
    if board_texture.get("flush_pressure", 0.0) >= 0.75:
        return None
    if board_texture.get("straight_pressure", 0.0) >= 0.65:
        return None
    if board_texture.get("dynamic", False):
        return None

    # 4. Sizing: 0.55-0.70x pot scaled by made_strength + opponent fold-to-turn.
    t = clamp((made_strength - 0.50) / 0.20, 0.0, 1.0)
    ratio = 0.55 + 0.15 * t
    confidence = opponent_model.get("confidence", 0.0)
    if confidence >= 0.20:
        fold_turn = opponent_model.get("fold_to_bet_turn", 0.44)
        if fold_turn >= 0.50:
            ratio += 0.05 * confidence
        call_down_ft = opponent_model.get("call_down_flop_turn", 0.35)
        if call_down_ft >= 0.55:
            ratio += 0.04 * confidence
    ratio = clamp(ratio, 0.55, 0.75)
    target = int(pot * ratio)
    amount = max(min_raise, target - my_round_bet)
    amount = min(amount, my_chips - 1)
    if amount < min_raise or amount >= my_chips:
        return None
    return amount


def value_maximizer_overbet(
    round_idx, to_call, made_strength, value_profile, board_texture,
    opponent_model, pot, my_chips, min_raise, my_round_bet, nutted_risk,
):
    """NEW offensive value-overbet path vs confirmed calling stations.

    Fires on turn/river when opponent's value_maximizer_index >= 0.75
    (rarely folds turn/river bets AND calls down consistently) AND we have
    a high-confidence read (confidence >= 0.30 per v136 mutation), holding
    a value hand on a non-dynamic board with low nutted_risk.

    DISTINCT from passive_exploit (caps river at 0.72x) and should_overbet
    (requires nut tier + dry board). Fills the STRONG/THIN tier gap: extracts
    0.75-1.20x pot from calling stations that existing primitives undercharge.

    3 reachable sizing branches: thin (0.75-0.85x), strong (0.95-1.05x),
    nut (1.10-1.20x). Returns raise-to-total amount (int) or None.
    """
    if round_idx < 2 or to_call > 0:
        return None
    # MUTATION v136 (option a — threshold adjustment, +20%): raise confidence
    # floor 0.25 -> 0.30 so the overbet only fires with a stronger multi-street
    # read on opponent stickiness. Experience pool insight #5 explicitly warns
    # "v134 overbet leaks vs tight foes" — at confidence 0.25 (low bar) the
    # overbet can fire on noisy ~15-game reads, producing high-variance
    # overbets against moderately sticky opponents that aren't truly calling
    # stations. The 0.30 floor requires ~30g+ of evidence before the overbet
    # activates, aligning with the experience pool's >=30g confidence gate
    # standard. Preserves all other gating (vm_index >= 0.75, tier checks,
    # nutted_risk <= 0.05, non-dynamic board).
    confidence = opponent_model.get('confidence', 0.0)
    if confidence < 0.30:
        return None
    vm_index = opponent_model.get('value_maximizer_index', 0.5)
    if vm_index < 0.75:
        return None
    tier = value_profile.get('tier', 'none') if value_profile else 'none'
    # Eligibility per tier (3 reachable value branches)
    if tier == 'nut':
        pass
    elif tier == 'strong':
        pass
    elif tier == 'thin' and made_strength >= 0.55:
        pass
    elif made_strength >= 0.62:
        pass  # unclassified but strong-made
    else:
        return None
    if nutted_risk > 0.05:
        return None
    if board_texture is not None and board_texture.get('dynamic', True):
        return None
    # 3 distinct sizing branches
    if tier == 'nut':
        ratio = 1.10 + 0.10 * clamp((made_strength - 0.75) / 0.20, 0.0, 1.0)
    elif tier == 'strong':
        ratio = 0.95 + 0.10 * clamp((made_strength - 0.60) / 0.15, 0.0, 1.0)
    elif tier == 'thin':
        ratio = 0.75 + 0.10 * clamp((made_strength - 0.55) / 0.15, 0.0, 1.0)
    else:  # unclassified strong (made_strength >= 0.62)
        ratio = 0.90
    # Ultra-sticky boost: extreme call-down rate justifies further sizing up
    call_down_tr = opponent_model.get('call_down_turn_river', 0.35)
    if call_down_tr >= 0.65:
        ratio += 0.05
    ratio = clamp(ratio, 0.75, 1.25)
    target = int(pot * ratio)
    amount = max(min_raise, target - my_round_bet)
    amount = min(amount, my_chips - 1)
    if amount < min_raise or amount >= my_chips:
        return None
    return amount


def _turn_oop_pot_control(
    round_idx, to_call, spot_info, made_strength, draw_strength,
    value_profile, board_texture, public_cards, my_chips, pot,
    opponent_model, anti_lock_pressure, min_raise,
):
    """v139 NEW: Turn OOP pot-control. Returns downsized raise-to-total (int) or None.

    Attacks the 0%-fold / river stack-off leak UPSTREAM at the turn sizing
    decision: caps the bot's OWN turn barrel on deteriorated boards when OOP
    with marginal made hand in mid-SPR. Keeps the pot small so river all-ins
    stop being geometric commitments. Distinct axis from _river_stackoff_guard
    (which fires AT the all-in call and is INERT by placement). Orthogonal to
    choose_raise() sizing knobs.
    """
    # 1. Hard eligibility gates
    if round_idx != 2 or to_call != 0:
        return None
    # OOP on turn = SB (dealer==BB in this engine; SB acts first every street).
    # DO NOT use spot_info['has_position'] for OOP — that flag means my_id==bb.
    if not spot_info.get('my_is_sb', False):
        return None
    if anti_lock_pressure:
        return None
    if value_profile is not None and value_profile.get('tier') == 'nut':
        return None
    if made_strength < 0.40 or made_strength > 0.65:
        return None
    if draw_strength >= 0.20:
        return None

    # 2. SPR danger zone: pot bloat → geometric commitment → river stack-off
    spr = my_chips / pot if pot > 0 else 999.0
    if spr < 1.5 or spr > 4.5:
        return None

    # 3. Board deterioration detection (turn card made board worse than flop)
    if board_texture is None or len(public_cards) != 4:
        return None
    # Fast-path: skip if board is neither dynamic nor has any pressure
    if (not board_texture.get('dynamic', False)
            and board_texture.get('flush_pressure', 0.0) < 0.35
            and board_texture.get('straight_pressure', 0.0) < 0.40):
        return None

    flop_texture = board_texture_profile(public_cards[:3])
    # Recompute turn texture from public_cards (same source as flop for consistency).
    turn_texture_recomputed = board_texture_profile(public_cards)
    fp_now = turn_texture_recomputed.get('flush_pressure', 0.0)
    fp_flop = flop_texture.get('flush_pressure', 0.0)
    sp_now = turn_texture_recomputed.get('straight_pressure', 0.0)
    sp_flop = flop_texture.get('straight_pressure', 0.0)
    flop_ranks = [card_number(c) for c in public_cards[:3]]
    turn_rank = card_number(public_cards[3])

    severity = 0.0
    if fp_now >= 1.0 and fp_flop < 1.0:
        severity += 0.40  # flush got there (draw completed on turn)
    elif fp_now >= 0.75 and fp_flop < 0.75:
        severity += 0.40  # flush draw emerged
    elif fp_now >= 0.35 and fp_flop < 0.35:
        severity += 0.20  # flush draw emerged (4th suit on turn)
    if sp_now >= 1.0 and sp_flop < 1.0:
        severity += 0.30  # straight completed (draw completed on turn)
    elif sp_now >= 0.65 and sp_flop < 0.65:
        severity += 0.30  # straight draw emerged
    elif sp_now > sp_flop + 0.20:
        severity += 0.15  # straight draw worsened
    if turn_rank > max(flop_ranks) and turn_rank >= 11:
        severity += 0.25  # overcard (J+) hit
    if turn_texture_recomputed.get('paired', False) and not flop_texture.get('paired', False):
        if max(flop_ranks) == turn_rank:
            severity += 0.20  # top flop card paired on turn (trips risk)

    if severity < 0.30:
        return None

    # 4. Opponent-model gating: only fire vs opponents who can exploit deterioration
    confidence = opponent_model.get('confidence', 0.0)
    if confidence < 0.15:
        return None

    # 5. Downsized sizing: 0.30-0.40x pot (vs standard 0.50-0.75x).
    # Stronger made hand → slightly larger ratio; stronger deterioration → smaller.
    t = clamp((made_strength - 0.40) / 0.25, 0.0, 1.0)
    ratio = 0.30 + 0.10 * t
    ratio -= 0.08 * clamp((severity - 0.30) / 0.50, 0.0, 1.0)
    ratio = clamp(ratio, 0.22, 0.40)
    target = int(pot * ratio)
    if target < min_raise or target >= my_chips:
        return None  # fall through to check / standard sizing
    return target


def _river_stackoff_guard(made_strength, value_profile, spot_info, opponent_model,
                          my_chips, pot, to_call):
    """River stack-off guard — fold weak hands facing large bets/all-ins.

    Targets the -15.5k to -20k river stack-off leak (~76 pairs v14-v137,
    0% postflop fold when beaten). Caller must check round_idx == 3.

    Returns True (force fold) when ALL hold:
      - value_profile tier in ('thin', 'none') (NOT strong/nut)
      - made_strength < 0.55 (catches one-pair / weak-two-pair / A-high)
      - bet is large: spot_info['last_raise_pot_ratio'] >= 0.75 OR facing_allin
      - pot odds fail: to_call / (pot + to_call) > made_strength + 0.10
      - NOT confirmed polarized overbettor (preserve v137 defense)

    Distinct from existing should_fold_postflop gates: fires UNCONDITIONALLY
    on river large bets (no call_margin stacking), placed EARLY in the
    to_call>0 block to prevent downstream overrides.

    Persistent firing fixture: see __main__ block at end of this file.
    """
    # 1. Hand-strength gate — never block strong/nut value
    if value_profile is not None and value_profile.get('tier') in ('strong', 'nut'):
        return False
    if made_strength >= 0.55:
        return False

    # 2. Bet-size gate — only fire on large bets / all-ins
    bet_ratio = spot_info.get('last_raise_pot_ratio', 0.0)
    facing_allin = spot_info.get('facing_allin', False)
    if not (facing_allin or bet_ratio >= 0.75):
        return False

    # 3. Overbettor defense — trust polarized range (preserve v137 relaxation)
    if opponent_model is not None:
        sizing = opponent_model.get('sizing_tendency')
        if (sizing is not None
                and sizing.get('samples', 0) >= 8
                and sizing.get('confidence', 0.0) >= 0.30
                and sizing.get('tendency') == 'overbettor'):
            street_over = sizing.get('per_street_overbet', {}).get(3, 0.0)
            if street_over >= 0.40:
                return False

    # 4. Pot-odds gate — only fold when math says equity insufficient
    if to_call <= 0 or pot <= 0:
        return False
    pot_odds_required = to_call / (pot + to_call)
    if pot_odds_required <= made_strength + 0.10:
        return False

    return True


def _river_value_ship_guard(round_idx, my_chips, made_strength, value_profile,
                            board_texture, pair_profile, raise_amount):
    """v140 NEW: BET-side river ship guard. Returns True if the bot's OWN river
    raise committing >=25% stack should be BLOCKED (caller downgrades to call).
    Permitted pivot after direction audit FORBADE fold-side _river_stackoff_guard
    (EXHAUSTED v135/v138/v139 placement-shadow). Targets -15k/-20k river
    stack-offs where bot re-raised all-in vs polarized opp raise with non-nut
    made hands (G4H45 bottom A-2 two-pair; G6H15 trip-2s weak kicker). Returns
    True (BLOCK raise -> call) when ALL hold: round_idx == 3; raise_amount >=
    25% of effective stack; NOT (made_strength >= 0.70 AND tier in strong/nut
    AND board safe). Board safe = no completed flush (flush_pressure < 0.75),
    no straight (straight_pressure < 0.65), and on paired board only ship with
    trips+ (pair_profile.made_class >= 3). Persistent fixture in __main__
    exercises BOTH the helper AND the strategy.py dispatch wire (grep guard).
    """
    if round_idx != 3:
        return False
    if raise_amount is None or raise_amount <= 0:
        return False
    if raise_amount < my_chips * 0.25:
        return False
    tier = value_profile.get('tier', 'none') if value_profile else 'none'
    strength_ok = made_strength >= 0.70
    tier_ok = tier in ('strong', 'nut')
    board_safe = True
    if board_texture is not None:
        if board_texture.get('flush_pressure', 0.0) >= 0.75:
            board_safe = False
        if board_texture.get('straight_pressure', 0.0) >= 0.65:
            board_safe = False
        if board_texture.get('paired', False):
            mc = pair_profile.get('made_class', -1) if pair_profile else -1
            if mc < 3:  # two-pair or worse on paired board = vulnerable
                board_safe = False
    if strength_ok and tier_ok and board_safe:
        return False  # ALLOW nutted value ship on safe board
    return True  # BLOCK non-nut big-commit river raise


if __name__ == "__main__":
    # v139 _turn_oop_pot_control fixture: 8 cases. Proves LIVE reachability
    # (project #1 INERTNESS failure mode). All cases must match expected.
    base_spot = {"my_is_sb": True, "has_position": False}
    ip_spot = {"my_is_sb": False, "has_position": True}
    bt_wet = {"flush_pressure": 0.75, "straight_pressure": 0.40,
              "dynamic": True, "paired": False, "wetness": 0.55}
    bt_safe = {"flush_pressure": 0.10, "straight_pressure": 0.10,
               "dynamic": False, "paired": False, "wetness": 0.05}
    opp = {"confidence": 0.30, "postflop_aggr": 0.42}

    # Case 1: OOP turn, made=0.55, deteriorated (flush got there), SPR=2.5 → DOWNSIZE
    pc = _turn_oop_pot_control(
        2, 0, base_spot, 0.55, 0.05, {"tier": "thin"}, bt_wet,
        [0, 4, 8, 12], 5000, 2000, opp, False, 100)
    assert pc is not None and 400 <= pc <= 800, \
        f"Case 1 (eligible downsize) expected 400-800, got {pc}"

    # Case 2: NOT OOP (IP / my_is_bb) → None
    pc = _turn_oop_pot_control(
        2, 0, ip_spot, 0.55, 0.05, {"tier": "thin"}, bt_wet,
        [0, 4, 8, 12], 5000, 2000, opp, False, 100)
    assert pc is None, f"Case 2 (IP) should return None, got {pc}"

    # Case 3: Nut tier → None (never downsize nuts)
    pc = _turn_oop_pot_control(
        2, 0, base_spot, 0.85, 0.05, {"tier": "nut"}, bt_wet,
        [0, 4, 8, 12], 5000, 2000, opp, False, 100)
    assert pc is None, f"Case 3 (nut tier) should return None, got {pc}"

    # Case 4: Safe non-dynamic board → None
    pc = _turn_oop_pot_control(
        2, 0, base_spot, 0.55, 0.05, {"tier": "thin"}, bt_safe,
        [0, 4, 8, 12], 5000, 2000, opp, False, 100)
    assert pc is None, f"Case 4 (safe board) should return None, got {pc}"

    # Case 5: Strong draw (draw_strength=0.30) → None (keep semi-bluff sizing)
    pc = _turn_oop_pot_control(
        2, 0, base_spot, 0.45, 0.30, {"tier": "none"}, bt_wet,
        [0, 4, 8, 12], 5000, 2000, opp, False, 100)
    assert pc is None, f"Case 5 (strong draw) should return None, got {pc}"

    # Case 6: High SPR (8.0) → None (deep, no geometric commitment)
    pc = _turn_oop_pot_control(
        2, 0, base_spot, 0.55, 0.05, {"tier": "thin"}, bt_wet,
        [0, 4, 8, 12], 16000, 2000, opp, False, 100)
    assert pc is None, f"Case 6 (SPR=8) should return None, got {pc}"

    # Case 7: Low SPR (1.0) → None (already committed, pot-control moot)
    pc = _turn_oop_pot_control(
        2, 0, base_spot, 0.55, 0.05, {"tier": "thin"}, bt_wet,
        [0, 4, 8, 12], 1500, 1500, opp, False, 100)
    assert pc is None, f"Case 7 (SPR=1.0) should return None, got {pc}"

    # Case 8: Made too weak (0.30, below 0.40 floor) → None
    pc = _turn_oop_pot_control(
        2, 0, base_spot, 0.30, 0.05, {"tier": "none"}, bt_wet,
        [0, 4, 8, 12], 5000, 2000, opp, False, 100)
    assert pc is None, f"Case 8 (weak hand) should return None, got {pc}"

    print("v139 _turn_oop_pot_control fixture: 8/8 PASS")

    # v139 _river_stackoff_guard regression fixture (2 cases). Restores
    # coverage lost when _turn_oop_pot_control displaced v138's standalone
    # self-test. Guard is still LIVE at strategy.py:1019.
    # R1: thin-tier stack-off, pot-odds 0.50 > 0.45 → FIRE (fold)
    guard_fire = _river_stackoff_guard(
        made_strength=0.35,
        value_profile={"tier": "none"},
        spot_info={"last_raise_pot_ratio": 1.0, "facing_allin": True},
        opponent_model=None,
        my_chips=5000, pot=5000, to_call=5000,
    )
    assert guard_fire is True, \
        f"R1 (thin stack-off) expected True, got {guard_fire}"

    # R2: nut-tier value ship → INERT
    guard_inert = _river_stackoff_guard(
        made_strength=0.92,
        value_profile={"tier": "nut"},
        spot_info={"last_raise_pot_ratio": 1.5, "facing_allin": True},
        opponent_model=None,
        my_chips=5000, pot=5000, to_call=5000,
    )
    assert guard_inert is False, \
        f"R2 (nut value ship) expected False, got {guard_inert}"

    print("v139 _river_stackoff_guard regression fixture: 2/2 PASS")

    # v140 _river_value_ship_guard fixture: 6 cases. Proves helper LIVE.
    bt_safe = {"flush_pressure": 0.10, "straight_pressure": 0.10, "dynamic": False, "paired": False, "wetness": 0.05}
    bt_flush = {"flush_pressure": 0.85, "straight_pressure": 0.10, "dynamic": True, "paired": False, "wetness": 0.40}
    bt_paired = {"flush_pressure": 0.10, "straight_pressure": 0.10, "dynamic": False, "paired": True, "wetness": 0.10}
    g = _river_value_ship_guard(3, 10000, 0.42, {"tier": "thin"}, bt_safe, {"made_class": 1}, 5000)  # F1 thin mid-pair ship -> BLOCK
    assert g is True, f"F1 expected True, got {g}"
    g = _river_value_ship_guard(3, 10000, 0.78, {"tier": "strong"}, bt_safe, {"made_class": 2}, 5000)  # F2 strong safe -> ALLOW
    assert g is False, f"F2 expected False, got {g}"
    g = _river_value_ship_guard(3, 10000, 0.78, {"tier": "strong"}, bt_flush, {"made_class": 2}, 5000)  # F3 flush completed -> BLOCK
    assert g is True, f"F3 expected True, got {g}"
    g = _river_value_ship_guard(3, 10000, 0.55, {"tier": "thin"}, bt_paired, {"made_class": 2}, 6000)  # F4 paired bottom two-pair -> BLOCK
    assert g is True, f"F4 expected True, got {g}"
    g = _river_value_ship_guard(3, 10000, 0.78, {"tier": "strong"}, bt_paired, {"made_class": 3}, 6000)  # F5 paired trips+ strong -> ALLOW
    assert g is False, f"F5 expected False, got {g}"
    g = _river_value_ship_guard(3, 10000, 0.42, {"tier": "thin"}, bt_safe, {"made_class": 1}, 1500)  # F6 small raise 15% -> ALLOW
    assert g is False, f"F6 expected False, got {g}"
    print("v140 _river_value_ship_guard fixture: 6/6 PASS")

    # Dispatch-reachability test (project #1 INERTNESS mandate). v138/v139 failed.
    import os
    strat_path = os.path.join(os.path.dirname(__file__), 'strategy.py')
    with open(strat_path) as f:
        src = f.read()
    n_refs = src.count('_river_value_ship_guard')
    assert n_refs >= 3, f"Dispatch-reachability FAIL: need >=3 refs in strategy.py, found {n_refs}"
    idx = src.find('if _river_value_ship_guard(round_idx, my_chips')
    assert idx > 0, "Dispatch-reachability FAIL: guard call not found at raise dispatch"
    ret_idx = src.find('return raise_amount', idx)
    assert ret_idx > 0, "Dispatch-reachability FAIL: 'return raise_amount' must follow guard call"
    print("v140 _river_value_ship_guard dispatch-reachability: PASS")

    # v141 _river_value_extraction_amplifier fixture: 6 cases.
    bt_safe = {"flush_pressure": 0.10, "straight_pressure": 0.10, "dynamic": False, "paired": False, "wetness": 0.05}
    bt_wet = {"flush_pressure": 0.75, "straight_pressure": 0.40, "dynamic": True, "paired": False, "wetness": 0.55}
    opp_under = {"confidence": 0.35, "value_maximizer_index": 0.70, "passivity_score": 0.65,
                 "sizing_tendency": {"samples": 12, "confidence": 0.60, "tendency": "underbettor"}}
    opp_std = {"confidence": 0.35, "value_maximizer_index": 0.60, "passivity_score": 0.58,
               "sizing_tendency": {"samples": 12, "confidence": 0.60, "tendency": "standard"}}
    opp_over = {"confidence": 0.35, "value_maximizer_index": 0.70, "passivity_score": 0.65,
                "sizing_tendency": {"samples": 12, "confidence": 0.60, "tendency": "overbettor"}}
    opp_low_conf = {"confidence": 0.10, "value_maximizer_index": 0.70, "passivity_score": 0.65,
                    "sizing_tendency": {"samples": 12, "confidence": 0.60, "tendency": "underbettor"}}
    a = _river_value_extraction_amplifier(3, 0, 0.60, {"tier": "thin"}, bt_safe, opp_under, 2000, 5000, 100, 0)
    assert a is not None and 1400 <= a <= 1700, f"F1 expected 1400-1700, got {a}"
    a = _river_value_extraction_amplifier(3, 0, 0.70, {"tier": "strong"}, bt_safe, opp_std, 2000, 5000, 100, 0)
    assert a is not None and 1500 <= a <= 1900, f"F2 expected 1500-1900, got {a}"
    a = _river_value_extraction_amplifier(3, 0, 0.65, {"tier": "thin"}, bt_safe, opp_over, 2000, 5000, 100, 0)
    assert a is None, f"F3 (overbettor) expected None, got {a}"
    a = _river_value_extraction_amplifier(3, 0, 0.40, {"tier": "none"}, bt_safe, opp_under, 2000, 5000, 100, 0)
    assert a is None, f"F4 (weak made) expected None, got {a}"
    a = _river_value_extraction_amplifier(3, 0, 0.60, {"tier": "thin"}, bt_wet, opp_under, 2000, 5000, 100, 0)
    assert a is None, f"F5 (dynamic board) expected None, got {a}"
    a = _river_value_extraction_amplifier(3, 0, 0.60, {"tier": "thin"}, bt_safe, opp_low_conf, 2000, 5000, 100, 0)
    assert a is None, f"F6 (low confidence) expected None, got {a}"
    print("v141 _river_value_extraction_amplifier fixture: 6/6 PASS")
    n_refs = src.count('_river_value_extraction_amplifier')
    assert n_refs >= 3, f"Dispatch-reachability FAIL: need >=3 refs in strategy.py, found {n_refs}"
    idx = src.find('_rva = _river_value_extraction_amplifier(')
    assert idx > 0, "Dispatch-reachability FAIL: _rva call not found"
    ret_idx = src.find('return _rva', idx)
    assert ret_idx > 0, "Dispatch-reachability FAIL: 'return _rva' must follow call"
    print("v141 _river_value_extraction_amplifier dispatch-reachability: PASS")
