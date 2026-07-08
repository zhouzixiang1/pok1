"""Equity + pot-odds EV policy (the new school).

Decision philosophy
-------------------
Unlike the existing bots (hand-strength lookup table + heuristic raise bands),
this policy makes decisions from first principles:

1. Estimate ``equity`` (probability of winning at showdown) via Monte-Carlo.
2. Fold / call / raise based on whether each action has positive expected
   chip value (EV), using pot-odds and the estimated equity.
3. Use a small amount of randomisation to avoid being exploited:
   - bluff-raise occasionally with very weak hands (frequency-based),
   - slow-play very strong hands sometimes (check/call instead of raise).
4. Aggression is a function of (equity, pot-odds, betting-round, stack/pot).

The policy is deliberately *opponent-agnostic* in its core: it plays a
near game-theoretically-defensible strategy that should hold up against any
style. It does not model a specific opponent's tendencies, which makes it
robust to the wide style range of the local bot pool (passive bot1/2/3,
aggressive bot4, simulation-heavy bot5/6, evolved claude_v279).

Tunable parameters are collected in :class:`PolicyConfig` so we can iterate
quickly against the local ladder without rewriting the decision logic.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import List, Optional

from .equity import estimate_equity, estimate_equity_ranged
from .opponent import OpponentModel, build_opponent_model
from .range_model import RangeModel, build_range_from_history
from .preflop import (classify_preflop_hand, class_rank, preflop_defense_equity_discount,
                      sb_open_action, TRASH, MARGINAL, PLAYABLE, STRONG, PREMIUM,
                      _CLASS_RANK)
from .state import GameState, BIG_BLIND, SMALL_BLIND


@dataclass
class PolicyConfig:
    # --- Monte-Carlo budget (scales with available info / time) ---------
    # Higher counts give a tighter equity estimate so our pot-odds calls
    # are not noisy. We can still afford this comfortably inside the 60s
    # budget (3000 sims ~ 0.1s on a single core).
    n_sim_preflop: int = 2000
    n_sim_postflop: int = 3500
    # Cap total sims to keep well under the 60s decision budget.
    n_sim_cap: int = 5000

    # --- Calling threshold (pot-odds) -----------------------------------
    # We call if  equity * pot  >=  call_cost * call_edge.
    # call_edge > 1 makes us slightly tighter than pure pot-odds.
    call_edge: float = 1.02

    # --- Raising thresholds (when facing a bet) -------------------------
    # Raise for value when equity >= raise_value_threshold.
    raise_value_threshold: float = 0.66
    # Pot-sized fraction used when value-raising (scales with strength).
    raise_value_frac_min: float = 0.55
    raise_value_frac_max: float = 1.10
    # Steal / bluff frequency from late position with marginal hands.
    bluff_frequency: float = 0.10
    bluff_raise_frac: float = 0.55

    # --- Betting thresholds (when checked to / no bet to call) ----------
    # We bet more liberally when we have the betting lead (opponent showed
    # no aggression): the value-bet threshold is lower because passive
    # opponents call with worse, and we lose nothing when behind if we keep
    # the size small.
    bet_value_threshold: float = 0.54      # bet for value when checked to
    bet_thin_value_threshold: float = 0.44 # thin value bet (small sizing)
    bet_thin_value_frac: float = 0.45
    bet_bluff_frequency: float = 0.25      # bluff when checked to (post-flop)
    bet_bluff_frac: float = 0.55

    # --- Fold equity / aggression ---------------------------------------
    # When we are semi-bluffing (draws), factor in fold-equity by treating
    # a fraction of the opponent's folding probability as extra equity.
    semi_bluff_min_equity: float = 0.30
    semi_bluff_assumed_fold: float = 0.35

    # --- Slow-play / trapping -------------------------------------------
    slowplay_threshold: float = 0.90       # nuts-ish: check/call instead
    slowplay_frequency: float = 0.25

    # --- Implied odds (draws) -------------------------------------------
    # When we have a draw (not made hand), each out we hit on a later street
    # can win extra bets. We scale call-EV by this factor on the flop/turn
    # for hands whose equity is mostly draw-driven.
    implied_odds_flop: float = 1.18
    implied_odds_turn: float = 1.08
    implied_odds_river: float = 1.0       # no more cards, no implied odds

    # --- Opponent-model tuning ------------------------------------------
    # When confidence > 0, we shift thresholds based on the model.
    # Tighten our call-down range vs value-heavy opponents.
    call_edge_vs_value_heavy: float = 1.18
    # Loosen vs calling stations (they bluff less, value-bet thin -> fold more).
    call_edge_vs_station: float = 1.25
    # Bluff less vs calling stations.
    bluff_freq_vs_station_mult: float = 0.25
    # Value-bet thinner vs stations (they call with worse).
    bet_value_threshold_vs_station_delta: float = -0.06
    # Vs tight folders (high fold_to_bet), bluff more.
    bluff_freq_vs_tight_mult: float = 2.0

    # --- Preflop range tightening ---------------------------------------
    # When facing a preflop raise, fold trash hands outright rather than
    # relying on noisy uniform-equity pot-odds.
    preflop_fold_trash_vs_raise: bool = True
    preflop_fold_marginal_vs_big_raise: bool = True  # fold marg vs >=4BB raise

    # --- Misc -----------------------------------------------------------
    # If we cannot even call the big blind worth of chips, just call/check.
    min_interesting_pot: int = BIG_BLIND
    # Use a deterministic seed if set (None = randomise).
    seed: Optional[int] = None


class Policy:
    """Decision policy returning a single legal action integer."""

    def __init__(self, config: Optional[PolicyConfig] = None,
                 rng: Optional[random.Random] = None):
        self.cfg = config or PolicyConfig()
        self.rng = rng or random.Random(self.cfg.seed)
        # Opponent model is rebuilt per decision from the cumulative
        # requests history (stateless across hands, like claude_v279).
        self._opp_model: Optional[OpponentModel] = None
        self._opp_model_for_hand: int = -1

    # ------------------------------------------------------------------
    def _build_opp_model(self, requests: list, my_id: int) -> OpponentModel:
        """Cache the opponent model per hand to avoid recomputing each
        decision within the same hand."""
        hand = -1
        if requests and isinstance(requests[-1], dict):
            hand = requests[-1].get("hand", -1)
        if self._opp_model is None or hand != self._opp_model_for_hand:
            self._opp_model = build_opponent_model(requests or [], my_id)
            self._opp_model_for_hand = hand
        return self._opp_model

    # ------------------------------------------------------------------
    def _n_sim(self, st: GameState) -> int:
        if st.betting_round == 0:
            n = self.cfg.n_sim_preflop
        else:
            n = self.cfg.n_sim_postflop
        return min(n, self.cfg.n_sim_cap)

    def _equity(self, st: GameState) -> float:
        win, tie = estimate_equity(
            st.my_cards, st.public_cards,
            n_sim=self._n_sim(st), rng=self.rng, opponents=1)
        return win + 0.5 * tie

    def _equity_ranged(self, st: GameState, opp_pfr: float = 0.5,
                        confidence: float = 0.0) -> float:
        """Range-restricted equity for when the opponent has shown strength.

        ``opp_pfr`` and ``confidence`` calibrate how tight the sampling
        filter is: a low-pfr / high-confidence opponent gets a tighter
        range (higher ``min_opp_strength``).
        """
        # Map pfr -> min_opp_strength bucket. pfr 0.15 -> very tight (~5);
        # pfr 0.5 -> ~3; pfr 0.9 -> ~1.
        pfr_eff = (1 - confidence) * 0.5 + confidence * opp_pfr
        pfr_eff = max(0.1, min(0.9, pfr_eff))
        # Higher pfr -> lower threshold (looser range).
        min_strength = max(1, int(round(6 - 5 * pfr_eff)))
        win, tie = estimate_equity_ranged(
            st.my_cards, st.public_cards,
            n_sim=self._n_sim(st), rng=self.rng,
            min_opp_strength=min_strength)
        return win + 0.5 * tie

    # ------------------------------------------------------------------
    def decide(self, st: GameState, requests: Optional[list] = None) -> int:
        """Return a legal action integer.

        ``>0`` raise-to-total, ``0`` call/check, ``-1`` fold, ``-2`` all-in.

        ``requests`` is the cumulative request list (used to build the
        opponent model). If omitted, the opponent model defaults to the
        empty/no-confidence prior.
        """
        cfg = self.cfg

        # Build opponent model (cached per hand).
        opp = self._build_opp_model(requests or [], st.my_id)
        self._opp = opp  # expose to helper methods

        # Terminal / no-decision states.
        if st.i_folded or st.my_allin:
            return 0

        # If opponent is already all-in, we either call or fold.
        if st.opp_allin:
            eq = self._equity(st)
            if st.to_call <= 0:
                return 0
            pot_after = st.pot + st.to_call
            # Vs value-heavy / tight opponents, an all-in is value-weighted.
            ce = self._call_edge(opp)
            if eq * pot_after >= st.to_call * ce:
                return 0
            return -1

        # ------------------------------------------------------------------
        # Combo-weighted equity: pinpoint the opponent's range from the
        # street/action history (mirrors claude_v279's combo-weighted MC).
        # We narrow the opponent's range whenever they have shown aggression
        # (raised preflop, or bet/raised postflop). When we have the betting
        # lead (opponent hasn't acted yet, or only check/called), we keep
        # uniform equity so we don't under-value-bet weaker opponents.
        # ------------------------------------------------------------------
        facing_raise_preflop = (st.betting_round == 0 and st.to_call > 0
                                and self._opp_raised_this_hand(st))
        opp_bet_postflop = (st.betting_round >= 1 and st.to_call > 0
                            and self._opp_bet_this_round(st))
        raw_eq = self._equity(st)

        # Build the opponent range once per decision.
        history = st.raw.get("history", []) or []
        range_model = build_range_from_history(history, st.my_id)
        opp_showed_strength = (facing_raise_preflop or opp_bet_postflop)

        if opp_showed_strength and not range_model.is_unconstrained():
            # Combo-weighted equity.
            win, tie = estimate_equity_ranged(
                st.my_cards, st.public_cards,
                n_sim=self._n_sim(st), rng=self.rng,
                range_model=range_model)
            eq = win + 0.5 * tie
        elif facing_raise_preflop:
            # Fallback to bucket-filter if range model somehow empty.
            eq = self._equity_ranged(st, opp.pfr, opp.confidence)
        else:
            eq = raw_eq

        pot = max(1, st.pot)
        to_call = st.to_call

        # ------------------------------------------------------------------
        # Preflop-specific filtering (fix the biggest leak: trash defense).
        # ------------------------------------------------------------------
        if st.betting_round == 0:
            action = self._preflop_decision(st, raw_eq, eq, opp, pot, to_call,
                                             facing_raise_preflop)
            if action is not None:
                return action

        # ------------------------------------------------------------------
        # Postflop / general decision tree (with implied odds + opp model).
        # ------------------------------------------------------------------
        if to_call == 0:
            return self._decide_no_cost(st, eq, pot, opp)

        # Facing a bet postflop: implied-odds scaled break-even.
        impl = self._implied_factor(st)
        break_even = to_call / (pot + to_call)
        ce = self._call_edge(opp)
        # Vs an opponent who barrels a lot (value-heavy), discount our raw
        # equity on the turn/river — our one-pair hands are behind their
        # value range more often than the uniform MC suggests.
        eq_eff = eq
        if opp.confidence >= 0.15 and st.betting_round >= 2:
            barrel = opp.barrel_freq
            vh = opp.value_heavy
            # Combine: up to 0.10 discount when opp barrels a lot AND is
            # value-heavy (we are likely beat on the turn/river with one pair).
            disc = 0.10 * max(0.0, barrel - 0.35) * vh * opp.confidence
            eq_eff = max(0.0, eq - disc)
        call_threshold = break_even * ce / impl

        # Slow-play monsters.
        if eq >= cfg.slowplay_threshold and self.rng.random() < cfg.slowplay_frequency:
            return 0

        # Value raise with strong hands.
        if eq >= cfg.raise_value_threshold:
            return self._value_raise(st, eq, pot, to_call)

        # Marginal hand: call if +EV with implied odds and opp-adjusted edge.
        # Use the discounted equity so we don't call down value-heavy barrels.
        if eq_eff >= call_threshold:
            # Semi-bluff raise occasionally with draws.
            if (eq >= cfg.semi_bluff_min_equity
                    and st.betting_round <= 2
                    and to_call <= pot * 0.6
                    and self.rng.random() < self._semibluff_freq(opp)):
                return self._value_raise(st, eq, pot, to_call,
                                         frac=cfg.bluff_raise_frac)
            return 0

        # Weak hand facing a bet: fold; occasionally bluff-raise vs tight.
        if (to_call <= BIG_BLIND
                and st.actions_this_round == 0
                and self.rng.random() < self._bluff_freq(opp)):
            return self._value_raise(st, eq, pot, to_call,
                                     frac=cfg.bluff_raise_frac)
        return -1

    # ------------------------------------------------------------------
    # Preflop decision (range-aware, fixes trash-defense leak).
    # ------------------------------------------------------------------
    def _preflop_decision(self, st: GameState, raw_eq: float, eq: float,
                          opp: OpponentModel, pot: int, to_call: int,
                          facing_raise: bool) -> Optional[int]:
        cfg = self.cfg
        cls = classify_preflop_hand(st.my_cards)
        rank = class_rank(cls)

        # --- We are the opener (no raise to face) ---
        if to_call <= SMALL_BLIND and not facing_raise:
            # SB open (or BB check after SB limp).
            if st.is_sb:
                sig = sb_open_action(st.my_cards, raw_eq)
                if sig == -1:
                    return -1
                if sig == 0:
                    return 0   # limp
                # raise: scale by class
                return self._preflop_open_raise(st, cls)
            # BB facing a limp: check or isolate-raise premiums.
            if rank >= _CLASS_RANK[STRONG]:
                return self._preflop_open_raise(st, cls, iso=True)
            return 0  # check

        # --- Facing a raise ---
        if facing_raise:
            raise_bb = st.round_bet / BIG_BLIND
            # Fold trash vs any raise.
            if cfg.preflop_fold_trash_vs_raise and rank <= _CLASS_RANK[TRASH]:
                # Even with pot-odds, trash has poor playability postflop.
                return -1
            # Fold marginal vs big raises (>= 4BB).
            if (cfg.preflop_fold_marginal_vs_big_raise
                    and rank <= _CLASS_RANK[MARGINAL]
                    and raise_bb >= 4.0):
                return -1
            # Premium / strong: 3-bet for value sometimes, else call.
            if rank >= _CLASS_RANK[PREMIUM]:
                # 3-bet premiums, but only some of the time to stay balanced.
                if self.rng.random() < 0.55:
                    return self._preflop_3bet(st, cls)
                # else fall through to call decision below
            elif rank >= _CLASS_RANK[STRONG] and self.rng.random() < 0.20:
                # Occasionally 3-bet strong hands as light 3-bets.
                return self._preflop_3bet(st, cls)
            # Call if discounted-equity pot-odds are favourable.
            break_even = to_call / (pot + to_call)
            if eq >= break_even * self._call_edge(opp):
                return 0
            return -1
        return None

    # ------------------------------------------------------------------
    # Preflop sizing helpers
    # ------------------------------------------------------------------
    def _preflop_open_raise(self, st: GameState, cls: str,
                            iso: bool = False) -> int:
        """SB open-raise or BB iso-raise sizing (raise-to-total)."""
        # Base sizing: 3BB for premium/strong, 2.5BB for playable.
        if cls == PREMIUM:
            target_bb = 4 if not iso else 5
        elif cls == STRONG:
            target_bb = 3
        else:
            target_bb = 2 if not iso else 3
        target = target_bb * BIG_BLIND
        target = max(target, st.min_raise_to)
        return self._legal_raise(st, target)

    def _preflop_3bet(self, st: GameState, cls: str) -> int:
        """3-bet sizing (raise-to-total) facing a preflop open."""
        # Standard ~3x the opponent's raise-to.
        target = st.round_bet * 3
        if cls == PREMIUM:
            target = int(st.round_bet * 3.2)
        target = max(target, st.min_raise_to)
        return self._legal_raise(st, target)

    # ------------------------------------------------------------------
    # Opponent-model-aware helpers
    # ------------------------------------------------------------------
    def _opp_raised_this_hand(self, st: GameState) -> bool:
        """True if the opponent has put in a preflop raise this hand."""
        for rec in st.raw.get("history", []):
            if rec.get("round", 0) != 0:
                continue
            if rec.get("player_id") != st.my_id and rec.get("action_type") == "raise":
                return True
        return False

    def _opp_bet_this_round(self, st: GameState) -> bool:
        """True if the opponent has bet/raised in the current betting round."""
        cur = st.betting_round
        for rec in st.raw.get("history", []):
            if rec.get("round", cur) != cur:
                continue
            if rec.get("player_id") != st.my_id and rec.get("action_type") in ("raise",):
                return True
        return False

    def _call_edge(self, opp: OpponentModel) -> float:
        """Adjust call tightness based on opponent tendencies.

        Only activates at high confidence (>=0.35) to avoid misjudging
        weak/passive bots early and value-cutting ourselves.
        """
        cfg = self.cfg
        if opp.confidence < 0.35:
            return cfg.call_edge
        vh = opp.value_heavy
        st_ = opp.is_calling_station
        edge = cfg.call_edge
        # Scale by (confidence-0.35)/(1-0.35) so the effect ramps gradually.
        ramp = (opp.confidence - 0.35) / 0.65
        edge += (cfg.call_edge_vs_value_heavy - cfg.call_edge) * vh * ramp
        edge += (cfg.call_edge_vs_station - cfg.call_edge) * st_ * ramp * 0.6
        return edge

    def _implied_factor(self, st: GameState) -> float:
        """Return the implied-odds multiplier for the current street."""
        cfg = self.cfg
        if st.betting_round == 1:
            return cfg.implied_odds_flop
        if st.betting_round == 2:
            return cfg.implied_odds_turn
        return cfg.implied_odds_river

    def _bluff_freq(self, opp: OpponentModel) -> float:
        cfg = self.cfg
        base = cfg.bluff_frequency
        if opp.confidence < 0.15:
            return base
        # Vs calling station, bluff much less.
        base *= (1.0 - (1 - cfg.bluff_freq_vs_station_mult) * opp.is_calling_station)
        # Vs tight folders (high fold_to_bet_river/turn), bluff more.
        fold_rate = (opp.fold_to_bet_river + opp.fold_to_bet_turn) / 2
        if fold_rate > 0.45:
            base *= cfg.bluff_freq_vs_tight_mult
        return max(0.0, min(0.5, base))

    def _semibluff_freq(self, opp: OpponentModel) -> float:
        # Semi-bluffs (with draws) are less opponent-dependent; reduce vs
        # stations only modestly.
        base = 0.18
        if opp.confidence >= 0.15:
            base *= (1.0 - 0.5 * opp.is_calling_station)
        return base

    # ------------------------------------------------------------------
    def _frac_for_equity(self, eq: float, override: Optional[float] = None) -> float:
        """Map equity to a bet/raise sizing fraction of the pot."""
        cfg = self.cfg
        if override is not None:
            return override
        base = cfg.bet_value_threshold
        span = max(1e-6, 1.0 - base)
        t = min(1.0, max(0.0, (eq - base) / span))
        return cfg.raise_value_frac_min + t * (
            cfg.raise_value_frac_max - cfg.raise_value_frac_min)

    def _bet_to_total(self, st: GameState, frac: float,
                      floor_to_min: bool = True) -> int:
        """Convert a pot-fraction desire into a raise-to-total integer.

        ``frac`` is a fraction of the *current pot*. The desired raise amount
        (delta over the current round bet we must match) is ``pot * frac``.
        The raise-to-total is ``round_bet + max(min_raise_delta, pot*frac)``.

        When ``floor_to_min`` is True we enforce the minimum-raise rule
        (raise-to >= ``st.min_raise_to``); set False for bluff lines where we
        prefer to fall back to a check if the sizing would be illegal.
        """
        desired_delta = max(1, int(round(st.pot * frac)))
        # raise-to-total must at least match the current round bet (call) plus
        # a raise delta, AND satisfy the minimum raise-to rule.
        target = max(st.round_bet + desired_delta, st.min_raise_to)
        if floor_to_min:
            return self._legal_raise(st, target)
        # Without floor: if target below min_raise, prefer check/call.
        if target < st.min_raise_to:
            return 0
        return self._legal_raise(st, target)

    # ------------------------------------------------------------------
    def _decide_no_cost(self, st: GameState, eq: float, pot: int,
                        opp: Optional[OpponentModel] = None) -> int:
        """Decision when ``to_call == 0`` (we may bet or check).

        Opponent-aware: vs calling stations we value-bet thinner; vs tight
        folders we bluff more.
        """
        cfg = self.cfg

        # Compute opp-adjusted thresholds.
        bet_value_thr = cfg.bet_value_threshold
        bet_thin_thr = cfg.bet_thin_value_threshold
        bet_bluff_freq = cfg.bet_bluff_frequency
        if opp is not None and opp.confidence >= 0.35:
            # Vs a calling station: value-bet thinner (they call with worse).
            thin_delta = cfg.bet_value_threshold_vs_station_delta * opp.is_calling_station
            bet_value_thr += thin_delta
            bet_thin_thr += thin_delta
            # Vs tight folder: bluff more.
            fold_rate = (opp.fold_to_bet_river + opp.fold_to_bet_turn) / 2
            if fold_rate > 0.45:
                bet_bluff_freq = min(0.45, bet_bluff_freq * cfg.bluff_freq_vs_tight_mult)
            # Vs calling station: bluff less.
            bet_bluff_freq *= (1.0 - 0.7 * opp.is_calling_station)

        # Slow-play monsters.
        if eq >= cfg.slowplay_threshold and self.rng.random() < cfg.slowplay_frequency:
            return 0

        # Full value bet.
        if eq >= bet_value_thr:
            frac = self._frac_for_equity(eq)
            return self._bet_to_total(st, frac)

        # Thin value bet.
        if eq >= bet_thin_thr:
            return self._bet_to_total(st, cfg.bet_thin_value_frac,
                                      floor_to_min=False)

        # Bluff (post-flop only).
        if (st.betting_round >= 1
                and self.rng.random() < bet_bluff_freq):
            return self._bet_to_total(st, cfg.bet_bluff_frac,
                                      floor_to_min=False)

        return 0  # check

    # ------------------------------------------------------------------
    def _value_raise(self, st: GameState, eq: float, pot: int,
                     to_call: int, frac: Optional[float] = None) -> int:
        """Return a legal raise-to-total for value when facing a bet."""
        f = self._frac_for_equity(eq, override=frac)
        return self._bet_to_total(st, f)

    # ------------------------------------------------------------------
    def _legal_raise(self, st: GameState, target: int) -> int:
        """Clamp a desired raise-to-total to a legal action.

        Returns one of: raise-to-total (>0), call/check (0), all-in (-2),
        fold (-1).

        Legal bounds:
        - target must be >= min_raise_to for a *raise*; otherwise it is a
          call.
        - target must be reachable with our remaining chips: the chips we
          need to put in are ``target - my_round_bet``; if that exceeds our
          stack, we go all-in (raise-to our stack-committed total).
        """
        # Chips needed to reach ``target`` this round.
        need = target - st.my_round_bet
        if need <= 0:
            return 0  # nothing to add -> check/call

        if need >= st.my_chips:
            # All-in: raise-to-total = my_round_bet + my_chips.
            # Engine treats all-in specially; returning -2 is the safe
            # canonical all-in action.
            if st.my_chips <= 0:
                return 0
            return -2

        # Ensure we still satisfy the minimum-raise rule. If ``target`` is
        # below min_raise_to, the action degenerates to a call.
        if target < st.min_raise_to:
            # Cannot legally raise; fall back to calling if +EV-ish.
            if st.to_call > 0:
                return 0
            return 0

        return int(target)


# ---------------------------------------------------------------------------
# Action sanitisation (defensive; the engine also validates, but we want to
# never emit an illegal value).
# ---------------------------------------------------------------------------

def sanitize_action(action: int, st: GameState) -> int:
    """Final safety net: guarantee the action is legal for ``st``.

    Rules implemented (mirror engine/judge.py behaviour):
    - If we have no decision to make (to_call == 0 and we cannot raise),
      return 0.
    - Fold stays -1.
    - All-in stays -2 if it makes sense (we have chips).
    - Raise (>0) is clamped to [min_raise_to, my_round_bet + my_chips].
      If the lower bound cannot be met (chips), becomes all-in (-2) or call.
    - Call/check is 0.
    """
    if st.i_folded or st.my_allin or st.my_chips <= 0:
        return 0 if st.to_call == 0 else 0  # no-op

    if action == -1:
        return -1
    if action == -2:
        return -2 if st.my_chips > 0 else 0
    if action == 0:
        return 0

    # Raise: clamp into legal range.
    target = int(action)
    min_legal = st.min_raise_to
    max_legal = st.my_round_bet + st.my_chips  # all-in ceiling

    if max_legal < min_legal:
        # Cannot make a legal raise with our stack: call / all-in.
        if st.to_call > 0:
            return -2 if st.my_chips > 0 else 0
        return 0

    if target < min_legal:
        # Not enough to raise: either call the bet or check.
        return 0
    if target > max_legal:
        # Raising all our chips: if that exactly equals max_legal, raise-to
        # is fine; otherwise convert to all-in (-2).
        if target == max_legal:
            return target
        return -2 if st.my_chips > 0 else 0
    return target


if __name__ == "__main__":  # pragma: no cover - manual sanity check
    from .state import reconstruct_state

    pol = Policy(PolicyConfig(seed=1))
    # AA preflop, SB facing 50 to call into pot 150.
    req = {"dealer_id": 0, "my_id": 0, "my_chips": 19950,
           "my_cards": [48, 49], "public_cards": [], "history": [],
           "hand": 0, "max_hand": 70, "total_win_chips": [0, 0]}
    st = reconstruct_state(req)
    a = pol.decide(st)
    print(f"AA preflop SB -> action {a} (expect a raise >0)")

    # Trash hand 72o (2c 3d = cards 3, 5) facing a pot-sized raise.
    req2 = {"dealer_id": 0, "my_id": 0, "my_chips": 19900,
            "my_cards": [3, 5], "public_cards": [],
            "history": [{"round": 0, "player_id": 1, "action": 200,
                         "action_type": "raise"}],
            "hand": 0, "max_hand": 70}
    st2 = reconstruct_state(req2)
    a2 = pol.decide(st2)
    print(f"72o facing raise -> action {a2} (expect fold -1 mostly)")

    # Flush draw on flop: Ah Kh + 2h 5h 9c -> 9 hearts flush already? no only 2 hearts hole + need board hearts
    # As Ks on Kc 7d 2h flop: strong top pair top kicker.
    req3 = {"dealer_id": 0, "my_id": 0, "my_chips": 19900,
            "my_cards": [50, 46],   # As Ks
            "public_cards": [51, 13, 0],  # Kc 5h 2h
            "history": [
                {"round": 0, "player_id": 0, "action": 0, "action_type": "call"},
                {"round": 0, "player_id": 1, "action": 0, "action_type": "check"},
                {"round": 1, "player_id": 1, "action": 100, "action_type": "raise"},
            ],
            "hand": 1, "max_hand": 70}
    st3 = reconstruct_state(req3)
    a3 = pol.decide(st3)
    print(f"AK top-pair flop facing bet -> action {a3} (expect call/raise)")
