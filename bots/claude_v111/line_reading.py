"""Opponent-line polarization classifier.

Reads board texture, opponent sizing/barrel patterns, and opponent-model
tendencies to classify the current facing line as value-heavy, bluff-heavy,
or balanced. Consumed by strategy.py to avoid paying off polarized value
lines with one-pair / draw-only hands.
"""
from card_utils import clamp
from postflop import bet_size_bucket, board_texture_profile

VALUE_PRESSURE_THRESHOLD = 0.65
BLUFF_OPPORTUNITY_THRESHOLD = 0.55


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
