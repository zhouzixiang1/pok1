from __future__ import annotations

import copy
import hashlib
import json
import socket

import pytest

from ...common_contracts.actions import Action, ActionKind
from ...common_contracts.cards import parse_cards_exact
from ...common_contracts.national_state import NationalGameState
from ...common_contracts.protocol import ProtocolStateError
from ..decisionholdem_like.common_native_entry import CommonA2StrategyRuntime
from ..decisionholdem_like.hunl_abstraction import (
    abstract_actions,
    information_abstraction,
)
from ..decisionholdem_like.hunl_blueprint import (
    HUNL_FALLBACK_CONTRACT,
    HUNL_TRAINED_BACKOFF_LEVELS,
    HUNLBlueprint,
    build_hunl_blueprint_payload,
    trained_backoff_key,
)
from ..decisionholdem_like.hunl_common_adapter import choose_hunl_blueprint_action
from ..decisionholdem_like.hunl_external_sampling import (
    HUNLExternalSamplingLCFR,
    HUNLTrainingConfig,
    deterministic_deal,
)
from ..decisionholdem_like.resolving import CoinTossResolveGame


SOURCE_COMMIT = "59275e9bf63cfd03d66df9d8a232040586465e65"


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@pytest.fixture(scope="module")
def blueprint_payload() -> dict[str, object]:
    trainer = HUNLExternalSamplingLCFR(HUNLTrainingConfig(seed=421))
    trainer.train_to(1)
    return build_hunl_blueprint_payload(trainer, source_commit=SOURCE_COMMIT)


@pytest.fixture(scope="module")
def blueprint(blueprint_payload) -> HUNLBlueprint:
    return HUNLBlueprint(blueprint_payload)


def _one_hot_payload(
    blueprint_payload: dict[str, object],
    rows: dict[str, str],
) -> dict[str, object]:
    payload = copy.deepcopy(blueprint_payload)
    body = payload["body"]
    policies = body["policies"]
    for key, selected in rows.items():
        legal = information_abstraction_key_legal(key)
        policies[key] = {action: float(action == selected) for action in legal}
    body["training"]["policy_rows"] = len(policies)
    payload["body_sha256"] = _digest(body)
    HUNLBlueprint(payload)
    return payload


def information_abstraction_key_legal(key: str) -> tuple[str, ...]:
    from ..decisionholdem_like.hunl_abstraction import parse_infoset_key

    return tuple(parse_infoset_key(key)["legal"])


def test_sparse_artifact_has_exact_and_trained_hierarchical_rows(blueprint) -> None:
    first, second, _ = deterministic_deal(421, 1)
    trained_state = NationalGameState.new_hand(
        1, small_blind=0, hole_cards=(first, second)
    )
    trained_abstraction = information_abstraction(trained_state, 0)
    trained_legal = tuple(spec.action_id for spec in abstract_actions(trained_state))
    assert (
        blueprint.lookup(trained_abstraction, trained_legal).source
        == "trained_exact_row"
    )

    cards = parse_cards_exact("<0,12><1,12>", expected=2)
    state = NationalGameState.new_hand(1, small_blind=0, hole_cards=(cards, ()))
    abstraction = information_abstraction(state, 0)
    lookup = blueprint.lookup(
        abstraction, tuple(spec.action_id for spec in abstract_actions(state))
    )
    assert lookup.source.startswith("trained_backoff_")
    assert lookup.matched_key is not None
    assert sum(lookup.probabilities.values()) == pytest.approx(1.0)


def test_uniform_emergency_is_only_after_all_content_bound_trained_levels_miss(
    blueprint_payload,
) -> None:
    cards = parse_cards_exact("<0,12><1,12>", expected=2)
    state = NationalGameState.new_hand(1, small_blind=0, hole_cards=(cards, ()))
    abstraction = information_abstraction(state, 0)
    payload = copy.deepcopy(blueprint_payload)
    for level, _ in HUNL_TRAINED_BACKOFF_LEVELS:
        payload["body"]["trained_backoff_policies"][level].pop(
            trained_backoff_key(abstraction, level), None
        )
        assert payload["body"]["trained_backoff_policies"][level]
    payload["body_sha256"] = _digest(payload["body"])
    lookup = HUNLBlueprint(payload).lookup(
        abstraction, tuple(spec.action_id for spec in abstract_actions(state))
    )
    assert lookup.source == HUNL_FALLBACK_CONTRACT["mode"]
    assert lookup.matched_key is None
    assert sum(lookup.probabilities.values()) == pytest.approx(1.0)


def test_blueprint_only_decision_does_not_call_network_resolve_or_search(
    blueprint,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network used")),
    )
    monkeypatch.setattr(
        CoinTossResolveGame,
        "plain_resolve",
        lambda self: (_ for _ in ()).throw(AssertionError("resolve used")),
    )
    cards = parse_cards_exact("<0,12><1,12>", expected=2)
    state = NationalGameState.new_hand(1, small_blind=0, hole_cards=(cards, ()))
    decision = choose_hunl_blueprint_action(
        blueprint, state=state, hero=0, random_unit=0.5
    )
    assert state.legal_actions().contains(decision.action)
    assert decision.lookup.source.startswith("trained_backoff_")


def test_hunl_common_runtime_handles_split_sticky_and_omitted_close(
    blueprint_payload,
) -> None:
    cards = parse_cards_exact("<0,12><0,11>", expected=2)
    state = NationalGameState.new_hand(1, small_blind=0, hole_cards=(cards, ()))
    preflop_key = information_abstraction(state, 0).key
    state = state.apply_action(Action(ActionKind.CALL))
    state = state.apply_action(Action(ActionKind.CHECK))
    flop = parse_cards_exact("<1,0><2,3><3,7>", expected=3)
    state = state.apply_chance(flop)
    state = state.apply_action(Action(ActionKind.CHECK))
    flop_key = information_abstraction(state, 0).key
    payload = _one_hot_payload(
        blueprint_payload,
        {preflop_key: "check_call", flop_key: "check_call"},
    )
    runtime = CommonA2StrategyRuntime("HUNLCommon", HUNLBlueprint(payload), seed=19)
    assert runtime.feed("namepreflop|SMALLBLIND|<0,12>")[-1][1] == "HUNLCommon"
    output = runtime.feed("<0,11>")
    assert output[-1][1] == "call"
    # The platform suppresses the peer BB check and jumps to flop; Common owns
    # the unique boundary inference before the HUNL policy sees a state.
    assert runtime.feed("flop|<1,0><2,3><3,7>")[-1][1] is None
    assert runtime.on_token("check")[1] == "call"
    assert runtime.trace[-1].lookup_source == "trained_exact_row"
    assert runtime.trace[-1].dropped_policy_mass == 0.0
    assert runtime.trace[-1].blueprint_schema.endswith("hunl-sparse-blueprint-v6")


def test_hunl_runtime_never_sends_during_allin_runout(blueprint_payload) -> None:
    cards = parse_cards_exact("<0,12><0,11>", expected=2)
    state = NationalGameState.new_hand(1, small_blind=0, hole_cards=(cards, ()))
    key = information_abstraction(state, 0).key
    payload = _one_hot_payload(blueprint_payload, {key: "allin"})
    runtime = CommonA2StrategyRuntime("HUNLAllin", HUNLBlueprint(payload), seed=23)
    assert runtime.on_token("name")[1] == "HUNLAllin"
    assert runtime.on_token("preflop|SMALLBLIND|<0,12><0,11>")[1] == "allin"
    assert runtime.on_token("call")[1] is None
    for token in (
        "flop|<1,0><2,3><3,7>",
        "turn|<1,8>",
        "river|<2,9>",
    ):
        assert runtime.on_token(token)[1] is None
    assert runtime.decisions_completed == 1
    assert runtime.session.pending_decision_id is None


def test_hunl_runtime_enforces_ordered_one_shot_name_handshake(blueprint) -> None:
    runtime = CommonA2StrategyRuntime("HUNLHandshake", blueprint, seed=29)
    with pytest.raises(ProtocolStateError, match="before the name handshake"):
        runtime.on_token("preflop|SMALLBLIND|<0,12><0,11>")
    event, outgoing = runtime.on_token("name")
    assert event.kind == "name_requested"
    assert outgoing == "HUNLHandshake"
    with pytest.raises(ProtocolStateError, match="stale"):
        runtime.session.name_response()
    with pytest.raises(ProtocolStateError, match="duplicate"):
        runtime.on_token("name")


@pytest.mark.parametrize("mutation", ("hash", "nan", "bool", "missing_action", "common"))
def test_blueprint_loader_fails_closed_on_corruption(blueprint_payload, mutation) -> None:
    payload = copy.deepcopy(blueprint_payload)
    body = payload["body"]
    if mutation == "hash":
        payload["body_sha256"] = "0" * 64
    elif mutation == "bool":
        body["training"]["iterations_completed"] = True
        payload["body_sha256"] = _digest(body)
    elif mutation == "common":
        body["common"]["tree_sha256"] = "f" * 64
        payload["body_sha256"] = _digest(body)
    else:
        key = next(iter(body["policies"]))
        row = body["policies"][key]
        action = next(iter(row))
        if mutation == "nan":
            row[action] = float("nan")
        else:
            del row[action]
        payload["body_sha256"] = _digest(body)
    with pytest.raises(ValueError):
        HUNLBlueprint(payload)


def test_blueprint_file_loader_is_atomic_strict_and_rejects_duplicate_keys(
    blueprint,
    tmp_path,
) -> None:
    path = tmp_path / "blueprint.json"
    blueprint.save(path)
    assert HUNLBlueprint.load(path).digest == blueprint.digest
    raw = path.read_text()
    path.write_text(raw[:-2] + ',\n  "schema": "duplicate"\n}\n')
    with pytest.raises(ValueError, match="duplicate JSON key"):
        HUNLBlueprint.load(path)


def test_exact_and_backoff_policies_rebuild_from_frozen_bound_checkpoint() -> None:
    trainer = HUNLExternalSamplingLCFR(HUNLTrainingConfig(seed=104729))
    trainer.train_to(2)
    direct = build_hunl_blueprint_payload(trainer, source_commit=SOURCE_COMMIT)
    resumed = HUNLExternalSamplingLCFR.from_checkpoint_payload(
        trainer.checkpoint_payload()
    )
    rebuilt = build_hunl_blueprint_payload(resumed, source_commit=SOURCE_COMMIT)
    assert rebuilt == direct
    assert (
        rebuilt["body"]["trained_backoff_policies"]
        == direct["body"]["trained_backoff_policies"]
    )
