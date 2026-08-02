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
        return {"validation_outcome": "running"}

    def reject(self, *, candidate_id, reason, completed_at):
        self.rejected[candidate_id] = reason
        self._terminal = True


class _FakeActivation:
    def __init__(self, ledger, *, task_done: bool, task_present: bool = True):
        self.ledger = ledger
        self._consumer_tasks: dict[str, object] = {}
        if task_present:
            self._consumer_tasks["candidate-v31"] = SimpleNamespace(
                done=lambda: task_done
            )


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
