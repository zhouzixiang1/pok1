from __future__ import annotations

import math
import json

import pytest

from ...common_contracts.actions import Action, ActionKind
from ...common_contracts.cards import all_hole_combinations, parse_cards_exact
from ...common_contracts.national_state import NationalGameState
from ..decisionholdem_like.hunl_abstraction import (
    HUNL_ACTION_IDS,
    abstract_actions,
    all_preflop_classes,
    hand_abstraction,
    information_abstraction,
    parse_infoset_key,
    perfect_recall_signature,
    preflop_class,
)


def _raised_pot_flop() -> NationalGameState:
    holes = (
        parse_cards_exact("<0,12><1,11>", expected=2),
        parse_cards_exact("<2,7><3,7>", expected=2),
    )
    state = NationalGameState.new_hand(1, small_blind=0, hole_cards=holes)
    state = state.apply_action(Action(ActionKind.RAISE, 200))
    state = state.apply_action(Action(ActionKind.CALL))
    return state.apply_chance(parse_cards_exact("<0,10><1,9><2,8>", expected=3))


def test_all_1326_exact_combos_preserve_the_real_169_preflop_path() -> None:
    combos = all_hole_combinations()
    classes = {preflop_class(combo) for combo in combos}
    exact_indices = {hand_abstraction(combo, ()).exact_combo_index for combo in combos}
    assert len(combos) == len(exact_indices) == 1326
    assert classes == set(all_preflop_classes())
    assert len(classes) == 169


def test_postflop_bucket_is_card_order_and_suit_isomorphism_invariant() -> None:
    hole = parse_cards_exact("<0,12><1,11>", expected=2)
    board = parse_cards_exact("<0,10><1,9><2,8>", expected=3)
    permuted_hole = parse_cards_exact("<2,12><3,11>", expected=2)
    permuted_board = parse_cards_exact("<2,10><3,9><0,8>", expected=3)
    first = hand_abstraction(hole, board)
    reordered = hand_abstraction(tuple(reversed(hole)), tuple(reversed(board)))
    permuted = hand_abstraction(permuted_hole, permuted_board)
    assert first.bucket == reordered.bucket == permuted.bucket
    assert first.legal_opponent_combos == math.comb(47, 2)
    assert first.legal_opponent_combos == permuted.legal_opponent_combos
    assert first.board_connectivity == 3


def test_golden_made_hand_texture_and_card_removal_features() -> None:
    hole = parse_cards_exact("<0,12><0,11>", expected=2)
    flop = parse_cards_exact("<0,10><0,9><0,8>", expected=3)
    abstraction = hand_abstraction(hole, flop)
    assert abstraction.made_category == "straight_flush"
    assert abstraction.board_suit_texture == "three_flush"
    assert abstraction.suit_blockers == 2
    assert abstraction.rank_blockers == 0
    assert abstraction.legal_opponent_combos == math.comb(47, 2)
    assert hand_abstraction(hole, ()).legal_opponent_combos == math.comb(50, 2)


def test_common_bounds_own_raise_to_sizing_and_minimum_200_to_400() -> None:
    holes = (
        parse_cards_exact("<0,12><1,11>", expected=2),
        parse_cards_exact("<2,7><3,7>", expected=2),
    )
    state = NationalGameState.new_hand(1, small_blind=0, hole_cards=holes)
    actions = abstract_actions(state)
    by_id = {item.action_id: item.action for item in actions}
    assert by_id["exact_min"].to_wire() == "raise 200"
    assert by_id["pot"].to_wire() == "raise 300"
    assert by_id["one_half_pot"].to_wire() == "raise 400"
    assert "half_pot" not in by_id  # exact 0.5-pot collides with min and is deduped
    raised = state.apply_action(by_id["exact_min"])
    response = {item.action_id: item.action for item in abstract_actions(raised)}
    assert response["exact_min"].to_wire() == "raise 400"
    assert all(state.legal_actions().contains(item.action) for item in actions)
    assert len({item.wire_action for item in actions}) == len(actions)


def test_all_required_size_families_exist_when_the_common_interval_admits_them() -> None:
    state = _raised_pot_flop()
    actions = abstract_actions(state)
    by_id = {item.action_id: item.action.to_wire() for item in actions}
    assert tuple(action for action in HUNL_ACTION_IDS if action in by_id) == (
        "fold",
        "check_call",
        "exact_min",
        "half_pot",
        "pot",
        "one_half_pot",
        "allin",
    )
    assert by_id == {
        "fold": "fold",
        "check_call": "check",
        "exact_min": "raise 100",
        "half_pot": "raise 200",
        "pot": "raise 400",
        "one_half_pot": "raise 600",
        "allin": "allin",
    }
    assert all(state.legal_actions().contains(item.action) for item in actions)


def test_four_street_infosets_are_versioned_and_exclude_match_context() -> None:
    state = _raised_pot_flop()
    seen = []
    for cards in (
        parse_cards_exact("<3,6>", expected=1),
        parse_cards_exact("<0,5>", expected=1),
    ):
        seen.append(information_abstraction(state, state.actor).street)
        state = state.apply_action(Action(ActionKind.CHECK))
        state = state.apply_action(Action(ActionKind.CALL))
        state = state.apply_chance(cards)
    seen.append(information_abstraction(state, state.actor).street)
    assert seen == ["flop", "turn", "river"]

    holes = (
        parse_cards_exact("<0,12><1,11>", expected=2),
        parse_cards_exact("<2,7><3,7>", expected=2),
    )
    first = NationalGameState.new_hand(1, small_blind=0, hole_cards=holes)
    later = NationalGameState.new_hand(
        70,
        small_blind=0,
        hole_cards=holes,
        match_net_before=(1234, -1234),
    )
    assert information_abstraction(first, 0).key == information_abstraction(later, 0).key
    assert first.full_state_id() != later.full_state_id()
    with pytest.raises(ValueError, match="acting player"):
        information_abstraction(first, False)  # type: ignore[arg-type]


def test_infoset_parser_rejects_noncanonical_scalar_aliases() -> None:
    holes = (
        parse_cards_exact("<0,12><1,11>", expected=2),
        parse_cards_exact("<2,7><3,7>", expected=2),
    )
    state = NationalGameState.new_hand(1, small_blind=0, hole_cards=holes)
    abstraction = information_abstraction(state, 0)
    prefix, encoded = abstraction.key.split("|", 1)
    payload = json.loads(encoded)
    payload["pot"] = True
    corrupt = prefix + "|" + json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValueError, match="scalar bucket"):
        parse_infoset_key(corrupt)


def _passive_turn_state(
    holes: tuple[tuple[int, int], tuple[int, int]],
    flop: tuple[int, int, int],
    turn: tuple[int],
) -> NationalGameState:
    state = NationalGameState.new_hand(1, small_blind=0, hole_cards=holes)
    state = state.apply_action(Action(ActionKind.CALL))
    state = state.apply_action(Action(ActionKind.CHECK))
    state = state.apply_chance(flop)
    state = state.apply_action(Action(ActionKind.CHECK))
    state = state.apply_action(Action(ActionKind.CALL))
    return state.apply_chance(turn)


def _retired_imperfect_recall_projection(payload: dict[str, object]) -> str:
    retired = {
        key: value
        for key, value in payload.items()
        if key not in {"action_recall", "observation_recall", "version"}
    }
    return json.dumps(retired, sort_keys=True, separators=(",", ":"))


def test_fixed_card_collision_requires_previous_private_observation_recall() -> None:
    # Fixed seed 2026071423 produced this collision after 12 samples.  The
    # current turn bucket and every retired public bucket are identical, while
    # the player's preflop/flop abstract observations differ.
    first = _passive_turn_state(
        ((5, 46), (23, 9)),
        (2, 27, 50),
        (39,),
    )
    second = _passive_turn_state(
        ((51, 27), (28, 21)),
        (34, 47, 5),
        (1,),
    )
    first_info = information_abstraction(first, 1)
    second_info = information_abstraction(second, 1)
    first_payload = parse_infoset_key(first_info.key)
    second_payload = parse_infoset_key(second_info.key)
    assert _retired_imperfect_recall_projection(first_payload) == (
        _retired_imperfect_recall_projection(second_payload)
    )
    assert first_payload["card_bucket"] == second_payload["card_bucket"]
    assert first_payload["observation_recall"] != second_payload[
        "observation_recall"
    ]
    assert first_info.key != second_info.key
    assert perfect_recall_signature(first, 1)["observation_recall"] == (
        first_payload["observation_recall"]
    )


def test_raise_size_collision_requires_exact_abstract_action_recall() -> None:
    holes = (
        parse_cards_exact("<0,12><1,11>", expected=2),
        parse_cards_exact("<2,7><3,7>", expected=2),
    )
    root = NationalGameState.new_hand(1, small_blind=0, hole_cards=holes)
    after_min = root.apply_action(Action(ActionKind.RAISE, 200))
    after_pot = root.apply_action(Action(ActionKind.RAISE, 300))
    min_info = information_abstraction(after_min, 1)
    pot_info = information_abstraction(after_pot, 1)
    min_payload = parse_infoset_key(min_info.key)
    pot_payload = parse_infoset_key(pot_info.key)
    assert _retired_imperfect_recall_projection(min_payload) == (
        _retired_imperfect_recall_projection(pot_payload)
    )
    assert min_payload["observation_recall"] == pot_payload["observation_recall"]
    assert min_payload["action_recall"][-1] == {
        "abstract_action": "exact_min",
        "actor": "other",
        "street": "preflop",
        "wire_action": "raise 200",
    }
    assert pot_payload["action_recall"][-1] == {
        "abstract_action": "pot",
        "actor": "other",
        "street": "preflop",
        "wire_action": "raise 300",
    }
    assert min_info.key != pot_info.key


def test_boundary_inference_audit_metadata_does_not_change_the_infoset_key() -> None:
    holes = (
        parse_cards_exact("<0,12><1,11>", expected=2),
        parse_cards_exact("<2,7><3,7>", expected=2),
    )
    root = NationalGameState.new_hand(1, small_blind=0, hole_cards=holes)
    explicit = root.apply_action(
        Action(ActionKind.CALL), inferred_from_boundary=False
    )
    inferred = root.apply_action(
        Action(ActionKind.CALL), inferred_from_boundary=True
    )
    assert explicit.hand_public_state_id() == inferred.hand_public_state_id()
    assert explicit.information_state_id(1) == inferred.information_state_id(1)
    explicit_info = information_abstraction(explicit, 1)
    inferred_info = information_abstraction(inferred, 1)
    assert explicit_info.key == inferred_info.key
    assert explicit_info.action_recall == inferred_info.action_recall
    assert parse_infoset_key(explicit_info.key)["action_recall"][-1] == {
        "abstract_action": "check_call",
        "actor": "other",
        "street": "preflop",
        "wire_action": "call",
    }


def test_bounded_abstract_traversal_key_has_one_recall_signature() -> None:
    holes = (
        parse_cards_exact("<0,12><1,11>", expected=2),
        parse_cards_exact("<2,7><3,7>", expected=2),
    )
    pending = [(NationalGameState.new_hand(1, small_blind=0, hole_cards=holes), 0)]
    current_groups: dict[str, set[str]] = {}
    retired_groups: dict[str, set[str]] = {}
    while pending:
        state, depth = pending.pop()
        if state.is_terminal or state.chance_pending or depth > 3:
            continue
        info = information_abstraction(state, state.actor)
        payload = parse_infoset_key(info.key)
        signature = json.dumps(
            {
                "action_recall": payload["action_recall"],
                "observation_recall": payload["observation_recall"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        current_groups.setdefault(info.key, set()).add(signature)
        retired_groups.setdefault(
            _retired_imperfect_recall_projection(payload), set()
        ).add(signature)
        for spec in abstract_actions(state):
            if spec.action_id in {"fold", "allin"}:
                continue
            pending.append((state.apply_action(spec.action), depth + 1))
    assert all(len(signatures) == 1 for signatures in current_groups.values())
    assert any(len(signatures) > 1 for signatures in retired_groups.values())
