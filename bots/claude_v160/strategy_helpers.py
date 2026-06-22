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

    # M6 TELEMETRY (v156): hoist to function scope - print TOTAL margin so the
    # 96.9% standard-arm contribution is visible, not just tendency arm B.
    # Previously the SIZING_MARGIN_ADJ print was nested inside the
    # `sizing is not None...` block, which only fires for ~0.04% of hands,
    # making the standard-arm-A path invisible to daemon grep.
    try:
        total_milli = round(margin * 1000)
        reason = 'no_margin' if total_milli == 0 else 'standard_arm'
        sys.stderr.write(
            'CALL_MARGIN_FINAL margin_milli=%+d reason=%s round=%d bucket=%s\n'
            % (total_milli, reason, round_idx, size_bucket)
        )
    except Exception:
        pass

    return clamp(margin, 0.0, 0.08)


def _multi_street_calldown_tax(spot_info, made_strength, draw_strength,
                                value_profile, round_idx):
    """v157 NEW: Multi-street cumulative call-down tax. Adds to postflop
    CALL MARGIN when opponent has bet on >=2 postflop streets, so marginal
    made hands (0.30-0.62) are folded on turn/river instead of calling down
    into -15k/-20k river stack-offs (the 0%-fold leak confirmed across ~620
    versions v79->v156).

    Distinct from every EXHAUSTED prior pattern:
      - NOT a to_call==0 offense detector (donk/bluff/barrel; v137-v152).
      - NOT a fold-side binary SPR/commitment/stackoff guard (v135-v154).
      - NOT a preflop sizing variant (v144/v145/v146).
    It is a CONTINUOUS margin additive on the LIVE to_call>0 consumer at
    strategy.py L1307 `if realized_rate < pot_odds + call_margin: return -1`.

    Continuous delta, no deadzone (satisfies M5/M6 INERTNESS rule).
    Returns delta in [0.0, 0.075].
    """
    # 1. Street gate: only turn/river (multi-street aggression needs >=2 streets).
    if round_idx < 2:
        return 0.0
    # 2. Hand-strength gates: never tax strong hands or live draws.
    if value_profile is not None and value_profile.get('tier') in ('strong', 'nut'):
        return 0.0
    if made_strength >= 0.62:
        return 0.0
    if draw_strength >= 0.18:
        return 0.0
    # 3. Multi-street aggression gate: opp must have bet on >=1 prior street.
    prior_bets = spot_info.get('opp_prior_postflop_raise_count', 0)
    if prior_bets < 1:
        return 0.0
    # 4. Marginal band: 0.30 <= made_strength < 0.62.
    if made_strength < 0.30:
        return 0.0
    # 5. Continuous delta (no thresholded elif chain — M5 rule):
    #    - street_base: river (0.045) > turn (0.030)
    #    - aggression multiplier: 1.0 at 1 prior bet, 1.25 at 2, capped 1.5
    #    - band_distance: peaks at made_strength=0.46 (one-pair weak kicker),
    #      linear ramp to 0 at band edges (0.30 / 0.62)
    street_base = 0.045 if round_idx == 3 else 0.030
    aggression_mult = min(1.5, 1.0 + 0.25 * (prior_bets - 1))
    band_dist = 1.0 - abs(made_strength - 0.46) / 0.16  # 1 at center, 0 at edges
    band_dist = max(0.0, min(1.0, band_dist))
    delta = street_base * aggression_mult * (0.5 + 0.5 * band_dist)
    delta = min(0.075, delta)
    # 6. Telemetry (M6 pattern: function-scope, unconditional, prints TOTAL delta
    #    with reason tag — guards against the project's recurring INERTNESS
    #    failure mode where block-scoped or thresholded telemetry hides 0-fires).
    try:
        reason = 'tax_fired' if delta > 0.005 else 'no_tax'
        sys.stderr.write(
            'CALLDOWN_TAX_FINAL delta_milli=%+d round=%d prior_bets=%d '
            'made=%.2f draw=%.2f reason=%s\n'
            % (round(delta * 1000), round_idx, prior_bets, made_strength,
               draw_strength, reason)
        )
    except Exception:
        pass
    return delta


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


# v150: Named thresholds for _street_fold_exploit_sizing_boost.
# Relaxed from inline 0.20/0.55 to break inertness loop (direction audit v150).
# Lower confidence gate ensures the boost fires earlier in matches.
# Lower fold threshold ensures the boost fires vs marginally-folding opps.
STREET_FOLD_BOOST_MIN_CONF = 0.15   # was inline 0.20
STREET_FOLD_BOOST_MIN_FOLD = 0.50   # was inline 0.55


def _street_fold_exploit_sizing_boost(opponent_model, round_idx):
    """v149 NEW OFFENSE: Per-street fold-exploit sizing boost.

    Returns a positive delta in [0.0, 0.25] to add to choose_raise `ratio` when
    the opponent's per-street fold-to-bet signal indicates sizing up is +EV.
    Per-opponent behavior profiles show opp fold-to-bet ~99% on flop vs v127
    (n=1482), yet v148 raises at 0.4x pot (match-analysis). Sizing up wins
    more chips per fold with no fold-equity risk (breakeven 44% at 0.80x pot).

    Trigger: fold_to_bet >= STREET_FOLD_BOOST_MIN_FOLD (0.50) with confidence
    >= STREET_FOLD_BOOST_MIN_CONF (0.15).
    Scale: 0.50 -> +0.08, 0.80 -> +0.17, 0.95+ -> +0.25 (capped), * confidence.

    PARAMETER_TUNING EXEMPT (experience pool): NEW opponent-signal gating -
    per-street fold_to_bet signals were previously consumed only at
    secondary paths (exploit_dispatch.should_barrel trigger, call-down rates),
    NEVER directly in choose_raise sizing ratio.
    """
    if round_idx not in (1, 2, 3):
        return 0.0
    confidence = opponent_model.get('confidence', 0.0)
    if confidence < STREET_FOLD_BOOST_MIN_CONF:
        return 0.0
    street_key = {1: 'fold_to_bet_flop', 2: 'fold_to_bet_turn', 3: 'fold_to_bet_river'}[round_idx]
    fold_rate = opponent_model.get(street_key, 0.44)
    if fold_rate < STREET_FOLD_BOOST_MIN_FOLD:
        return 0.0
    raw = 0.08 + (fold_rate - STREET_FOLD_BOOST_MIN_FOLD) * 0.30
    raw = clamp(raw, 0.08, 0.25)
    boost = raw * confidence
    try:
        sys.stderr.write(
            "STREET_FOLD_BOOST round=%d fold=%.2f raw=%.3f boost=%.3f conf=%.2f\n"
            % (round_idx, fold_rate, raw, boost, confidence)
        )
    except Exception:
        pass
    return boost


def _turn_value_extraction_floor(opponent_model, round_idx, value_profile,
                                 board_texture, made_strength):
    """v160 NEW OFFENSE: Turn value-extraction sizing floor vs calling stations.

    INVERSE of v149 _street_fold_exploit_sizing_boost (which fires when opp FOLDS
    >= 0.50). This fires when opp CALLS often (fold_to_bet_turn <= 0.42), lifting
    thin/strong made-hand turn sizing above the 0.30x thin_cap toward 0.40-0.50x
    to extract value from worse calling hands and charge draws more.

    PARAMETER_TUNING EXEMPT: NEW opponent-signal gating — fold_to_bet_turn is read
    in the LOW direction (calling-station), used by no other turn-sizing function.
    Returns additive sizing delta (float) or 0.0.
    """
    if round_idx != 2:
        return 0.0
    tier = value_profile.get('tier', 'none') if value_profile else 'none'
    if tier not in ('thin', 'strong'):
        return 0.0
    if made_strength < 0.45:
        return 0.0
    confidence = opponent_model.get('confidence', 0.0)
    if confidence < 0.15:
        return 0.0
    fold_turn = opponent_model.get('fold_to_bet_turn', 0.44)
    # Calling station: folds <= 42% of turn bets (calls >= 58%).
    if fold_turn > 0.42:
        return 0.0
    # Continuous delta (no deadzone): 0 at fold=0.42, ramping to +0.15 at fold=0.28.
    call_stickiness = clamp((0.42 - fold_turn) / 0.14, 0.0, 1.0)
    delta = 0.15 * call_stickiness * confidence
    # Draw-completed boards add a protection premium (charge draws more).
    if board_texture and board_texture.get('dynamic', False):
        delta += 0.04 * clamp(board_texture.get('wetness', 0.0), 0.0, 1.0)
    try:
        sys.stderr.write(
            'TURN_VALUE_FLOOR delta_milli=%+d fold_turn=%.2f stick=%.2f '
            'tier=%s made=%.2f conf=%.2f\n'
            % (round(delta * 1000), fold_turn, call_stickiness,
               tier, made_strength, confidence))
    except Exception:
        pass
    return delta


def _delayed_calldown_bluff(
    round_idx, to_call, made_strength, draw_strength, board_texture,
    opponent_model, pot, my_chips, min_raise, my_round_bet,
):
    '''v151 NEW: Delayed bluff vs opponents with street-declining call-down.

    Fires on turn/river (round 2 or 3) when to_call==0 (opp checked) with
    weak air hands. Exploits opponents who call flop bets frequently
    (sticky early) but fold turn/river bets (give up later).

    Signal: calldown_profile (per-street call-down rate, NEW v151).
    No existing function reads this signal.
    '''
    if to_call != 0 or round_idx not in (2, 3):
        return None
    if made_strength >= 0.28 or draw_strength >= 0.20:
        return None
    if opponent_model is None:
        return None
    confidence = opponent_model.get('confidence', 0.0)
    if confidence < 0.20:
        return None
    profile = opponent_model.get('calldown_profile')
    if profile is None:
        return None
    flop_data = profile.get(1, {})
    curr_data = profile.get(round_idx, {})
    if flop_data.get('samples', 0) < 4 or curr_data.get('samples', 0) < 3:
        return None
    flop_call_rate = flop_data.get('rate', 0.5)
    curr_call_rate = curr_data.get('rate', 0.5)
    # Pattern: sticky on flop (call >=0.50) but foldy now (call <=0.45)
    if flop_call_rate < 0.50 or curr_call_rate > 0.45:
        return None
    # Don't bluff into dynamic boards (draws may have arrived)
    if board_texture is not None and board_texture.get('dynamic', False):
        return None
    # Sizing: 0.50-0.60x pot based on fold rate
    fold_rate = 1.0 - curr_call_rate
    ratio = 0.50 + 0.10 * clamp((fold_rate - 0.50) / 0.30, 0.0, 1.0)
    target = int(pot * ratio)
    amount = max(min_raise, target - my_round_bet)
    amount = min(amount, my_chips - 1)
    if amount < min_raise or amount >= my_chips:
        return None
    try:
        sys.stderr.write(
            'DELAYED_BLUFF street=%d flop_cr=%.2f curr_cr=%.2f ratio=%.2f\n'
            % (round_idx, flop_call_rate, curr_call_rate, ratio)
        )
    except Exception:
        pass
    return amount


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
    # v159 NEW: Gate strong band on opponent river-call-size tolerance.
    # Continuous license from river_call_size_ratio (tracks avg pot-fraction
    # of river bets opponent called). Removes unconditional 10pp cliff.
    # Unlicensed opponents (folding/unknown) get gentle slope (0.45->0.60x).
    # Confirmed calling stations get strong slope (0.45->0.85x).
    _river_call_size = opponent_model.get('river_call_size_ratio', 0.50)
    _call_size_license = clamp((_river_call_size - 0.45) / 0.25, 0.0, 1.0)
    _conf_license = clamp((confidence - 0.10) / 0.10, 0.0, 1.0)
    _license = _call_size_license * _conf_license

    t = clamp((made_strength - 0.45) / 0.37, 0.0, 1.0)
    ratio = 0.45 + (0.15 + _license * 0.25) * t
    fold_river = opponent_model.get('fold_to_bet_river', 0.44)
    if confidence >= 0.20 and fold_river <= 0.30:
        ratio += 0.08 * confidence
    if confidence >= 0.20 and fold_river >= 0.55:
        ratio -= 0.05 * confidence
    ratio = max(0.45, min(ratio, 0.85))
    try:
        sys.stderr.write(
            'RIVER_VALUE_TIER ratio_milli=%+d license_milli=%+d call_size=%.2f '
            'made=%.2f tier=%s conf=%.2f\n'
            % (round(ratio * 1000), round(_license * 1000), _river_call_size,
               made_strength, tier, confidence)
        )
    except Exception:
        pass
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


def _post_missed_cbet_exploit(
    round_idx, spot_info, opponent_model, made_strength, draw_strength,
    value_profile, pot, my_chips, min_raise, my_round_bet,
):
    """v155 NEW: Barrel-abandonment exploit. Returns additive sizing delta in [0.0, 0.10].

    When the opponent was the prior-street aggressor but checked back (giving
    up range advantage), their range is capped. Our betting range can widen
    and size up for value. Returns a CONTINUOUS additive delta [0.0, 0.10]
    for raise sizing, or 0.0 if not eligible.

    Signal: barrel_abandon_turn (1 - flop-to-turn barrel freq) and
    barrel_abandon_river (1 - turn-to-river barrel freq). Both default to
    typical values (0.55 / 0.65) when no hands observed yet.

    Dispatch: wire in strategy.py to_call==0 bet paths on turn/river.
    Telemetry: prints BARREL_ABANDON to stderr for daemon verification (NO
    `if delta != 0` gate per M5 INERTNESS HARD RULE).
    """
    if round_idx not in (2, 3):
        return 0.0

    confidence = opponent_model.get('confidence', 0.0)
    if confidence < 0.15:
        return 0.0

    if round_idx == 2:
        abandon_rate = opponent_model.get('barrel_abandon_turn', 0.55)
    else:
        abandon_rate = opponent_model.get('barrel_abandon_river', 0.65)

    # Sub-prior opponents who reliably continue barreling are not exploitable.
    if abandon_rate < 0.45:
        return 0.0

    # Eligible hands: thin+ value or draw with equity (air hands skipped).
    tier = value_profile.get('tier', 'none') if value_profile else 'none'
    has_value = tier in ('thin', 'strong', 'nut') or made_strength >= 0.45
    has_draw = draw_strength >= 0.10
    if not has_value and not has_draw:
        return 0.0

    # CONTINUOUS delta formula (no deadzone, M5 compliant):
    #   abandon_rate=0.45 -> 0.0
    #   abandon_rate=0.60 -> +0.04
    #   abandon_rate=0.80 -> +0.0933
    #   abandoned*confidence scaling for sample-size reliability.
    raw_delta = 0.04 * (abandon_rate - 0.45) / 0.15
    delta = clamp(raw_delta * confidence, 0.0, 0.10)

    # M5 HARD RULE: telemetry MUST print unconditionally (NO `!= 0` gate).
    try:
        sys.stderr.write(
            "BARREL_ABANDON r=%d abandon=%.2f conf=%.2f delta=%+d "
            "tier=%s made=%.2f draw=%.2f\n"
            % (round_idx, abandon_rate, confidence, int(delta * 1000),
               tier, made_strength, draw_strength)
        )
    except Exception:
        pass

    return delta


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


def _river_bet_commit_guard(bet_amount, round_idx, my_chips, made_strength,
                            value_profile, board_texture, pair_profile,
                            anti_lock_pressure):
    """v143 NEW: Extends _river_value_ship_guard to to_call==0 river bets.
    The v140 guard was wired only in the to_call>0 raise path. This wrapper
    applies identical BET-SIDE logic to the bot's OWN river bets (open-jams /
    big bets after opponent check). Targets G3H25 (AJ one-pair open-jam
    to_call=0 -20000) and G2H44 (AQ ace-high open-jam to_call=0 -20000).
    Returns 0 (check) if blocked, else returns bet_amount unchanged."""
    if round_idx != 3 or anti_lock_pressure:
        return bet_amount
    if bet_amount is None or bet_amount <= 0:
        return bet_amount
    if bet_amount < my_chips * 0.25:
        return bet_amount
    if _river_value_ship_guard(round_idx, my_chips, made_strength, value_profile,
                               board_texture, pair_profile, bet_amount):
        return 0
    return bet_amount


def _spr_commitment_gate(round_idx, my_chips, made_strength, value_profile,
                         to_call, pot, win_rate, anti_lock_pressure,
                         line_label, draw_strength):
    """v147 NEW: SPR commitment gate — closed-form fold for marginal river hands
    facing stack-covering all-ins. Wired INSIDE the `to_call >= my_chips:` block
    BEFORE the early return, fixing the 7-gen placement shadow on _river_stackoff_guard.

    Returns True (force fold) iff: round_idx==3 AND not anti_lock AND tier in
    {thin, none} AND made_strength < 0.55 AND not(value_heavy line with live draw)
    AND win_rate < pot_odds + 0.03 where pot_odds = to_call/(pot+to_call).
    """
    # v148: extend to all postflop streets (flop=1, turn=2, river=3).
    # Preflop (round_idx==0) remains excluded — preflop all-in uses shove_odds math.
    if round_idx not in (1, 2, 3) or anti_lock_pressure:
        return False
    # Never fold strong/nut made hands.
    if value_profile is not None and value_profile.get('tier') in ('strong', 'nut'):
        return False
    # Only fold marginal one-pair / weak-two-pair / A-high hands.
    if made_strength >= 0.55:
        return False
    # v148: street-aware draw protection.
    # River: only value_heavy lines with live draws protected (no more cards).
    # Turn: any draw >= 0.15 (one card to come, implied odds).
    # Flop: any draw >= 0.12 (two cards to come, max implied odds).
    if round_idx == 3:
        if line_label == 'value_heavy' and draw_strength >= 0.18:
            return False
    elif round_idx == 2:
        if draw_strength >= 0.15:
            return False
    else:  # round_idx == 1 (flop)
        if draw_strength >= 0.12:
            return False
    # Guard against degenerate inputs.
    if to_call <= 0 or pot <= 0:
        return False
    # Closed-form pot-odds check. The +0.03 margin demands HIGH-confidence folds
    # (avoids borderline monte_carlo-noise folds). pot_odds = to_call/(pot+to_call)
    # matching how pot already includes opponent's bet (state.py L316).
    pot_odds = to_call / (pot + to_call)
    if win_rate >= pot_odds + 0.03:
        return False
    return True


def _river_weak_made_hand_gate(round_idx, pot, my_chips, made_strength,
                                value_profile, draw_strength, anti_lock_pressure):
    """v154 NEW: River weak-made-hand all-in gate. Complements _spr_commitment_gate
    by catching the 0.55-0.60 made_strength band (SPR gate handles < 0.55).

    Returns True (force fold) iff: river + facing all-in + large pot (>= 65% of
    stack) + weak made hand (made_strength < 0.60, tier not strong/nut) + no live
    draw (draw_strength < 0.18) + not anti_lock pressure.

    Targets -20k river stack-offs with hands like TT one-pair, bottom-two-pair
    that fall in the gap between the SPR gate (< 0.55) and the jam_buffer gate
    (requires win_rate >= jam_odds + 0.14 ≈ 0.64).
    """
    if round_idx != 3:
        return False
    if anti_lock_pressure:
        return False
    if made_strength >= 0.60:
        return False
    if value_profile is not None and value_profile.get('tier') in ('strong', 'nut'):
        return False
    if draw_strength >= 0.18:
        return False
    if pot < my_chips * 0.65:
        return False
    return True


def _vulnerable_made_protection_floor(
    round_idx, to_call, made_strength, value_profile, board_texture,
    pair_profile, opponent_model, min_raise, my_round_bet, my_chips, pot,
):
    """v142 NEW: Made-hand protection sizing floor on flop/turn.

    Targets the 0.4x-pot underbet leak (match-analysis avg_raise flop/turn).
    Lifts thin/strong-tier value bet floor to 0.55-0.70x pot when ALL hold:
      - round_idx in (1, 2) (flop or turn)
      - value_profile tier in ('thin', 'strong') (NOT nut — nuts have own sizing)
      - pair_profile: vulnerable made (top-pair-GOOD-kicker / overpair /
        two-pair / weak-trips) with strength in 0.45-0.75 band
      - board has draw pressure (flush_pressure >= 0.40 OR straight_pressure
        >= 0.40 OR wetness >= 0.35)
      - opponent is STICKY (vm_idx >= 0.50 OR passivity_score >= 0.55) AND
        folds <= 50% to raises
      - NOT confirmed overbettor (preserve v137 defense relaxation)

    PARAMETER_TUNING EXEMPT (experience pool): NEW gating on
    value_maximizer_index + passivity_score (existing signals, NEW combo).
    Does NOT touch choose_raise() body.

    Returns raise-amount (int, same format as choose_raise output) or None.
    """
    if round_idx not in (1, 2):
        return None
    if value_profile is None or value_profile.get('tier') not in ('thin', 'strong'):
        return None
    if pair_profile is None or board_texture is None:
        return None
    if made_strength < 0.45 or made_strength >= 0.78:
        return None

    # 1. Vulnerable made-hand eligibility (charge draws with these hands)
    made_class = pair_profile.get('made_class', -1)
    pair_type = pair_profile.get('pair_type', 'none')
    weak_kicker = pair_profile.get('weak_kicker', True)
    eligible = (
        (made_class == 1 and pair_type == 'top_pair' and not weak_kicker)
        or (made_class == 1 and pair_type == 'overpair')
        or (made_class == 2)
        or (made_class == 3 and made_strength < 0.70)  # weak trips, charge draws
    )
    if not eligible:
        return None

    # 2. Board draw pressure (only protect vs plausible draws)
    flush_p = board_texture.get('flush_pressure', 0.0)
    straight_p = board_texture.get('straight_pressure', 0.0)
    wetness = board_texture.get('wetness', 0.0)
    draw_pressure = max(flush_p, straight_p)
    if draw_pressure < 0.40 and wetness < 0.35:
        return None

    # 3. NEW opponent-signal gating (PARAMETER_TUNING exempt clause)
    confidence = opponent_model.get('confidence', 0.0)
    if confidence < 0.20:
        return None
    vm_idx = opponent_model.get('value_maximizer_index', 0.5)
    passivity = opponent_model.get('passivity_score', 0.5)
    fold_to_raise = opponent_model.get('fold_to_raise', 0.44)
    if vm_idx < 0.50 and passivity < 0.55:
        return None  # opponent not sticky; smaller bets extract via folds
    if fold_to_raise >= 0.50:
        return None  # opponent folds enough to raises; no need to charge draws
    sizing = opponent_model.get('sizing_tendency')
    if (sizing is not None and sizing.get('samples', 0) >= 8
            and sizing.get('confidence', 0.0) >= 0.30
            and sizing.get('tendency') == 'overbettor'):
        return None  # preserve v137 polarized defense relaxation

    # 4. Compute protection floor (0.55-0.70x pot)
    base = 0.55
    if draw_pressure >= 0.75:
        base += 0.07
    elif draw_pressure >= 0.55:
        base += 0.04
    if wetness >= 0.50:
        base += 0.03
    if round_idx == 2:
        base += 0.03  # turn: one card to come, charge more
    if made_strength >= 0.65:
        base += 0.02
    base = min(base, 0.70)

    target = int(pot * base)
    amount = max(min_raise, target - my_round_bet)
    amount = min(amount, my_chips - 1)
    if amount < min_raise or amount >= my_chips:
        return None

    try:
        sys.stderr.write(
            "PROTECT_FLOOR r=%d made=%.2f tier=%s pair=%s draw=%.2f wet=%.2f "
            "vm=%.2f pass=%.2f fold=%.2f base=%.2f amount=%d\n"
            % (round_idx, made_strength, value_profile.get('tier'),
               pair_type, draw_pressure, wetness, vm_idx, passivity,
               fold_to_raise, base, amount)
        )
    except Exception:
        pass
    return amount


def _preflop_steal_defense_widen(hand_cat, opponent_model, pot_odds,
                                 preflop_strength, win_rate, facing_3bet=False):
    """v152 NEW: Widen preflop defense vs steal-like opens and frequent 3-bets.

    Targets the 99-100% fold-to-bet leak (152/153 vs v137, 108/108 vs v136).
    Returns 'call'/'fold'/None (None = use existing logic). NOT a sizing-delta
    (forbidden v144-v146 axis); pure CALL/FOLD range decision using existing
    opponent signals (vpip/pfr/threebet_vs_open/confidence).
    """
    confidence = opponent_model.get('confidence', 0.0)
    vpip = opponent_model.get('vpip', 0.58)
    pfr = opponent_model.get('pfr', 0.28)
    threebet = opponent_model.get('threebet_vs_open', 0.16)

    if not facing_3bet:
        # v156 FIX: removed `or vpip >= 0.55` — that OR-gate caught passive
        # limpers (high vpip, LOW pfr) whose raises are premium-heavy, so
        # widening our call range vs them was -EV. Only ACTUAL steal-openers
        # (pfr >= 0.30) and unknown opponents (confidence < 0.20) trigger widen.
        unknown_or_loose = (confidence < 0.20) or (pfr >= 0.30)
        if not unknown_or_loose:
            return None  # tight confirmed opener -> defer to existing logic
        if hand_cat == 'playable':  # KJo, QJo, KTo
            if pot_odds <= 0.32 or win_rate >= pot_odds - 0.02:
                return 'call'
            return None
        if hand_cat in ('mid_pair', 'strong_pair'):
            if 0.32 < pot_odds <= 0.45 and win_rate >= pot_odds - 0.04:
                return 'call'
            return None
        if hand_cat in ('suited_connector', 'suited_ace'):
            if pot_odds <= 0.36 or win_rate >= pot_odds - 0.02:
                return 'call'
            return None
        return None

    # SB vs 3-bet: implied-odds defense vs frequent 3-bettors.
    if confidence < 0.20 or threebet < 0.10:
        return None
    if hand_cat == 'small_pair':
        return 'call' if pot_odds <= 0.36 else None
    if hand_cat in ('suited_connector', 'suited_ace', 'broadway_suited'):
        return 'call' if pot_odds <= 0.35 else None
    if hand_cat == 'mid_pair':
        return 'call' if pot_odds <= 0.35 else None
    return None


def sb_open_opp_sizing_delta(opponent_model, preflop_strength):
    """v144 NEW: Opponent-adaptive SB-open sizing adjustment (BB units).

    Returns an additive delta on the 2.5BB base open size, in [-0.50, +0.75].
      * vs fold-prone opps (fold_to_open_preflop >= 0.50): size UP to exploit
        fold equity (more uncontested pots).
      * vs sticky/passive callers (fold_open <= 0.32, threebet <= 0.20): size
        DOWN with value to keep pots small out of position from BB.
      * vs 3bet-heavy opps (threebet >= 0.28): size DOWN to reduce pot
        inflation before the 3bet comes.
      * Premium hands (preflop_strength >= 0.72): damp adjustment by 0.4
        (we want standard sizings with the nuts).
    """
    open_conf = opponent_model.get('open_response_confidence', 0.0)
    if open_conf < 0.25:
        return 0.0  # insufficient reads — stay at base 2.5BB

    fold_open = opponent_model.get(
        'fold_to_open_preflop',
        opponent_model.get('fold_to_raise', 0.42),
    )
    threebet = opponent_model.get('threebet_vs_open', 0.16)

    delta = 0.0
    # ATTACK fold equity vs folders
    if fold_open >= 0.50:
        delta += 0.50 * clamp((fold_open - 0.42) / 0.20, 0.0, 1.0)
    # DEFEND vs sticky-passive callers — smaller value opens
    elif fold_open <= 0.32 and threebet <= 0.20:
        delta -= 0.30
    # vs 3bet-heavy opps — shrink to reduce pre-3bet inflation
    if threebet >= 0.28:
        delta -= 0.20

    # Damp adjustment for premiums (standard sizing with the nuts)
    if preflop_strength is not None and preflop_strength >= 0.72:
        delta *= 0.4

    return clamp(delta, -0.50, 0.75)


def bb_vs_limp_opp_sizing_delta(opponent_model, preflop_strength):
    """v145 NEW: Opponent-adaptive BB iso-raise sizing vs limpers (BB units).

    Mirrors sb_open_opp_sizing_delta architecture but uses BB-defense signals.
    Returns additive delta on the 3.2BB base iso size, in [-0.70, +0.80].
      * vs frequent limp-folders (fold_to_raise >= 0.50): size UP to exploit
        fold equity when iso-raising the limper.
      * vs limp-callers (fold_to_raise <= 0.36 AND vpip >= 0.55): size DOWN
        to keep pots small with marginal value hands OOP.
      * vs limp-heavy passive opps (limp_rate = vpip-pfr >= 0.32): wide weak
        range, size UP with value for extraction.
      * vs aggressive limpers (pfr >= 0.30 — suggests limp-reraise risk):
        size DOWN to reduce pre-3bet pot inflation.
      * Premium hands (preflop_strength >= 0.72): damp adjustment by 0.4.
    """
    confidence = opponent_model.get('confidence', 0.0)
    if confidence < 0.25:
        return 0.0  # insufficient reads — stay at base 3.2BB

    fold_to_raise = opponent_model.get('fold_to_raise', 0.44)
    vpip = opponent_model.get('vpip', 0.58)
    pfr = opponent_model.get('pfr', 0.28)
    limp_rate = max(0.0, vpip - pfr)

    delta = 0.0
    # ATTACK fold equity vs folders (limp-folders)
    if fold_to_raise >= 0.50:
        delta += 0.50 * clamp((fold_to_raise - 0.42) / 0.20, 0.0, 1.0)
    # DEFEND vs limp-callers — keep pots small with iso-raises
    elif fold_to_raise <= 0.36 and vpip >= 0.55:
        delta -= 0.30
    # ATTACK limp-heavy passive opps — wide weak range
    if limp_rate >= 0.32:
        delta += 0.30 * clamp((limp_rate - 0.28) / 0.18, 0.0, 1.0)
    # DEFEND vs aggressive limpers (limp-reraise risk) — shrink pre-3bet pot
    if pfr >= 0.30:
        delta -= 0.20

    # Damp adjustment for premiums (standard sizing with the nuts)
    if preflop_strength is not None and preflop_strength >= 0.72:
        delta *= 0.4

    return clamp(delta, -0.70, 0.80)


def bb_vs_raise_opp_sizing_delta(opponent_model, preflop_strength):
    """v146: Opponent-adaptive BB 3bet sizing vs SB opens (BB Units).

    Third preflop offense delta (v144 sb_open, v145 bb_vs_limp, v146 bb_vs_raise).
    Returns additive delta on ~3.5BB 3bet base, in [-0.60, +0.90].
    """
    open_conf = opponent_model.get('open_response_confidence', 0.0)
    if open_conf < 0.25:
        return 0.0

    fold_to_raise = opponent_model.get('fold_to_raise', 0.44)
    vpip = opponent_model.get('vpip', 0.58)
    threebet = opponent_model.get('threebet_vs_open', 0.16)

    delta = 0.0
    if fold_to_raise >= 0.50:
        delta += 0.55 * clamp((fold_to_raise - 0.42) / 0.20, 0.0, 1.0)
    # DEFEND vs sticky callers who defend too wide — shrink 3bet pot
    elif fold_to_raise <= 0.34 and vpip >= 0.55:
        delta -= 0.35
    # DEFEND vs heavy 3bettors — they'll 4bet lighter, shrink pre-pot
    if threebet >= 0.22:
        delta -= 0.25

    # Damp adjustment for premiums (standard sizing with the nuts)
    if preflop_strength is not None and preflop_strength >= 0.72:
        delta *= 0.4

    return clamp(delta, -0.60, 0.90)


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
    # v143 update: return _rva is now wrapped by _river_bet_commit_guard; the
    # call site + _guarded return path together prove LIVE reachability.
    guarded_idx = src.find('_guarded = _river_bet_commit_guard(_rva', idx)
    assert guarded_idx > 0, "Dispatch-reachability FAIL: _rva guard not found"
    print("v141 _river_value_extraction_amplifier dispatch-reachability: PASS (v143-guarded)")

    # v142 _vulnerable_made_protection_floor fixture: 8 cases
    bt_safe = {"flush_pressure": 0.10, "straight_pressure": 0.10,
               "dynamic": False, "paired": False, "wetness": 0.05}
    bt_wet = {"flush_pressure": 0.75, "straight_pressure": 0.40,
              "dynamic": True, "paired": False, "wetness": 0.55}
    bt_turn_flush = {"flush_pressure": 0.80, "straight_pressure": 0.30,
                     "dynamic": True, "paired": False, "wetness": 0.50}
    pair_tpgk = {"made_class": 1, "pair_type": "top_pair", "weak_kicker": False}
    pair_overpair = {"made_class": 1, "pair_type": "overpair", "weak_kicker": False}
    pair_twopair = {"made_class": 2, "pair_type": "two_pair", "weak_kicker": False}
    pair_weak_trips = {"made_class": 3, "pair_type": "trips", "weak_kicker": True}
    pair_top_weak = {"made_class": 1, "pair_type": "top_pair", "weak_kicker": True}
    opp_sticky = {"confidence": 0.35, "value_maximizer_index": 0.65,
                  "passivity_score": 0.60, "fold_to_raise": 0.42}
    opp_foldy = {"confidence": 0.35, "value_maximizer_index": 0.45,
                 "passivity_score": 0.45, "fold_to_raise": 0.55}
    opp_over = {"confidence": 0.35, "value_maximizer_index": 0.65,
                "passivity_score": 0.60, "fold_to_raise": 0.42,
                "sizing_tendency": {"samples": 12, "confidence": 0.60,
                                    "tendency": "overbettor"}}

    # F1: thin top-pair-good-kicker, wet flop, sticky opp -> floor (550-700)
    a = _vulnerable_made_protection_floor(
        1, 0, 0.55, {"tier": "thin"}, bt_wet, pair_tpgk, opp_sticky,
        100, 0, 5000, 1000)
    assert a is not None and 550 <= a <= 700, f"F1 expected 550-700, got {a}"

    # F2: dry board -> None
    a = _vulnerable_made_protection_floor(
        1, 0, 0.55, {"tier": "thin"}, bt_safe, pair_tpgk, opp_sticky,
        100, 0, 5000, 1000)
    assert a is None, f"F2 (dry board) expected None, got {a}"

    # F3: turn overpair with flush draw -> floor (580-720)
    a = _vulnerable_made_protection_floor(
        2, 0, 0.60, {"tier": "strong"}, bt_turn_flush, pair_overpair, opp_sticky,
        100, 0, 5000, 1000)
    assert a is not None and 580 <= a <= 720, f"F3 expected 580-720, got {a}"

    # F4: two-pair dynamic flop -> floor (550-700)
    a = _vulnerable_made_protection_floor(
        1, 0, 0.58, {"tier": "strong"}, bt_wet, pair_twopair, opp_sticky,
        100, 0, 5000, 1000)
    assert a is not None and 550 <= a <= 700, f"F4 expected 550-700, got {a}"

    # F5: top_pair_weak_kicker -> None (ineligible)
    a = _vulnerable_made_protection_floor(
        1, 0, 0.45, {"tier": "thin"}, bt_wet, pair_top_weak, opp_sticky,
        100, 0, 5000, 1000)
    assert a is None, f"F5 (weak kicker) expected None, got {a}"

    # F6: nut tier -> None (nuts have own path)
    a = _vulnerable_made_protection_floor(
        1, 0, 0.85, {"tier": "nut"}, bt_wet, pair_twopair, opp_sticky,
        100, 0, 5000, 1000)
    assert a is None, f"F6 (nut tier) expected None, got {a}"

    # F7: non-sticky opp -> None
    a = _vulnerable_made_protection_floor(
        1, 0, 0.55, {"tier": "thin"}, bt_wet, pair_tpgk, opp_foldy,
        100, 0, 5000, 1000)
    assert a is None, f"F7 (foldy opp) expected None, got {a}"

    # F8: overbettor opp -> None (preserve v137 defense)
    a = _vulnerable_made_protection_floor(
        1, 0, 0.55, {"tier": "thin"}, bt_wet, pair_tpgk, opp_over,
        100, 0, 5000, 1000)
    assert a is None, f"F8 (overbettor) expected None, got {a}"

    print("v142 _vulnerable_made_protection_floor fixture: 8/8 PASS")

    # Dispatch-reachability assertion (project #1 INERTNESS mandate).
    import os
    strat_path = os.path.join(os.path.dirname(__file__), 'strategy.py')
    with open(strat_path) as f:
        src = f.read()
    n_refs = src.count('_vulnerable_made_protection_floor')
    assert n_refs >= 3, f"Dispatch-reachability FAIL: need >=3 refs, found {n_refs}"
    idx_a = src.find('_prot_floor = _vulnerable_made_protection_floor(')
    assert idx_a > 0, "Dispatch-reachability FAIL: SITE A wire not found"
    idx_b = src.find('_prot_floor = _vulnerable_made_protection_floor(', idx_a + 1)
    assert idx_b > 0, "Dispatch-reachability FAIL: SITE B wire not found"
    print("v142 _vulnerable_made_protection_floor dispatch-reachability: PASS (helper+import+2 wires)")

    # v143 _river_bet_commit_guard fixture: 5 cases
    bt_safe = {"flush_pressure": 0.10, "straight_pressure": 0.05, "paired": False}
    bt_flush = {"flush_pressure": 0.85, "straight_pressure": 0.10, "paired": False}
    # F1: thin one-pair, 75% stack bet -> BLOCK
    g = _river_bet_commit_guard(15000, 3, 20000, 0.50, {"tier": "thin"}, bt_safe, {"made_class": 1}, False)
    assert g == 0, f"F1 fail: {g}"
    # F2: nut hand, big bet -> ALLOW
    g = _river_bet_commit_guard(15000, 3, 20000, 0.88, {"tier": "nut"}, bt_safe, {"made_class": 4}, False)
    assert g == 15000, f"F2 fail: {g}"
    # F3: thin hand, small bet (5% stack) -> ALLOW
    g = _river_bet_commit_guard(1000, 3, 20000, 0.50, {"tier": "thin"}, bt_safe, {"made_class": 1}, False)
    assert g == 1000, f"F3 fail: {g}"
    # F4: strong hand on flush board, 40% stack -> BLOCK (unsafe board)
    g = _river_bet_commit_guard(8000, 3, 20000, 0.75, {"tier": "strong"}, bt_flush, {"made_class": 2}, False)
    assert g == 0, f"F4 fail: {g}"
    # F5: anti_lock_pressure=True -> ALLOW (endgame bypass)
    g = _river_bet_commit_guard(15000, 3, 20000, 0.50, {"tier": "thin"}, bt_safe, {"made_class": 1}, True)
    assert g == 15000, f"F5 fail: {g}"
    print("v143 _river_bet_commit_guard fixture: 5/5 PASS")
    # Dispatch-reachability grep assertion
    with open('strategy.py') as f:
        src = f.read()
    n_calls = src.count('_river_bet_commit_guard(')
    assert n_calls >= 4, f"Expected >=4 call sites in strategy.py, got {n_calls}"
    print(f"v143 dispatch-reachability: PASS ({n_calls} sites)")

    # v144 sb_open_opp_sizing_delta fixture: 6 cases
    opp_blank = {"open_response_confidence": 0.10}  # sub-confidence
    opp_foldy = {"open_response_confidence": 0.50, "fold_to_open_preflop": 0.60,
                 "threebet_vs_open": 0.14}
    opp_sticky = {"open_response_confidence": 0.50, "fold_to_open_preflop": 0.25,
                  "threebet_vs_open": 0.12}
    opp_3bet = {"open_response_confidence": 0.50, "fold_to_open_preflop": 0.45,
                "threebet_vs_open": 0.32}
    opp_mixed = {"open_response_confidence": 0.50, "fold_to_open_preflop": 0.30,
                 "threebet_vs_open": 0.30}
    # F1: low confidence -> 0.0
    d = sb_open_opp_sizing_delta(opp_blank, 0.55)
    assert d == 0.0, f"F1 fail: {d}"
    # F2: fold-prone -> >= +0.30
    d = sb_open_opp_sizing_delta(opp_foldy, 0.55)
    assert d >= 0.30, f"F2 fail: {d}"
    # F3: sticky passive -> <= -0.20
    d = sb_open_opp_sizing_delta(opp_sticky, 0.55)
    assert d <= -0.20, f"F3 fail: {d}"
    # F4: 3bet-heavy -> <= -0.15
    d = sb_open_opp_sizing_delta(opp_3bet, 0.55)
    assert d <= -0.15, f"F4 fail: {d}"
    # F5: premium + fold-prone -> dampened but positive (0.0 to +0.30]
    d = sb_open_opp_sizing_delta(opp_foldy, 0.85)
    assert 0.0 < d <= 0.30, f"F5 fail: {d}"
    # F6: 3bet-heavy with neutral fold_open (neither foldy nor sticky-passive) -> <= -0.15
    # Brief noted "combined sticky + 3bet" but per the function's `elif`, sticky-passive
    # requires threebet <= 0.20 while 3bet-heavy requires threebet >= 0.28 — those
    # branches cannot both fire on the same opponent. This case proves the 3bet
    # branch fires even when fold_open is in the neutral band (not foldy, not sticky).
    d = sb_open_opp_sizing_delta(opp_mixed, 0.55)
    assert d <= -0.15, f"F6 fail: {d}"
    print("v144 sb_open_opp_sizing_delta fixture: 6/6 PASS")

    # Dispatch-reachability grep
    with open('strategy.py') as f:
        src = f.read()
    n_calls = src.count('sb_open_opp_sizing_delta')
    assert n_calls >= 2, f"Dispatch-reachability FAIL: need >=2 refs (import + call), found {n_calls}"
    print(f"v144 sb_open_opp_sizing_delta dispatch-reachability: PASS ({n_calls} refs)")

    # v145 bb_vs_limp_opp_sizing_delta fixture: 6 cases
    opp_blank = {"confidence": 0.10}  # sub-confidence -> 0
    opp_foldy = {"confidence": 0.50, "fold_to_raise": 0.60, "vpip": 0.55, "pfr": 0.22}
    # sticky: vpip=0.55, pfr=0.25 -> limp_rate=0.30 (below 0.32 threshold so the
    # limp-heavy UP branch does NOT cancel out the sticky DOWN adjustment)
    opp_sticky = {"confidence": 0.50, "fold_to_raise": 0.30, "vpip": 0.55, "pfr": 0.25}
    opp_limp_heavy = {"confidence": 0.50, "fold_to_raise": 0.45, "vpip": 0.65, "pfr": 0.20}
    opp_aggr = {"confidence": 0.50, "fold_to_raise": 0.45, "vpip": 0.50, "pfr": 0.34}
    # F1: blank (sub-confidence) -> 0.0
    d = bb_vs_limp_opp_sizing_delta(opp_blank, 0.55)
    assert d == 0.0, f"F1 fail: {d}"
    # F2: fold-prone (limp-folder) -> >= +0.30
    d = bb_vs_limp_opp_sizing_delta(opp_foldy, 0.55)
    assert d >= 0.30, f"F2 fail: {d}"
    # F3: sticky limp-caller -> <= -0.20
    d = bb_vs_limp_opp_sizing_delta(opp_sticky, 0.55)
    assert d <= -0.20, f"F3 fail: {d}"
    # F4: limp-heavy passive (limp_rate=0.45) -> >= +0.20
    d = bb_vs_limp_opp_sizing_delta(opp_limp_heavy, 0.55)
    assert d >= 0.20, f"F4 fail: {d}"
    # F5: aggressive limper (pfr=0.34) -> <= -0.15
    d = bb_vs_limp_opp_sizing_delta(opp_aggr, 0.55)
    assert d <= -0.15, f"F5 fail: {d}"
    # F6: premium + fold-prone -> dampened but positive (0.0, +0.30]
    d = bb_vs_limp_opp_sizing_delta(opp_foldy, 0.85)
    assert 0.0 < d <= 0.30, f"F6 fail: {d}"
    print("v145 bb_vs_limp_opp_sizing_delta fixture: 6/6 PASS")

    # Dispatch-reachability grep
    with open('strategy.py') as f:
        src = f.read()
    n_calls = src.count('bb_vs_limp_opp_sizing_delta')
    assert n_calls >= 2, f"Dispatch-reachability FAIL: need >=2 refs (import + call), found {n_calls}"
    print(f"v145 bb_vs_limp_opp_sizing_delta dispatch-reachability: PASS ({n_calls} refs)")

    # v146 bb_vs_raise_opp_sizing_delta fixture: 6 cases
    opp_46_blank = {'open_response_confidence': 0.10}  # sub-confidence -> 0
    opp_46_foldy = {'open_response_confidence': 0.50, 'fold_to_raise': 0.62, 'vpip': 0.55, 'threebet_vs_open': 0.15}
    opp_46_sticky = {'open_response_confidence': 0.50, 'fold_to_raise': 0.28, 'vpip': 0.58, 'threebet_vs_open': 0.15}
    opp_46_4bet = {'open_response_confidence': 0.50, 'fold_to_raise': 0.44, 'vpip': 0.50, 'threebet_vs_open': 0.28}
    opp_46_mixed = {'open_response_confidence': 0.50, 'fold_to_raise': 0.48, 'vpip': 0.50, 'threebet_vs_open': 0.15}
    # F1: sub-confidence -> 0
    d = bb_vs_raise_opp_sizing_delta(opp_46_blank, 0.55)
    assert d == 0.0, f'F1 fail: {d}'
    # F2: fold-prone (ftb=0.62) -> >= +0.30
    d = bb_vs_raise_opp_sizing_delta(opp_46_foldy, 0.55)
    assert d >= 0.30, f'F2 fail: {d}'
    # F3: sticky (ftb=0.28, vpip=0.58) -> <= -0.20
    d = bb_vs_raise_opp_sizing_delta(opp_46_sticky, 0.55)
    assert d <= -0.20, f'F3 fail: {d}'
    # F4: 3bet-heavy (3bet=0.28) -> <= -0.15
    d = bb_vs_raise_opp_sizing_delta(opp_46_4bet, 0.55)
    assert d <= -0.15, f'F4 fail: {d}'
    # F5: mixed (ftb=0.48, 3bet=0.15) -> 0.0 (below fold-prone, below sticky)
    d = bb_vs_raise_opp_sizing_delta(opp_46_mixed, 0.55)
    assert d == 0.0, f'F5 fail: {d}'
    # F6: premium + fold-prone -> dampened but positive (0.0, +0.30]
    d = bb_vs_raise_opp_sizing_delta(opp_46_foldy, 0.85)
    assert 0.0 < d <= 0.30, f'F6 fail: {d}'
    print('v146 bb_vs_raise_opp_sizing_delta fixture: 6/6 PASS')

    with open('strategy.py') as f:
        src = f.read()
    # bb_vs_raise_opp_sizing_delta appears at import + 1 call site (assigns var);
    # the variable _bb_raise_opp_size_bb proves 3-site dispatch (assignment +
    # 3 choose_raise kwarg passes: value/bluff/thin_value).
    n_fn_refs = src.count('bb_vs_raise_opp_sizing_delta')
    n_var_refs = src.count('_bb_raise_opp_size_bb')
    assert n_fn_refs >= 2, f'Dispatch-reachability FAIL: need >=2 fn refs (import + call), found {n_fn_refs}'
    assert n_var_refs >= 4, f'Dispatch-reachability FAIL: need >=4 var refs (assign + 3 kwarg), found {n_var_refs}'
    print(f'v146 bb_vs_raise_opp_sizing_delta dispatch-reachability: PASS ({n_fn_refs} fn refs, {n_var_refs} var refs)')

    # v148 _spr_commitment_gate fixture: 9 cases (extended to flop/turn)
    vp_thin = {"tier": "thin"}
    vp_strong = {"tier": "strong"}
    # F1: TURN (round_idx=2), tier=thin, made=0.42, to_call=5000, pot=5000, win=0.30
    #   pot_odds=0.5, 0.30 < 0.53, draw=0.05 < 0.15 -> True (NEW: was False in v147)
    g = _spr_commitment_gate(2, 5000, 0.42, vp_thin, 5000, 5000, 0.30, False, 'balanced', 0.05)
    assert g is True, f"F1 fail: {g}"
    # F2: river, tier=strong, made=0.78 -> False (never fold strong/nut)
    g = _spr_commitment_gate(3, 5000, 0.78, vp_strong, 5000, 5000, 0.30, False, 'balanced', 0.05)
    assert g is False, f"F2 fail: {g}"
    # F3: river, tier=thin, made=0.42, win=0.30 -> True (pot_odds=0.5, clear fold)
    g = _spr_commitment_gate(3, 5000, 0.42, vp_thin, 5000, 5000, 0.30, False, 'balanced', 0.05)
    assert g is True, f"F3 fail: {g}"
    # F4: river, tier=thin, made=0.50, win=0.55 -> False (equity sufficient)
    g = _spr_commitment_gate(3, 5000, 0.50, vp_thin, 5000, 5000, 0.55, False, 'balanced', 0.05)
    assert g is False, f"F4 fail: {g}"
    # F5: river, anti_lock=True -> False
    g = _spr_commitment_gate(3, 5000, 0.42, vp_thin, 5000, 5000, 0.30, True, 'balanced', 0.05)
    assert g is False, f"F5 fail: {g}"
    # F6: river overbet, tier=none, made=0.35, win=0.20 -> True
    g = _spr_commitment_gate(3, 10000, 0.35, None, 10000, 2000, 0.20, False, 'balanced', 0.05)
    assert g is True, f"F6 fail: {g}"
    # F7: FLOP (round_idx=1), tier=thin, made=0.42, win=0.30, draw=0.05 -> True
    #   pot_odds=0.5, 0.30 < 0.53, draw 0.05 < 0.12 -> fold
    g = _spr_commitment_gate(1, 5000, 0.42, vp_thin, 5000, 5000, 0.30, False, 'balanced', 0.05)
    assert g is True, f"F7 fail: {g}"
    # F8: FLOP, tier=thin, made=0.42, win=0.30, draw=0.15 -> False (draw protection)
    g = _spr_commitment_gate(1, 5000, 0.42, vp_thin, 5000, 5000, 0.30, False, 'balanced', 0.15)
    assert g is False, f"F8 fail: {g}"
    # F9: TURN, tier=thin, made=0.42, win=0.30, draw=0.16 -> False (draw protection)
    g = _spr_commitment_gate(2, 5000, 0.42, vp_thin, 5000, 5000, 0.30, False, 'balanced', 0.16)
    assert g is False, f"F9 fail: {g}"
    print("v148 _spr_commitment_gate fixture: 9/9 PASS")

    # Dispatch-reachability: gate symbol must appear in strategy.py (import + call site)
    with open('strategy.py') as f:
        src = f.read()
    n_refs = src.count('_spr_commitment_gate')
    assert n_refs >= 2, f"Dispatch-reachability FAIL: need >=2 refs (import + call), found {n_refs}"
    # Placement invariant: the call must appear BEFORE the `return -2 if win_rate >= shove_odds`
    # line within the to_call >= my_chips block. Verify by checking the call index < return index
    # within the function body.
    call_idx = src.find('_spr_commitment_gate(round_idx')
    return_idx = src.find('return -2 if win_rate >= shove_odds + shove_buffer')
    assert call_idx != -1 and return_idx != -1 and call_idx < return_idx, \
        f"PLACEMENT INVARIANT FAIL: call@{call_idx} must precede return@{return_idx}"
    print(f"v148 _spr_commitment_gate dispatch-reachability + placement: PASS ({n_refs} refs, call before return)")

    # v149 _street_fold_exploit_sizing_boost self-test
    # NOTE: expected values computed from the v150 RELAXED formula
    # raw=clamp(0.08+(fold-0.50)*0.30, 0.08, 0.25) then * confidence,
    # with gates STREET_FOLD_BOOST_MIN_CONF=0.15 and
    # STREET_FOLD_BOOST_MIN_FOLD=0.50 (relaxed from inline 0.20/0.55 in v149).
    def _t_fold_boost(model, r, expected, label):
        got = _street_fold_exploit_sizing_boost(model, r)
        assert abs(got - expected) < 0.01, (label, got, expected)
        print(f"  OK {label}: {got:.3f}")
    _t_fold_boost({'confidence': 0.0, 'fold_to_bet_flop': 0.99}, 1, 0.0, 'zero-conf no-fire')
    _t_fold_boost({'confidence': 0.14, 'fold_to_bet_flop': 0.99}, 1, 0.0, 'below-min-conf no-fire')
    _t_fold_boost({'confidence': 0.5, 'fold_to_bet_flop': 0.44}, 1, 0.0, 'low-fold no-fire')
    _t_fold_boost({'confidence': 0.5, 'fold_to_bet_flop': 0.49}, 1, 0.0, 'below-min-fold no-fire')
    _t_fold_boost({'confidence': 0.5, 'fold_to_bet_flop': 0.50}, 1, 0.04, 'threshold fold=0.50 conf=0.5')
    _t_fold_boost({'confidence': 1.0, 'fold_to_bet_flop': 0.99}, 1, 0.227, 'ultra-fold flop near-max')
    _t_fold_boost({'confidence': 1.0, 'fold_to_bet_turn': 0.80}, 2, 0.17, 'turn 0.80')
    _t_fold_boost({'confidence': 1.0, 'fold_to_bet_river': 0.70}, 3, 0.14, 'river 0.70')
    _t_fold_boost({'confidence': 0.15, 'fold_to_bet_flop': 0.99}, 0, 0.0, 'preflop no-fire')
    print("_street_fold_exploit_sizing_boost: ALL PASS")

    # v150 dispatch-reachability: function symbol must appear in strategy.py
    # (1 import + >=3 dispatch sites after Worker 1 donk/probe wiring).
    # v149 required >=3 (import + 2 call assigns); v150 raises to >=4 because
    # Worker 1 added donk + probe dispatch call sites.
    with open('strategy.py') as f:
        src = f.read()
    n_fn_refs = src.count('_street_fold_exploit_sizing_boost')
    assert n_fn_refs >= 4, f'Dispatch-reachability FAIL: need >=4 fn refs (import + 3+ dispatch after donk/probe wiring), found {n_fn_refs}'
    n_kwarg_refs = src.count('street_fold_boost=street_fold_boost')
    assert n_kwarg_refs >= 2, f'Dispatch-reachability FAIL: need >=2 kwarg passes, found {n_kwarg_refs}'
    print(f'v150 _street_fold_exploit_sizing_boost dispatch-reachability: PASS ({n_fn_refs} fn refs, {n_kwarg_refs} kwarg passes)')

    # v152 _preflop_steal_defense_widen fixture cases
    _w_opp_unknown = {'confidence': 0.10, 'vpip': 0.58, 'pfr': 0.28, 'fold_to_raise': 0.44}
    _w_opp_loose = {'confidence': 0.30, 'vpip': 0.65, 'pfr': 0.40,
                    'fold_to_raise': 0.44, 'threebet_vs_open': 0.18}
    _w_opp_tight = {'confidence': 0.30, 'vpip': 0.45, 'pfr': 0.18,
                    'fold_to_raise': 0.44, 'threebet_vs_open': 0.05}
    _w_opp_3bet = {'confidence': 0.30, 'vpip': 0.55, 'pfr': 0.32,
                   'fold_to_raise': 0.44, 'threebet_vs_open': 0.14}

    assert _preflop_steal_defense_widen('playable', _w_opp_unknown, 0.25, 0.55, 0.50) == 'call'
    assert _preflop_steal_defense_widen('playable', _w_opp_tight, 0.25, 0.55, 0.50) is None
    assert _preflop_steal_defense_widen('mid_pair', _w_opp_loose, 0.36, 0.62, 0.60) == 'call'
    assert _preflop_steal_defense_widen('small_pair', _w_opp_3bet, 0.28, 0.50, 0.45, facing_3bet=True) == 'call'
    assert _preflop_steal_defense_widen('small_pair', _w_opp_tight, 0.28, 0.50, 0.45, facing_3bet=True) is None
    # NEW (v156): passive limper (high vpip, LOW pfr) should NOT trigger widen.
    # Such opponents have premium-heavy raising ranges; widening is -EV.
    _w_opp_passive = {'confidence': 0.30, 'vpip': 0.60, 'pfr': 0.15,
                      'fold_to_raise': 0.44, 'threebet_vs_open': 0.08}
    assert _preflop_steal_defense_widen('mid_pair', _w_opp_passive, 0.36, 0.62, 0.60) is None, \
        'FIX: passive limper (vpip=0.60, pfr=0.15) must NOT trigger widen - their raises are premium-heavy'
    print("v156 PREFLOP_DEFEND_WIDEN misfire fix: 6/6 fixture cases PASS")

    # v156 M6 fixture: LIVE POOL DEFAULTS must produce non-zero margin on the
    # STANDARD arm (the 96.9% contribution that the previous nested
    # SIZING_MARGIN_ADJ telemetry made invisible). This guards against the
    # project's recurring INERTNESS failure mode — confirms the function
    # actually emits non-zero margin with default opponent-model inputs.
    _pcm_spot = {'last_raise_pot_ratio': 0.5, 'facing_postflop_aggression': True,
                 'opp_postflop_bet_count': 1}
    _pcm_opp = {'confidence': 0.5, 'postflop_aggr': 0.40, 'sizing_tendency': None}
    _pcm_result = postflop_call_margin(_pcm_spot, _pcm_opp, 0.15, 0.05, 2, True)
    assert _pcm_result > 0, f'STANDARD arm must return non-zero margin: {_pcm_result}'
    assert round(_pcm_result * 1000) > 0, f'margin_milli must be positive: {round(_pcm_result*1000)}'
    print(f'postflop_call_margin M6 fixture PASS: margin={_pcm_result:.4f} milli={round(_pcm_result*1000)}')

    # v154 _spr_commitment_gate ACTIVE-PATH self-test: 5 scenarios.
    # Verifies the gate works correctly when called from the opponent_allin
    # block (active path) rather than the dead to_call>=my_chips block.
    # Scenario 1: marginal river all-in, thin tier, low win -> FOLD
    assert _spr_commitment_gate(3, 19500, 0.48, {'tier': 'thin'}, 19500, 1000,
                                0.45, False, 'balanced', 0.0) is True, \
        "v154 S1: marginal river all-in should fold"
    # Scenario 2: strong hand river all-in -> NO FOLD
    assert _spr_commitment_gate(3, 19500, 0.72, {'tier': 'strong'}, 19500, 1000,
                                0.70, False, 'balanced', 0.0) is False, \
        "v154 S2: strong hand river all-in should not fold"
    # Scenario 3: marginal turn all-in with draw >= 0.15 -> NO FOLD (draw protection)
    assert _spr_commitment_gate(2, 19500, 0.45, {'tier': 'thin'}, 19500, 1000,
                                0.42, False, 'balanced', 0.16) is False, \
        "v154 S3: turn with draw >= 0.15 should not fold"
    # Scenario 4: marginal flop all-in, no draw -> FOLD
    assert _spr_commitment_gate(1, 19500, 0.40, {'tier': 'none'}, 19500, 500,
                                0.35, False, 'balanced', 0.0) is True, \
        "v154 S4: flop no-draw marginal should fold"
    # Scenario 5: anti-lock pressure active -> NO FOLD
    assert _spr_commitment_gate(3, 19500, 0.40, {'tier': 'none'}, 19500, 1000,
                                0.35, True, 'balanced', 0.0) is False, \
        "v154 S5: anti-lock pressure should override fold"
    print("v154 _spr_commitment_gate ACTIVE-PATH self-test: 5/5 PASS")

    # v154 dispatch-reachability: now expect 3+ refs (1 import + 2 call sites:
    # active opponent_allin path + dead to_call>=my_chips path preserved)
    with open('strategy.py') as f:
        src = f.read()
    n_refs = src.count('_spr_commitment_gate')
    assert n_refs >= 3, f"v154 Dispatch-reachability FAIL: need >=3 refs (import + 2 calls), found {n_refs}"
    # Verify ACTIVE path call exists with [ACTIVE_PATH] marker
    assert 'SPR_FOLD' in src and '[ACTIVE_PATH]' in src, \
        "v154 Dispatch-reachability FAIL: ACTIVE_PATH marker not found in strategy.py"
    print(f"v154 _spr_commitment_gate dispatch-reachability: PASS ({n_refs} refs, ACTIVE_PATH present)")

    # v154 _river_weak_made_hand_gate self-test: 7 scenarios.
    # Verifies the gate catches the 0.55-0.60 made_strength band on river.
    # Scenario 1: river, made=0.57, large pot, no draw -> FOLD (core case)
    assert _river_weak_made_hand_gate(3, 14000, 20000, 0.57,
                                      {'tier': 'thin'}, 0.0, False) is True, \
        "v154 RW1: river 0.57 large-pot should fold"
    # Scenario 2: river, made=0.56, pot 65% of stack -> FOLD (boundary)
    assert _river_weak_made_hand_gate(3, 13000, 20000, 0.56,
                                      {'tier': 'thin'}, 0.0, False) is True, \
        "v154 RW2: river 0.56 pot=65% should fold"
    # Scenario 3: river, made=0.60 -> NO FOLD (upper bound excluded)
    assert _river_weak_made_hand_gate(3, 14000, 20000, 0.60,
                                      {'tier': 'thin'}, 0.0, False) is False, \
        "v154 RW3: made=0.60 should NOT fold (>= 0.60 excluded)"
    # Scenario 4: river, strong tier -> NO FOLD (never fold premium)
    assert _river_weak_made_hand_gate(3, 14000, 20000, 0.58,
                                      {'tier': 'strong'}, 0.0, False) is False, \
        "v154 RW4: strong tier should not fold"
    # Scenario 5: river, draw >= 0.18 -> NO FOLD (draw protection)
    assert _river_weak_made_hand_gate(3, 14000, 20000, 0.57,
                                      {'tier': 'thin'}, 0.19, False) is False, \
        "v154 RW5: draw >= 0.18 should not fold"
    # Scenario 6: river, small pot (< 65% stack) -> NO FOLD
    assert _river_weak_made_hand_gate(3, 10000, 20000, 0.57,
                                      {'tier': 'thin'}, 0.0, False) is False, \
        "v154 RW6: small pot should not fold"
    # Scenario 7: turn (not river) -> NO FOLD (river-only gate)
    assert _river_weak_made_hand_gate(2, 14000, 20000, 0.57,
                                      {'tier': 'thin'}, 0.0, False) is False, \
        "v154 RW7: turn should not fold (river-only)"
    # Scenario 8: river, anti_lock pressure -> NO FOLD
    assert _river_weak_made_hand_gate(3, 14000, 20000, 0.57,
                                      {'tier': 'thin'}, 0.0, True) is False, \
        "v154 RW8: anti_lock should not fold"
    print("v154 _river_weak_made_hand_gate self-test: 8/8 PASS")

    # v154 _river_weak_made_hand_gate dispatch-reachability
    with open('strategy.py') as f:
        src = f.read()
    n_gates = src.count('_river_weak_made_hand_gate')
    assert n_gates >= 2, f"v154 RW dispatch-reachability FAIL: need >=2 refs (import + call), found {n_gates}"
    assert 'RIVER_WEAK_FOLD' in src, \
        "v154 RW dispatch-reachability FAIL: RIVER_WEAK_FOLD marker not found in strategy.py"
    print(f"v154 _river_weak_made_hand_gate dispatch-reachability: PASS ({n_gates} refs, RIVER_WEAK_FOLD present)")

    # v155 _post_missed_cbet_exploit self-test: 2 scenarios.
    # Verifies (a) standard bucket defaults (confidence=0.5, abandon=0.55) produce
    # NON-ZERO delta (M5 INERTNESS HARD RULE: no deadzone gap), and (b) low
    # abandon_rate (sub-prior) returns 0.0.
    test_opp_standard = {'confidence': 0.5,
                         'barrel_abandon_turn': 0.55, 'barrel_abandon_river': 0.65}
    test_value = {'tier': 'thin'}
    delta = _post_missed_cbet_exploit(
        2, {'last_raise_pot_ratio': 0.4}, test_opp_standard,
        0.50, 0.10, test_value, 500, 10000, 200, 0,
    )
    assert delta > 0, f"v155 S1: standard bucket must produce non-zero delta, got {delta}"
    print(f"v155 S1: standard bucket delta={delta:.4f} PASS")

    test_opp_low = {'confidence': 0.5,
                    'barrel_abandon_turn': 0.30, 'barrel_abandon_river': 0.30}
    delta_low = _post_missed_cbet_exploit(
        2, {'last_raise_pot_ratio': 0.4}, test_opp_low,
        0.50, 0.10, test_value, 500, 10000, 200, 0,
    )
    assert delta_low == 0.0, f"v155 S2: low abandon must produce 0, got {delta_low}"
    print(f"v155 S2: low abandon delta={delta_low:.4f} PASS")

    print("v155 _post_missed_cbet_exploit self-test: 2/2 PASS")

    # v155 dispatch-reachability: need >=4 refs (1 import + 3 call sites) in strategy.py.
    with open('strategy.py') as f:
        src = f.read()
    n_refs = src.count('_post_missed_cbet_exploit')
    assert n_refs >= 4, f"v155 dispatch-reachability FAIL: need >=4 refs (1 import + 3 sites), found {n_refs}"
    assert 'BARREL_ABANDON' in src, \
        "v155 dispatch-reachability FAIL: BARREL_ABANDON marker not found in strategy.py"
    print(f"v155 _post_missed_cbet_exploit dispatch-reachability: PASS ({n_refs} refs, BARREL_ABANDON present)")

    # v157 M5/M6 fixture: LIVE POOL DEFAULTS must produce non-zero tax.
    # This is the SOLE machine gate against INERTNESS — proves the function
    # actually fires for the standard marginal-hand / multi-street-aggression
    # case rather than returning 0 for the 96.9% standard bucket.
    _mst_spot = {'last_raise_pot_ratio': 0.5, 'facing_postflop_aggression': True,
                 'opp_prior_postflop_raise_count': 1,
                 'opp_postflop_bet_count': 2}
    _mst_tax = _multi_street_calldown_tax(_mst_spot, 0.46, 0.05, None, 3)
    assert _mst_tax > 0.0, f'tax must be non-zero for marginal made + multi-street aggr: {_mst_tax}'
    assert round(_mst_tax * 1000) > 0, f'delta_milli must be positive: {round(_mst_tax*1000)}'
    # Sanity: strong made hand returns 0.
    _mst_strong = _multi_street_calldown_tax(_mst_spot, 0.72, 0.05, {'tier': 'strong'}, 3)
    assert _mst_strong == 0.0, f'strong/nut must be exempt: {_mst_strong}'
    # Sanity: flop returns 0.
    _mst_flop = _multi_street_calldown_tax(_mst_spot, 0.46, 0.05, None, 1)
    assert _mst_flop == 0.0, f'flop must be exempt: {_mst_flop}'
    # Sanity: no prior bets returns 0.
    _mst_noprior = _multi_street_calldown_tax(
        {'opp_prior_postflop_raise_count': 0}, 0.46, 0.05, None, 3)
    assert _mst_noprior == 0.0, f'no prior bets must be exempt: {_mst_noprior}'
    print(f'MULTI_STREET_CALLDOWN_TAX fixture PASS: river tax={_mst_tax:.4f} '
          f'milli={round(_mst_tax*1000)} (must be >0); strong={_mst_strong}; '
          f'flop={_mst_flop}; no_prior={_mst_noprior}')

    # v158 river_value_raise_tier two-band self-test
    _tier_opp = {'confidence': 0.30, 'fold_to_bet_river': 0.40}
    _tier_static = {'dynamic': False}
    _tier_vp_strong = {'tier': 'strong'}
    _tier_vp_none = {'tier': 'none'}
    # Thin band: made_str=0.50 should return ~0.50x pot
    _thin = river_value_raise_tier(3, 0, 0.50, _tier_vp_none, _tier_static, _tier_opp, 1000, 10000, 100, 0)
    assert _thin is not None, 'v158 thin: must return amount'
    _thin_ratio = (_thin + 0) / 1000  # pot=1000, my_round_bet=0
    assert 0.44 <= _thin_ratio <= 0.56, f'v158 thin band: ratio {_thin_ratio:.3f} outside 0.44-0.56'
    print(f'v158 thin: ratio={_thin_ratio:.3f} PASS')
    # Strong band: made_str=0.60, default opp (river_call_size=0.50 → unlicensed gentle slope)
    # v159: ratio now ~0.53x (was ~0.69x under old unconditional two-band strong tier)
    _strong = river_value_raise_tier(3, 0, 0.60, _tier_vp_strong, _tier_static, _tier_opp, 1000, 10000, 100, 0)
    assert _strong is not None, 'v158 strong060: must return amount'
    _strong_ratio = _strong / 1000
    assert 0.48 <= _strong_ratio <= 0.58, f'v159 strong060 (unlicensed): ratio {_strong_ratio:.3f} outside 0.48-0.58'
    print(f'v159 strong060 (unlicensed): ratio={_strong_ratio:.3f} PASS')
    # Strong band: made_str=0.70, unlicensed → gentle slope ~0.59x
    _strong2 = river_value_raise_tier(3, 0, 0.70, _tier_vp_strong, _tier_static, _tier_opp, 1000, 10000, 100, 0)
    assert _strong2 is not None, 'v158 strong070: must return amount'
    _strong2_ratio = _strong2 / 1000
    assert 0.53 <= _strong2_ratio <= 0.63, f'v159 strong070 (unlicensed): ratio {_strong2_ratio:.3f} outside 0.53-0.63'
    print(f'v159 strong070 (unlicensed): ratio={_strong2_ratio:.3f} PASS')
    # Strong band: made_str=0.80, unlicensed → gentle slope ~0.64x
    _strong3 = river_value_raise_tier(3, 0, 0.80, _tier_vp_strong, _tier_static, _tier_opp, 1000, 10000, 100, 0)
    assert _strong3 is not None, 'v158 strong080: must return amount'
    _strong3_ratio = _strong3 / 1000
    assert 0.58 <= _strong3_ratio <= 0.68, f'v159 strong080 (unlicensed): ratio {_strong3_ratio:.3f} outside 0.58-0.68'
    print(f'v159 strong080 (unlicensed): ratio={_strong3_ratio:.3f} PASS')
    # Opponent fold_river adjustment still applies: fold<=0.30 -> +0.08*0.30
    _fold_adj_opp = {'confidence': 0.30, 'fold_to_bet_river': 0.25}
    _fold_adj = river_value_raise_tier(3, 0, 0.60, _tier_vp_strong, _tier_static, _fold_adj_opp, 1000, 10000, 100, 0)
    assert _fold_adj is not None and _fold_adj > _strong, f'v158 fold_adj: must increase sizing'
    print(f'v158 fold_adj: fold<=0.30 increases from {_strong} to {_fold_adj} PASS')
    print('v158 river_value_raise_tier (v159 license-gated): 5/5 PASS')

    # v159 river_call_size_ratio gating self-test: 4 scenarios.
    _default_opp = {'confidence': 0.5, 'fold_to_bet_river': 0.44, 'river_call_size_ratio': 0.50}
    _calling_opp = {'confidence': 0.5, 'fold_to_bet_river': 0.44, 'river_call_size_ratio': 0.75}
    _low_conf_opp = {'confidence': 0.05, 'fold_to_bet_river': 0.44, 'river_call_size_ratio': 0.75}

    _bt = {'wetness': 0.15, 'dynamic': False, 'flush_pressure': 0.2, 'straight_pressure': 0.2, 'paired': False}
    _v = river_value_raise_tier(3, 0, 0.65, {'tier': 'thin'}, _bt, _default_opp, 1000, 5000, 100, 0)
    assert _v is not None and 450 < _v < 700, f'DEFAULT unlicensed made=0.65: {_v}'
    _v = river_value_raise_tier(3, 0, 0.65, {'tier': 'thin'}, _bt, _calling_opp, 1000, 5000, 100, 0)
    assert _v is not None and 550 < _v < 850, f'CALLING licensed made=0.65: {_v}'
    _v54 = river_value_raise_tier(3, 0, 0.54, {'tier': 'thin'}, _bt, _default_opp, 1000, 5000, 100, 0)
    _v56 = river_value_raise_tier(3, 0, 0.56, {'tier': 'thin'}, _bt, _default_opp, 1000, 5000, 100, 0)
    assert _v54 is not None and _v56 is not None and abs(_v56 - _v54) < 60, f'CLIFF: {_v54} vs {_v56}'
    print('v159 river_call_size_ratio gating self-test: 4/4 PASS')

    # v160 turn_value_extraction_floor self-test.
    _tvf_bt = {'wetness': 0.4, 'dynamic': True}
    _tvf_thin = {'tier': 'thin'}
    _tvf_call = {'confidence': 0.5, 'fold_to_bet_turn': 0.30}
    _tvf_fold = {'confidence': 0.5, 'fold_to_bet_turn': 0.55}
    _tvf_lowconf = {'confidence': 0.05, 'fold_to_bet_turn': 0.30}
    # Calling station fires.
    _d1 = _turn_value_extraction_floor(_tvf_call, 2, _tvf_thin, _tvf_bt, 0.55)
    assert _d1 > 0.0, f'v160 calling-station must fire: {_d1}'
    # Folder does not fire.
    _d2 = _turn_value_extraction_floor(_tvf_fold, 2, _tvf_thin, _tvf_bt, 0.55)
    assert _d2 == 0.0, f'v160 folder must return 0: {_d2}'
    # Low confidence does not fire.
    _d3 = _turn_value_extraction_floor(_tvf_lowconf, 2, _tvf_thin, _tvf_bt, 0.55)
    assert _d3 == 0.0, f'v160 low-conf must return 0: {_d3}'
    # River does not fire.
    _d4 = _turn_value_extraction_floor(_tvf_call, 3, _tvf_thin, _tvf_bt, 0.55)
    assert _d4 == 0.0, f'v160 river must return 0: {_d4}'
    print('v160 turn_value_extraction_floor: 4/4 PASS')
