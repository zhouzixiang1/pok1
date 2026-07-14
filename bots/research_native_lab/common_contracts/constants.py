"""Versioned national-game and online-decision constants."""

CONTRACT_VERSION = "national-research-contract-v1"

PLAYERS = 2
INITIAL_CHIPS = 20_000
SMALL_BLIND = 50
BIG_BLIND = 100
HANDS_PER_MATCH = 70
DECISION_TIMEOUT_SEC = 60.0
OFFICIAL_ACTION_DELAY_SEC = 0.30

MIN_RAISE_PREFLOP = 200
MIN_RAISE_POSTFLOP = 100
RAISE_TO_MULTIPLIER = 2

# Search must stop before the platform's hard timeout.  The 50 second frozen
# comparison point remains below this ceiling; a full-budget run stops at 54s.
ANYTIME_MILESTONES_SEC = (0.250, 2.0, 8.0, 20.0, 40.0)
FROZEN_EVALUATION_BUDGETS_SEC = (0.250, 5.0, 20.0, 50.0)
ABSOLUTE_COMPUTE_STOP_SEC = 54.0
