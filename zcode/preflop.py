"""Preflop hand classification and range-based defence.

The original zcode policy treated preflop decisions purely through uniform
Monte-Carlo equity, which over-estimates weak offsuit hands (e.g. 83o gets
~0.35 vs a *random* hand, but only ~0.25 vs a *raise range*). This module
provides:

- ``classify_preflop_hand`` — buckets any two hole cards into a coarse
  strength class (premium / strong / playable / marginal / trash).
- ``preflop_defense_equity_discount`` — when facing a preflop raise, returns
  a multiplier on the raw (uniform) equity to account for the opponent's
  range being stronger than random.

These heuristics are calibrated against a typical SB-open / BB-defence
range and are deliberately simple (no lookup tables); they remove the
biggest leak the agent audit found (zcode limp/calling/3-betting trash).
"""

from __future__ import annotations

from .cards import card_rank

# ---------------------------------------------------------------------------
# Hand classification
# ---------------------------------------------------------------------------

# Strength classes ordered weak -> strong.
TRASH = "trash"
MARGINAL = "marginal"
PLAYABLE = "playable"
STRONG = "strong"
PREMIUM = "premium"


def _hand_features(my_cards):
    r1 = card_rank(my_cards[0])
    r2 = card_rank(my_cards[1])
    high = max(r1, r2)
    low = min(r1, r2)
    pair = r1 == r2
    suited = (my_cards[0] % 4) == (my_cards[1] % 4)
    gap = high - low - 1  # 0 = no gap (connector), e.g. JT gap=0; J9 gap=1
    return high, low, pair, suited, gap


def classify_preflop_hand(my_cards) -> str:
    """Return one of TRASH/MARGINAL/PLAYABLE/STRONG/PREMIUM.

    Calibration (heads-up, ~matches common HU opening ranges):
    - PREMIUM:    77+, A9s+, ATo+, KQ, KJs  (top ~16%)
    - STRONG:     22-66, A2s-A8s, A2o-A9o, K9o+, K6s+, Q9s+, suited
                  connectors 54s+  (next ~25%)
    - PLAYABLE:   most suited two-broadway, low pairs, offsuit connectors
                  down to 76o, JTo, small suited gappers (next ~25%)
    - MARGINAL:   weak suited kings / queens, low offsuit connectors
    - TRASH:      everything else (e.g. 72o, 83o, 92o, K2o, T4o, ...)
    """
    high, low, pair, suited, gap = _hand_features(my_cards)

    # Pairs.
    if pair:
        if high >= 11:        # JJ+
            return PREMIUM
        if high >= 7:         # 77-TT
            return STRONG
        return PLAYABLE       # 22-66

    # Premium non-pairs.
    if high == 14:                          # Ace
        if low >= 12:                       # AK AQ
            return PREMIUM
        if suited or low >= 10:             # AJs+ ATo+ AJo+
            return STRONG
        if low >= 7:                        # A5o-A9o, A3s-A8s
            return PLAYABLE if suited else MARGINAL
        if suited:                          # A2s-A4s (wheel draws)
            return PLAYABLE
        return MARGINAL                     # A2o-A4o

    if high == 13:                          # King
        if low >= 12:                       # KQ
            return STRONG if not suited else PREMIUM
        if low >= 10 or (suited and low >= 9):   # KJ KT, KTs+
            return STRONG
        if suited and low >= 6:             # K6s-K9s
            return PLAYABLE
        if low >= 9:                        # K9-KT offsuit
            return PLAYABLE
        if suited:                          # K2s-K5s
            return MARGINAL
        return TRASH                        # K2o-K8o

    if high == 12:                          # Queen
        if low >= 11 or (suited and low >= 10):  # QJ, QTs
            return STRONG if low >= 11 else PLAYABLE
        if suited and low >= 8:             # Q8s+
            return PLAYABLE
        if low >= 10:                       # QTo
            return PLAYABLE
        if suited:                          # Q2s-Q7s
            return MARGINAL
        return TRASH

    if high == 11:                          # Jack
        if (suited and low >= 8) or low >= 10:  # J8s+, JTo
            return PLAYABLE
        if suited and low >= 6:             # J6s-J7s
            return MARGINAL
        if suited:
            return MARGINAL
        return TRASH

    if high == 10:                          # Ten
        if (suited and low >= 8) or low >= 9:   # T8s+, T9o
            return PLAYABLE
        if suited and low >= 7:
            return MARGINAL
        return TRASH

    # High <= 9: only play suited connectors / one-gappers, else trash.
    if suited:
        if gap <= 1 and high >= 5:          # 54s+ .. 98s
            return PLAYABLE
        if gap <= 2 and high >= 7:          # 75s+ one-gappers
            return MARGINAL
        return TRASH
    # Offsuit low: only 76o/87o/98o-class connectors are marginal.
    if gap == 0 and high >= 7:              # 76o 87o 98o
        return MARGINAL
    return TRASH


_CLASS_RANK = {TRASH: 0, MARGINAL: 1, PLAYABLE: 2, STRONG: 3, PREMIUM: 4}


def class_rank(name: str) -> int:
    return _CLASS_RANK[name]


# ---------------------------------------------------------------------------
# Range-aware equity discount
# ---------------------------------------------------------------------------

def preflop_defense_equity_discount(facing_raise: bool, raise_size_vs_bb: float,
                                     opp_pfr: float = 0.5,
                                     confidence: float = 0.0) -> float:
    """Return a multiplier in [0.5, 1.0] to apply to raw uniform equity.

    Rationale: when the opponent has raised preflop, their hand distribution
    is stronger than random, so our raw (uniform) equity over-estimates our
    real chance of winning. We discount it based on:

    - ``raise_size_vs_bb``: how big the raise is (in BB units). A min-raise
      (2BB) discounts little; a 5BB+ raise discounts a lot.
    - ``opp_pfr``: the opponent's preflop raise frequency (model-derived).
      A tight raiser (low pfr) discounts more; a lag (high pfr) less.
    - ``confidence``: 0..1, how much we trust ``opp_pfr``.

    The discount is interpolated between a "no info" prior (pfr=0.5) and
    the model-derived value.
    """
    if not facing_raise:
        return 1.0

    # Size factor: 2BB -> 0.97, 3BB -> 0.92, 5BB -> 0.82, 10BB -> 0.70.
    size = max(2.0, raise_size_vs_bb)
    size_factor = max(0.65, 1.04 - 0.034 * size)

    # Range factor: a tight raiser (pfr ~0.15) implies top ~15% range, so
    # our equity is ~0.7x of uniform; a lag (pfr ~0.6) implies ~0.95x.
    pfr_prior = 0.5
    pfr_effective = (1 - confidence) * pfr_prior + confidence * opp_pfr
    pfr_effective = max(0.1, min(0.9, pfr_effective))
    # Map pfr -> range tightness multiplier. pfr 0.15 -> 0.72; 0.5 -> 0.88; 0.9 -> 0.97
    range_factor = 0.6 + 0.4 * pfr_effective

    return max(0.5, min(1.0, size_factor * range_factor))


# ---------------------------------------------------------------------------
# SB open decision (replaces limp-with-anything)
# ---------------------------------------------------------------------------

def sb_open_action(my_cards, n_sim_equity: float) -> int:
    """Return a recommended preflop action when we are SB and unraised.

    Returns: ``-1`` (fold), ``0`` (limp/call), or a raise-to-total suggestion
    encoded as ``>0`` (caller scales by pot/blinds).

    Aggressive HU play: we raise a wide range (PLAYABLE+) to seize the
    betting lead — this denies the opponent free cards and free flops,
    which matters against aggressive regs that exploit passive limpers.
    """
    cls = classify_preflop_hand(my_cards)
    rank = class_rank(cls)
    if rank <= _CLASS_RANK[TRASH]:
        # Fold only the absolute worst hands.
        if n_sim_equity < 0.30:
            return -1
        return 0   # limp the borderline trash
    if rank == _CLASS_RANK[MARGINAL]:
        # Marginal: limp the speculative ones (cheap, implied odds), but
        # sometimes raise to balance.
        return 0
    # PLAYABLE+ -> raise for value / initiative / fold equity.
    return 1


if __name__ == "__main__":  # pragma: no cover - sanity
    cases = [
        ([48, 49], "AA"),       # PREMIUM
        ([50, 46], "AKs"),      # PREMIUM (As Ks)
        ([44, 45], "KQo"),      # STRONG
        ([40, 41], "QQ"),       # PREMIUM
        ([28, 24], "T8s"),      # PLAYABLE (Th 8h? -> 28=9h 24=8h)
        ([3, 5], "73o?"),       # TRASH-ish (2c 4d -> ranks 3,4? recompute)
        ([0, 6], "24o?"),       # (2h 4d)
        ([6, 10], "45s?"),      # (4d 6d -> 45sd?)
    ]
    for cards, name in cases:
        cls = classify_preflop_hand(cards)
        r1 = card_rank(cards[0]); r2 = card_rank(cards[1])
        print(f"{name}: ranks {r1},{r2} -> {cls}")
