"""Versioned hero-hand features missing from the legacy 48-d state vector."""
from __future__ import annotations

import itertools
from typing import Any


HAND_CONTEXT_SCHEMA = "hero_hand_context_v1"
HAND_CONTEXT_DIM = 18


def _clip(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _rank(card: int) -> int:
    return int(card) // 4 + 2


def _suit(card: int) -> int:
    return int(card) % 4


def _cards(raw: Any) -> list[int]:
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    seen = set()
    for value in raw:
        try:
            card = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if 0 <= card < 52 and card not in seen:
            seen.add(card)
            out.append(card)
    return out


def _score_five(cards: tuple[int, ...]) -> tuple[int, ...]:
    ranks = sorted((_rank(card) for card in cards), reverse=True)
    suits = [_suit(card) for card in cards]
    counts = {rank: ranks.count(rank) for rank in set(ranks)}
    groups = sorted(
        ((count, rank) for rank, count in counts.items()), reverse=True
    )
    unique = sorted(counts, reverse=True)
    straight_high = 0
    if len(unique) == 5:
        if unique[0] - unique[-1] == 4:
            straight_high = unique[0]
        elif set(unique) == {14, 2, 3, 4, 5}:
            straight_high = 5
    flush = len(set(suits)) == 1
    if flush and straight_high:
        return (8, straight_high)
    if groups[0][0] == 4:
        quad = groups[0][1]
        return (7, quad, max(rank for rank in ranks if rank != quad))
    if groups[0][0] == 3 and len(groups) > 1 and groups[1][0] == 2:
        return (6, groups[0][1], groups[1][1])
    if flush:
        return (5, *ranks)
    if straight_high:
        return (4, straight_high)
    if groups[0][0] == 3:
        trips = groups[0][1]
        return (3, trips, *(rank for rank in ranks if rank != trips))
    if groups[0][0] == 2 and len(groups) > 1 and groups[1][0] == 2:
        high_pair = max(groups[0][1], groups[1][1])
        low_pair = min(groups[0][1], groups[1][1])
        kicker = max(
            rank for rank in ranks if rank not in (high_pair, low_pair)
        )
        return (2, high_pair, low_pair, kicker)
    if groups[0][0] == 2:
        pair = groups[0][1]
        return (1, pair, *(rank for rank in ranks if rank != pair))
    return (0, *ranks)


def _best_score_and_hole_use(
    hole_cards: list[int], public_cards: list[int]
) -> tuple[tuple[int, ...], int]:
    cards = hole_cards + public_cards
    if len(cards) < 5:
        return (0, 0), 0
    hole_set = set(hole_cards)
    best_score: tuple[int, ...] | None = None
    best_hole_use = 0
    for combo in itertools.combinations(cards, 5):
        score = _score_five(combo)
        hole_use = sum(card in hole_set for card in combo)
        if (
            best_score is None
            or score > best_score
            or (score == best_score and hole_use > best_hole_use)
        ):
            best_score = score
            best_hole_use = hole_use
    return best_score or (0, 0), best_hole_use


def _straight_features(
    hole_cards: list[int], public_cards: list[int]
) -> tuple[float, float, float]:
    all_ranks = {_rank(card) for card in hole_cards + public_cards}
    hole_ranks = {_rank(card) for card in hole_cards}
    if 14 in all_ranks:
        all_ranks.add(1)
    if 14 in hole_ranks:
        hole_ranks.add(1)
    best_count = 0
    best_hole = 0
    for start in range(1, 11):
        window = set(range(start, start + 5))
        count = len(all_ranks & window)
        hole_count = len(hole_ranks & window)
        if count > best_count or (count == best_count and hole_count > best_hole):
            best_count = count
            best_hole = hole_count
    return _clip(best_count / 5.0), float(best_count >= 4), _clip(best_hole / 2.0)


def encode_hand_context(request: dict[str, Any]) -> list[float]:
    """Encode made-hand and draw relations without depending on suit identity."""
    hole_cards = _cards(request.get("my_cards"))[:2]
    public_cards = _cards(request.get("public_cards"))[:5]
    if len(hole_cards) < 2:
        return [0.0] * HAND_CONTEXT_DIM

    hole_ranks = [_rank(card) for card in hole_cards]
    board_ranks = [_rank(card) for card in public_cards]
    board_suits = [_suit(card) for card in public_cards]
    made, hole_use = _best_score_and_hole_use(hole_cards, public_cards)

    suit_rows = []
    for suit in range(4):
        all_suited = [
            card for card in hole_cards + public_cards if _suit(card) == suit
        ]
        hole_suited = [card for card in hole_cards if _suit(card) == suit]
        high_hole = max((_rank(card) for card in hole_suited), default=0)
        suit_rows.append((len(all_suited), len(hole_suited), high_hole))
    best_suit_count, hole_best_suit, _ = max(suit_rows)

    board_rank_set = set(board_ranks)
    rank_matches = sum(rank in board_rank_set for rank in hole_ranks)
    max_board_rank = max(board_ranks, default=2)
    overcards = sum(rank > max_board_rank for rank in hole_ranks)
    board_rank_max = max(
        (board_ranks.count(rank) for rank in board_rank_set), default=0
    )
    board_suit_max = max(
        (board_suits.count(suit) for suit in set(board_suits)), default=0
    )
    straight_density, straight_draw, straight_hole = _straight_features(
        hole_cards, public_cards
    )
    category = int(made[0])
    primary_rank = int(made[1]) if len(made) > 1 else 0
    features = [
        _clip(len(public_cards) / 5.0),
        _clip(category / 8.0),
        _clip(primary_rank / 14.0),
        _clip(hole_use / 2.0),
        _clip(rank_matches / 2.0),
        _clip(overcards / 2.0),
        float(category >= 1),
        float(category >= 2),
        float(category >= 3),
        _clip(best_suit_count / 5.0),
        _clip(hole_best_suit / 2.0),
        float(best_suit_count == 4 and hole_best_suit > 0),
        float(best_suit_count == 3 and hole_best_suit > 0),
        straight_density,
        straight_draw,
        straight_hole,
        _clip(board_rank_max / 3.0),
        _clip(board_suit_max / 3.0),
    ]
    if len(features) != HAND_CONTEXT_DIM:
        raise RuntimeError("unexpected hand-context feature dimension")
    return features
