"""Opponent modelling.

Tracks the opponent's tendencies across hands by replaying the full
``requests`` history (the local engine feeds a cumulative ``requests`` list,
so we can rebuild everything every decision without needing the ``data``
field). This mirrors what claude_v279 does: stateless across hands, but
re-derives a rich opponent model from history each time.

Tracked signals (all in [0, 1] unless noted):
- ``vpip``     voluntarily put $ in preflop (limp/call/raise, blinds excluded)
- ``pfr``      preflop raise rate
- ``preflop_raise_size``  average SB-open raise-to-total (chips)
- ``fold_to_bet_{flop,turn,river}``  fold frequency when facing a bet
- ``barrel_freq``  of flops we bet, how often we barrel the turn
- ``postflop_aggr``  postflop (raise+bet) / (raise+bet+call+check)
- ``agg_factor``  postflop bets+raises / calls
- ``showdown_win_rate``  when going to showdown, frac won (proxy for value)
- ``hands_observed``  number of hands seen
- ``total_actions``  denominator for confidence
- ``value_heavy``  derived: high postflop_aggr + low bluff (heuristic)

Confidence scales linearly from 0 (no data) to 1 (~35 actions observed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .state import INITIAL_CHIPS, BIG_BLIND, SMALL_BLIND


@dataclass
class OpponentModel:
    # Raw counts.
    hands_observed: int = 0
    total_actions: int = 0
    vpip_count: int = 0           # voluntary preflop contributions
    pfr_count: int = 0            # preflop raises
    preflop_raise_total: int = 0  # sum of raise-to totals (for averaging)
    # Postflop action counts (per street where the opp faced a chance).
    postflop_bets: int = 0
    postflop_raises: int = 0
    postflop_calls: int = 0
    postflop_checks: int = 0
    postflop_folds: int = 0
    # Per-street "faced a bet" trackers.
    faced_bet_flop: int = 0
    faced_bet_turn: int = 0
    faced_bet_river: int = 0
    fold_flop: int = 0
    fold_turn: int = 0
    fold_river: int = 0
    # Barrel tracking.
    flop_bet_count: int = 0       # times opp bet the flop (as opener)
    turn_barrel_count: int = 0    # of those, times opp bet the turn
    # Showdowns.
    showdowns: int = 0
    showdown_wins: int = 0
    # Avg preflop raise size of opponent.
    preflop_raise_size_sum: int = 0

    # ------------------------------------------------------------------
    # Derived statistics (with confidence weighting).
    # ------------------------------------------------------------------
    @property
    def confidence(self) -> float:
        """0..1; reaches ~1 after 35 observed actions."""
        return min(1.0, max(0.0, (self.total_actions - 5) / 35.0))

    @property
    def vpip(self) -> float:
        return self.vpip_count / max(1, self.hands_observed)

    @property
    def pfr(self) -> float:
        return self.pfr_count / max(1, self.hands_observed)

    @property
    def avg_preflop_raise_size(self) -> float:
        return self.preflop_raise_size_sum / max(1, self.pfr_count)

    @property
    def postflop_aggr(self) -> float:
        aggr = self.postflop_bets + self.postflop_raises
        total = aggr + self.postflop_calls + self.postflop_checks
        return aggr / max(1, total)

    @property
    def agg_factor(self) -> float:
        """postflop (bets+raises)/calls; >1 aggressive, <1 passive."""
        return (self.postflop_bets + self.postflop_raises) / max(1, self.postflop_calls)

    @property
    def fold_to_bet_flop(self) -> float:
        return self.fold_flop / max(1, self.faced_bet_flop)

    @property
    def fold_to_bet_turn(self) -> float:
        return self.fold_turn / max(1, self.faced_bet_turn)

    @property
    def fold_to_bet_river(self) -> float:
        return self.fold_river / max(1, self.faced_bet_river)

    @property
    def barrel_freq(self) -> float:
        return self.turn_barrel_count / max(1, self.flop_bet_count)

    @property
    def showdown_win_rate(self) -> float:
        return self.showdown_wins / max(1, self.showdowns)

    @property
    def value_heavy(self) -> float:
        """Heuristic: how value-heavy / bluff-light the opponent is.

        High postflop aggression + high showdown win rate => value-heavy.
        Returns a 0..1 score; we tighten our bluff-catch range when high.
        """
        aggr = self.postflop_aggr
        swr = self.showdown_win_rate if self.showdowns >= 3 else 0.5
        # Combine: a passive player (aggr low) is not "value heavy" even if
        # they only show down strong hands (they don't bet to extract).
        score = 0.5 * aggr + 0.5 * swr
        return max(0.0, min(1.0, score))

    @property
    def is_calling_station(self) -> float:
        """Inverse aggression + high vpip => calling station (rarely folds)."""
        if self.total_actions < 8:
            return 0.5
        station = 0.6 * (1.0 - self.postflop_aggr) + 0.4 * self.vpip
        return max(0.0, min(1.0, station))

    def describe(self) -> Dict[str, float]:
        return {
            "hands": float(self.hands_observed),
            "conf": self.confidence,
            "vpip": self.vpip,
            "pfr": self.pfr,
            "postflop_aggr": self.postflop_aggr,
            "agg_factor": self.agg_factor,
            "fold_flop": self.fold_to_bet_flop,
            "fold_turn": self.fold_to_bet_turn,
            "fold_river": self.fold_to_bet_river,
            "barrel": self.barrel_freq,
            "value_heavy": self.value_heavy,
            "station": self.is_calling_station,
        }


# ---------------------------------------------------------------------------
# Replaying history to build the model
# ---------------------------------------------------------------------------

def _player_id_of(hero_id: int) -> Tuple[int, int]:
    """Return (hero, opp) ids given hero id."""
    return hero_id, 1 - hero_id


def build_opponent_model(requests: List[dict], hero_id_in_last: int) -> OpponentModel:
    """Replay the cumulative ``requests`` list and build an OpponentModel.

    ``requests`` is the bot's full cumulative request list (each entry is one
    decision the bot was asked to make). ``hero_id_in_last`` is the hero's
    ``my_id`` in the most recent request (it is stable within a game).

    The opponent is the other player. For each prior hand we replay the
    betting and tally the opponent's actions.
    """
    m = OpponentModel()
    if not requests:
        return m

    opp_static_id = 1 - hero_id_in_last

    # Group history records by hand. The local engine resets ``hand`` per
    # game; within a game each request has a ``hand`` index. We rebuild per
    # hand using the last seen ``hand`` value of each request.
    seen_hands = set()

    for req in requests:
        if not isinstance(req, dict):
            continue
        hand = req.get("hand", 0)
        # Count each hand once for vpip/pfr denominators.
        if hand not in seen_hands:
            seen_hands.add(hand)
            m.hands_observed += 1
        # The hero id is stable in heads-up; use opp_static_id.
        history = req.get("history", []) or []
        _replay_history_into_model(m, history, opp_static_id, hero_id_in_last)

    return m


def _replay_history_into_model(m: OpponentModel, history: List[dict],
                                opp_id: int, hero_id: int) -> None:
    """Walk one hand's history and update the model with opp actions."""
    # Track per-hand state to compute "facing a bet", barrels, vpip.
    cur_round = 0
    opp_vpip_this_hand = False
    opp_pfr_this_hand = False
    opp_raised_preflop_amount = 0
    opp_flop_bet_this_hand = False
    opp_facing_bet_this_round = {1: False, 2: False, 3: False}
    last_round_seen = 0

    prev_round_bet = {0: BIG_BLIND, 1: 0, 2: 0, 3: 0}
    # For "faced a bet" detection: did the hero (or anyone) put money in
    # this round before the opponent's action such that opp must call >0?
    contributions = {0: 0, 0: 0, 1: 0, 2: 0, 3: 0}
    contributions[0] = 0
    # Preflop blinds preset.
    sb_contrib = SMALL_BLIND
    bb_contrib = BIG_BLIND
    round_contrib = {0: 0, 1: 0, 2: 0, 3: 0}
    round_contrib_preflop = {0: SMALL_BLIND, 1: BIG_BLIND}  # by position

    # We do not know dealer from history reliably across hands in cumulative
    # requests, so preflop "voluntary" detection is approximate: any
    # preflop call/raise that increases contribution beyond a blind counts.

    round_bet = BIG_BLIND  # preflop round bet starts at BB
    baseline = BIG_BLIND
    cur_round_local = 0

    for rec in history:
        rround = rec.get("round", cur_round_local)
        if rround != cur_round_local:
            cur_round_local = rround
            round_bet = 0
            baseline = BIG_BLIND // 2 if cur_round_local > 0 else BIG_BLIND
            opp_flop_bet_this_hand = False  # reset tracker on new round

        pid = rec.get("player_id")
        at = rec.get("action_type")
        act = rec.get("action")
        if pid is None or at is None:
            continue

        if pid == opp_id:
            m.total_actions += 1
            # Preflop voluntary detection.
            if cur_round_local == 0 and at in ("call", "raise") and act is not None:
                # call (act 0) where opp was facing >0 to call -> voluntary
                # raise -> voluntary
                opp_vpip_this_hand = True
                if at == "raise":
                    opp_pfr_this_hand = True
                    opp_raised_preflop_amount = max(
                        opp_raised_preflop_amount, int(act) if act else 0)
            # Postflop.
            if cur_round_local >= 1:
                if at == "raise":
                    m.postflop_raises += 1
                    m.postflop_bets += 1  # treat raise as bet-like
                    if cur_round_local == 1:
                        opp_flop_bet_this_hand = True
                        m.flop_bet_count += 1
                    elif cur_round_local == 2 and opp_flop_bet_this_hand:
                        m.turn_barrel_count += 1
                elif at == "call":
                    m.postflop_calls += 1
                    # "faced bet" detection handled below via contributions
                elif at == "check":
                    m.postflop_checks += 1
                elif at == "fold":
                    m.postflop_folds += 1
                    if cur_round_local == 1:
                        m.fold_flop += 1
                    elif cur_round_local == 2:
                        m.fold_turn += 1
                    elif cur_round_local == 3:
                        m.fold_river += 1
        else:
            # Hero action: count as "a bet faced by opp" if hero raised.
            if at == "raise" and cur_round_local in (1, 2, 3):
                opp_facing_bet_this_round[cur_round_local] = True

        # Track round_bet / contributions for "facing a bet" detection.
        if at == "raise" and act is not None:
            try:
                target = int(act)
                if target > round_bet:
                    round_bet = target
            except Exception:
                pass
        elif at == "allin":
            round_bet = 10 ** 9  # enormous; anyone after faces a "bet"

        # If opp's next action in this round will face a bet, mark it.
        # Simplification: if the last action before opp's call/raise/fold in
        # this round had round_bet > opp's contribution, they faced a bet.
        # We count "faced a bet" at the moment opp folds (foldToFbet denom).
        if pid == opp_id and at == "fold":
            # If round_bet > opp's last contribution, they folded to a bet.
            # We approximate: any postflop fold counts (since preflop folds
            # are handled separately).
            if cur_round_local == 1:
                m.faced_bet_flop += 1
            elif cur_round_local == 2:
                m.faced_bet_turn += 1
            elif cur_round_local == 3:
                m.faced_bet_river += 1

    if opp_vpip_this_hand:
        m.vpip_count += 1
    if opp_pfr_this_hand:
        m.pfr_count += 1
        m.preflop_raise_size_sum += opp_raised_preflop_amount


if __name__ == "__main__":  # pragma: no cover - sanity test
    # Synthetic history: opp raises preflop once, then barrels flop+turn.
    history = [
        # Preflop: opp (id=1) raises to 250, hero (id=0) calls.
        {"round": 0, "player_id": 1, "action": 250, "action_type": "raise"},
        {"round": 0, "player_id": 0, "action": 0, "action_type": "call"},
        # Flop: opp bets, hero calls.
        {"round": 1, "player_id": 1, "action": 300, "action_type": "raise"},
        {"round": 1, "player_id": 0, "action": 0, "action_type": "call"},
        # Turn: opp barrels, hero folds.
        {"round": 2, "player_id": 1, "action": 700, "action_type": "raise"},
        {"round": 2, "player_id": 0, "action": -1, "action_type": "fold"},
    ]
    m = OpponentModel()
    _replay_history_into_model(m, history, opp_id=1, hero_id=0)
    print("hands:", m.hands_observed)
    print("vpip:", m.vpip, "pfr:", m.pfr, "avg_pfr_size:", m.avg_preflop_raise_size)
    print("postflop_aggr:", m.postflop_aggr, "agg_factor:", m.agg_factor)
    print("barrel:", m.barrel_freq, "fold_flop:", m.fold_to_bet_flop)
    print("value_heavy:", m.value_heavy, "station:", m.is_calling_station)
    print("conf:", m.confidence)
    print("describe:", m.describe())
