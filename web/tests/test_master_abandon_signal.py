"""Tests for the Master-abandon signal mechanism.

The signal lets the tool layer request a generation abandon that the
orchestrator loop finalizes against a quiescent checkpoint, instead of running
the publication-authority ``_do_abandon_generation`` inline from inside a tool
dispatch (which raced the concurrently-mutated checkpoint and lost every CAS
revalidation — the v161/v106 livelock class).
"""

import asyncio
import threading

import master_abandon_signal
import pytest


@pytest.fixture(autouse=True)
def _reset_signal():
    """Each test starts with a clean signal."""
    master_abandon_signal.clear()
    yield
    master_abandon_signal.clear()


def test_request_and_consume_basic():
    """request_abandon stores the reason; consume_pending pops it once."""
    assert master_abandon_signal.consume_pending() is None
    master_abandon_signal.request_abandon("master_validation_failed v999")
    assert master_abandon_signal.pending_reason() == "master_validation_failed v999"
    popped = master_abandon_signal.consume_pending()
    assert popped == "master_validation_failed v999"
    # Consumed — subsequent reads are empty.
    assert master_abandon_signal.consume_pending() is None
    assert master_abandon_signal.pending_reason() is None


def test_consume_is_idempotent_when_empty():
    """consume_pending returns None when nothing is pending (no error)."""
    assert master_abandon_signal.consume_pending() is None
    assert master_abandon_signal.consume_pending() is None


def test_clear_resets_everything():
    """clear() wipes a pending request and resets loop binding."""
    master_abandon_signal.request_abandon("some reason")
    assert master_abandon_signal.pending_reason() is not None
    master_abandon_signal.clear()
    assert master_abandon_signal.pending_reason() is None
    assert master_abandon_signal.consume_pending() is None


def test_pending_age_seconds():
    """pending_age_seconds reports wait time, None when empty."""
    assert master_abandon_signal.pending_age_seconds() is None
    master_abandon_signal.request_abandon("reason")
    age = master_abandon_signal.pending_age_seconds()
    assert age is not None and age >= 0.0
    master_abandon_signal.consume_pending()
    assert master_abandon_signal.pending_age_seconds() is None


def test_request_overwrites_previous():
    """A later request overwrites an earlier pending one (latest is authoritative)."""
    master_abandon_signal.request_abandon("first")
    master_abandon_signal.request_abandon("second")
    assert master_abandon_signal.consume_pending() == "second"


def test_request_from_worker_thread():
    """request_abandon is safe from a non-loop thread (native gates run off-loop)."""
    errors = []

    def _request():
        try:
            master_abandon_signal.request_abandon("from_thread")
        except Exception as exc:
            errors.append(exc)

    t = threading.Thread(target=_request)
    t.start()
    t.join()
    assert not errors
    assert master_abandon_signal.consume_pending() == "from_thread"


def test_consume_from_loop_binds_event():
    """consume_pending from a running loop lazily binds the asyncio.Event."""
    async def _run():
        # First consume binds the event to this loop.
        assert master_abandon_signal.consume_pending() is None
        # Request + consume within the same loop.
        master_abandon_signal.request_abandon("loop_reason")
        return master_abandon_signal.consume_pending()

    result = asyncio.get_event_loop().run_until_complete(_run()) \
        if not asyncio.iscoroutinefunction(_run) else asyncio.run(_run())
    assert result == "loop_reason"


def test_none_reason_normalizes():
    """request_abandon(None) normalizes to a default reason, not an error."""
    master_abandon_signal.request_abandon(None)
    reason = master_abandon_signal.consume_pending()
    assert reason is not None and isinstance(reason, str) and reason
