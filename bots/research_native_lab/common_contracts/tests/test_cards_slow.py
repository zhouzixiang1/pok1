from __future__ import annotations

import os
from collections import Counter
from itertools import combinations

import pytest

from bots.research_native_lab.common_contracts.cards import rank_five


def _card(rank: int, suit: int) -> int:
    return (rank - 2) * 4 + suit


def test_rank_five_wheel_and_straight_flush_fixtures() -> None:
    wheel = (
        _card(14, 0),
        _card(2, 1),
        _card(3, 2),
        _card(4, 3),
        _card(5, 0),
    )
    six_high = (
        _card(2, 0),
        _card(3, 1),
        _card(4, 2),
        _card(5, 3),
        _card(6, 0),
    )
    wheel_straight_flush = tuple(_card(rank, 2) for rank in (14, 2, 3, 4, 5))

    assert rank_five(wheel) == (4, 5)
    assert rank_five(six_high) == (4, 6)
    assert rank_five(six_high) > rank_five(wheel)
    assert rank_five(wheel_straight_flush) == (8, 5)


def test_rank_five_kickers_break_ties() -> None:
    pair_of_aces_jack_kicker = (
        _card(14, 0),
        _card(14, 1),
        _card(13, 2),
        _card(12, 3),
        _card(11, 0),
    )
    pair_of_aces_ten_kicker = (
        _card(14, 2),
        _card(14, 3),
        _card(13, 1),
        _card(12, 0),
        _card(10, 2),
    )

    assert rank_five(pair_of_aces_jack_kicker) == (1, 14, 13, 12, 11)
    assert rank_five(pair_of_aces_ten_kicker) == (1, 14, 13, 12, 10)
    assert rank_five(pair_of_aces_jack_kicker) > rank_five(
        pair_of_aces_ten_kicker
    )


def test_rank_five_flush_detection_and_suit_independence() -> None:
    ranks = (14, 13, 9, 5, 2)
    heart_flush = tuple(_card(rank, 0) for rank in ranks)
    diamond_flush = tuple(_card(rank, 1) for rank in ranks)
    mixed_suits = tuple(_card(rank, suit) for rank, suit in zip(ranks, (0, 1, 2, 3, 0)))

    assert rank_five(heart_flush) == (5, 14, 13, 9, 5, 2)
    assert rank_five(diamond_flush) == rank_five(heart_flush)
    assert rank_five(mixed_suits) == (0, 14, 13, 9, 5, 2)
    assert rank_five(heart_flush) > rank_five(mixed_suits)


def test_rank_five_rejects_invalid_shapes_and_duplicates() -> None:
    with pytest.raises(ValueError, match="five cards required"):
        rank_five((0, 1, 2, 3))
    with pytest.raises(ValueError, match="duplicate cards"):
        rank_five((0, 0, 1, 2, 3))


@pytest.mark.skipif(
    os.environ.get("POK_RUN_SLOW_M2") != "1",
    reason="set POK_RUN_SLOW_M2=1 to run the exhaustive M2 oracle",
)
def test_m2_all_five_card_hands_have_exact_category_counts() -> None:
    expected = {
        0: 1_302_540,
        1: 1_098_240,
        2: 123_552,
        3: 54_912,
        4: 10_200,
        5: 5_108,
        6: 3_744,
        7: 624,
        8: 40,
    }

    actual = Counter(rank_five(hand)[0] for hand in combinations(range(52), 5))

    assert sum(actual.values()) == 2_598_960
    assert dict(sorted(actual.items())) == expected
