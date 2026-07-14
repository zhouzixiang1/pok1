"""Decision-local capture of exact rule-path features for trace mode only."""
from __future__ import annotations

import math
from typing import Any

from strategy_context_schema import (
    STRATEGY_CONTEXT_DIM,
    STRATEGY_CONTEXT_SCHEMA,
    encode_strategy_context,
    summarize_range_weights,
)


_LAST_CONTEXT: dict[str, Any] | None = None


def reset_strategy_context() -> None:
    global _LAST_CONTEXT
    _LAST_CONTEXT = None


def publish_strategy_context(context: dict[str, Any]) -> None:
    global _LAST_CONTEXT
    _LAST_CONTEXT = context


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def consume_strategy_context() -> dict[str, Any]:
    global _LAST_CONTEXT
    context = _LAST_CONTEXT if isinstance(_LAST_CONTEXT, dict) else {}
    _LAST_CONTEXT = None
    weights = context.get("range_weights")
    compact = {
        key: value for key, value in context.items() if key != "range_weights"
    }
    compact["range_summary"] = summarize_range_weights(weights)
    features = encode_strategy_context(context)
    if len(features) != STRATEGY_CONTEXT_DIM:
        raise RuntimeError("strategy trace feature dimension mismatch")
    return {
        "schema": STRATEGY_CONTEXT_SCHEMA,
        "dim": STRATEGY_CONTEXT_DIM,
        "available": bool(context),
        "features": features,
        "raw": _json_safe(compact),
    }
