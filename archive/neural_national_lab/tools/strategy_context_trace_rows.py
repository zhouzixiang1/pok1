"""Validate and attach exact strategy trace context to counterfactual rows."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from strategy_context_schema import STRATEGY_CONTEXT_DIM, STRATEGY_CONTEXT_SCHEMA


def _integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        number = int(value)
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if not math.isfinite(numeric) or numeric != number:
        raise ValueError(f"{field} must be an integer")
    return number


def decision_key(row: dict[str, Any]) -> tuple[int, int, int]:
    return (
        _integer(row.get("hand"), field="hand"),
        _integer(row.get("hand_decision_index"), field="hand_decision_index"),
        _integer(row.get("decision_serial"), field="decision_serial"),
    )


def _context_fields(decision: dict[str, Any]) -> dict[str, Any]:
    context = decision.get("strategy_context")
    if not isinstance(context, dict):
        raise ValueError("decision trace is missing strategy_context")
    if context.get("schema") != STRATEGY_CONTEXT_SCHEMA:
        raise ValueError("decision trace has wrong strategy context schema")
    if _integer(context.get("dim"), field="strategy_context.dim") != (
        STRATEGY_CONTEXT_DIM
    ):
        raise ValueError("decision trace has wrong strategy context dimension")
    raw_features = context.get("features")
    if not isinstance(raw_features, list) or len(raw_features) != STRATEGY_CONTEXT_DIM:
        raise ValueError("decision trace has malformed strategy context features")
    features = []
    for value in raw_features:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("strategy context feature is not numeric") from exc
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise ValueError("strategy context feature is outside [0, 1]")
        features.append(number)
    raw = context.get("raw")
    if not isinstance(raw, dict):
        raise ValueError("decision trace has malformed raw strategy context")
    canonical = json.dumps(
        {
            "schema": STRATEGY_CONTEXT_SCHEMA,
            "features": features,
            "raw": raw,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "strategy_context_schema": STRATEGY_CONTEXT_SCHEMA,
        "strategy_context_features": features,
        "strategy_context_raw": raw,
        "strategy_context_available": bool(context.get("available")),
        "strategy_context_sha256": hashlib.sha256(canonical).hexdigest(),
        "strategy_context_value_head_only": True,
        "strategy_context_response_head_allowed": False,
    }


def attach_strategy_context(
    rows: list[dict[str, Any]],
    decision_trace: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    trace_by_key = {}
    for decision in decision_trace:
        if not isinstance(decision, dict) or decision.get("type") != "decision":
            continue
        key = decision_key(decision)
        if key in trace_by_key:
            raise ValueError(f"duplicate decision trace key: {key}")
        trace_by_key[key] = decision
    attached = []
    context_hashes = set()
    for row in rows:
        key = decision_key(row)
        decision = trace_by_key.get(key)
        if decision is None:
            raise ValueError(f"counterfactual row has no matching decision trace: {key}")
        if _integer(row.get("rule_final"), field="rule_final") != _integer(
            decision.get("final_action"), field="final_action"
        ):
            raise ValueError(f"rule action disagrees with decision trace: {key}")
        fields = _context_fields(decision)
        context_hashes.add(fields["strategy_context_sha256"])
        attached.append({**row, **fields})
    return attached, {
        "schema": "strategy_context_trace_join_v1",
        "rows": len(attached),
        "trace_decisions": len(trace_by_key),
        "unique_contexts": len(context_hashes),
        "strategy_context_schema": STRATEGY_CONTEXT_SCHEMA,
        "strategy_context_dim": STRATEGY_CONTEXT_DIM,
        "value_head_only": True,
        "response_head_allowed": False,
    }
