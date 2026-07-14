"""Common-authoritative HUNL sparse-blueprint decision adapter."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ...common_contracts.actions import Action, LegalActionSet
from ...common_contracts.national_state import NationalGameState
from .hunl_abstraction import (
    HUNLActionSpec,
    HUNLInformationAbstraction,
    abstract_actions,
    information_abstraction,
)
from .hunl_blueprint import HUNLBlueprint, HUNLPolicyLookup


@dataclass(frozen=True, slots=True)
class HUNLCommonDecision:
    action: Action
    action_spec: HUNLActionSpec
    abstraction: HUNLInformationAbstraction
    lookup: HUNLPolicyLookup
    legal_actions: LegalActionSet
    full_state_id: str
    hand_public_state_id: str
    information_state_id: str

    def assert_fresh(self, state: NationalGameState, *, hero: int = 0) -> None:
        if type(state) is not NationalGameState:
            raise TypeError("state must be the exact Common NationalGameState type")
        state.assert_invariants()
        if state.actor != hero:
            raise ValueError("bound HUNL decision is no longer the pending hero action")
        if (
            state.full_state_id() != self.full_state_id
            or state.hand_public_state_id() != self.hand_public_state_id
            or state.information_state_id(hero) != self.information_state_id
        ):
            raise ValueError("bound HUNL decision is stale for the current state")
        if not state.legal_actions().contains(self.action):
            raise ValueError("bound HUNL action is no longer Common-legal")


def choose_hunl_blueprint_action(
    blueprint: HUNLBlueprint,
    *,
    state: NationalGameState,
    random_unit: float,
    hero: int = 0,
) -> HUNLCommonDecision:
    """Choose only from the exact Common-derived action signature."""

    if type(blueprint) is not HUNLBlueprint:
        raise TypeError("blueprint must be the exact HUNLBlueprint type")
    if type(random_unit) not in (int, float):
        raise ValueError("random_unit must be numeric")
    draw = float(random_unit)
    if not math.isfinite(draw) or not 0.0 <= draw < 1.0:
        raise ValueError("random_unit must lie in [0, 1)")
    if type(state) is not NationalGameState:
        raise TypeError("state must be the exact Common NationalGameState type")
    state.assert_invariants()
    if state.actor != hero:
        raise ValueError("Common state does not contain a pending hero decision")
    specs = abstract_actions(state)
    abstraction = information_abstraction(state, hero)
    action_ids = tuple(spec.action_id for spec in specs)
    lookup = blueprint.lookup(abstraction, action_ids)
    if set(lookup.probabilities) != set(action_ids):
        raise ValueError("HUNL blueprint returned a partial or excess legal policy")
    total = sum(lookup.probabilities.values())
    if not math.isfinite(total) or abs(total - 1.0) > 1e-12:
        raise ValueError("HUNL blueprint policy mass is invalid")
    cumulative = 0.0
    selected = specs[-1]
    for spec in specs:
        probability = lookup.probabilities[spec.action_id]
        if not math.isfinite(probability) or probability < 0.0:
            raise ValueError("HUNL blueprint probability is invalid")
        cumulative += probability
        if draw < cumulative:
            selected = spec
            break
    legal = state.legal_actions()
    if not legal.contains(selected.action):
        raise AssertionError("HUNL abstraction emitted an action rejected by Common")
    decision = HUNLCommonDecision(
        action=selected.action,
        action_spec=selected,
        abstraction=abstraction,
        lookup=lookup,
        legal_actions=legal,
        full_state_id=state.full_state_id(),
        hand_public_state_id=state.hand_public_state_id(),
        information_state_id=state.information_state_id(hero),
    )
    decision.assert_fresh(state, hero=hero)
    return decision
