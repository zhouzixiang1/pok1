"""70-hand match controller for Route B.

Tracks cumulative chips across the match and adjusts strategy based on
match position (early/mid/late) and chip differential.
"""

from __future__ import annotations
import math
from dataclasses import dataclass

HANDS_PER_MATCH = 70
INITIAL_CHIPS = 20000


@dataclass
class MatchState:
    hand_number: int = 0
    cumulative_net: int = 0
    hands_played: int = 0
    hands_won: int = 0
    hands_lost: int = 0
    hands_drawn: int = 0


class MatchController:
    """Manages 70-hand match-level strategy adjustments."""

    def __init__(self):
        self.state = MatchState()
        self._phase_thresholds = {
            "early": 20,
            "mid": 50,
        }

    def record_hand_result(self, hand_number: int, net_chips: int):
        self.state.hand_number = hand_number
        self.state.cumulative_net += net_chips
        self.state.hands_played += 1
        if net_chips > 0:
            self.state.hands_won += 1
        elif net_chips < 0:
            self.state.hands_lost += 1
        else:
            self.state.hands_drawn += 1

    @property
    def phase(self) -> str:
        h = self.state.hands_played
        if h < self._phase_thresholds["early"]:
            return "early"
        elif h < self._phase_thresholds["mid"]:
            return "mid"
        else:
            return "late"

    @property
    def chip_differential(self) -> float:
        return self.state.cumulative_net / INITIAL_CHIPS

    def risk_adjustment(self) -> dict:
        """Return strategy modifiers based on match position and chip diff.

        - early: explore, moderate aggression
        - mid: standard play, start exploiting
        - late: if ahead, tighten up; if behind, increase aggression
        """
        phase = self.phase
        diff = self.chip_differential

        if phase == "early":
            return {"aggression_mult": 1.0, "bluff_mult": 1.0, "call_threshold_mult": 1.0}
        elif phase == "mid":
            return {"aggression_mult": 1.0, "bluff_mult": 1.0, "call_threshold_mult": 1.0}
        else:  # late
            if diff > 0.1:  # ahead by 10%+
                return {"aggression_mult": 0.8, "bluff_mult": 0.7, "call_threshold_mult": 0.9}
            elif diff < -0.1:  # behind by 10%+
                return {"aggression_mult": 1.3, "bluff_mult": 1.4, "call_threshold_mult": 1.1}
            else:
                return {"aggression_mult": 1.0, "bluff_mult": 1.0, "call_threshold_mult": 1.0}

    def time_budget_hint(self) -> float:
        """Suggest per-decision time budget based on match phase."""
        if self.phase == "early":
            return 5.0
        elif self.phase == "mid":
            return 10.0
        else:
            return 20.0

    def summary(self) -> dict:
        return {
            "hand_number": self.state.hand_number,
            "hands_played": self.state.hands_played,
            "cumulative_net": self.state.cumulative_net,
            "hands_won": self.state.hands_won,
            "hands_lost": self.state.hands_lost,
            "phase": self.phase,
            "chip_diff": round(self.chip_differential, 3),
            "risk_adjustment": {k: round(v, 2) for k, v in self.risk_adjustment().items()},
        }
