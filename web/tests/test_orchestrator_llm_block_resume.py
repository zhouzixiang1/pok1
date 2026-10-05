"""P2 (2026-10-05): keep the orchestrator task alive across waitable LLM pauses.

F9 evidence: four same-day stalls (10:29/16:29/18:41/20:24); 16:29-18:08 was
70 minutes of zero provider flow (~38M tokens lost) after
``_resume_generation_loop_after_llm_block`` returned False for a *waitable*
GLM 1302 cooldown whose durable pause re-armed past the single
``_honor_active_llm_pause`` expiry check — ``orchestrator_loop`` then exited
silently with ``ORCH_LLM_AVAILABILITY_BLOCKED_COST`` (-99995.0) and only a
manual start or deploy restart recovered it.

Contracts under test:

* ``_honor_active_llm_pause`` re-queries the durable store after the
  projected cooldown elapses instead of a single ``is None`` check; a pause
  that extended itself keeps the caller waiting.  Manual billing/auth
  pauses still return False immediately.
* ``_resume_generation_loop_after_llm_block`` polls ``active_llm_pause`` on
  a bounded 5s cadence until the pause clears or shutdown starts; a manual
  pause or an unreadable pause store still stops the loop.
* ``server.state._is_crash_outcome`` treats -99995.0 as restartable so the
  P2 auto-restart supervisor is a second safety net (double insurance).
"""

from __future__ import annotations

import asyncio

import pytest

import orchestrator
import orchestrator_abandon_and_cost as pac
import server.state as state_module


class _UI:
    def __init__(self):
        self.history = []
        self.status = []

    def log_history(self, message, level="info"):
        self.history.append((level, message))

    def set_status(self, message, is_working=False):
        self.status.append((message, is_working))


class _ShutdownMgr:
    def __init__(self, shutting_down=False):
        self.is_shutting_down = shutting_down
        self._event = asyncio.Event()
        if shutting_down:
            self._event.set()

    async def wait_for_shutdown(self):
        await self._event.wait()


def _pause_state(*, category="service_unavailable", active=True, resume_in=15.0):
    return {
        "schema_version": 2,
        "active": active,
        "category": category,
        "summary": "GLM 1302 rate limit",
        "retry_policy": "cooldown",
        "requires_manual_resume": False,
        "evidence_digest": "digest-1",
        "auto_resume_at": resume_in,  # opaque to the unit under test
    }


def _manual_pause_state():
    return {
        "schema_version": 2,
        "active": True,
        "category": "manual_billing",
        "summary": "billing paused",
        "retry_policy": "manual",
        "requires_manual_resume": True,
        "evidence_digest": "digest-2",
    }


@pytest.fixture
def quiet_events(monkeypatch):
    monkeypatch.setattr(orchestrator, "log_system_event", lambda *a, **k: None)
    monkeypatch.setattr(pac, "_o", orchestrator)


@pytest.fixture(autouse=True)
def fast_recheck(monkeypatch):
    monkeypatch.setattr(pac, "_RESUME_RECHECK_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(pac, "_REQUERY_MIN_GAP_SEC", 0.005)
    monkeypatch.setattr(pac, "_HONOR_MAX_REQUERY_SEC", 5.0)
    monkeypatch.setattr(pac, "_MAX_LLM_BLOCK_RESUME_WAIT_SEC", 2.0)


# ── _honor_active_llm_pause ───────────────────────────────────────────────


def test_no_pause_proceeds(monkeypatch):
    monkeypatch.setattr(pac, "active_llm_pause", lambda: None)
    assert asyncio.run(pac._honor_active_llm_pause(None, None)) is True


def test_manual_pause_stops_immediately(monkeypatch):
    monkeypatch.setattr(pac, "active_llm_pause", lambda: _manual_pause_state())
    monkeypatch.setattr(pac, "pause_wait_seconds", lambda state, **k: None)
    ui = _UI()
    assert asyncio.run(pac._honor_active_llm_pause(ui, None)) is False
    assert any("manually paused" in message for _lvl, message in ui.history)


def test_cooldown_expiry_requeries_instead_of_single_check(monkeypatch):
    """The F9 wedge: pause still active after its projected cooldown window."""

    reads = {"n": 0}

    def fake_pause():
        reads["n"] += 1
        if reads["n"] <= 3:
            # Cooldown keeps re-arming/ extending (sustained 1302 pressure).
            return _pause_state()
        return None

    waits = []

    def fake_wait(state, **kwargs):
        return 0.005  # projected cooldown elapses almost immediately

    monkeypatch.setattr(pac, "active_llm_pause", fake_pause)
    monkeypatch.setattr(pac, "pause_wait_seconds", fake_wait)
    ui = _UI()
    assert asyncio.run(pac._honor_active_llm_pause(ui, None)) is True
    assert reads["n"] >= 3  # it re-queried instead of deciding after one read


def test_cooldown_clears_after_one_window(monkeypatch):
    calls = {"n": 0}

    def fake_pause():
        calls["n"] += 1
        return _pause_state() if calls["n"] == 1 else None

    monkeypatch.setattr(pac, "active_llm_pause", fake_pause)
    monkeypatch.setattr(pac, "pause_wait_seconds", lambda state, **k: 0.005)
    assert asyncio.run(pac._honor_active_llm_pause(None, None)) is True
    assert calls["n"] == 2  # initial read + one re-query


def test_shutdown_during_cooldown_stops(monkeypatch):
    monkeypatch.setattr(pac, "active_llm_pause", lambda: _pause_state())
    monkeypatch.setattr(pac, "pause_wait_seconds", lambda state, **k: 30.0)
    mgr = _ShutdownMgr()
    result = asyncio.run(pac._honor_active_llm_pause(None, mgr))
    assert result is False


def test_manual_upgrade_mid_cooldown_stops(monkeypatch):
    reads = {"n": 0}

    def fake_pause():
        reads["n"] += 1
        return _pause_state() if reads["n"] == 1 else _manual_pause_state()

    def fake_wait(state, **kwargs):
        if "manual_billing" in str(state.get("category")):
            return None
        return 0.005

    monkeypatch.setattr(pac, "active_llm_pause", fake_pause)
    monkeypatch.setattr(pac, "pause_wait_seconds", fake_wait)
    assert asyncio.run(pac._honor_active_llm_pause(None, None)) is False


# ── _resume_generation_loop_after_llm_block ───────────────────────────────


def test_waitable_pause_resumes_when_store_clears(monkeypatch):
    reads = {"n": 0}

    def fake_active_pause():
        reads["n"] += 1
        return _pause_state() if reads["n"] <= 3 else None

    monkeypatch.setattr(pac, "active_llm_pause", fake_active_pause)
    monkeypatch.setattr(
        orchestrator, "load_llm_pause", lambda: _pause_state(resume_in=0.0)
    )
    monkeypatch.setattr(pac, "pause_wait_seconds", lambda state, **k: 0.0)
    result = asyncio.run(pac._resume_generation_loop_after_llm_block())
    assert result is True


def test_waitable_pause_polls_until_cleared(monkeypatch):
    polls = {"n": 0}

    def fake_load():
        polls["n"] += 1
        return _pause_state() if polls["n"] <= 4 else None

    monkeypatch.setattr(orchestrator, "load_llm_pause", fake_load)
    # The honor-level re-query stays wedged (pause never clears through
    # active_llm_pause inside one episode) so the resume loop itself must
    # keep re-reading the durable store on its 5s cadence.
    monkeypatch.setattr(pac, "active_llm_pause", lambda: _pause_state())
    monkeypatch.setattr(pac, "pause_wait_seconds", lambda state, **k: 0.0)
    monkeypatch.setattr(pac, "_HONOR_EPISODE_BUDGET_SEC", 0.05)
    assert asyncio.run(pac._resume_generation_loop_after_llm_block()) is True
    assert polls["n"] >= 4  # re-queried the durable store on the 5s cadence


def test_manual_pause_stops_the_loop(monkeypatch):
    monkeypatch.setattr(
        orchestrator,
        "load_llm_pause",
        lambda: _manual_pause_state(),
    )
    monkeypatch.setattr(pac, "active_llm_pause", lambda: _manual_pause_state())
    monkeypatch.setattr(pac, "pause_wait_seconds", lambda state, **k: None)
    assert asyncio.run(pac._resume_generation_loop_after_llm_block()) is False


def test_shutdown_stops_the_loop(monkeypatch):
    monkeypatch.setattr(orchestrator, "load_llm_pause", lambda: _pause_state())
    monkeypatch.setattr(pac, "active_llm_pause", lambda: _pause_state())
    monkeypatch.setattr(pac, "pause_wait_seconds", lambda state, **k: 0.0)
    mgr = _ShutdownMgr(shutting_down=True)
    assert asyncio.run(pac._resume_generation_loop_after_llm_block(None, mgr)) is False


def test_unreadable_pause_store_stops(monkeypatch):
    def boom():
        raise RuntimeError("store corrupted")

    monkeypatch.setattr(orchestrator, "load_llm_pause", boom)
    assert asyncio.run(pac._resume_generation_loop_after_llm_block()) is False


def test_inactive_pause_continues(monkeypatch):
    monkeypatch.setattr(orchestrator, "load_llm_pause", lambda: None)
    assert asyncio.run(pac._resume_generation_loop_after_llm_block()) is True


def test_bounded_wait_gives_up(monkeypatch):
    """A pause that never clears must not hang the loop forever."""

    monkeypatch.setattr(
        orchestrator, "load_llm_pause", lambda: _pause_state(resume_in=1e9)
    )
    monkeypatch.setattr(pac, "active_llm_pause", lambda: _pause_state())
    monkeypatch.setattr(pac, "pause_wait_seconds", lambda state, **k: 1e9)
    monkeypatch.setattr(pac, "_MAX_LLM_BLOCK_RESUME_WAIT_SEC", 0.05)
    assert asyncio.run(pac._resume_generation_loop_after_llm_block()) is False


# ── auto-restart classification (double insurance) ────────────────────────


def test_llm_block_outcome_is_crash_restartable():
    assert state_module._is_crash_outcome(-99995.0) is True
    assert state_module._is_crash_outcome(orchestrator.ORCH_LLM_AVAILABILITY_BLOCKED_COST) is True
    assert state_module._is_crash_outcome(-1.0) is True


def test_other_sentinels_stay_non_restartable():
    for outcome in (0.0, -99997.0, -99990.0, 7.5, True, None, "x"):
        assert state_module._is_crash_outcome(outcome) is False
