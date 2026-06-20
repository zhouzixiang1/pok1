import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from strategy import should_fold_postflop
from strategy_helpers import check_raise_pressure, barrel_pressure_profile

SCENARIOS = [
    # (name, round_idx, made, draw, size_ratio, opp_check_cnt, opp_bet_cnt, barrel_freq, post_aggr)
    ("river_mid_pair_vs_barreler",    3, 0.40, 0.05, 0.50, 0, 1, 0.55, 0.42),  # expect fold after rewire
    ("river_weak_pair_vs_barreler",   3, 0.30, 0.05, 0.50, 0, 1, 0.55, 0.42),  # already folds upstream
    ("river_strong_pair_vs_barreler", 3, 0.55, 0.05, 0.50, 0, 1, 0.55, 0.42),  # MUST stay call
    ("turn_mid_pair_vs_barreler",     2, 0.35, 0.05, 0.60, 0, 1, 0.50, 0.40),  # expect fold after rewire
    ("river_mid_pair_vs_cr_trap",     3, 0.45, 0.05, 0.75, 1, 1, 0.45, 0.36),  # expect fold after rewire
    ("river_overpair_vs_cr_trap",     3, 0.60, 0.05, 0.75, 1, 1, 0.45, 0.36),  # MUST stay call
]

def run():
    for name, rnd, made, draw, size, chk, bet, bf, pa in SCENARIOS:
        spot = {
            'facing_postflop_aggression': True,
            'last_raise_pot_ratio': size,
            'opp_current_round_check_count': chk,
            'opp_current_round_bet_count': bet,
            'has_position': False,
        }
        opp = {'confidence': 0.50, 'barrel_freq': bf, 'postflop_aggr': pa}
        vp = {'tier': 'thin'}
        cr = check_raise_pressure(spot, opp)
        bp = barrel_pressure_profile(spot, opp, rnd)
        fold = should_fold_postflop(rnd, made, draw, vp, spot, opp, spr=10.0)
        pot_odds_req = size / (1.0 + size)
        print(f"{name}: made={made} size={size} pot_odds_req={pot_odds_req:.3f} cr={cr} bp={bp} should_fold={fold}")

if __name__ == "__main__":
    run()
