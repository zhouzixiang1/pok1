from __future__ import annotations

import copy
import itertools

import pytest

from bots.research_native_lab.common_contracts.actions import Action, ActionKind
from bots.research_native_lab.common_contracts.national_state import NationalGameState
from bots.research_native_lab.neural_conformance.public_family import (
    canonical_board_under_suit_isomorphism,
    public_family_id,
    public_family_payload,
    public_family_payload_id,
    validate_public_family_payload,
)


def _permute_card(card: int, permutation: tuple[int, ...]) -> int:
    return (card // 4) * 4 + permutation[card % 4]


def _flop_state(
    *,
    board: tuple[int, int, int],
    holes: tuple[tuple[int, int], tuple[int, int]],
    hand_number: int = 1,
    match_net: tuple[int, int] = (0, 0),
) -> NationalGameState:
    state = NationalGameState.new_hand(
        hand_number,
        small_blind=0,
        hole_cards=holes,
        match_net_before=match_net,
    )
    state = state.apply_action(Action(ActionKind.CALL))
    state = state.apply_action(Action(ActionKind.CHECK))
    return state.apply_chance(board)


def test_board_canonicalization_is_exhaustively_suit_invariant() -> None:
    board = (0, 5, 10, 15, 20)
    canonical = canonical_board_under_suit_isomorphism(board)
    for permutation in itertools.permutations(range(4)):
        mapped = tuple(_permute_card(card, permutation) for card in board)
        assert canonical_board_under_suit_isomorphism(mapped) == canonical


def test_public_family_ignores_holes_match_context_and_flop_order() -> None:
    first = _flop_state(
        board=(8, 13, 18),
        holes=((48, 49), (0, 1)),
    )
    second = _flop_state(
        board=(18, 8, 13),
        holes=((44, 45), (4, 5)),
        hand_number=69,
        match_net=(2500, -2500),
    )
    assert public_family_id(first) == public_family_id(second)
    encoded = repr(public_family_payload(first))
    assert "hole_cards" not in encoded
    assert "hand_number" not in encoded
    assert "match_net_before" not in encoded


def test_public_family_is_suit_invariant_but_retains_turn_order_and_actions() -> None:
    base = _flop_state(
        board=(8, 13, 18),
        holes=((48, 49), (0, 1)),
    )
    permutation = (2, 0, 3, 1)
    mapped = _flop_state(
        board=tuple(_permute_card(card, permutation) for card in (8, 13, 18)),
        holes=((40, 41), (4, 5)),
    )
    assert public_family_id(base) == public_family_id(mapped)

    checked = base.apply_action(Action(ActionKind.CHECK))
    assert public_family_id(base) != public_family_id(checked)

    closed = checked.apply_action(Action(ActionKind.CALL))
    turn_a = closed.apply_chance((24,))
    turn_a = turn_a.apply_action(Action(ActionKind.CHECK))
    turn_a = turn_a.apply_action(Action(ActionKind.CALL))
    river_a = turn_a.apply_chance((29,))

    # Swapping the observed turn and river is not a suit relabeling.
    closed_b = checked.apply_action(Action(ActionKind.CALL))
    turn_b = closed_b.apply_chance((29,))
    turn_b = turn_b.apply_action(Action(ActionKind.CHECK))
    turn_b = turn_b.apply_action(Action(ActionKind.CALL))
    river_b = turn_b.apply_chance((24,))
    assert public_family_id(river_a) != public_family_id(river_b)


def test_public_family_rejects_terminal_and_bad_boards() -> None:
    terminal = NationalGameState.new_hand(1, small_blind=0).apply_action(
        Action(ActionKind.FOLD)
    )
    with pytest.raises(ValueError, match="terminal"):
        public_family_payload(terminal)
    with pytest.raises(ValueError, match="length"):
        canonical_board_under_suit_isomorphism((1,))
    with pytest.raises(ValueError, match="duplicate"):
        canonical_board_under_suit_isomorphism((1, 1, 2))


def test_public_family_rejects_every_bool_integer_alias() -> None:
    state = NationalGameState.new_hand(1, small_blind=0).apply_action(
        Action(ActionKind.RAISE, 200)
    )
    canonical = public_family_payload(state)
    mutations = []

    def alias(path: tuple[object, ...], value: object) -> None:
        payload = copy.deepcopy(canonical)
        cursor = payload
        for part in path[:-1]:
            cursor = cursor[part]
        cursor[path[-1]] = value
        mutations.append((path, payload))

    alias(("legal_action_support", "allin"), 1)
    alias(("legal_action_support", "call"), 1)
    alias(("legal_action_support", "check"), 0)
    alias(("legal_action_support", "fold"), 1)
    alias(("legal_action_support", "min_raise_to"), False)
    alias(("public_state", "small_blind"), False)
    alias(("public_state", "actor"), True)
    alias(("public_state", "stacks", 0), False)
    alias(("public_state", "total_contributions", 0), False)
    alias(("public_state", "street_bets", 0), False)
    alias(("public_state", "action_counts", 1), False)
    alias(("public_state", "allin_occurred"), 0)
    alias(("public_state", "chance_pending"), 0)
    alias(("public_state", "runout_pending"), 0)
    alias(("public_state", "street_actions", 0, "actor"), True)
    alias(("public_state", "street_actions", 0, "amount"), True)
    alias(("public_state", "hand_history", 0, "actor"), True)
    alias(("public_state", "hand_history", 0, "amount"), True)

    for path, payload in mutations:
        with pytest.raises(ValueError, match="exact (integer|boolean)"):
            validate_public_family_payload(payload)

    assert public_family_payload_id(canonical) == public_family_id(state)
