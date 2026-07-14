"""Route-A2 strategy adapter for the frozen Common M0--M2 interfaces.

This module owns no poker rules.  It projects an already validated
``NationalGameState`` into A2's versioned abstraction and intersects A2 action
sizes with the authoritative ``LegalActionSet`` before policy sampling.  It is
the M3 integration seam; the provisional standalone TCP/export shell remains an
explicit M4 prototype and is not silently treated as Common-authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...common_contracts.actions import Action, LegalActionSet
from ...common_contracts.cards import int_to_tcp_card
from ...common_contracts.constants import CONTRACT_VERSION
from ...common_contracts.national_state import NationalGameState, Street
from .a2_runtime import (
    ActionContext,
    ActionSpec,
    BlueprintDecision,
    SparseBlueprint,
    choose_blueprint_action,
    legal_action_specs,
    tcp_card_id,
)


COMMON_CONTRACT_VERSION = CONTRACT_VERSION
COMMON_ADAPTER_VERSION = "route-a2-common-adapter-v1"


@dataclass(frozen=True, slots=True)
class CommonBlueprintDecision:
    action: Action
    route_decision: BlueprintDecision
    context: ActionContext
    legal_actions: LegalActionSet
    full_state_id: str
    hand_public_state_id: str
    information_state_id: str

    def assert_fresh(self, state: NationalGameState, *, hero: int = 0) -> None:
        """Reject delayed sends after any Common state transition or copy."""

        if type(state) is not NationalGameState:
            raise TypeError("state must be the exact frozen Common NationalGameState type")
        state.assert_invariants()
        if state.actor != hero:
            raise ValueError("bound Common decision is no longer the pending hero action")
        if (
            state.full_state_id() != self.full_state_id
            or state.hand_public_state_id() != self.hand_public_state_id
            or state.information_state_id(hero) != self.information_state_id
        ):
            raise ValueError("bound Common decision is stale for the current state")
        if not state.legal_actions().contains(self.action):
            raise ValueError("bound Common action is no longer legal")


@dataclass(frozen=True, slots=True)
class CommonLegalityAudit:
    accepted: tuple[ActionSpec, ...]
    rejected: tuple[ActionSpec, ...]


def common_card_to_route_card(card: int) -> int:
    """Convert Common's rank-major canonical card to A2's legacy projection."""

    suit, rank = int_to_tcp_card(card)
    return tcp_card_id(suit, rank)


def _stage_actions(state: NationalGameState) -> tuple[tuple[str, int | None], ...]:
    return tuple(
        (record.action.kind.value, record.action.amount)
        for record in state.street_actions
    )


def action_context_from_common_state(
    state: NationalGameState,
    *,
    hero: int = 0,
) -> ActionContext:
    """Project only public/action fields; Common remains the legality owner."""

    if type(state) is not NationalGameState:
        raise TypeError("state must be the exact frozen Common NationalGameState type")
    state.assert_invariants()
    if hero not in (0, 1):
        raise ValueError("hero must be player 0 or 1")
    if state.actor != hero:
        raise ValueError("Common state does not contain a pending hero decision")
    opponent = 1 - hero
    responding_to_check = bool(
        state.street is not Street.PREFLOP
        and state.street_actions
        and state.street_actions[-1].actor == opponent
        and state.street_actions[-1].action.to_wire() == "check"
    )
    return ActionContext(
        street=state.street.value,
        pot=state.pot,
        hero_bet=state.street_bets[hero],
        opponent_bet=state.street_bets[opponent],
        hero_chips=state.stacks[hero],
        is_small_blind=state.small_blind == hero,
        hero_action_count=state.action_counts[hero],
        stage_actions=_stage_actions(state),
        responding_to_check=responding_to_check,
        opponent_allin=state.allin_occurred,
    )


def audit_legal_specs_from_common_state(
    state: NationalGameState,
    *,
    hero: int = 0,
) -> CommonLegalityAudit:
    """Expose every route/Common mismatch; never sanitize it silently."""

    context = action_context_from_common_state(state, hero=hero)
    legal = state.legal_actions()
    accepted: list[ActionSpec] = []
    rejected: list[ActionSpec] = []
    for spec in legal_action_specs(context):
        try:
            action = Action.from_wire(spec.wire_action)
        except ValueError:
            rejected.append(spec)
            continue
        (accepted if legal.contains(action) else rejected).append(spec)
    return CommonLegalityAudit(tuple(accepted), tuple(rejected))


def legal_specs_from_common_state(
    state: NationalGameState,
    *,
    hero: int = 0,
) -> tuple[ActionSpec, ...]:
    """Fail closed unless every A2-generated candidate agrees with Common."""

    audit = audit_legal_specs_from_common_state(state, hero=hero)
    if audit.rejected:
        rendered = ",".join(spec.wire_action for spec in audit.rejected)
        raise ValueError(f"A2/Common legality disagreement: {rendered}")
    if not audit.accepted:
        raise RuntimeError("A2 abstraction has no action admitted by Common")
    return audit.accepted


def choose_blueprint_action_from_common_state(
    blueprint: SparseBlueprint,
    *,
    state: NationalGameState,
    random_unit: float,
    hero: int = 0,
) -> CommonBlueprintDecision:
    """Sample the blueprint only after Common-authoritative legal filtering."""

    context = action_context_from_common_state(state, hero=hero)
    cards = tuple(state.hole_cards[hero])
    if len(cards) != 2:
        raise ValueError("A2 decision requires exactly two known hero cards")
    route_private = tuple(common_card_to_route_card(card) for card in cards)
    route_board = tuple(common_card_to_route_card(card) for card in state.board)
    specs = legal_specs_from_common_state(state, hero=hero)
    route_decision = choose_blueprint_action(
        blueprint,
        context=context,
        private_cards=route_private,
        board=route_board,
        random_unit=random_unit,
        legal_specs_override=specs,
    )
    if route_decision.used_legality_fallback:
        raise ValueError("A2 policy has zero Common-legal probability mass")
    if route_decision.dropped_policy_mass > 0.0:
        raise ValueError(
            "A2 policy assigns probability mass to an unavailable Common action: "
            f"{route_decision.dropped_policy_mass}"
        )
    action = Action.from_wire(route_decision.action.wire_action)
    legal = state.legal_actions()
    if not legal.contains(action):
        raise AssertionError("Common-filtered A2 policy emitted an illegal action")
    decision = CommonBlueprintDecision(
        action,
        route_decision,
        context,
        legal,
        state.full_state_id(),
        state.hand_public_state_id(),
        state.information_state_id(hero),
    )
    decision.assert_fresh(state, hero=hero)
    return decision
