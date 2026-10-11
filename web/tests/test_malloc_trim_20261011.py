"""Selective malloc_trim countermeasure (2026-10-11): call + cadence + degrade.

Covers ``web/core/malloc_trim.py`` (new) and its memory-heartbeat wiring:

- default (``POK_MALLOC_TRIM_INTERVAL_SEC`` unset / 0) never trims;
- with an interval, the first call trims and the next call inside the window
  does not, then the boundary trims again (injectable fake libc + clock);
- a raising or symbol-less libc degrades silently (returns False, raises
  nothing);
- the env spellings that must disable are exactly 0/absent/garbage.
"""

from __future__ import annotations

import pytest

from malloc_trim import (
    malloc_trim_interval_sec,
    maybe_malloc_trim,
    reset_malloc_trim_state_for_tests,
)


class FakeLibc:
    def __init__(self, *, fail: bool = False, missing_symbol: bool = False):
        self.calls = []
        self._fail = fail
        self._missing = missing_symbol

    def malloc_trim(self, pad):
        if self._missing:
            raise AttributeError("no malloc_trim")
        if self._fail:
            raise RuntimeError("simulated allocator failure")
        self.calls.append(pad)
        return 0


@pytest.fixture(autouse=True)
def _clean_trim_state():
    reset_malloc_trim_state_for_tests()
    yield
    reset_malloc_trim_state_for_tests()


def test_interval_env_default_and_spellings_20261011(monkeypatch):
    monkeypatch.delenv("POK_MALLOC_TRIM_INTERVAL_SEC", raising=False)
    assert malloc_trim_interval_sec() == 0.0
    monkeypatch.setenv("POK_MALLOC_TRIM_INTERVAL_SEC", "0")
    assert malloc_trim_interval_sec() == 0.0
    monkeypatch.setenv("POK_MALLOC_TRIM_INTERVAL_SEC", "not-a-number")
    assert malloc_trim_interval_sec() == 0.0
    monkeypatch.setenv("POK_MALLOC_TRIM_INTERVAL_SEC", "-5")
    assert malloc_trim_interval_sec() == 0.0
    monkeypatch.setenv("POK_MALLOC_TRIM_INTERVAL_SEC", "1800")
    assert malloc_trim_interval_sec() == 1800.0


def test_disabled_interval_never_calls_libc_20261011(monkeypatch):
    monkeypatch.setenv("POK_MALLOC_TRIM_INTERVAL_SEC", "0")
    libc = FakeLibc()
    assert maybe_malloc_trim(now=0.0, libc=libc) is False
    assert maybe_malloc_trim(now=1000.0, libc=libc) is False
    assert libc.calls == []


def test_trim_fires_once_per_interval_window_20261011():
    libc = FakeLibc()
    interval = 300.0
    # First call: no last-trim stamp yet -> trims immediately.
    assert maybe_malloc_trim(now=1000.0, libc=libc, interval_sec=interval) is True
    assert libc.calls == [0]
    # Inside the window: suppressed regardless of caller.
    assert maybe_malloc_trim(now=1299.9, libc=libc, interval_sec=interval) is False
    assert libc.calls == [0]
    # Boundary elapsed (when - last >= interval): trims again, and the
    # window restarts from that trim.
    assert maybe_malloc_trim(now=1300.0, libc=libc, interval_sec=interval) is True
    assert libc.calls == [0, 0]
    assert maybe_malloc_trim(now=1300.5, libc=libc, interval_sec=interval) is False
    assert maybe_malloc_trim(now=1599.9, libc=libc, interval_sec=interval) is False
    assert libc.calls == [0, 0]
    assert maybe_malloc_trim(now=1600.0, libc=libc, interval_sec=interval) is True
    assert libc.calls == [0, 0, 0]


def test_env_driven_interval_matches_injected_interval_20261011(monkeypatch):
    monkeypatch.setenv("POK_MALLOC_TRIM_INTERVAL_SEC", "100")
    libc = FakeLibc()
    assert maybe_malloc_trim(now=0.0, libc=libc) is True
    assert maybe_malloc_trim(now=99.0, libc=libc) is False
    assert maybe_malloc_trim(now=100.0, libc=libc) is True
    assert maybe_malloc_trim(now=100.5, libc=libc) is False
    assert libc.calls == [0, 0]


def test_failing_or_symbol_less_libc_degrades_silently_20261011():
    failing = FakeLibc(fail=True)
    assert maybe_malloc_trim(now=0.0, libc=failing, interval_sec=10.0) is False
    missing = FakeLibc(missing_symbol=True)
    assert maybe_malloc_trim(now=0.0, libc=missing, interval_sec=10.0) is False
    # A libc object with no attribute at all.
    class Empty:
        pass

    assert maybe_malloc_trim(now=0.0, libc=Empty(), interval_sec=10.0) is False
    # Real-libc path on this box must also degrade silently rather than
    # raise, whatever _resolve_libc finds (glibc with the symbol -> a real,
    # harmless trim of this tiny test heap; anything else -> False).
    assert maybe_malloc_trim(now=0.0, interval_sec=10.0) in (True, False)
