"""Selective glibc ``malloc_trim`` for the web process (2026-10-11 heap work).

The service heap historically grows in RSS steps that never fall back
(``pok.memory`` heartbeat evidence; ``MemoryHigh`` pressure on the 3.6 GiB
VM).  Two complementary countermeasures were added 2026-10-11:

1. ``MALLOC_ARENA_MAX=2`` in ``deploy/tencent-cloud/env.runtime`` (loaded by
   the unit's ``EnvironmentFile=``) bounds glibc's per-thread arenas (default
   8 x cores = 32 on this 4-vCPU box), the main suspect for the
   never-returned RSS steps.
2. This module: a periodic ``libc.malloc_trim(0)`` driven from the existing
   memory heartbeat (``server/app.py::_memory_heartbeat`` 1s tick), enabled
   only when ``POK_MALLOC_TRIM_INTERVAL_SEC > 0`` (default 0 = off).

``malloc_trim(0)`` asks glibc to return free top-of-heap pages (across
arenas, where the glibc build supports it) to the OS.  With the arena cap
in place the trim has bounded work to do and cannot livelock; everything
here is best-effort and fail-silent: a libc without ``malloc_trim`` (musl,
exotic platforms) or any ctypes failure simply disables the behavior —
it must never affect the dispatch or lifespan paths.

Testing seam: ``maybe_malloc_trim`` accepts an injectable ``libc`` (the
``ctypes`` object or any fake exposing ``malloc_trim``) plus an injectable
``interval_sec``/``now`` so the regression tests prove call invocation and
cadence without a real allocator.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time

_LIBC = None
_LIBC_RESOLVED = False
_LIBC_LOCK = threading.Lock()

_LAST_TRIM_MONOTONIC: "float | None" = None
_PERIOD_LOCK = threading.Lock()


def malloc_trim_interval_sec() -> float:
    """``POK_MALLOC_TRIM_INTERVAL_SEC`` (seconds); ``<= 0`` (default) is off."""

    raw = os.environ.get("POK_MALLOC_TRIM_INTERVAL_SEC", "")
    try:
        value = float(raw) if raw else 0.0
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, value)


def _resolve_libc():
    """Best-effort ``ctypes`` handle on glibc; ``None`` when unavailable."""

    global _LIBC, _LIBC_RESOLVED
    with _LIBC_LOCK:
        if not _LIBC_RESOLVED:
            _LIBC_RESOLVED = True
            try:
                candidate = ctypes.CDLL("libc.so.6", use_errno=True)
                _LIBC = candidate if hasattr(candidate, "malloc_trim") else None
            except (OSError, AttributeError):
                _LIBC = None
        return _LIBC


def maybe_malloc_trim(
    now: "float | None" = None,
    *,
    libc=None,
    interval_sec: "float | None" = None,
) -> bool:
    """Run ``libc.malloc_trim(0)`` when the interval has elapsed.

    Returns ``True`` only when a trim was actually attempted and returned
    success.  Disabled (``interval <= 0``), an unresolvable libc, a missing
    ``malloc_trim`` symbol, or any ctypes error degrades silently to
    ``False``.  Thread-safe: the cadence is guarded by a lock so concurrent
    callers cannot double-trim inside one interval.
    """

    global _LAST_TRIM_MONOTONIC
    try:
        interval = (
            malloc_trim_interval_sec() if interval_sec is None else float(interval_sec)
        )
    except (TypeError, ValueError):
        return False
    if interval <= 0:
        return False
    when = time.monotonic() if now is None else float(now)
    with _PERIOD_LOCK:
        last = _LAST_TRIM_MONOTONIC
        if last is not None and (when - last) < interval:
            return False
        _LAST_TRIM_MONOTONIC = when
    try:
        lib = libc if libc is not None else _resolve_libc()
        if lib is None or not hasattr(lib, "malloc_trim"):
            return False
        lib.malloc_trim(0)
        return True
    except Exception:
        # Silent degradation by contract (see module docstring): never let
        # allocator housekeeping surface as a service error.
        return False


def reset_malloc_trim_state_for_tests() -> None:
    """Drop the cadence/libc caches so a test starts from a clean slate."""

    global _LAST_TRIM_MONOTONIC, _LIBC, _LIBC_RESOLVED
    with _PERIOD_LOCK:
        _LAST_TRIM_MONOTONIC = None
    with _LIBC_LOCK:
        _LIBC = None
        _LIBC_RESOLVED = False
