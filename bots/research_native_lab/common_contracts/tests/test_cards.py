from __future__ import annotations

import random

from sever.engine.deck import Card
from sever.engine.evaluator import compare_hands as sever_compare_hands

from bots.research_native_lab.common_contracts.cards import (
    all_hole_combinations,
    card_to_wire,
    combo_index,
    compare_hands,
    int_to_tcp_card,
    legal_combo_mask,
    parse_cards_exact,
    tcp_card_to_int,
)


def _sever_card(card: int) -> Card:
    suit, rank = int_to_tcp_card(card)
    return Card(suit, rank)


def test_all_card_mappings_round_trip() -> None:
    wires = set()
    for card in range(52):
        suit, rank = int_to_tcp_card(card)
        assert tcp_card_to_int(suit, rank) == card
        wire = card_to_wire(card)
        assert parse_cards_exact(wire, expected=1) == (card,)
        wires.add(wire)
    assert len(wires) == 52


def test_hole_combination_contract_has_1326_stable_entries() -> None:
    combinations = all_hole_combinations()
    assert len(combinations) == 1326
    assert combinations[0] == (0, 1)
    assert combinations[-1] == (50, 51)
    assert combo_index((51, 50)) == 1325
    mask = legal_combo_mask((0, 51))
    assert len(mask) == 1326
    assert sum(mask) == 1225  # C(50, 2)


def test_hand_evaluator_matches_server_on_random_seven_card_pairs() -> None:
    rng = random.Random(2026071201)
    deck = list(range(52))
    for _ in range(500):
        cards = rng.sample(deck, 9)
        board = cards[:5]
        first = cards[5:7] + board
        second = cards[7:9] + board
        expected = sever_compare_hands(
            [_sever_card(card) for card in first],
            [_sever_card(card) for card in second],
        )
        assert compare_hands(first, second) == expected
