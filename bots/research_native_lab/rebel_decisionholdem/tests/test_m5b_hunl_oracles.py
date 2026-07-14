from __future__ import annotations

import numpy as np
import pytest

from ...common_contracts.actions import Action, ActionKind
from ...common_contracts.national_state import NationalGameState
from ..rebel_like.hunl_pbs import (
    HUNL_COMBO_COUNT,
    HUNLReachFactorPublicBeliefState,
    build_public_action_support,
)
from ..rebel_like.m5b_search import ReachFactors, abstract_actions


HOLES = ((48, 49), (44, 45))  # AA versus KK in Common card encoding.
FLOP = (0, 5, 10)
TURN = (15,)
RIVER = (20,)


def _advance(state: NationalGameState, *actions: Action) -> NationalGameState:
    for action in actions:
        state = state.apply_action(action)
    return state


def _to_flop(state: NationalGameState) -> NationalGameState:
    state = _advance(
        state,
        Action(ActionKind.CALL),
        Action(ActionKind.CHECK),
    )
    return state.apply_chance(FLOP)


def _close_checked_street(state: NationalGameState) -> NationalGameState:
    return _advance(
        state,
        Action(ActionKind.CHECK),
        Action(ActionKind.CALL),
    )


def _to_turn(state: NationalGameState) -> NationalGameState:
    state = _to_flop(state)
    state = _close_checked_street(state)
    return state.apply_chance(TURN)


def _to_river(state: NationalGameState) -> NationalGameState:
    state = _to_turn(state)
    state = _close_checked_street(state)
    return state.apply_chance(RIVER)


def test_physical_combo_fold_micro_oracle() -> None:
    state = NationalGameState.new_hand(1, small_blind=0, hole_cards=HOLES)
    terminal = state.apply_action(Action(ActionKind.FOLD))
    assert terminal.terminal_utility() == (-50, 50)


def test_physical_combo_complete_showdown_micro_oracle() -> None:
    state = _to_river(
        NationalGameState.new_hand(1, small_blind=0, hole_cards=HOLES)
    )
    terminal = _close_checked_street(state)
    assert terminal.is_terminal
    assert terminal.terminal_utility() == (100, -100)


def test_physical_combo_river_one_decision_micro_oracle() -> None:
    state = _to_river(
        NationalGameState.new_hand(1, small_blind=0, hole_cards=HOLES)
    )
    one_decision = state.apply_action(Action(ActionKind.CHECK))
    assert one_decision.actor == 0
    assert one_decision.legal_actions().call
    terminal = one_decision.apply_action(Action(ActionKind.CALL))
    assert terminal.terminal_utility() == (100, -100)


def test_physical_combo_turn_allin_runout_micro_oracle() -> None:
    state = _to_turn(
        NationalGameState.new_hand(1, small_blind=0, hole_cards=HOLES)
    )
    state = state.apply_action(Action(ActionKind.ALLIN))
    state = state.apply_action(Action(ActionKind.CALL))
    assert state.chance_pending and state.runout_pending
    terminal = state.apply_chance(RIVER)
    assert terminal.is_terminal
    assert terminal.terminal_utility() == (20000, -20000)


def _combo_policy(matrix: np.ndarray, support) -> list[dict[str, float]]:
    active_slots = [
        slot for slot, wire in enumerate(abstract_actions(_ROOT).wires) if wire is not None
    ]
    wires = [abstract_actions(_ROOT).wires[slot] for slot in active_slots]
    assert tuple(wires) == support.action_wires
    return [
        {wire: float(matrix[row, slot]) for wire, slot in zip(wires, active_slots, strict=True)}
        for row in range(HUNL_COMBO_COUNT)
    ]


_ROOT = NationalGameState.new_hand(1, small_blind=0)


def test_actor_bayes_matches_frozen_m5a_pbs_oracle() -> None:
    state = _ROOT
    pbs = HUNLReachFactorPublicBeliefState.from_state(state)
    actions = abstract_actions(state)
    mask = actions.mask
    matrix = np.zeros((HUNL_COMBO_COUNT, 9), dtype=np.float64)
    strength = np.linspace(0.1, 0.9, HUNL_COMBO_COUNT)
    active = np.flatnonzero(mask)
    for slot in active:
        matrix[:, slot] = 1.0
    call_slot = actions.slot_for(Action(ActionKind.CALL))
    matrix[:, call_slot] = strength * len(active)
    matrix[:, active] /= matrix[:, active].sum(axis=1, keepdims=True)
    base = tuple(action for action in actions.slot_actions if action is not None)
    support = build_public_action_support(
        state, base, observed_action=Action(ActionKind.CALL)
    )
    rows = _combo_policy(matrix, support)
    oracle = pbs.observe_action(
        state, support, rows, belief_policy_kind="current_policy"
    )
    fast = ReachFactors.from_pbs(pbs).observe_action(0, matrix[:, call_slot])
    assert np.asarray(oracle.reach_factors) == pytest.approx(fast.factors, abs=1e-14)
    assert np.asarray(oracle.projected_marginal(0)) == pytest.approx(
        fast.projected_marginal(0), abs=1e-14
    )
    assert np.asarray(oracle.projected_marginal(1)) == pytest.approx(
        fast.projected_marginal(1), abs=1e-14
    )


def _observe_uniform(pbs, state, action):
    actions = abstract_actions(state)
    base = tuple(candidate for candidate in actions.slot_actions if candidate is not None)
    support = build_public_action_support(state, base, observed_action=action)
    probability = 1.0 / len(support.action_wires)
    rows = [
        {wire: probability for wire in support.action_wires}
        for _ in range(HUNL_COMBO_COUNT)
    ]
    return pbs.observe_action(
        state, support, rows, belief_policy_kind="fixed_profile"
    )


def test_public_chance_conditioning_matches_frozen_m5a_pbs_oracle() -> None:
    state = _ROOT
    pbs = HUNLReachFactorPublicBeliefState.from_state(state)
    call = Action(ActionKind.CALL)
    pbs = _observe_uniform(pbs, state, call)
    state = state.apply_action(call)
    check = Action(ActionKind.CHECK)
    pbs = _observe_uniform(pbs, state, check)
    state = state.apply_action(check)
    assert state.chance_pending
    next_state = state.apply_chance(FLOP)
    oracle = pbs.observe_public_chance(state, next_state)
    fast = ReachFactors.from_pbs(pbs).observe_public_cards(next_state.board)
    assert np.asarray(oracle.reach_factors) == pytest.approx(fast.factors, abs=1e-14)
    blocked = np.logical_not(np.asarray(oracle.legal_mask()))
    assert not np.any(fast.factors[:, blocked])


def test_zero_action_evidence_fails_closed() -> None:
    pbs = HUNLReachFactorPublicBeliefState.from_state(_ROOT)
    fast = ReachFactors.from_pbs(pbs)
    with pytest.raises(ValueError, match="zero evidence"):
        fast.observe_action(0, np.zeros(HUNL_COMBO_COUNT))

