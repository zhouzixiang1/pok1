"""Root fix for the 2026-10-09 v532 receipt-eviction + silent-stop wedge.

Incident (root-caused read-only): during the GLM 1302 storm
(``service_unavailable`` occurrences=179) a deferred durable Worker effect
held ``evidence_digest`` D1 (frozen 05:06).  Every pause reconcile archives
one resume receipt, but the store's FIFO ``resume_receipt_history`` was capped
at 8 (``llm_availability_store.RESUME_RECEIPT_HISTORY_CAP``); the 8 rapid
resume cycles between 05:01-05:32 evicted D1's receipt.  From then on every
orchestrator revival re-validated the deferred effect
(``_worker_availability_resume_receipt_errors``), got
``evidence_digest_mismatch + no_archived_match`` →
``WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID`` → orchestrator
``-99995`` → the crash-revival supervisor burned its 5/30min window →
05:28:34 ``restart_rate_limited`` terminal stop.  That terminal wrote only
``events.jsonl`` (event_bus dispatch never touches app.log nor the webui
history), so the stop sat unseen for 3.1h.

Contract of this fix:

* F-A (receipt immune to store churn):
  - the receipt-history cap and the durable scan limit both rise 8 -> 64;
  - ``_worker_availability_resume_receipt_errors`` additionally accepts the
    deferral-time snapshot *already frozen inside the deferred record* as an
    alternative authorization.  **2026-10-09 re-review (F-H/F-M1)**: the real
    v532 incident shape is *suppressed divergence* — the exception path
    freezes ``exc.pause_state()`` and persists it onto an ACTIVE
    same-category record, so the frozen digest never becomes a record digest
    and never has a receipt (any cap is irrelevant; the incident-time archive
    held 2 receipts).  The authorization lane is therefore the
    **suppressed-evidence chain**: the frozen digest must match the store's
    ``last_suppressed_evidence_digest`` marker (on the inactive audit record
    or its archived receipt projections — structurally bound to the digest,
    F-M1; the earlier capacity-based lane authorized forged digests and was
    removed) and the snapshot's system-owned resume horizon (exact
    ``auto_resume_at`` when frozen, else the category's conservative deadline
    bound derived from ``observed_at`` / ``provider_reset_at``) must have
    elapsed, while the live audit record is inactive.  A genuine mismatch
    still fails closed: manual pauses never self-authorize, a horizon that
    has not elapsed rejects, an active audit record rejects, and a digest
    absent from the marker chain rejects at ANY archive depth.
* F-B (terminal observability + slow-retry lane):
  - the ``restart_rate_limited`` branch alarms through the orchestrator
    logger (app.log) and the webui ``log_history`` in addition to
    ``events.jsonl``;
  - after the rate-limited stop the supervisor enters a slow-retry lane
    (one alarmed restart attempt per ``POK_ORCHESTRATOR_RESTART_SLOW_RETRY_SEC``
    / default 1800s), still counted and still guarded by the sliding-window
    counter; owner drift / shutdown / non-crash terminals never enter the
    lane.  **F-M3 (re-review)**: while parked the lane re-arms ``running``
    before each sleep and verifies it at each wake — a bare
    ``stop_running`` during the park ends the supervisor
    (``stopped_no_restart``) instead of silently reviving; the alarm copy
    documents the Stop-then-Start contract.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging

import pytest

import evolution_infra
from llm_availability import (
    BILLING_CYCLE_LIMIT,
    SERVICE_UNAVAILABLE,
    classify_llm_availability,
    build_llm_pause_state,
)
import llm_availability_store as store
import server.state as state_module
import system_log
import tool_planning_worker  # noqa: F401  (parent-first import order)
import tool_planning_worker_durable
from tool_planning_worker_durable import _worker_availability_resume_receipt_errors


BASE = datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc)


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.delenv(store.RESUME_ENV, raising=False)
    return tmp_path


def _service_issue(text: str = "HTTP 529 service unavailable"):
    issue = classify_llm_availability([text], statuses=[529])
    assert issue is not None and issue.category == SERVICE_UNAVAILABLE
    return issue


def _billing_issue():
    issue = classify_llm_availability(
        ["HTTP 403: You've reached your usage limit for this billing cycle"],
        statuses=[403],
    )
    assert issue is not None and issue.category == BILLING_CYCLE_LIMIT
    return issue


def _cool_down(now):
    """Advance past the live record's own cooldown window and reconcile."""
    record = store.load_llm_pause()
    due = store._parse_time((record or {}).get("auto_resume_at")) if record else None
    if due is None:
        return store.active_llm_pause(now=now + timedelta(seconds=9))
    return store.active_llm_pause(now=due + timedelta(seconds=1))


# ---------------------------------------------------------------------------
# F-A: receipt immune to store churn
# ---------------------------------------------------------------------------


def test_receipt_history_cap_and_scan_limit_raised_to_64(isolated_store):
    assert store.RESUME_RECEIPT_HISTORY_CAP == 64
    assert tool_planning_worker_durable._RESUME_RECEIPT_HISTORY_SCAN_LIMIT >= 64


def test_incident_shaped_storm_nine_reconciles_still_authorizes(isolated_store):
    """Production-shape reproduction: 9 rapid resume cycles after the deferral.

    At the old cap 8 the 9th archived receipt evicted D1's receipt — exactly
    the 05:01-05:32 storm — and the deferred effect could never resume.
    """
    pause_d1 = store.persist_llm_pause(
        _service_issue("HTTP 529 GLM storm D1"), now=BASE
    )
    deferred = dict(pause_d1)  # claim-boundary freeze: full store record
    assert _cool_down(BASE) is None  # D1 reconciled -> receipt exists

    moment = BASE + timedelta(seconds=30)
    for index in range(9):
        store.persist_llm_pause(
            _service_issue(f"HTTP 529 storm blip {index}"), now=moment
        )
        assert _cool_down(moment) is None
        moment = moment + timedelta(seconds=30)

    audit = store.load_llm_pause()
    assert audit["active"] is False
    errors = _worker_availability_resume_receipt_errors(
        deferred, audit, now=moment + timedelta(hours=1)
    )
    assert errors == [], errors


def test_churned_archive_without_store_held_evidence_fails_closed(
    isolated_store, monkeypatch
):
    """F-M1 (2026-10-09 re-review): the capacity-based snapshot lane is gone.

    The earlier fix authorized ANY transient deferral snapshot against a
    demonstrably full archive — a forged digest therefore passed (EXP-1).
    Authorization is now structurally bound to the frozen digest through the
    store-held suppression marker chain.  A claim-boundary freeze whose
    receipt was evicted (D1 became a record, reconciled, then churned out of
    a cap-2 archive) left no marker anywhere: the store holds no evidence for
    that digest, so the resume fails closed and only the operator evidence
    digest (``POK_LLM_RESUME_EVIDENCE_DIGEST``) can reconcile it.
    """
    monkeypatch.setattr(store, "RESUME_RECEIPT_HISTORY_CAP", 2)
    pause_d1 = store.persist_llm_pause(
        _service_issue("HTTP 529 churn D1"), now=BASE
    )
    deferred = dict(pause_d1)
    assert _cool_down(BASE) is None

    moment = BASE + timedelta(seconds=30)
    for index in range(3):
        store.persist_llm_pause(
            _service_issue(f"HTTP 529 churn blip {index}"), now=moment
        )
        assert _cool_down(moment) is None
        moment = moment + timedelta(seconds=30)

    audit = store.load_llm_pause()
    history = audit.get("resume_receipt_history") or []
    assert all(
        entry.get("evidence_digest") != pause_d1["evidence_digest"]
        for entry in history
    ), "precondition: D1's receipt is evicted"
    assert audit.get("last_suppressed_evidence_digest") != (
        pause_d1["evidence_digest"]
    ), "precondition: D1 was never a suppressed incoming digest"

    errors = _worker_availability_resume_receipt_errors(
        deferred, audit, now=moment + timedelta(minutes=10)
    )
    assert errors
    assert "global_pause_resume_receipt_no_archived_match" in errors


def test_exception_path_freeze_uses_observed_at_horizon(isolated_store, monkeypatch):
    """The exception-path deferral freezes ``pause_state()`` (no auto_resume_at).

    F-H (2026-10-09 re-review): the production shape is *suppressed
    divergence* — the freeze is persisted onto an ACTIVE same-category
    record, so its digest never becomes a record digest and never has a
    receipt; the store-held suppression marker is the only durable proof.
    The frozen projection carries ``observed_at``; its conservative horizon
    (observed_at + the 120s service-cooldown cap) must authorize the resume
    once elapsed, and must NOT authorize before it elapses.
    """
    monkeypatch.setattr(store, "RESUME_RECEIPT_HISTORY_CAP", 2)
    issue = _service_issue("HTTP 529 exception-path D1")
    freeze = build_llm_pause_state(issue, observed_at=BASE.isoformat())
    assert "auto_resume_at" not in freeze  # the real exception-path shape

    # An ACTIVE same-category record exists when the freeze is persisted
    # (production: the 05:06 saturator pause) -> suppression divergence.
    store.persist_llm_pause(
        _service_issue("HTTP 529 active record before the freeze"),
        now=BASE - timedelta(seconds=10),
    )
    persisted = store.persist_llm_pause(freeze, now=BASE)
    assert persisted["evidence_digest"] != freeze["evidence_digest"]
    assert (
        persisted.get("last_suppressed_evidence_digest")
        == freeze["evidence_digest"]
    )
    due = store._parse_time(persisted["auto_resume_at"])
    assert store.active_llm_pause(now=due + timedelta(seconds=1)) is None
    audit = store.load_llm_pause()
    assert audit["active"] is False

    horizon = BASE + timedelta(seconds=120)
    early = _worker_availability_resume_receipt_errors(
        freeze, audit, now=horizon - timedelta(seconds=1)
    )
    assert early, "a horizon that has not elapsed must still fail closed"
    # No receipts were archived in this minimal shape, so the reject surfaces
    # through the current-record mismatch token (no archive was scanned).
    assert "global_pause_resume_receipt_evidence_digest_mismatch" in early

    late = _worker_availability_resume_receipt_errors(
        freeze, audit, now=horizon + timedelta(seconds=1)
    )
    assert late == [], late


def test_manual_pause_snapshot_never_self_authorizes(isolated_store, monkeypatch):
    """A manual pause in the deferred record keeps requiring the operator receipt."""
    monkeypatch.setattr(store, "RESUME_RECEIPT_HISTORY_CAP", 2)
    billing = store.persist_llm_pause(_billing_issue(), now=BASE)
    deferred = dict(billing)
    assert deferred["requires_manual_resume"] is True

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(store.RESUME_ENV, billing["evidence_digest"])
        resumed = store.consume_operator_resume_ack_from_env(
            now=BASE + timedelta(minutes=1)
        )
    assert resumed["active"] is False

    moment = BASE + timedelta(minutes=2)
    for index in range(3):
        store.persist_llm_pause(
            _service_issue(f"HTTP 529 post-manual blip {index}"), now=moment
        )
        assert _cool_down(moment) is None
        moment = moment + timedelta(seconds=30)

    audit = store.load_llm_pause()
    history = audit.get("resume_receipt_history") or []
    assert all(
        entry.get("evidence_digest") != billing["evidence_digest"]
        for entry in history
    )
    errors = _worker_availability_resume_receipt_errors(
        deferred, audit, now=moment + timedelta(hours=2)
    )
    assert errors, "a manual pause must never self-authorize via the snapshot"
    assert "global_pause_resume_receipt_no_archived_match" in errors


def test_active_audit_record_never_self_authorizes(isolated_store, monkeypatch):
    """While the live store record is still active, no snapshot authorizes."""
    monkeypatch.setattr(store, "RESUME_RECEIPT_HISTORY_CAP", 2)
    pause_d1 = store.persist_llm_pause(_service_issue("HTTP 529 active D1"), now=BASE)
    deferred = dict(pause_d1)
    assert _cool_down(BASE) is None

    moment = BASE + timedelta(seconds=30)
    for index in range(3):
        store.persist_llm_pause(
            _service_issue(f"HTTP 529 active blip {index}"), now=moment
        )
        if index < 2:
            assert _cool_down(moment) is None
        moment = moment + timedelta(seconds=30)
    audit = store.load_llm_pause()
    assert audit["active"] is True

    errors = _worker_availability_resume_receipt_errors(
        deferred, audit, now=moment + timedelta(hours=2)
    )
    assert errors


# ---------------------------------------------------------------------------
# F-B: terminal observability + slow-retry lane
# ---------------------------------------------------------------------------


class _FakeClock:
    """Deterministic ``time`` shim for the supervisor module only."""

    def __init__(self, start: float = 1000.0):
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def time(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _RecordingUI:
    def __init__(self):
        self.history = []

    def log_history(self, msg, status="info"):
        self.history.append({"msg": msg, "status": status})


@pytest.fixture
def supervisor_env(monkeypatch):
    """Fast knobs + a deterministic clock, sleep seam, event and UI capture."""
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_INITIAL_BACKOFF_SEC", "0.001")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_MAX_BACKOFF_SEC", "0.004")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_MAX_BURST", "2")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_WINDOW_SEC", "30")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_STABLE_RUN_SEC", "999999")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_SLOW_RETRY_SEC", "5")

    clock = _FakeClock()
    monkeypatch.setattr(state_module, "time", clock)

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    monkeypatch.setattr(state_module, "_restart_backoff_sleep", fake_sleep)

    events = []
    monkeypatch.setattr(
        system_log,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            {"type": event_type, "severity": severity, "message": message, "data": data or {}}
        ),
    )

    ui = _RecordingUI()
    import tool_helpers

    monkeypatch.setattr(tool_helpers, "_get_ui", lambda: ui)
    return {"clock": clock, "sleeps": sleeps, "events": events, "ui": ui}


async def _drive(outcomes):
    from server.state import app_state, run_evolution_task

    calls = []
    owner_id = app_state.begin_runtime_owner()
    assert owner_id is not None

    def factory():
        calls.append(len(calls) + 1)

        async def body():
            return outcomes[len(calls) - 1] if len(calls) <= len(outcomes) else 0.0

        return body()

    task = asyncio.create_task(
        run_evolution_task(factory(), owner_id=owner_id, restart_factory=factory)
    )
    app_state.set_task(task, owner_id=owner_id)
    result = await task
    return result, calls


@pytest.fixture(autouse=True)
def _quiesce_app_state():
    from server.state import app_state

    app_state.set_running(False)
    app_state._last_orchestrator_crash = None
    yield
    app_state.set_running(False)


def test_slow_retry_default_is_1800_seconds(monkeypatch):
    monkeypatch.delenv("POK_ORCHESTRATOR_RESTART_SLOW_RETRY_SEC", raising=False)
    assert state_module._restart_slow_retry_seconds() == 1800.0


def test_rate_limited_stop_alarms_and_slow_lane_recovers(
    supervisor_env, monkeypatch, caplog
):
    caplog.set_level(logging.ERROR, logger="pok.orchestrator")

    async def scenario():
        result, calls = await _drive([-1.0, -1.0, -1.0, 0.0])
        # Fast lane allowed 2 restarts (calls 1..3 all crash) -> rate limited.
        assert calls == [1, 2, 3, 4]
        assert result == 0.0

    asyncio.run(scenario())

    env = supervisor_env
    statuses = [
        e["data"].get("status")
        for e in env["events"]
        if e["type"] == "pipeline.orchestrator_auto_restart"
    ]
    # (1) Observability: the rate-limited stop is still evented as
    # operator-action-required …
    assert "restart_rate_limited" in statuses
    terminal = [
        e
        for e in env["events"]
        if e["data"].get("status") == "restart_rate_limited"
    ][0]
    assert terminal["data"].get("operator_action_required") is True
    # … and it reached app.log via the orchestrator logger …
    applog = [
        r for r in caplog.records if r.name == "pok.orchestrator" and r.levelno >= logging.ERROR
    ]
    assert applog, "the rate-limited stop must reach app.log at ERROR level"
    assert any("慢速重试" in r.getMessage() or "rate" in r.getMessage() for r in applog)
    # … and the webui log_history.
    assert env["ui"].history, "the rate-limited stop must reach webui log_history"
    assert any(entry["status"] == "error" for entry in env["ui"].history)

    # (2) Slow lane: exactly one 5s-interval cadence, window-guarded.
    assert "slow_retry_scheduled" in statuses
    slow_sleeps = [s for s in env["sleeps"] if s >= 5.0]
    assert slow_sleeps, env["sleeps"]
    # window=30s, slow=5s: the two fast stamps stay live for 30s, so the slow
    # lane must wait 6 x 5s before its single guarded attempt — proving the
    # supervisor counters still protect the slow lane.
    assert len(slow_sleeps) == 6
    slow_events = [
        e for e in env["events"] if e["data"].get("status") == "slow_retry_scheduled"
    ]
    assert len(slow_events) == 1


def test_owner_drift_in_slow_lane_stays_stopped(supervisor_env):
    from server.state import app_state

    async def scenario():
        calls = []

        def factory():
            calls.append(len(calls) + 1)

            async def body():
                # Crash twice fast, then on the slow-lane attempt flip the
                # owner: a fencing outcome must end the supervisor without
                # further restarts.
                if len(calls) == 4:
                    app_state.set_running(False)
                    app_state.begin_runtime_owner()
                return -1.0

            return body()

        owner_id = app_state.begin_runtime_owner()
        assert owner_id is not None
        task = asyncio.create_task(
            state_module.run_evolution_task(
                factory(), owner_id=owner_id, restart_factory=factory
            )
        )
        result = await task
        return result, calls

    result, calls = asyncio.run(scenario())
    assert result == -1.0
    assert calls == [1, 2, 3, 4]
    statuses = [
        e["data"].get("status")
        for e in supervisor_env["events"]
        if e["type"] == "pipeline.orchestrator_auto_restart"
    ]
    assert "restart_rate_limited" in statuses
    assert "owner_lost_no_restart" in statuses


def test_non_crash_terminal_never_enters_slow_lane(supervisor_env):
    async def scenario():
        result, calls = await _drive([0.0])
        assert result == 0.0
        assert calls == [1]

    asyncio.run(scenario())
    statuses = [
        e["data"].get("status")
        for e in supervisor_env["events"]
        if e["type"] == "pipeline.orchestrator_auto_restart"
    ]
    assert statuses == []
    assert supervisor_env["sleeps"] == []
    assert supervisor_env["ui"].history == []
