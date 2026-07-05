"""Runtime memory for structurally incompatible crossover parent pairs."""

from __future__ import annotations

import time
from typing import Any


CACHE_FILENAME = "crossover_incompatibilities.json"


def _version(value: int | str) -> int:
    return int(value)


def crossover_pair_key(parent_a: int | str, parent_b: int | str) -> str:
    """Return the canonical unordered key for a crossover pair."""
    a = _version(parent_a)
    b = _version(parent_b)
    lo, hi = sorted((a, b))
    return f"{lo}x{hi}"


def _cache_path():
    import evolution_infra as infra

    return infra.RESULTS_DIR / CACHE_FILENAME


def _read_cache() -> dict[str, Any]:
    import evolution_infra as infra

    data = infra.read_locked_json(_cache_path(), default={})
    if not isinstance(data, dict):
        return {}
    pairs = data.get("pairs")
    if not isinstance(pairs, dict):
        data["pairs"] = {}
    history = data.get("history")
    if not isinstance(history, list):
        data["history"] = []
    return data


def _write_cache(data: dict[str, Any]) -> None:
    import evolution_infra as infra

    infra.write_locked_json(_cache_path(), data)


def is_crossover_pair_blocked(parent_a: int | str, parent_b: int | str) -> bool:
    """Return True when this parent pair has been rejected as incompatible."""
    key = crossover_pair_key(parent_a, parent_b)
    record = (_read_cache().get("pairs") or {}).get(key)
    return bool(isinstance(record, dict) and record.get("blocked") is True)


def get_blocked_crossover_pairs() -> dict[str, dict[str, Any]]:
    """Return blocked pair records keyed by canonical pair key."""
    pairs = _read_cache().get("pairs") or {}
    return {
        str(key): dict(value)
        for key, value in pairs.items()
        if isinstance(value, dict) and value.get("blocked") is True
    }


def record_incompatible_crossover(
    parent_a: int | str,
    parent_b: int | str,
    *,
    target_v: int | str | None = None,
    compatibility: dict[str, Any] | None = None,
    reason: str = "compatibility_audit",
) -> dict[str, Any]:
    """Persist an incompatible parent-pair rejection under runtime results."""
    a = _version(parent_a)
    b = _version(parent_b)
    key = crossover_pair_key(a, b)
    compat = compatibility if isinstance(compatibility, dict) else {}
    now = time.time()

    score = compat.get("compatibility_score")
    try:
        score = int(score) if score is not None else None
    except (TypeError, ValueError):
        score = None

    conflicts = compat.get("conflict_areas") or []
    if not isinstance(conflicts, list):
        conflicts = [str(conflicts)]
    conflicts = [str(item)[:300] for item in conflicts[:8]]

    entry = {
        "pair_key": key,
        "parent_a": a,
        "parent_b": b,
        "target_v": _version(target_v) if target_v is not None else None,
        "reason": str(reason),
        "compatibility_score": score,
        "conflict_areas": conflicts,
        "suggested_merge_approach": str(compat.get("suggested_merge_approach") or "")[:500],
        "timestamp": now,
    }

    data = _read_cache()
    pairs = data.setdefault("pairs", {})
    existing = pairs.get(key) if isinstance(pairs.get(key), dict) else {}
    first_seen = existing.get("first_seen_ts") or now
    count = int(existing.get("count") or 0) + 1
    record = {
        "pair": sorted((a, b)),
        "blocked": True,
        "first_seen_ts": first_seen,
        "last_seen_ts": now,
        "count": count,
        "last": entry,
    }
    pairs[key] = record

    history = data.setdefault("history", [])
    if isinstance(history, list):
        history.append(entry)
        del history[:-200]
    _write_cache(data)
    return record
