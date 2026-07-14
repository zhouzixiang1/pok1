from __future__ import annotations

from dataclasses import replace
from itertools import product

import pytest

from ...common_contracts.actions import Action, ActionKind
from ...common_contracts.cards import (
    card_to_wire,
    parse_cards_exact,
)
from ...common_contracts.national_state import NationalGameState, StateInvariantError

from ..decisionholdem_like.a2_runtime import SparseBlueprint, preflop_class
from ..decisionholdem_like.blueprint import BlueprintTrainer
from ..decisionholdem_like.common_adapter import (
    COMMON_ADAPTER_VERSION,
    COMMON_CONTRACT_VERSION,
    action_context_from_common_state,
    audit_legal_specs_from_common_state,
    choose_blueprint_action_from_common_state,
    common_card_to_route_card,
    legal_specs_from_common_state,
)


def _blueprint(action_id: str) -> SparseBlueprint:
    trainer = BlueprintTrainer()
    trainer.train_to(2)
    payload = trainer.blueprint_payload()
    payload["policies"] = {
        key: {action_id: 1.0} for key in payload["policies"]
    }
    return SparseBlueprint(payload)


def _mixed_blueprint(probabilities: dict[str, float]) -> SparseBlueprint:
    trainer = BlueprintTrainer()
    trainer.train_to(2)
    payload = trainer.blueprint_payload()
    payload["policies"] = {
        key: dict(probabilities) for key in payload["policies"]
    }
    return SparseBlueprint(payload)


def _new_hand(*, small_blind: int = 0) -> NationalGameState:
    # Common canonical ids for As/Ks; the adapter must not reinterpret these as
    # A2's historical suit-major ids.
    cards = parse_cards_exact("<0,12><0,11>", expected=2)
    holes = (cards, ()) if small_blind == 0 else (cards, ())
    return NationalGameState.new_hand(1, small_blind=small_blind, hole_cards=holes)


def test_common_card_mapping_is_exact_for_all_52_protocol_cards() -> None:
    assert COMMON_CONTRACT_VERSION == "national-research-contract-v1"
    assert COMMON_ADAPTER_VERSION == "route-a2-common-adapter-v1"
    seen = set()
    for suit, rank in product(range(4), range(13)):
        common_card = parse_cards_exact(f"<{suit},{rank}>", expected=1)[0]
        assert card_to_wire(common_card) == f"<{suit},{rank}>"
        seen.add(common_card_to_route_card(common_card))
    assert seen == set(range(52))
    state = _new_hand()
    route_cards = tuple(common_card_to_route_card(card) for card in state.hole_cards[0])
    assert preflop_class(route_cards) == "AKs"


def test_initial_preflop_context_and_every_route_size_are_common_legal() -> None:
    state = _new_hand(small_blind=0)
    context = action_context_from_common_state(state)
    assert (context.street, context.pot, context.hero_bet, context.opponent_bet) == (
        "preflop",
        150,
        50,
        100,
    )
    specs = legal_specs_from_common_state(state)
    assert specs
    assert all(state.legal_actions().contains(Action.from_wire(spec.wire_action)) for spec in specs)
    minimum = next(spec for spec in specs if spec.action_id == "exact_min")
    assert minimum.wire_action == "raise 200"

    decision = choose_blueprint_action_from_common_state(
        _blueprint("exact_min"), state=state, random_unit=0.5
    )
    assert decision.action == Action(ActionKind.RAISE, 200)
    assert decision.legal_actions.contains(decision.action)
    decision.assert_fresh(state)


def test_postflop_peer_check_maps_to_common_call_not_route_check() -> None:
    state = _new_hand(small_blind=0)
    state = state.apply_action(Action(ActionKind.CALL))
    state = state.apply_action(Action(ActionKind.CHECK))
    state = state.apply_chance(parse_cards_exact("<1,0><2,3><3,7>", expected=3))
    assert state.actor == 1
    state = state.apply_action(Action(ActionKind.CHECK))
    assert state.actor == 0

    context = action_context_from_common_state(state)
    assert context.responding_to_check
    specs = legal_specs_from_common_state(state)
    pass_action = next(spec for spec in specs if spec.action_id == "check_call")
    assert pass_action.wire_action == "call"
    assert not state.legal_actions().contains(Action(ActionKind.CHECK))

    decision = choose_blueprint_action_from_common_state(
        _blueprint("check_call"), state=state, random_unit=0.5
    )
    assert decision.action == Action(ActionKind.CALL)


def test_adapter_rejects_wrong_actor_unknown_cards_and_noncanonical_override() -> None:
    state = _new_hand(small_blind=1)
    with pytest.raises(ValueError, match="pending hero"):
        action_context_from_common_state(state)

    no_cards = NationalGameState.new_hand(1, small_blind=0)
    with pytest.raises(ValueError, match="two known"):
        choose_blueprint_action_from_common_state(
            _blueprint("fold"), state=no_cards, random_unit=0.5
        )

    with pytest.raises(TypeError, match="exact frozen Common"):
        action_context_from_common_state(object())  # type: ignore[arg-type]

    # Even a byte-for-byte structural dataclass copy lacks Common's
    # instance/content-bound issuance capability.
    forged = replace(_new_hand())
    with pytest.raises(StateInvariantError, match="replay validation"):
        action_context_from_common_state(forged)


def test_common_decision_is_state_bound_and_legality_mismatch_fails_closed(
    monkeypatch,
) -> None:
    state = _new_hand()
    decision = choose_blueprint_action_from_common_state(
        _blueprint("check_call"), state=state, random_unit=0.5
    )
    advanced = state.apply_action(decision.action)
    with pytest.raises(ValueError, match="no longer|stale"):
        decision.assert_fresh(advanced)

    from ..decisionholdem_like import common_adapter
    from ..decisionholdem_like.a2_runtime import ActionSpec

    original = common_adapter.legal_action_specs
    monkeypatch.setattr(
        common_adapter,
        "legal_action_specs",
        lambda context: original(context) + (ActionSpec("check_call", "check"),),
    )
    audit = audit_legal_specs_from_common_state(state)
    assert tuple(spec.wire_action for spec in audit.rejected) == ("check",)
    with pytest.raises(ValueError, match="legality disagreement: check"):
        legal_specs_from_common_state(state)


def test_policy_projection_ignores_match_context_but_stale_guard_does_not() -> None:
    cards = parse_cards_exact("<0,12><0,11>", expected=2)
    first = NationalGameState.new_hand(
        1,
        small_blind=0,
        hole_cards=(cards, ()),
        match_net_before=(0, 0),
    )
    later = NationalGameState.new_hand(
        70,
        small_blind=0,
        hole_cards=(cards, ()),
        match_net_before=(12_345, -12_345),
    )
    assert first.hand_public_state_id() == later.hand_public_state_id()
    assert first.information_state_id(0) == later.information_state_id(0)
    assert first.full_state_id() != later.full_state_id()

    blueprint = _blueprint("check_call")
    first_decision = choose_blueprint_action_from_common_state(
        blueprint, state=first, random_unit=0.314
    )
    later_decision = choose_blueprint_action_from_common_state(
        blueprint, state=later, random_unit=0.314
    )
    assert first_decision.route_decision.lookup == later_decision.route_decision.lookup
    assert first_decision.action == later_decision.action
    with pytest.raises(ValueError, match="stale"):
        first_decision.assert_fresh(later)


@pytest.mark.parametrize(
    "blueprint",
    (
        pytest.param(_blueprint("exact_min"), id="all-mass-unavailable"),
        pytest.param(
            _mixed_blueprint({"check_call": 1.0 - 1e-16, "exact_min": 1e-16}),
            id="sub-tolerance-mass-unavailable",
        ),
    ),
)
def test_common_entry_never_hides_unavailable_policy_mass(blueprint) -> None:
    cards = parse_cards_exact("<0,12><0,11>", expected=2)
    state = NationalGameState.new_hand(
        1,
        small_blind=1,
        hole_cards=(cards, ()),
    )
    state = state.apply_action(Action(ActionKind.ALLIN))
    assert state.actor == 0 and state.allin_occurred
    with pytest.raises(ValueError, match="zero Common-legal|unavailable Common"):
        choose_blueprint_action_from_common_state(
            blueprint,
            state=state,
            random_unit=0.25,
        )


def test_coarse_leduc_projection_is_explicitly_blocked_from_common_entry() -> None:
    trainer = BlueprintTrainer()
    trainer.train_to(2)
    prototype = SparseBlueprint(trainer.blueprint_payload())
    with pytest.raises(ValueError, match="unavailable Common action"):
        choose_blueprint_action_from_common_state(
            prototype,
            state=_new_hand(),
            random_unit=0.5,
        )
