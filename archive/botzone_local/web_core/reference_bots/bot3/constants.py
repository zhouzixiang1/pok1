"""
Bot 3 - Constants: game parameters, preflop table, style definitions, EXP3 params, sizing tables.
"""

N_PLAYERS = 2
INITIAL_CHIPS = 20000
SMALL_BLIND = 50
BIG_BLIND = 100
TOTAL_HANDS = 50
LOCK_WIN_MARGIN = 1500

HAND_CLASS_SCORE = [0.08, 0.22, 0.40, 0.58, 0.69, 0.76, 0.84, 0.93, 0.98]
SIMULATIONS_BY_PUBLIC_COUNT = {
    0: 500,
    3: 700,
    4: 900,
    5: 0,
}
EXTRA_SIMULATIONS_BY_PUBLIC_COUNT = {
    0: 200,
    3: 220,
    4: 180,
}

# Improvement 2: Preflop 169-hand lookup table
# Key: (high_rank, low_rank, suited) -> normalized strength [0,1]
# Values derived from Chen formula + empirical equity data
PREFLOP_STRENGTH_TABLE = {}
_chen_base = {
    (14,14):1.00,(13,13):0.95,(12,12):0.90,(11,11):0.86,(10,10):0.82,
    (9,9):0.78,(8,8):0.74,(7,7):0.70,(6,6):0.66,(5,5):0.62,
    (4,4):0.58,(3,3):0.55,(2,2):0.52,
    (14,13):0.68,(14,12):0.65,(14,11):0.62,(14,10):0.59,(14,9):0.54,
    (14,8):0.48,(14,7):0.44,(14,6):0.41,(14,5):0.38,(14,4):0.36,
    (14,3):0.35,(14,2):0.34,
    (13,12):0.58,(13,11):0.55,(13,10):0.52,(13,9):0.47,(13,8):0.42,
    (13,7):0.39,(13,6):0.36,(13,5):0.34,(13,4):0.32,(13,3):0.31,
    (13,2):0.30,
    (12,11):0.51,(12,10):0.48,(12,9):0.43,(12,8):0.38,(12,7):0.36,
    (12,6):0.33,(12,5):0.31,(12,4):0.30,(12,3):0.29,(12,2):0.28,
    (11,10):0.45,(11,9):0.40,(11,8):0.36,(11,7):0.34,(11,6):0.31,
    (11,5):0.29,(11,4):0.28,(11,3):0.27,(11,2):0.26,
    (10,9):0.38,(10,8):0.34,(10,7):0.32,(10,6):0.29,(10,5):0.27,
    (10,4):0.26,(10,3):0.25,(10,2):0.24,
    (9,8):0.32,(9,7):0.30,(9,6):0.27,(9,5):0.25,(9,4):0.24,
    (9,3):0.23,(9,2):0.22,
    (8,7):0.29,(8,6):0.26,(8,5):0.24,(8,4):0.23,(8,3):0.22,(8,2):0.21,
    (7,6):0.25,(7,5):0.23,(7,4):0.22,(7,3):0.21,(7,2):0.20,
    (6,5):0.22,(6,4):0.21,(6,3):0.20,(6,2):0.19,
    (5,4):0.21,(5,3):0.20,(5,2):0.19,
    (4,3):0.19,(4,2):0.18,
    (3,2):0.17,
}
for (_h, _l), _v in _chen_base.items():
    if _h == _l:
        PREFLOP_STRENGTH_TABLE[(_h, _l, False)] = _v
    else:
        PREFLOP_STRENGTH_TABLE[(_h, _l, True)] = min(1.0, _v + 0.04)
        PREFLOP_STRENGTH_TABLE[(_h, _l, False)] = max(0.0, _v - 0.02)
        if _h - _l == 1:
            PREFLOP_STRENGTH_TABLE[(_h, _l, True)] = min(1.0, _v + 0.06)
            PREFLOP_STRENGTH_TABLE[(_h, _l, False)] = _v
        if _h - _l == 2:
            PREFLOP_STRENGTH_TABLE[(_h, _l, True)] = min(1.0, _v + 0.05)
        if _h == 14 and _l >= 10:
            PREFLOP_STRENGTH_TABLE[(_h, _l, True)] = min(1.0, _v + 0.06)
            PREFLOP_STRENGTH_TABLE[(_h, _l, False)] = min(1.0, _v + 0.02)

# ---------------------------------------------------------------
# Innovation 8: Multi-Style Strategy Portfolio
# ---------------------------------------------------------------
STYLE_NAMES = ("gto", "exploit_fold", "exploit_call", "exploit_maniac", "exploit_nit")
N_STYLES = len(STYLE_NAMES)

# Each style is a delta overlay on the base bot_2 thresholds.
# Parameters:
#   open_threshold_delta  - added to preflop open threshold
#   call_threshold_delta  - added to postflop call margin
#   bluff_frequency_mult  - bluff threshold is DIVIDED by this (higher = bluff more)
#   sizing_tier           - affects raise sizing: "small","medium","standard","large","polar"
#   threebet_range_width_delta - added to 3bet candidate strength range
#   trap_probability      - probability to slowplay nuts
STYLE_PARAMS = {
    "gto": {
        "open_threshold_delta": 0.00,
        "call_threshold_delta": 0.00,
        "bluff_frequency_mult": 1.0,
        "sizing_tier": "standard",
        "threebet_range_width_delta": 0.00,
        "trap_probability": 0.00,
    },
    "exploit_fold": {
        "open_threshold_delta": -0.03,
        "call_threshold_delta": 0.02,
        "bluff_frequency_mult": 1.4,
        "sizing_tier": "large",
        "threebet_range_width_delta": 0.08,
        "trap_probability": 0.05,
    },
    "exploit_call": {
        "open_threshold_delta": 0.01,
        "call_threshold_delta": -0.02,
        "bluff_frequency_mult": 0.3,
        "sizing_tier": "polar",
        "threebet_range_width_delta": -0.04,
        "trap_probability": 0.15,
    },
    "exploit_maniac": {
        "open_threshold_delta": 0.04,
        "call_threshold_delta": -0.04,
        "bluff_frequency_mult": 0.2,
        "sizing_tier": "medium",
        "threebet_range_width_delta": 0.05,
        "trap_probability": 0.30,
    },
    "exploit_nit": {
        "open_threshold_delta": -0.06,
        "call_threshold_delta": 0.04,
        "bluff_frequency_mult": 1.6,
        "sizing_tier": "small",
        "threebet_range_width_delta": 0.15,
        "trap_probability": 0.00,
    },
}

SIZING_TIER_RATIOS = {
    "small": -0.06,
    "medium": -0.02,
    "standard": 0.00,
    "large": 0.05,
    "polar": 0.03,
}

# ---------------------------------------------------------------
# Innovation 10: Adaptive Sizing Table
# key = (round_idx, hand_tier) -> base_ratio of pot
# ---------------------------------------------------------------
SIZING_TABLE = {
    # round_idx 0 = preflop
    (0, "none"): 0.55, (0, "thin"): 0.60, (0, "strong"): 0.75, (0, "nut"): 1.00,
    # round_idx 1 = flop
    (1, "none"): 0.50, (1, "thin"): 0.55, (1, "strong"): 0.70, (1, "nut"): 0.85,
    # round_idx 2 = turn
    (2, "none"): 0.55, (2, "thin"): 0.60, (2, "strong"): 0.75, (2, "nut"): 0.90,
    # round_idx 3 = river
    (3, "none"): 0.60, (3, "thin"): 0.65, (3, "strong"): 0.80, (3, "nut"): 1.10,
}
