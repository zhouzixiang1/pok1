"""Tests for the Slice 2b primary park-sleep helper.

Regression coverage for ``orchestrator_loop_phases._parked_sleep``: when the
primary is parked at ``workers_done`` for the background consumer gate chain it
must sleep for a bounded interval (instead of re-cycling every second, which
caused a CPU/log busy-spin) and remain shutdown-interruptible.
"""

import asyncio

import pytest


def _import_parked_sleep():
    # Import lazily so the heavyweight orchestrator module is only loaded when
    # this test shard runs.
    from web.core import orchestrator_loop_phases as mod

    return mod._parked_sleep


class _ShutdownMgr:
    """Minimal shutdown manager double exposing ``wait_for_shutdown``."""

    def __init__(self, *, fire_after: float | None = None):
        # fire_after=None means "never fires" (wait forever).
        self._fire_after = fire_after
        self._event = asyncio.Event()

    async def wait_for_shutdown(self):
        if self._fire_after is not None:
            await asyncio.sleep(self._fire_after)
            self._event.set()
        else:
            await self._event.wait()

    def set(self):
        self._event.set()


@pytest.mark.asyncio
async def test_parked_sleep_timeout_returns_false(monkeypatch):
    parked_sleep = _import_parked_sleep()
    # A shutdown manager that never fires: the bounded timeout must elapse.
    mgr = _ShutdownMgr(fire_after=None)
    t0 = asyncio.get_event_loop().time()
    result = await parked_sleep(mgr, seconds=0.05)
    elapsed = asyncio.get_event_loop().time() - t0
    assert result is False
    assert elapsed >= 0.04  # waited ~the full bounded interval


@pytest.mark.asyncio
async def test_parked_sleep_shutdown_interrupts_returns_true():
    parked_sleep = _import_parked_sleep()
    # Shutdown fires well before the bounded timeout -> returns True quickly.
    mgr = _ShutdownMgr(fire_after=0.02)
    t0 = asyncio.get_event_loop().time()
    result = await parked_sleep(mgr, seconds=5.0)
    elapsed = asyncio.get_event_loop().time() - t0
    assert result is True
    assert elapsed < 1.0  # did not wait the full 5s


@pytest.mark.asyncio
async def test_parked_sleep_no_shutdown_mgr_uses_plain_sleep():
    parked_sleep = _import_parked_sleep()
    t0 = asyncio.get_event_loop().time()
    result = await parked_sleep(None, seconds=0.05)
    elapsed = asyncio.get_event_loop().time() - t0
    assert result is False
    assert elapsed >= 0.04
