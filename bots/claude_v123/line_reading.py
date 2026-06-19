"""Opponent-line polarization classifier.

Reads board texture, opponent sizing/barrel patterns, and opponent-model
tendencies to classify the current facing line as value-heavy, bluff-heavy,
or balanced. Consumed by strategy.py to avoid paying off polarized value
lines with one-pair / draw-only hands.
"""
from card_utils import clamp
from postflop import bet_size_bucket, board_texture_profile

# CROSSOVER v123 (v116 × v108): revert BLUFF_OPPORTUNITY_THRESHOLD from v116's
# 0.48 mutation back to v108's original 0.55. H2H evidence (>=100g on v108):
# v108 beats the modern aggressive field where v116 only ties/loses — v93
# (v116 0.480 / v108 0.577), v96 (0.480 / 0.558), v97 (0.440 / 0.554),
# v109 (0.540 / 0.583), v113 (0.475 / 0.540). v116's lower 0.48 threshold
# fires the bluff_heavy label too eagerly against value-betting modern bots,
# causing bluff_heavy_call_widen() to over-widen calls and pay off value bets.
# The 0.55 threshold requires stronger fold-equity evidence (ftr_river>=0.55
# + low post_aggr/barrel_freq) before labeling a line as bluff-heavy, keeping
# call-widening reserved for genuinely passive/trappy opponents. v116's v102
# probe_mode fix (orthogonal correctness axis) is RETAINED from the base.
BLUFF_OPPORTUNITY_THRESHOLD = 0.55

# MUTATION v123 (option a — threshold adjustment, ~10.8% lower): lower
# VALUE_PRESSURE_THRESHOLD from 0.65 to 0.58 so the value_heavy line label
# fires more readily against moderately aggressive lines. This complements the
# crossover (raising bluff_heavy threshold): together they shift the label
# distribution away from 'bluff_heavy' and toward 'value_heavy' against the
# modern aggressive cohort. When facing multi-street barrels from opponents
# with post_aggr>=0.42 (value signals: size_bucket large + barrel>=2 + dynamic
# board = ~0.51-0.57 pressure), the old 0.65 gate left them 'balanced', so
# downstream bluff_heavy_call_widen could still widen calls via residual
# bluff signals. At 0.58, these same lines reach 'value_heavy', blocking the
# call-widening path and preserving chips vs value-heavy barrels. Balanced
# opponents (value_pressure ~0.30-0.50) are unaffected — still 'balanced'.
VALUE_PRESSURE_THRESHOLD = 0.58


def line_polarization_profile(public_cards, history, state, spot_info, opponent_model, round_idx):
    profile = {'value_pressure': 0.0, 'bluff_opportunity': 0.0, 'line_label': 'balanced'}
    if round_idx <= 0 or not public_cards:
        return profile
    last_ratio = spot_info.get('last_raise_pot_ratio', 0.0)
    size_bucket = bet_size_bucket(last_ratio)
    board = board_texture_profile(public_cards)
    barrel_count = spot_info.get('opp_postflop_bet_count', 0)
    opp_allin = state.get('opponent_allin', False)
    conf = opponent_model.get('confidence', 0.0)
    post_aggr = opponent_model.get('postflop_aggr', 0.36)
    barrel_freq = opponent_model.get('barrel_freq', 0.45)
    ftr_river = opponent_model.get('fold_to_bet_river', 0.44)

    value_signals = []
    if opp_allin:
        value_signals.append(0.35)
    if size_bucket == 'large':
        value_signals.append(0.25)
    elif size_bucket == 'medium':
        value_signals.append(0.12)
    if barrel_count >= 2:
        value_signals.append(0.18)
    if conf >= 0.15 and post_aggr >= 0.42 and barrel_freq >= 0.50:
        value_signals.append(0.14)
    if conf >= 0.15 and ftr_river <= 0.25:
        value_signals.append(0.12)
    if board.get('dynamic'):
        value_signals.append(0.08)
    value_pressure = min(sum(value_signals), 1.0)

    bluff_signals = []
    if conf >= 0.15 and ftr_river >= 0.55:
        bluff_signals.append(0.22)
    if conf >= 0.15 and post_aggr <= 0.28 and barrel_freq <= 0.30:
        bluff_signals.append(0.18)
    if size_bucket == 'large' and barrel_count == 1 and spot_info.get('last_opp_action_type') == 'raise':
        bluff_signals.append(0.10)
    if not board.get('dynamic') and board.get('wetness', 0.0) < 0.20:
        bluff_signals.append(0.08)
    bluff_opportunity = min(sum(bluff_signals), 1.0)

    label = 'balanced'
    if value_pressure >= VALUE_PRESSURE_THRESHOLD and value_pressure > bluff_opportunity + 0.10:
        label = 'value_heavy'
    elif bluff_opportunity >= BLUFF_OPPORTUNITY_THRESHOLD and bluff_opportunity > value_pressure + 0.10:
        label = 'bluff_heavy'

    profile['value_pressure'] = value_pressure
    profile['bluff_opportunity'] = bluff_opportunity
    profile['line_label'] = label
    return profile
