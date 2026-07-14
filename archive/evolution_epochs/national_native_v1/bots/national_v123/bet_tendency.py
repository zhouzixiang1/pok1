import sys
from card_utils import clamp

OVERBET_POT_FRAC_THRESHOLD = 1.0
OVERBET_SHOVE_RATE_THRESHOLD = 0.25
MIN_TENDENCY_SAMPLES = 3
MIN_TENDENCY_CONFIDENCE = 0.12


def classify_bet_tendency(opponent_model, round_idx):
    """Classify opponent's bet-size tendency per street.
    Returns 'overbet_polarized', 'standard', or 'unknown'.
    Combines pot-fraction raise sizes (from opponent.py) with per-street shove_rate.
    """
    if round_idx <= 0:
        return 'unknown'
    conf = opponent_model.get('confidence', 0.0)
    if conf < MIN_TENDENCY_CONFIDENCE:
        return 'unknown'
    frac_key = {1: 'flop_pot_frac', 2: 'turn_pot_frac', 3: 'river_pot_frac'}.get(round_idx)
    samples_key = {1: 'flop_pot_frac_samples', 2: 'turn_pot_frac_samples', 3: 'river_pot_frac_samples'}.get(round_idx)
    shove_key = {1: 'flop_shove_rate', 2: 'turn_shove_rate', 3: 'river_shove_rate'}.get(round_idx)
    if not frac_key:
        return 'unknown'
    smoothed_frac = opponent_model.get(frac_key, 0.75)
    frac_samples = opponent_model.get(samples_key, 0)
    shove_rate = opponent_model.get(shove_key, 0.08)
    overbet_by_frac = frac_samples >= MIN_TENDENCY_SAMPLES and smoothed_frac >= OVERBET_POT_FRAC_THRESHOLD
    overbet_by_shove = shove_rate >= OVERBET_SHOVE_RATE_THRESHOLD
    if overbet_by_frac or overbet_by_shove:
        cls = 'overbet_polarized'
    else:
        cls = 'standard'
    sys.stderr.write(
        f'BET_TENDENCY street={round_idx} cls={cls} '
        f'frac={smoothed_frac:.3f}({frac_samples}) shove={shove_rate:.3f} conf={conf:.2f}\n'
    )
    return cls


def river_tendency_hero_call(opponent_model, made_strength, value_profile, spot_info, pot_odds, win_rate, round_idx):
    """Hero-call marginal river bluffcatchers vs confirmed overbet-polarized opponents.
    Returns True if hero-call override should fire (call instead of fold).
    """
    if round_idx != 3:
        return False
    if not spot_info.get('facing_postflop_aggression', False):
        return False
    tendency = classify_bet_tendency(opponent_model, 3)
    if tendency != 'overbet_polarized':
        return False
    tier = value_profile.get('tier', 'none') if value_profile else 'none'
    if tier in ('nut', 'strong'):
        return False
    if not (0.22 <= made_strength < 0.55):
        return False
    if pot_odds > 0.45:
        return False
    if win_rate < pot_odds - 0.08:
        return False
    sys.stderr.write(
        f'RIVER_TENDENCY_HERO_CALL fired=1 made={made_strength:.3f} '
        f'win_rate={win_rate:.3f} pot_odds={pot_odds:.3f} tier={tier}\n'
    )
    return True


if __name__ == '__main__':
    _std = classify_bet_tendency({'confidence': 0.5, 'river_pot_frac': 0.75, 'river_pot_frac_samples': 5, 'river_shove_rate': 0.08}, 3)
    assert _std == 'standard', f'expected standard, got {_std}'
    _over = classify_bet_tendency({'confidence': 0.5, 'river_pot_frac': 1.3, 'river_pot_frac_samples': 5, 'river_shove_rate': 0.08}, 3)
    assert _over == 'overbet_polarized', f'expected overbet, got {_over}'
    _shove = classify_bet_tendency({'confidence': 0.5, 'river_pot_frac': 0.75, 'river_pot_frac_samples': 5, 'river_shove_rate': 0.35}, 3)
    assert _shove == 'overbet_polarized', f'expected overbet by shove, got {_shove}'
    _hero = river_tendency_hero_call({'confidence': 0.5, 'river_pot_frac': 1.3, 'river_pot_frac_samples': 5, 'river_shove_rate': 0.08}, 0.35, {'tier': 'thin'}, {'facing_postflop_aggression': True}, 0.33, 0.30, 3)
    assert _hero == True, f'expected hero-call, got {_hero}'
    _no = river_tendency_hero_call({'confidence': 0.5, 'river_pot_frac': 1.3, 'river_pot_frac_samples': 5, 'river_shove_rate': 0.08}, 0.35, {'tier': 'strong'}, {'facing_postflop_aggression': True}, 0.33, 0.30, 3)
    assert _no == False
    print(f'self-test ok: std={_std} over={_over} shove={_shove} hero={_hero}')
