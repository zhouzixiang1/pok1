"""P5 (2026-10-05): write-failure visibility for the event / role-IO sinks.

Three Never-raises sinks historically swallowed every failure silently:
``event_bus._dispatch`` (``except Exception: pass`` on both the events.jsonl
append and the SSE broadcast), ``system_log.log_system_event`` (debug-level
forwarding failure), and ``llm_role_observability._append_role_io``
(``except Exception: pass``). A full disk or permission drift therefore
dropped events.jsonl rows and role-IO evidence bytes with zero operator
signal while the web process logs at INFO. The never-raises contract is
unchanged; the failures now surface as process-logger WARNINGs plus (for
``_dispatch``) module failure counters. No event is emitted from inside the
except handlers (recursion guard), and ``reset_for_test`` clears the
counters so they cannot leak across tests.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = WEB_DIR / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import event_bus  # noqa: E402
import system_log  # noqa: E402


@pytest.fixture
def isolated_events(tmp_path, monkeypatch):
    monkeypatch.setattr(event_bus, "EVENTS_FILE", tmp_path / "events.jsonl")
    event_bus.reset_for_test()
    yield tmp_path
    event_bus.reset_for_test()


def _minimal_event() -> dict:
    return {
        "ts": 0.0,
        "type": "pipeline.test",
        "severity": "info",
        "message": "m",
        "data": {},
    }


def test_dispatch_persist_failure_warns_and_counts(isolated_events, monkeypatch, caplog):
    """A events.jsonl append failure must not raise (never-raises contract)
    but must warn through the process logger and bump a failure counter —
    the old ``except: pass`` hid ledger loss from a full disk entirely."""
    import evolution_infra

    def _boom(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(evolution_infra, "append_locked_jsonl", _boom)
    before = event_bus.dispatch_persist_failures
    with caplog.at_level(logging.WARNING, logger="event_bus"):
        event_bus._dispatch(_minimal_event())  # must not raise
    assert event_bus.dispatch_persist_failures == before + 1
    records = [r for r in caplog.records if "events.jsonl" in r.getMessage()]
    assert records and "No space left on device" in records[0].getMessage(), (
        "the persist failure must be visible as a warning carrying the "
        f"underlying error; got: {[r.getMessage() for r in caplog.records]}"
    )


def test_dispatch_broadcast_failure_warns_and_counts(isolated_events, monkeypatch, caplog):
    """An SSE broadcast failure must equally warn + count without raising."""
    import system_log as sl

    def _boom(_entry):
        raise RuntimeError("SSE loop is closed")

    monkeypatch.setattr(sl, "broadcast_system_event", _boom)
    before = event_bus.dispatch_broadcast_failures
    with caplog.at_level(logging.WARNING, logger="event_bus"):
        event_bus._dispatch(_minimal_event())  # must not raise
    assert event_bus.dispatch_broadcast_failures == before + 1
    assert any("SSE loop is closed" in r.getMessage() for r in caplog.records)


def test_dispatch_failure_warning_emits_no_recursively_new_event(
    isolated_events, monkeypatch, caplog
):
    """The failure handler must stay side-effect-free on the event path:
    a persist failure whose warning machinery is fine may not dispatch
    another canonical event (recursion guard)."""
    import evolution_infra

    def _boom(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(evolution_infra, "append_locked_jsonl", _boom)
    dispatch_calls: list[dict] = []
    real_dispatch = event_bus._dispatch

    def _counting_dispatch(event):
        dispatch_calls.append(event)
        return real_dispatch(event)

    monkeypatch.setattr(event_bus, "_dispatch", _counting_dispatch)
    _counting_dispatch(_minimal_event())
    assert len(dispatch_calls) == 1, (
        "the failure path must not re-enter _dispatch with a new event"
    )
    ledger = isolated_events / "events.jsonl"
    assert not ledger.exists() or ledger.read_text(encoding="utf-8") == "", (
        "no event row may be written for the failure itself"
    )


def test_log_system_event_forward_failure_warns(monkeypatch, caplog):
    """log_system_event's forwarding failure must log at WARNING (was debug,
    invisible under the INFO-level web process) and still never raise."""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("emit exploded")

    monkeypatch.setattr(event_bus, "emit", _boom)
    with caplog.at_level(logging.WARNING, logger="system_log"):
        system_log.log_system_event("x.y", "warn", "m")  # must not raise
    records = [
        r
        for r in caplog.records
        if "x.y" in r.getMessage() or "emit exploded" in r.getMessage()
    ]
    assert records, (
        "the forwarding failure must be visible at WARNING level; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


def test_append_role_io_failure_warns(monkeypatch, tmp_path, caplog):
    """A role-IO append failure must stay non-fatal (docstring contract:
    'Returns silently on any error' — the stream result is unaffected) but
    become at least a process-logger WARNING instead of ``except: pass``."""
    import evolution_infra
    import llm_role_observability as obs

    def _boom(*_args, **_kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(evolution_infra, "locked_file", _boom)
    with caplog.at_level(logging.WARNING, logger="llm_role_observability"):
        obs._append_role_io(tmp_path / "role_io.txt", "terminal text")  # no raise
    assert any(
        "Permission denied" in r.getMessage() for r in caplog.records
    ), (
        "the role-IO append failure must be visible as a warning; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
