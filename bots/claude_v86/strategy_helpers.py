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
