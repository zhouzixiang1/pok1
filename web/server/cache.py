"""Shared file cache for route modules — eliminates per-module cache duplication."""

import fcntl
import json
import time
from pathlib import Path
from typing import Any

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 2.0


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
