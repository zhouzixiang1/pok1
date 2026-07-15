"""Legal-observation opponent posterior tracker."""

from __future__ import annotations
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional


@dataclass
class ActionObservation:
    hand: int
    street: str
    action_kind: str
    raise_amount: Optional[int] = None
    pot_before: int = 0
    to_call: int = 0


@dataclass
class OpponentProfile:
    total_actions: int = 0
    fold_count: int = 0
    check_count: int = 0
    call_count: int = 0
    raise_count: int = 0
    allin_count: int = 0
    total_raise_amount: int = 0
    preflop_aggression: float = 0.0
    postflop_aggression: float = 0.0
    avg_raise_size: float = 0.0

    @property
    def fold_rate(self):
        return self.fold_count / max(1, self.total_actions)

    @property
    def aggression_factor(self):
        passive = self.check_count + self.call_count
        aggressive = self.raise_count + self.allin_count
        return aggressive / max(1, passive)

    @property
    def vpip(self):
        voluntary = self.call_count + self.raise_count + self.allin_count
        return voluntary / max(1, self.total_actions)


class OpponentTracker:
    """Connection-lived opponent posterior tracker."""

    def __init__(self):
        self.profile = OpponentProfile()
        self.hand_count = 0
        self._per_street_actions = defaultdict(list)
        self._showdown_hands = []

    def record_hand_start(self, hand):
        self.hand_count = hand

    def record_action(self, hand, street, action_kind, raise_amount=None,
                      pot_before=0, to_call=0, is_opponent=True):
        if not is_opponent:
            return
        self._per_street_actions[street].append(
            ActionObservation(hand, street, action_kind, raise_amount, pot_before, to_call))
        p = self.profile
        p.total_actions += 1
        if action_kind == "fold": p.fold_count += 1
        elif action_kind == "check": p.check_count += 1
        elif action_kind == "call": p.call_count += 1
        elif action_kind == "raise":
            p.raise_count += 1
            if raise_amount: p.total_raise_amount += raise_amount
        elif action_kind == "allin": p.allin_count += 1
        if p.raise_count > 0:
            p.avg_raise_size = p.total_raise_amount / p.raise_count
        pf = self._per_street_actions.get("preflop", [])
        if pf:
            aggr = sum(1 for a in pf if a.action_kind in ("raise", "allin"))
            p.preflop_aggression = aggr / len(pf)
        post = []
        for s in ("flop", "turn", "river"):
            post.extend(self._per_street_actions.get(s, []))
        if post:
            aggr = sum(1 for a in post if a.action_kind in ("raise", "allin"))
            p.postflop_aggression = aggr / len(post)

    def record_showdown(self, hole_cards):
        self._showdown_hands.append(hole_cards)

    def posterior_type(self):
        if self.profile.total_actions < 10:
            return "unknown"
        af = self.profile.aggression_factor
        vpip = self.profile.vpip
        fr = self.profile.fold_rate
        if fr > 0.5: return "tight_passive"
        elif af > 2.0 and vpip > 0.5: return "loose_aggressive"
        elif af > 2.0 and vpip < 0.4: return "tight_aggressive"
        elif vpip > 0.6: return "loose_passive"
        else: return "balanced"

    def confidence(self):
        return 1.0 / (1.0 + math.exp(-(self.profile.total_actions - 30) / 8))

    def exploit_adjustment(self):
        ctype = self.posterior_type()
        conf = self.confidence()
        base = {"aggression_mult": 1.0, "call_mult": 1.0, "bluff_mult": 1.0}
        adjustments = {
            "tight_passive": {"aggression_mult": 1.3, "call_mult": 0.8, "bluff_mult": 1.5},
            "loose_aggressive": {"aggression_mult": 0.7, "call_mult": 1.3, "bluff_mult": 0.5},
            "tight_aggressive": {"aggression_mult": 0.9, "call_mult": 1.0, "bluff_mult": 1.2},
            "loose_passive": {"aggression_mult": 1.4, "call_mult": 1.2, "bluff_mult": 0.8},
            "balanced": {"aggression_mult": 1.0, "call_mult": 1.0, "bluff_mult": 1.0},
            "unknown": {"aggression_mult": 1.0, "call_mult": 1.0, "bluff_mult": 1.0},
        }
        adj = adjustments.get(ctype, base)
        return {k: base[k] * (1 - conf) + adj[k] * conf for k in base}

    def summary(self):
        return {
            "total_actions": self.profile.total_actions,
            "fold_rate": round(self.profile.fold_rate, 3),
            "aggression_factor": round(self.profile.aggression_factor, 3),
            "vpip": round(self.profile.vpip, 3),
            "posterior_type": self.posterior_type(),
            "confidence": round(self.confidence(), 3),
            "exploit_adjustment": {k: round(v, 3) for k, v in self.exploit_adjustment().items()},
        }
