"""Shared file cache for route modules — eliminates per-module cache duplication."""

import fcntl
import json
import time
from pathlib import Path
from typing import Any, Callable

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 2.0

# mtime-keyed cache: (mtime_ns, ttl_deadline, value). The entry is valid only
# while the source file's mtime is unchanged AND the TTL has not expired. The
# daemon rewrites results files every few seconds during active rating, so the
# mtime check auto-invalidates the moment new data lands; the TTL is a short
# backstop that prevents unbounded growth during a long quiet period and caps
# staleness if the source file is touched without new content.
_MTIME_CACHE: dict[str, tuple[int, float, Any]] = {}
_MTIME_CACHE_TTL = 2.0


def read_locked(path: Path) -> Any:
    with open(path, "r") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            data = json.load(f)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return None
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return data


def cached_read(key: str, path: Path) -> Any:
    now = time.time()
    if key in _CACHE:
        mtime, data = _CACHE[key]
        if now - mtime < _CACHE_TTL:
            return data
    if not path.exists():
        return None
    try:
        data = read_locked(path)
    except (OSError, FileNotFoundError):
        return None
    if data is not None:
        _CACHE[key] = (now, data)
    return data


def cached_by_mtime(
    key: str,
    source_path: Path,
    producer: Callable[[], Any],
    *,
    ttl: float = _MTIME_CACHE_TTL,
) -> Any:
    """Cache ``producer()`` keyed on ``source_path``'s mtime.

    The expensive read-only projections behind the offloaded API handlers
    (ratings snapshot, metrics summary) reopen multiple JSON/JSONL files and
    recompute derived structures on every poll. The frontend polls several of
    these endpoints in a single burst, so within one burst the recomputation is
    pure waste.

    The cache entry is keyed on the source file's mtime (the daemon writes new
    data by atomically republishing ``evaluation_cycle_manifest.json`` for the
    ratings snapshot, and by appending to ``llm_call_metrics.jsonl`` for the
    metrics summary). The entry auto-invalidates the instant the source file's
    mtime changes, and a short TTL caps staleness / bounds memory if the file is
    touched without new content. A missing source file yields a cache miss and
    lets ``producer`` decide the empty-state value.

    Returns the cached value, or the freshly computed ``producer()`` result.
    """
    now = time.time()
    try:
        mtime_ns = source_path.stat().st_mtime_ns
    except (OSError, FileNotFoundError):
        # Source missing: fall through to producer (do not cache None, so the
        # next poll re-checks once the daemon publishes the first cycle).
        return producer()
    cached = _MTIME_CACHE.get(key)
    if cached is not None:
        cached_mtime, deadline, value = cached
        if cached_mtime == mtime_ns and now < deadline:
            return value
    value = producer()
    _MTIME_CACHE[key] = (mtime_ns, now + ttl, value)
    return value


def clear_mtime_cache() -> None:
    """Drop every mtime-keyed cache entry (test isolation helper)."""
    _MTIME_CACHE.clear()
