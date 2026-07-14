from __future__ import annotations

import copy
import dataclasses
import random

import pytest

from sever.engine.validator import validate_action as sever_validate_action

from bots.research_native_lab.common_contracts.actions import Action, ActionKind
from bots.research_native_lab.common_contracts.national_state import (
    IllegalActionError,
    NationalGameState,
    Street,
    StateInvariantError,
)


def _action_candidates(state: NationalGameState) -> list[Action]:
    result = [
        Action(ActionKind.FOLD),
        Action(ActionKind.CHECK),
        Action(ActionKind.CALL),
        Action(ActionKind.ALLIN),
    ]
    if state.actor is None:
        return result
    actor = state.actor
    own_bet = state.street_bets[actor]
    allin_total = own_bet + state.stacks[actor]
    values = {0, 1, 50, 99, 100, 199, 200, 399, 400, allin_total - 1, allin_total}
    legal = state.legal_actions()
    if legal.min_raise_to is not None:
        values.update(
            {
                legal.min_raise_to - 1,
                legal.min_raise_to,
                legal.max_raise_to,
                legal.max_raise_to + 1,
            }
        )
    result.extend(Action(ActionKind.RAISE, value) for value in sorted(values) if value >= 0)
    return result


def _assert_validator_differential(state: NationalGameState) -> None:
    projection = state.to_validator_state()
    for action in _action_candidates(state):
        ours = state.validate_action(action)[0]
        theirs = sever_validate_action(action.kind.value, action.amount, projection)[0]
        assert ours == theirs, (state.canonical_json(), action, ours, theirs)


def _checkdown_to_flop(state: NationalGameState, cards=(8, 13, 18)) -> NationalGameState:
    state = state.apply_action(Action(ActionKind.CALL))
    state = state.apply_action(Action(ActionKind.CHECK))
    assert state.chance_pending
    return state.apply_chance(cards)


def test_exact_raise_to_boundary_and_allin_keyword_rule() -> None:
    state = NationalGameState.new_hand(1, small_blind=0)
    _assert_validator_differential(state)
    state = state.apply_action(Action(ActionKind.RAISE, 200))
    _assert_validator_differential(state)
    assert not state.validate_action(Action(ActionKind.RAISE, 399))[0]
    assert state.validate_action(Action(ActionKind.RAISE, 400)) == (True, "")
    allin_total = state.street_bets[state.actor] + state.stacks[state.actor]
    assert not state.validate_action(Action(ActionKind.RAISE, allin_total))[0]
    assert state.validate_action(Action(ActionKind.ALLIN)) == (True, "")


def test_national_check_call_closing_semantics() -> None:
    state = NationalGameState.new_hand(1, small_blind=0)
    state = state.apply_action(Action(ActionKind.CALL))
    assert not state.validate_action(Action(ActionKind.CALL))[0]
    assert state.validate_action(Action(ActionKind.CHECK))[0]
    state = state.apply_action(Action(ActionKind.CHECK))
    state = state.apply_chance((8, 13, 18))
    assert state.street is Street.FLOP and state.actor == state.big_blind
    assert not state.validate_action(Action(ActionKind.CALL))[0]
    state = state.apply_action(Action(ActionKind.CHECK))
    assert not state.validate_action(Action(ActionKind.CHECK))[0]
    assert state.validate_action(Action(ActionKind.CALL))[0]
    state = state.apply_action(Action(ActionKind.CALL))
    assert state.chance_pending


def test_boundary_inference_is_limited_to_proven_closing_action() -> None:
    state = NationalGameState.new_hand(1, small_blind=0)
    state = state.apply_action(Action(ActionKind.CALL))
    state, record = state.infer_omitted_closing_action()
    assert record.actor == 1
    assert record.action.kind is ActionKind.CHECK
    assert record.inferred_from_boundary
    state = state.apply_chance((8, 13, 18))
    state = state.apply_action(Action(ActionKind.CHECK))
    state, record = state.infer_omitted_closing_action()
    assert record.action.kind is ActionKind.CALL
    assert state.chance_pending


def test_transport_inference_provenance_does_not_split_information_sets() -> None:
    base = NationalGameState.new_hand(1, small_blind=0).apply_action(Action(ActionKind.CALL))
    explicit = base.apply_action(Action(ActionKind.CHECK))
    inferred, record = base.infer_omitted_closing_action()
    assert record.inferred_from_boundary
    assert explicit.hand_public_state_id() == inferred.hand_public_state_id()
    assert explicit.information_state_id(0) == inferred.information_state_id(0)
    assert explicit.full_state_id() != inferred.full_state_id()


def test_allin_call_runs_out_without_another_decision() -> None:
    state = NationalGameState.new_hand(
        1,
        small_blind=0,
        hole_cards=((48, 49), (0, 1)),
    )
    state = state.apply_action(Action(ActionKind.ALLIN))
    assert state.actor == 1 and state.allin_occurred
    assert not state.validate_action(Action(ActionKind.ALLIN))[0]
    assert not state.validate_action(Action(ActionKind.RAISE, 200))[0]
    state = state.apply_action(Action(ActionKind.CALL))
    assert state.chance_pending and state.runout_pending and state.actor is None
    state = state.apply_chance((4, 8, 12))
    assert state.chance_pending and state.actor is None
    state = state.apply_chance((16,))
    state = state.apply_chance((20,))
    assert state.is_terminal and state.terminal_reason == "showdown"
    utility = state.terminal_utility()
    assert sum(utility) == 0


def test_fold_and_showdown_utilities_are_zero_sum() -> None:
    fold = NationalGameState.new_hand(1, small_blind=0).apply_action(Action(ActionKind.FOLD))
    assert fold.terminal_utility() == (-50, 50)

    state = NationalGameState.new_hand(
        2,
        small_blind=1,
        hole_cards=((48, 49), (0, 1)),
    )
    state = _checkdown_to_flop(state)
    for cards in ((24,), (28,)):
        state = state.apply_action(Action(ActionKind.CHECK))
        state = state.apply_action(Action(ActionKind.CALL))
        state = state.apply_chance(cards)
    state = state.apply_action(Action(ActionKind.CHECK))
    state = state.apply_action(Action(ActionKind.CALL))
    assert state.is_terminal
    assert sum(state.terminal_utility()) == 0


def test_state_round_trip_and_hash_are_canonical() -> None:
    state = NationalGameState.new_hand(7, small_blind=1, match_net_before=(325, -325))
    state = state.apply_action(Action(ActionKind.RAISE, 200))
    restored = NationalGameState.from_dict(state.to_dict())
    assert restored == state
    assert restored.hand_public_state_id() == state.hand_public_state_id()
    assert restored.information_state_id(0) == state.information_state_id(0)
    assert restored.full_state_id() == state.full_state_id()


def test_deserialization_replays_and_rejects_tampered_history() -> None:
    state = NationalGameState.new_hand(1, small_blind=0)
    state = _checkdown_to_flop(state)
    state = state.apply_action(Action(ActionKind.CHECK))
    payload = state.to_dict()
    payload["hand_history"][-1]["actor"] = 0
    with pytest.raises(StateInvariantError, match="suffix|actor order"):
        NationalGameState.from_dict(payload)

    payload = state.to_dict()
    payload["hand_history"][0]["kind"] = "raise"
    payload["hand_history"][0]["amount"] = 199
    with pytest.raises(StateInvariantError, match="illegal action"):
        NationalGameState.from_dict(payload)


def test_trusted_history_cannot_survive_replace_copy_or_in_place_tamper() -> None:
    state = NationalGameState.new_hand(1, small_blind=0)
    forged = dataclasses.replace(state, action_counts=(1, 0))
    clones = (forged, copy.copy(state), copy.deepcopy(state))
    for clone in clones:
        with pytest.raises(StateInvariantError, match="replay validation"):
            clone.validate_action(Action(ActionKind.RAISE, 100))
        with pytest.raises(StateInvariantError, match="replay validation"):
            clone.assert_invariants()

    tampered = NationalGameState.from_dict(state.to_dict())
    object.__setattr__(tampered, "action_counts", (1, 0))
    with pytest.raises(StateInvariantError, match="replay validation"):
        tampered.legal_actions()


def test_distinct_prior_street_lines_never_collapse_after_chance() -> None:
    base = _checkdown_to_flop(NationalGameState.new_hand(1, small_blind=0))

    # Line A: BB opens 100, SB calls.
    line_a = base.apply_action(Action(ActionKind.RAISE, 100))
    line_a = line_a.apply_action(Action(ActionKind.CALL))
    line_a = line_a.apply_chance((24,))

    # Line B: BB checks, SB opens 100, BB calls.
    line_b = base.apply_action(Action(ActionKind.CHECK))
    line_b = line_b.apply_action(Action(ActionKind.RAISE, 100))
    line_b = line_b.apply_action(Action(ActionKind.CALL))
    line_b = line_b.apply_chance((24,))

    assert line_a.street is line_b.street is Street.TURN
    assert line_a.board == line_b.board
    assert line_a.hand_history != line_b.hand_history
    assert line_a.hand_public_state_id() != line_b.hand_public_state_id()


def test_hand_infoset_id_excludes_match_controller_context() -> None:
    first = NationalGameState.new_hand(1, small_blind=0, match_net_before=(0, 0))
    late = NationalGameState.new_hand(69, small_blind=0, match_net_before=(2500, -2500))
    assert first.hand_public_state_id() == late.hand_public_state_id()
    assert first.information_state_id(0) == late.information_state_id(0)
    assert first.match_context_id() != late.match_context_id()
    assert first.observation_id(0) != late.observation_id(0)


def test_illegal_actions_are_rejected_before_sanitization() -> None:
    state = NationalGameState.new_hand(1, small_blind=0)
    with pytest.raises(IllegalActionError):
        state.apply_action(Action(ActionKind.CHECK))


def test_reachable_state_fuzz_matches_server_validator() -> None:
    rng = random.Random(2026071202)
    for hand in range(1, 151):
        state = NationalGameState.new_hand(((hand - 1) % 70) + 1, small_blind=hand % 2)
        for _step in range(120):
            state.assert_invariants()
            if state.is_terminal:
                break
            if state.chance_pending:
                needed = 3 if state.street is Street.PREFLOP else 1
                unused = [card for card in range(52) if card not in state.board]
                state = state.apply_chance(rng.sample(unused, needed))
                continue
            _assert_validator_differential(state)
            state = state.apply_action(rng.choice(state.legal_actions().representative_actions()))
        else:
            raise AssertionError("reachable game failed to terminate within safety bound")
