from card_utils import clamp
from postflop import bet_size_bucket


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
    (rarely folds turn/river bets AND calls down consistently) and we hold
    a value hand on a non-dynamic board with low nutted_risk.

    DISTINCT from passive_exploit (caps river at 0.72x) and should_overbet
    (requires nut tier + dry board). Fills the STRONG/THIN tier gap: extracts
    0.75-1.20x pot from calling stations that existing primitives undercharge.

    3 reachable sizing branches: thin (0.75-0.85x), strong (0.95-1.05x),
    nut (1.10-1.20x). Returns raise-to-total amount (int) or None.
    """
    if round_idx < 2 or to_call > 0:
        return None
    confidence = opponent_model.get('confidence', 0.0)
    if confidence < 0.25:
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
