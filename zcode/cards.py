"""Card representation and 5-card hand evaluation.

Independent implementation (no dependency on engine/judge.py or any existing
bot module). Cards are integers 0..51 with the same encoding as the local
engine:

    rank = card // 4 + 2     # 2..14, Ace = 14
    suit = card % 4          # 0=Heart, 1=Diamond, 2=Spade, 3=Club

A poker hand is ranked by a single integer ``score`` whose decimal groups are
``[category][rank1][rank2][rank3][rank4][rank5]`` padded so that lexical
comparison of the integer equals hand-strength comparison.
"""

from __future__ import annotations

import random
from typing import Iterable, List, Tuple

# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------

RANK_BY_VAL = {2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8",
               9: "9", 10: "T", 11: "J", 12: "Q", 13: "K", 14: "A"}
SUIT_BY_VAL = {0: "h", 1: "d", 2: "s", 3: "c"}  # local engine encoding


def card_rank(card: int) -> int:
    return card // 4 + 2


def card_suit(card: int) -> int:
    return card % 4


def card_str(card: int) -> str:
    return RANK_BY_VAL[card_rank(card)] + SUIT_BY_VAL[card_suit(card)]


def full_deck() -> List[int]:
    return list(range(52))


# ---------------------------------------------------------------------------
# Hand evaluation
# ---------------------------------------------------------------------------
#
# Hand category codes (match common convention):
#   8 straight-flush, 7 four-of-a-kind, 6 full-house, 5 flush, 4 straight,
#   3 trips, 2 two-pair, 1 one-pair, 0 high-card.
#
# ``score`` packs category in the highest decimal digit followed by the
# tie-breaker ranks, so a larger integer always means a stronger hand.

_CAT_SHIFT = 10 ** 10  # category lives above 10 digits of tie-breakers


def _score_from(category: int, tiebreakers: Iterable[int]) -> int:
    tb = list(tiebreakers)
    while len(tb) < 5:
        tb.append(0)
    value = category * _CAT_SHIFT
    mult = 10 ** 8
    for r in tb[:5]:
        value += r * mult
        mult //= 100
    return value


def _rank_counts(ranks: List[int]) -> List[int]:
    counts = [0] * 15
    for r in ranks:
        counts[r] += 1
    return counts


def _straight_high(ranks: List[int]) -> int:
    """Return the high card rank of a straight in ``ranks`` or 0 if none.

    The wheel A-2-3-4-5 is recognised (high card = 5).
    """
    bit = 0
    for r in set(ranks):
        bit |= 1 << r
    # Ace can play low: add a "1" bit if ace present.
    if bit & (1 << 14):
        bit |= 1 << 1
    # Scan 5 consecutive bits from high to low; wheel high = 5.
    for high in range(14, 4, -1):
        mask = (0b11111 << (high - 4))
        if (bit & mask) == mask:
            return high
    return 0


def evaluate_best(cards: List[int]) -> int:
    """Return the score of the best 5-card hand from ``cards`` (len >= 5).

    Optimised: if there are exactly 5 cards we evaluate directly; otherwise
    we try flush / straight detection on the full set and fall back to a
    combination scan only when needed (keeps 6/7 card evaluation cheap).
    """
    n = len(cards)
    if n < 5:
        raise ValueError("need at least 5 cards to evaluate")

    ranks = [card_rank(c) for c in cards]
    suits = [card_suit(c) for c in cards]
    rank_counts = _rank_counts(ranks)

    suit_counts = [0, 0, 0, 0]
    suit_cards: List[List[int]] = [[], [], [], []]
    for c, s in zip(cards, suits):
        suit_counts[s] += 1
        suit_cards[s].append(c)

    flush_ranks: List[int] = []
    for s in range(4):
        if suit_counts[s] >= 5:
            flush_ranks = sorted((card_rank(c) for c in suit_cards[s]),
                                 reverse=True)[:5]
            break

    straight_high = _straight_high(ranks)

    # Straight flush (only possible if a flush suit is available).
    sf_high = 0
    for s in range(4):
        if suit_counts[s] >= 5:
            sf_high = _straight_high([card_rank(c) for c in suit_cards[s]])
            if sf_high:
                break

    if sf_high:
        return _score_from(8, [sf_high])

    # Four of a kind.
    quads = [r for r in range(14, 1, -1) if rank_counts[r] == 4]
    if quads:
        q = quads[0]
        kicker = max((r for r in ranks if r != q), default=0)
        return _score_from(7, [q, kicker])

    # Full house: best trips plus best pair/trips kicker.
    trips = [r for r in range(14, 1, -1) if rank_counts[r] == 3]
    if trips:
        t = trips[0]
        pair_candidates = ([r for r in range(14, 1, -1)
                            if r != t and rank_counts[r] >= 2])
        if pair_candidates:
            return _score_from(6, [t, pair_candidates[0]])

    # Flush.
    if flush_ranks:
        return _score_from(5, flush_ranks)

    # Straight.
    if straight_high:
        return _score_from(4, [straight_high])

    # Trips.
    if trips:
        t = trips[0]
        kickers = sorted((r for r in ranks if r != t), reverse=True)[:2]
        return _score_from(3, [t] + kickers)

    # Two pair / one pair.
    pairs = [r for r in range(14, 1, -1) if rank_counts[r] == 2]
    if len(pairs) >= 2:
        p1, p2 = pairs[0], pairs[1]
        kicker = max((r for r in ranks if r not in (p1, p2)), default=0)
        return _score_from(2, [p1, p2, kicker])
    if len(pairs) == 1:
        p = pairs[0]
        kickers = sorted((r for r in ranks if r != p), reverse=True)[:3]
        return _score_from(1, [p] + kickers)

    # High card.
    high = sorted(ranks, reverse=True)[:5]
    return _score_from(0, high)


def compare_hands(a: List[int], b: List[int]) -> int:
    """Return 1 if hand ``a`` beats ``b``, -1 if ``b`` beats ``a``, 0 tie."""
    sa, sb = evaluate_best(a), evaluate_best(b)
    if sa > sb:
        return 1
    if sa < sb:
        return -1
    return 0


# ---------------------------------------------------------------------------
# Small ad-hoc self-test (run with: python -m zcode.cards)
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover - manual sanity check
    # Royal flush: Ah Kh Qh Jh Th  -> cards 40,36,32,28,24
    rf = [40, 36, 32, 28, 24]
    # Four aces: Ah Ad As Ac Kd -> 40,41,42,43,37
    quads = [40, 41, 42, 43, 37]
    # Full house: Ah Ad As Kd Kh -> 40,41,42,37,33
    boat = [40, 41, 42, 37, 33]
    # Flush: Ah 8h 5h 3h 2h -> 40,12,4,0...
    # Ah=40, 3h=(3-2)*4+0=4, 2h=(2-2)*4+0=0, 8h=(8-2)*4+0=24
    flush = [40, 24, 4, 0, 16]  # Ah 8h 5h? recompute: 5h=(5-2)*4=12,2h=0,3h=4
    # Straight: 9-T-J-Q-K -> 9=(9-2)*4=28.. pick any suits
    straight = [28, 32, 36, 40, 44]  # mixed suits but distinct ranks
    samples = [("royal_flush", rf), ("quads", quads), ("full_house", boat),
               ("flush", flush), ("straight", straight)]
    prev = None
    for name, hand in samples:
        sc = evaluate_best(hand)
        print(f"{name}: {hand} -> score {sc}")
        if prev is not None:
            assert prev > sc, f"ordering wrong at {name}"
        prev = sc
    # Wheel straight: A-2-3-4-5
    wheel = [40, 0, 4, 8, 12]  # Ah 2x 3x 4x 5x (any suits)
    print("wheel straight high should be 5:", evaluate_best(wheel),
          "(category 4)")
    # Two different flushes
    h1 = [40, 36, 32, 28, 24]   # royal
    h2 = [40, 36, 32, 28, 16]   # A-K-Q-J-9 flush (lower)
    print("compare royal vs lower flush:", compare_hands(h1, h2), "(expect 1)")
    print("cards self-test OK")
