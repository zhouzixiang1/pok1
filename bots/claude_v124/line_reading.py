"""Opponent-line polarization classifier.

Reads board texture, opponent sizing/barrel patterns, and opponent-model
tendencies to classify the current facing line as value-heavy, bluff-heavy,
or balanced. Consumed by strategy.py to avoid paying off polarized value
lines with one-pair / draw-only hands.
"""
from card_utils import clamp
from postflop import bet_size_bucket, board_texture_profile

VALUE_PRESSURE_THRESHOLD = 0.65
# MUTATION v116 (a — threshold adjustment, ~13% lower): relax bluff_heavy
# detection threshold 0.55 -> 0.48 so the bluff-heavy line label fires more
# readily on opponents whose stats indicate weak/passive aggression
# (low postflop_aggr + high fold_to_bet_river + small recent barrels).
# Downstream `bluff_heavy_call_widen` (strategy.py call path) increases
# call-margin against perceived bluffers, an OFFENSIVE bluff-catch axis
# distinct from the EXHAUSTED probe_mode/defensive-guard chain. Per
# experience pool BLUFF_CALIBRATION: only fires with confidence>=0.15 and
# concrete fold-equity evidence, so widening 0.55->0.48 stays gated.
BLUFF_OPPORTUNITY_THRESHOLD = 0.55  # CROSSOVER v124 (v118×v108): restored from v118's 0.48 mutation back to v108's 0.55 — STRUCTURAL CORRECTNESS REPAIR aligning label threshold with bluff_heavy_call_widen() boost baseline (0.55 in strategy_helpers.py). v118 inherited v116's inconsistency: label fired at 0.48 but boost stayed at flat 0.03 floor in 0.48-0.55 range, so threshold change was largely inert for boost amplitude while still activating the label downstream. H2H evidence (>=130g v108): v108 wins vs top defensive cohort where v118 ties/loses — v93 (0.577 vs 0.50), v109 (0.577 vs 0.40), v94 (0.55 vs —), v92 (0.543 vs 0.45). Honest value-bettors rarely bluff, so v118's wider 0.48 mislabels value lines as 'bluff_heavy', paying off value bets via call-widening. v118's river_value_raise_tier widening (0.45-0.50 thin-value band) is RETAINED — orthogonal offensive axis.


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
