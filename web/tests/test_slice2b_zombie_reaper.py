"""Regression tests for the slice2b zombie-consumer reaper.

The reaper (``_slice2b_reap_dead_consumer`` in
``orchestrator_deterministic_route``) detects a candidate left non-terminal
(``consuming``) whose asyncio consumer task is done/absent with an expired
effect lease, and marks it ``rejected`` so the primary lane canonically
abandons the generation instead of busy-parking forever.

These tests stub the activation registry + ledger + workflow store so the
reaper logic can be exercised in isolation without a full orchestrator.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

WEB_CORE = Path(__file__).resolve().parent.parent / "core"
if str(WEB_CORE) not in sys.path:
    sys.path.insert(0, str(WEB_CORE))


class _FakeLedger:
    """Minimal ledger stub recording reject() calls."""

    def __init__(self, *, terminal: bool = False):
        self._terminal = terminal
        self.rejected: dict[str, str] = {}

    def is_terminal(self, candidate_id: str) -> bool:
        return self._terminal

    def snapshot(self, candidate_id: str):
        if self._terminal:
            return {"validation_outcome": "rejected"}
        return {
            "validation_outcome": "running",
            "envelope_effect_id": "producer-consumer-job:test",
        }

    def reject(self, *, candidate_id, reason, completed_at):
        self.rejected[candidate_id] = reason
        self._terminal = True


class _FakeActivation:
    def __init__(
        self,
        ledger,
        *,
        task_done: bool,
        task_present: bool = True,
        scheduled_factory: bool = False,
    ):
        self.ledger = ledger
        self._consumer_tasks: dict[str, object] = {}
        if task_present:
            self._consumer_tasks["candidate-v31"] = SimpleNamespace(
                done=lambda: task_done
            )
        # A scheduled factory means recover_at_boot re-stashed the consumer but
        # ensure_consumer_running has not yet materialized the asyncio.Task --
        # the restart-recovery window.
        self._scheduled_factories: dict[str, tuple] = {}
        if scheduled_factory:
            self._scheduled_factories["candidate-v31"] = ()


def _seed_expired_effect(results_dir: Path, *, expired: bool = True) -> None:
    """Write a workflow events.sqlite3 with an expired/valid consumer effect."""
    db = results_dir / "workflow" / "events.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "CREATE TABLE effects (effect_id TEXT PRIMARY KEY, kind TEXT, status TEXT, "
        "lease_until REAL)"
    )
    lease_until = (time.time() - 100.0) if expired else (time.time() + 1000.0)
    conn.execute(
        "INSERT INTO effects VALUES (?, ?, ?, ?)",
        ("producer-consumer-job:test", "producer-consumer-job:quality-static",
         "running", lease_until),
    )
    conn.commit()
    conn.close()


def test_reaper_marks_zombie_candidate_rejected(monkeypatch, tmp_path):
    """A non-terminal candidate with a done consumer task + expired lease is reaped."""
    import orchestrator as _o
    import orchestrator_deterministic_route as route

    results_dir = tmp_path / "results"
    _seed_expired_effect(results_dir, expired=True)
    monkeypatch.setattr("evolution_infra.RESULTS_DIR", results_dir)

    ledger = _FakeLedger(terminal=False)
    activation = _FakeActivation(ledger, task_done=True)

    monkeypatch.setattr(
        route, "_slice2b_ensure_activation", lambda: activation
    )
    monkeypatch.setattr(
        "producer_consumer_slice2b_activation.slice2b_active", lambda: True
    )

    checkpoint = {"candidate_id": "candidate-v31", "next_v": 31}
    reaped = route._slice2b_reap_dead_consumer(checkpoint, 31)

    assert reaped is True
    assert ledger.rejected.get("candidate-v31") == "consumer_task_zombie_reaped"


def test_reaper_does_not_reap_live_consumer_task(monkeypatch, tmp_path):
    """A non-terminal candidate with a LIVE (not-done) consumer task is NOT reaped."""
    import orchestrator as _o
    import orchestrator_deterministic_route as route

    results_dir = tmp_path / "results"
    _seed_expired_effect(results_dir, expired=True)
    monkeypatch.setattr("evolution_infra.RESULTS_DIR", results_dir)

    ledger = _FakeLedger(terminal=False)
    activation = _FakeActivation(ledger, task_done=False)  # task still running

    monkeypatch.setattr(
        route, "_slice2b_ensure_activation", lambda: activation
    )
    monkeypatch.setattr(
        "producer_consumer_slice2b_activation.slice2b_active", lambda: True
    )

    checkpoint = {"candidate_id": "candidate-v31", "next_v": 31}
    reaped = route._slice2b_reap_dead_consumer(checkpoint, 31)

    assert reaped is False
    assert "candidate-v31" not in ledger.rejected


def test_reaper_reaps_wedged_consumer_with_stale_checkpoint(monkeypatch, tmp_path):
    """A live task whose consumer checkpoint is unchanged for >45 min is reaped.

    The task may be alive (``done()=False``) but wedged inside a gate handler
    that never returns (e.g. ``run_precommit_eval`` blocked on a hung native
    match).  A 45-min silent gap (no checkpoint progress) is a real stall, not
    normal slow operation -- the gate chain writes intermediate progress.
    """
    import orchestrator as _o
    import orchestrator_deterministic_route as route

    results_dir = tmp_path / "results"
    _seed_expired_effect(results_dir, expired=True)
    monkeypatch.setattr("evolution_infra.RESULTS_DIR", results_dir)

    ledger = _FakeLedger(terminal=False)
    activation = _FakeActivation(ledger, task_done=True)  # wedged (done)

    monkeypatch.setattr(
        route, "_slice2b_ensure_activation", lambda: activation
    )
    monkeypatch.setattr(
        "producer_consumer_slice2b_activation.slice2b_active", lambda: True
    )

    # Write a stale consumer checkpoint (mtime 1 hour ago).
    consumer_slot = "consumer-candidate-v31"
    (results_dir).mkdir(parents=True, exist_ok=True)
    import os
    cp_path = results_dir / f"pipeline_state_{consumer_slot}.json"
    cp_path.write_text("{}")
    stale_time = time.time() - 3600.0  # 1 hour ago
    os.utime(cp_path, (stale_time, stale_time))

    def fake_pipeline_state_path(slot_id):
        return results_dir / f"pipeline_state_{slot_id}.json"

    monkeypatch.setattr("evolution_infra.pipeline_state_path", fake_pipeline_state_path)

    class _LedgerWithSlot(_FakeLedger):
        def consumer_checkpoint_slot(self, candidate_id):
            return consumer_slot

    activation.ledger = _LedgerWithSlot(terminal=False)

    checkpoint = {"candidate_id": "candidate-v31", "next_v": 31}
    reaped = route._slice2b_reap_dead_consumer(checkpoint, 31)

    assert reaped is True
    assert activation.ledger.rejected.get("candidate-v31") == "consumer_task_zombie_reaped"


def test_reaper_does_not_reap_when_lease_still_valid(monkeypatch, tmp_path):
    """A non-terminal candidate with a done task but VALID lease is NOT reaped
    (the consumer may be mid-renewal; fail-open to avoid killing live work)."""
    import orchestrator as _o
    import orchestrator_deterministic_route as route

    results_dir = tmp_path / "results"
    _seed_expired_effect(results_dir, expired=False)  # lease still valid
    monkeypatch.setattr("evolution_infra.RESULTS_DIR", results_dir)

    ledger = _FakeLedger(terminal=False)
    activation = _FakeActivation(ledger, task_done=True)

    monkeypatch.setattr(
        route, "_slice2b_ensure_activation", lambda: activation
    )
    monkeypatch.setattr(
        "producer_consumer_slice2b_activation.slice2b_active", lambda: True
    )

    checkpoint = {"candidate_id": "candidate-v31", "next_v": 31}
    reaped = route._slice2b_reap_dead_consumer(checkpoint, 31)

    assert reaped is False
    assert "candidate-v31" not in ledger.rejected


def test_reaper_does_not_reap_in_restart_recovery_window(monkeypatch, tmp_path):
    """A non-terminal candidate with NO task but a SCHEDULED FACTORY is NOT reaped.

    Regression: after a process restart, _consumer_tasks is empty (in-memory).
    recover_at_boot re-stashes the consumer factory into _scheduled_factories,
    but does NOT launch the asyncio.Task -- only ensure_consumer_running
    (called later in the loop) materializes it. The reaper used to treat the
    empty task registry as a death signal and reaped EVERY restart, abandoning
    a legitimately-running (or about-to-be-relaunched) consumer. A scheduled
    factory is positive proof the task has not been relaunched yet; do NOT reap.
    """
    import orchestrator_deterministic_route as route

    results_dir = tmp_path / "results"
    _seed_expired_effect(results_dir, expired=True)  # even an expired lease
    monkeypatch.setattr("evolution_infra.RESULTS_DIR", results_dir)

    ledger = _FakeLedger(terminal=False)
    # No task present (restart), but a factory IS scheduled (recover_at_boot).
    activation = _FakeActivation(
        ledger, task_done=True, task_present=False, scheduled_factory=True
    )

    monkeypatch.setattr(route, "_slice2b_ensure_activation", lambda: activation)
    monkeypatch.setattr(
        "producer_consumer_slice2b_activation.slice2b_active", lambda: True
    )

    checkpoint = {"candidate_id": "candidate-v31", "next_v": 31}
    reaped = route._slice2b_reap_dead_consumer(checkpoint, 31)

    assert reaped is False
    assert "candidate-v31" not in ledger.rejected


def test_reaper_fails_open_when_lease_unreadable(monkeypatch, tmp_path):
    """If the consumer effect lease cannot be read, the reaper must NOT reap.

    Regression: the lease read used ``except Exception: pass`` which silently
    converted any read failure (locked sqlite, missing db, schema mismatch)
    into a reap. Reaping abandons a generation -- an irreversible action that
    must require positive proof of death, not the absence of a readable lease.
    Fail OPEN (return False) on lease-read errors.
    """
    import orchestrator_deterministic_route as route

    results_dir = tmp_path / "results"
    # No events.sqlite3 seeded at all -> the lease read finds no db and the
    # scoped lookup raises / returns nothing.
    monkeypatch.setattr("evolution_infra.RESULTS_DIR", results_dir)

    ledger = _FakeLedger(terminal=False)
    # Task is done (genuinely absent), no scheduled factory, no readable lease.
    activation = _FakeActivation(ledger, task_done=True, task_present=False)

    monkeypatch.setattr(route, "_slice2b_ensure_activation", lambda: activation)
    monkeypatch.setattr(
        "producer_consumer_slice2b_activation.slice2b_active", lambda: True
    )

    checkpoint = {"candidate_id": "candidate-v31", "next_v": 31}
    reaped = route._slice2b_reap_dead_consumer(checkpoint, 31)

    # No positive proof of death (lease unreadable) -> must NOT reap.
    assert reaped is False
    assert "candidate-v31" not in ledger.rejected


def test_reaper_skips_already_terminal_candidate(monkeypatch, tmp_path):
    """A candidate already terminal (promoted/rejected) is never reaped."""
    import orchestrator as _o
    import orchestrator_deterministic_route as route

    results_dir = tmp_path / "results"
    _seed_expired_effect(results_dir, expired=True)
    monkeypatch.setattr("evolution_infra.RESULTS_DIR", results_dir)

    ledger = _FakeLedger(terminal=True)  # already rejected
    activation = _FakeActivation(ledger, task_done=True)

    monkeypatch.setattr(
        route, "_slice2b_ensure_activation", lambda: activation
    )
    monkeypatch.setattr(
        "producer_consumer_slice2b_activation.slice2b_active", lambda: True
    )

    checkpoint = {"candidate_id": "candidate-v31", "next_v": 31}
    reaped = route._slice2b_reap_dead_consumer(checkpoint, 31)

    assert reaped is False
    assert "candidate-v31" not in ledger.rejected
