from __future__ import annotations

from ..decisionholdem_like.a2_runtime import SparseBlueprint
from ..decisionholdem_like.blueprint import BlueprintTrainer
from ..decisionholdem_like.common_native_entry import CommonA2StrategyRuntime


def _runtime(action_id: str) -> CommonA2StrategyRuntime:
    trainer = BlueprintTrainer()
    trainer.train_to(2)
    payload = trainer.blueprint_payload()
    payload["policies"] = {
        key: {action_id: 1.0} for key in payload["policies"]
    }
    return CommonA2StrategyRuntime("A2Common", SparseBlueprint(payload), seed=19)


def test_strategy_entry_uses_common_session_for_preflop_and_postflop_close() -> None:
    runtime = _runtime("check_call")
    event, send = runtime.on_token("name")
    assert event.kind == "name_requested"
    assert send == "A2Common"

    event, send = runtime.on_token("preflop|SMALLBLIND|<0,12><0,11>")
    assert event.kind == "hand_started"
    assert send == "call"
    assert runtime.session.current is not None
    assert runtime.session.current.actor == 1
    assert runtime.session.pending_decision_id is None

    _, send = runtime.on_token("check")
    assert send is None
    assert runtime.session.current.chance_pending
    _, send = runtime.on_token("flop|<1,0><2,3><3,7>")
    assert send is None
    assert runtime.session.current.actor == 1
    _, send = runtime.on_token("check")
    assert send == "call"
    assert runtime.session.current.chance_pending
    assert runtime.trace[-1].action == "call"
    assert "call" in runtime.trace[-1].legal_actions
    assert "check" not in runtime.trace[-1].legal_actions
    assert not runtime.trace[-1].used_legality_fallback
    assert runtime.trace[-1].available_policy_mass == 1.0
    assert runtime.trace[-1].dropped_policy_mass == 0.0


def test_common_entry_sticky_decoder_and_one_shot_lease() -> None:
    runtime = _runtime("fold")
    outputs = runtime.feed("namepreflop|SMALLBLIND|<0,12>")
    assert [(event.kind, send) for event, send in outputs] == [
        ("name_requested", "A2Common")
    ]
    outputs = runtime.feed("<0,11>")
    assert [(event.kind, send) for event, send in outputs] == [
        ("hand_started", "fold")
    ]
    assert runtime.decisions_completed == 1
    assert runtime.session.pending_decision_id is None
    assert runtime.session.current.is_terminal
    assert runtime._act_if_pending() is None


def test_common_entry_does_not_act_during_allin_runout() -> None:
    runtime = _runtime("allin")
    assert runtime.on_token("name")[1] == "A2Common"
    _, send = runtime.on_token("preflop|SMALLBLIND|<0,12><0,11>")
    assert send == "allin"
    _, send = runtime.on_token("call")
    assert send is None
    assert runtime.session.current.runout_pending
    for token in (
        "flop|<1,0><2,3><3,7>",
        "turn|<1,8>",
        "river|<2,9>",
    ):
        _, send = runtime.on_token(token)
        assert send is None
    assert runtime.session.current.is_terminal
    assert runtime.decisions_completed == 1


def test_counter_based_policy_rng_replays_and_changes_by_decision_occurrence() -> None:
    first = _runtime("check_call")
    replay = _runtime("check_call")
    # The route may hash the Common information state and bot RNG seed, but it
    # must not use observation_id/match_context_id reserved for a later 70-hand
    # controller.
    info_id = "a" * 64
    draws = [first._random_unit(info_id, serial) for serial in (1, 2, 3)]
    assert len(set(draws)) == 3
    assert draws == [replay._random_unit(info_id, serial) for serial in (1, 2, 3)]
