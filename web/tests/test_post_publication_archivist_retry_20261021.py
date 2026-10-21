"""Bounded post-publication Archivist retry (v585 loop class, 2026-10-21).

v585 published at 04:56 and then its durable handoff could not converge for
4.1 hours: 214 identical ``POST_PUBLICATION_ARCHIVIST_PENDING`` route failures
(8-30s apart) while the handoff's stability reproof compared the frozen
receipt against a projection poisoned by the b50e19c8 exact-pin TypeError.
b50e19c8 fixed that root cause, and the deploy restart's reset row let the
reproof witness the receipt (handoff completed at 09:15).

The retry *cadence* was still unbounded: every failure fell through to a fresh
provider stream and the next cycle re-ran the route.  These tests pin the
replacement contract:

* exponential backoff 30s -> 600s cap, at most 8 attempts per handoff episode;
* the retry is parked on the backoff instead of re-entering the provider;
* exhaustion produces one typed ``recovery_blocked`` / operator boundary, not
  a completed handoff and not a successor;
* the durable handoff journal is never touched (``archivist_completed`` stays
  false), so the pending-handoff launch barrier remains in force;
* a completed route resets the episode.
"""

from __future__ import annotations

import json

import pytest


PENDING_ERROR = "POST_PUBLICATION_ARCHIVIST_PENDING"
PENDING_DETAIL = (
    "PostPublicationHandoffError: "
    "handoff_stability_observation_reproof_mismatch"
)


def _pending_recovery():
    checkpoint = {
        "post_publication_handoff_route": True,
        "post_publication_handoff_identity_digest": "a" * 64,
        "post_publication_id": "b" * 64,
        "next_v": 585,
        "source_v": 579,
        "parent2_v": None,
        "workflow_run_id": "generation:585:workflow-v1",
        "checkpoint_revision": 0,
        "stage": "archived",
    }
    return {
        "action": "resume",
        "checkpoint": checkpoint,
        "session_id": None,
        "stage": "post_publication_handoff",
        "next_v": 585,
        "source_v": 579,
        "post_publication_handoff": True,
    }


def _pending_result():
    return {
        "error": PENDING_ERROR,
        "version": 585,
        "source_v": 579,
        "archivist_completed": False,
        "checkpoint_cleared_by_archivist": False,
        "detail": PENDING_DETAIL,
        "directive": (
            "Repair the required effect and retry run_archivist for the same "
            "durable handoff; do not prepare another generation."
        ),
    }


class _RouteHarness:
    def __init__(self):
        self.events = []
        self.delays = []
        self.handler_calls = 0
        self._results = []

    def queue(self, result):
        self._results.append(result)

    def event_types(self):
        return [args[0] for args, _kwargs in self.events]

    def event(self, name):
        for args, _kwargs in self.events:
            if args[0] == name:
                return args
        return None


@pytest.fixture
def route_harness(monkeypatch):
    import orchestrator as orch
    import orchestrator_deterministic_route as odr

    harness = _RouteHarness()

    async def fake_handler(_args):
        harness.handler_calls += 1
        result = (
            harness._results.pop(0)
            if harness._results
            else _pending_result()
        )
        return {
            "content": [
                {"type": "text", "text": json.dumps(result)},
            ]
        }

    async def fake_run_off_loop(handler, args, **_kwargs):
        return await handler(args)

    async def fake_backoff(_shutdown_mgr, seconds):
        harness.delays.append(float(seconds))

    monkeypatch.setattr(orch, "_bind_generation_cost_runtime", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_check_generation_cost_policy", lambda *a, **k: None)
    monkeypatch.setattr(
        orch,
        "_resolve_recovery_route",
        lambda _checkpoint: {
            "next_tool": "run_archivist",
            "next_v": 585,
            "source_v": 579,
            "parent2_v": None,
            "stage": "archived",
        },
    )
    monkeypatch.setattr(
        orch, "_deterministic_route_requires_llm", lambda *a, **k: False
    )
    monkeypatch.setattr(orch, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(
        orch,
        "_deterministic_route_handler_and_args",
        lambda *a, **k: (fake_handler, {"version": 585, "source_v": 579}),
    )
    monkeypatch.setattr(orch, "_raise_for_llm_availability_tool_result", lambda _d: None)
    monkeypatch.setattr(
        orch,
        "log_system_event",
        lambda event_type, severity, message, data=None: harness.events.append(
            ((event_type, severity, message, data), {})
        ),
    )
    monkeypatch.setattr(odr, "run_async_off_event_loop", fake_run_off_loop)
    monkeypatch.setattr(
        odr, "_wait_post_publication_archivist_backoff", fake_backoff
    )

    reset_retry_state()
    yield harness
    reset_retry_state()


def reset_retry_state():
    import orchestrator_deterministic_route as odr

    with odr._ARCHIVIST_RETRY_LOCK:
        odr._ARCHIVIST_RETRY_STATE.clear()


def test_pending_retry_backoff_is_exponential_and_capped():
    import orchestrator_deterministic_route as odr

    reset_retry_state()
    checkpoint = _pending_recovery()["checkpoint"]
    result = _pending_result()
    decisions = [
        odr._archivist_pending_retry_step(checkpoint, result, PENDING_DETAIL)
        for _ in range(odr._ARCHIVIST_RETRY_MAX_ATTEMPTS)
    ]

    assert [row["attempt"] for row in decisions] == list(
        range(1, odr._ARCHIVIST_RETRY_MAX_ATTEMPTS + 1)
    )
    assert [row["delay_sec"] for row in decisions] == [
        30.0,
        60.0,
        120.0,
        240.0,
        480.0,
        600.0,
        600.0,
        600.0,
    ]
    assert [row["exhausted"] for row in decisions] == [
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        True,
    ]
    assert decisions[-1]["last_error"] == PENDING_DETAIL

    # A different handoff identity is an independent episode.
    other = dict(checkpoint)
    other["post_publication_handoff_identity_digest"] = "c" * 64
    fresh = odr._archivist_pending_retry_step(other, result, PENDING_DETAIL)
    assert (fresh["attempt"], fresh["delay_sec"], fresh["exhausted"]) == (
        1,
        30.0,
        False,
    )

    # A completed route clears the episode.
    odr._reset_post_publication_archivist_retry(checkpoint, result)
    restarted = odr._archivist_pending_retry_step(
        checkpoint, result, PENDING_DETAIL
    )
    assert (restarted["attempt"], restarted["delay_sec"]) == (1, 30.0)
    reset_retry_state()


@pytest.mark.asyncio
async def test_pending_route_parks_on_backoff_then_blocks(route_harness):
    import orchestrator as orch
    import orchestrator_deterministic_route as odr

    recovery = _pending_recovery()
    outcomes = []
    for attempt in range(1, odr._ARCHIVIST_RETRY_MAX_ATTEMPTS + 1):
        outcome = {}
        routed = await orch._try_deterministic_checkpoint_route(
            recovery,
            outcome=outcome,
        )
        outcomes.append((attempt, routed, outcome))
        if attempt < odr._ARCHIVIST_RETRY_MAX_ATTEMPTS:
            # Under the cap the route PARKS on the backoff (routed=True with a
            # resume recovery) instead of falling through to a provider stream.
            assert routed is True
            assert "recovery_blocked" not in outcome["result"]
            assert outcome["result"]["retry"]["attempt"] == attempt
            assert outcome["result"]["retry"]["exhausted"] is False

    assert route_harness.delays == [30.0, 60.0, 120.0, 240.0, 480.0, 600.0, 600.0]
    assert route_harness.handler_calls == odr._ARCHIVIST_RETRY_MAX_ATTEMPTS

    attempt, routed, outcome = outcomes[-1]
    assert attempt == odr._ARCHIVIST_RETRY_MAX_ATTEMPTS
    # The cap returns False so _advance_deterministic_recovery can classify
    # the typed blocked result (never a completion).
    assert routed is False
    blocked = outcome["result"]
    assert blocked["recovery_blocked"] is True
    assert blocked["failure_class"] == "post_publication_handoff_blocked"
    assert blocked["action"] == "operator_action_required"
    assert blocked["archivist_completed"] is False
    assert blocked["checkpoint_cleared_by_archivist"] is False
    assert blocked["validation_errors"][0].startswith(
        "post_publication_archivist_retry_exhausted:8/8:"
    )
    assert blocked["validation_errors"][0].endswith(PENDING_DETAIL)

    event_types = route_harness.event_types()
    retry_events = [
        name
        for name in event_types
        if name == "pipeline.post_publication_archivist_retry_pending"
    ]
    assert len(retry_events) == odr._ARCHIVIST_RETRY_MAX_ATTEMPTS - 1
    blocked_event = route_harness.event(
        "pipeline.post_publication_archivist_blocked"
    )
    assert blocked_event is not None
    blocked_payload = blocked_event[3]
    assert blocked_payload["operator_action_required"] is True
    assert blocked_payload["handoff_authority_preserved"] is True
    assert blocked_payload["handoff_state"] == "pending"
    assert blocked_payload["attempts"] == odr._ARCHIVIST_RETRY_MAX_ATTEMPTS
    assert blocked_payload["last_error"] == PENDING_DETAIL
    # The generic per-attempt failure event is still emitted for audit.
    assert "pipeline.deterministic_route_failed" in event_types

    classified = orch._classify_recovery_after_deterministic_route(
        recovery,
        outcome,
        recovery,
    )
    assert classified["action"] == "blocked"
    assert classified["reason"] == PENDING_ERROR
    assert classified["diagnostics"]["operator_action"] == (
        "operator_action_required"
    )
    assert classified["diagnostics"]["failure_class"] == (
        "post_publication_handoff_blocked"
    )
    assert classified["diagnostics"]["issues"][0].startswith(
        "post_publication_archivist_retry_exhausted:8/8:"
    )


@pytest.mark.asyncio
async def test_advance_parks_on_backoff_then_returns_typed_blocked(
    route_harness, monkeypatch
):
    """The outer recovery step spends the whole cap before one blocked stop."""

    import orchestrator as orch
    import orchestrator_deterministic_route as odr

    recovery = _pending_recovery()
    monkeypatch.setattr(
        orch, "_checkpoint_recovery_context", lambda *a, **k: recovery
    )
    advanced_rows = []
    for _ in range(odr._ARCHIVIST_RETRY_MAX_ATTEMPTS):
        advanced_rows.append(
            await orch._advance_deterministic_recovery(
                recovery,
                None,
                cost_policy=None,
                shutdown_mgr=None,
            )
        )

    for advanced in advanced_rows[:-1]:
        # Parked on the backoff: routed, not a provider fall-through, and the
        # same pending-handoff recovery is handed back to the loop.
        assert advanced["routed"] is True
        assert advanced["terminal_action"] == "resume"
        assert advanced["recovery"] is recovery

    last = advanced_rows[-1]
    assert last["routed"] is True
    assert last["terminal_action"] == "blocked"
    assert last["recovery"]["action"] == "blocked"
    assert last["recovery"]["reason"] == PENDING_ERROR
    assert last["outcome"]["result"]["recovery_blocked"] is True
    assert route_harness.delays == [
        30.0,
        60.0,
        120.0,
        240.0,
        480.0,
        600.0,
        600.0,
    ]


@pytest.mark.asyncio
async def test_pending_retry_state_resets_after_completion(route_harness):
    import orchestrator as orch
    import orchestrator_deterministic_route as odr

    recovery = _pending_recovery()
    outcome = {}
    await orch._try_deterministic_checkpoint_route(recovery, outcome=outcome)
    assert outcome["result"]["retry"]["attempt"] == 1

    route_harness.queue(
        {
            "version": 585,
            "source_v": 579,
            "archivist_completed": True,
            "handoff_identity_digest": "d" * 64,
            "publication_id": "b" * 64,
            "steps": {},
            "next_tool": "prepare_generation",
        }
    )
    outcome = {}
    routed = await orch._try_deterministic_checkpoint_route(
        recovery,
        outcome=outcome,
    )
    assert routed is True
    assert outcome["result"]["archivist_completed"] is True

    with odr._ARCHIVIST_RETRY_LOCK:
        assert odr._ARCHIVIST_RETRY_STATE == {}

    # A later pending failure starts a fresh bounded episode.
    outcome = {}
    await orch._try_deterministic_checkpoint_route(recovery, outcome=outcome)
    assert outcome["result"]["retry"]["attempt"] == 1
