"""Canonical cards, protocol conversion, combinations and exact hand ranks."""

from __future__ import annotations

import re
from functools import lru_cache
from itertools import combinations
from typing import Iterable, Sequence


CARD_RE = re.compile(r"<(\d+),(\d+)>")

# Canonical integers follow engine/judge.py: rank * 4 + local suit, where ranks
# are 0=2 .. 12=A and local suits are Heart, Diamond, Spade, Club.
TCP_TO_LOCAL_SUIT = {0: 2, 1: 0, 2: 1, 3: 3}
LOCAL_TO_TCP_SUIT = {value: key for key, value in TCP_TO_LOCAL_SUIT.items()}


def validate_card(card: int) -> int:
    if not isinstance(card, int) or isinstance(card, bool) or not 0 <= card < 52:
        raise ValueError(f"card must be an integer in [0, 51], got {card!r}")
    return card


def tcp_card_to_int(suit: int, rank: int) -> int:
    if suit not in TCP_TO_LOCAL_SUIT or not 0 <= rank <= 12:
        raise ValueError(f"invalid TCP card <{suit},{rank}>")
    return rank * 4 + TCP_TO_LOCAL_SUIT[suit]


def int_to_tcp_card(card: int) -> tuple[int, int]:
    card = validate_card(card)
    return LOCAL_TO_TCP_SUIT[card % 4], card // 4


def card_to_wire(card: int) -> str:
    suit, rank = int_to_tcp_card(card)
    return f"<{suit},{rank}>"


def cards_to_wire(cards: Iterable[int]) -> str:
    return "".join(card_to_wire(card) for card in cards)


def parse_cards_exact(raw: str, expected: int | None = None) -> tuple[int, ...]:
    matches = list(CARD_RE.finditer(raw))
    if "".join(match.group(0) for match in matches) != raw:
        raise ValueError(f"invalid card sequence: {raw!r}")
    cards = tuple(tcp_card_to_int(int(m.group(1)), int(m.group(2))) for m in matches)
    if expected is not None and len(cards) != expected:
        raise ValueError(f"expected {expected} cards, got {len(cards)}")
    if len(set(cards)) != len(cards):
        raise ValueError("duplicate cards")
    return cards


@lru_cache(maxsize=1)
def all_hole_combinations() -> tuple[tuple[int, int], ...]:
    return tuple(combinations(range(52), 2))


@lru_cache(maxsize=1)
def _combo_index() -> dict[tuple[int, int], int]:
    return {combo: index for index, combo in enumerate(all_hole_combinations())}


def canonical_combo(cards: Sequence[int]) -> tuple[int, int]:
    if len(cards) != 2:
        raise ValueError("a private hand must contain exactly two cards")
    first, second = sorted(validate_card(card) for card in cards)
    if first == second:
        raise ValueError("a private hand cannot contain duplicate cards")
    return first, second


def combo_index(cards: Sequence[int]) -> int:
    return _combo_index()[canonical_combo(cards)]


def legal_combo_mask(excluded_cards: Iterable[int]) -> tuple[bool, ...]:
    excluded = {validate_card(card) for card in excluded_cards}
    return tuple(a not in excluded and b not in excluded for a, b in all_hole_combinations())


def _rank_five_normalized(cards: Sequence[int]) -> tuple[int, ...]:
    """Rank five already validated, distinct cards.

    This private fast path is shared by the public five-card oracle and the
    five-to-seven-card evaluator.  The returned tuple is ordered
    lexicographically from weakest to strongest category.
    """

    ranks = sorted((card // 4 + 2 for card in cards), reverse=True)
    suits = [card % 4 for card in cards]
    counts: dict[int, int] = {}
    for rank in ranks:
        counts[rank] = counts.get(rank, 0) + 1
    groups = sorted(((count, rank) for rank, count in counts.items()), reverse=True)

    unique = sorted(set(ranks), reverse=True)
    straight_high = 0
    if len(unique) == 5:
        if unique[0] - unique[-1] == 4:
            straight_high = unique[0]
        elif unique == [14, 5, 4, 3, 2]:
            straight_high = 5
    flush = len(set(suits)) == 1

    if flush and straight_high:
        return (8, straight_high)
    if groups[0][0] == 4:
        return (7, groups[0][1], groups[1][1])
    if [count for count, _ in groups] == [3, 2]:
        return (6, groups[0][1], groups[1][1])
    if flush:
        return (5, *ranks)
    if straight_high:
        return (4, straight_high)
    if groups[0][0] == 3:
        kickers = sorted((rank for count, rank in groups[1:] if count == 1), reverse=True)
        return (3, groups[0][1], *kickers)
    pairs = sorted((rank for count, rank in groups if count == 2), reverse=True)
    singles = sorted((rank for count, rank in groups if count == 1), reverse=True)
    if len(pairs) == 2:
        return (2, pairs[0], pairs[1], singles[0])
    if len(pairs) == 1:
        return (1, pairs[0], *singles)
    return (0, *ranks)


def rank_five(cards: Sequence[int]) -> tuple[int, ...]:
    """Return the exact rank tuple for one five-card poker hand.

    Category numbers run from ``0`` (high card) through ``8`` (straight
    flush).  Remaining tuple elements are category-specific tie breakers, so
    normal tuple comparison implements complete five-card hand comparison.
    """

    if len(cards) != 5:
        raise ValueError("five cards required")
    normalized = tuple(validate_card(card) for card in cards)
    if len(set(normalized)) != len(normalized):
        raise ValueError("duplicate cards in hand evaluation")
    return _rank_five_normalized(normalized)


def rank_seven(cards: Sequence[int]) -> tuple[int, ...]:
    if not 5 <= len(cards) <= 7:
        raise ValueError("hand evaluation requires five to seven cards")
    normalized = tuple(validate_card(card) for card in cards)
    if len(set(normalized)) != len(normalized):
        raise ValueError("duplicate cards in hand evaluation")
    return max(_rank_five_normalized(combo) for combo in combinations(normalized, 5))


def compare_hands(first: Sequence[int], second: Sequence[int]) -> int:
    rank_first = rank_seven(first)
    rank_second = rank_seven(second)
    return (rank_first > rank_second) - (rank_first < rank_second)
