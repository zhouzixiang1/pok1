#!/usr/bin/env python3
"""Frozen process-environment contract for native v4 strength matches."""
from __future__ import annotations

import math
import os
from typing import Any, Mapping


RUNTIME_CONTRACT_SCHEMA = "opponent_multitask_v4_native_runtime_contract_v1"
DEFAULT_MATCH_TIMEOUT_SEC = 90.0
RUNTIME_ENVIRONMENT_OVERRIDES = {
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": os.defpath,
    "PYTHONIOENCODING": "utf-8",
    "TMPDIR": "/tmp",
    "TZ": "UTC",
    "OMP_NUM_THREADS": "2",
    "MKL_NUM_THREADS": "2",
    "OPENBLAS_NUM_THREADS": "2",
    "NUMEXPR_NUM_THREADS": "2",
    "VECLIB_MAXIMUM_THREADS": "2",
    "POK_NATIVE_LOCAL_ACTION_DELAY": "0",
    "POK_NATIVE_DECISION_HARD_DEADLINE_SEC": "2.0",
    "POK_NATIVE_DECISION_REFINEMENT_BUDGET_SEC": "1.8",
    "POK_NATIVE_DECISION_BASELINE_TARGET_SEC": "0.20",
    "POK_NATIONAL_STREAM_IDLE_FLUSH": "0.10",
}
RUNTIME_CONTRACT_KEYS = {
    "schema",
    "parent_environment_policy",
    "match_timeout_sec",
    "environment_overrides",
    "decision_controls",
}


def native_strength_runtime_contract(
    match_timeout_sec: float = DEFAULT_MATCH_TIMEOUT_SEC,
    *,
    trace_decisions: bool = False,
    force_hand: int | None = None,
    force_decision: int | None = None,
    force_action: int | None = None,
) -> dict[str, Any]:
    if isinstance(match_timeout_sec, bool) or not isinstance(match_timeout_sec, (int, float)):
        raise ValueError("match_timeout_sec must be a finite positive number")
    timeout = float(match_timeout_sec)
    if not math.isfinite(timeout) or timeout <= 0.0:
        raise ValueError("match_timeout_sec must be a finite positive number")
    if type(trace_decisions) is not bool:
        raise ValueError("trace_decisions must be a boolean")
    controls = {
        "trace_decisions": trace_decisions,
        "force_hand": force_hand,
        "force_decision": force_decision,
        "force_action": force_action,
    }
    if any(
        controls[name] is not None and type(controls[name]) is not int
        for name in ("force_hand", "force_decision", "force_action")
    ):
        raise ValueError("force controls must be integers or null")
    return {
        "schema": RUNTIME_CONTRACT_SCHEMA,
        "parent_environment_policy": "empty_then_exact_overrides_v1",
        "match_timeout_sec": timeout,
        "environment_overrides": dict(RUNTIME_ENVIRONMENT_OVERRIDES),
        "decision_controls": controls,
    }


def validate_native_strength_runtime_contract(
    raw: Any,
    *,
    require_default_timeout: bool,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("native strength runtime contract must be an object")
    if set(raw) != RUNTIME_CONTRACT_KEYS:
        raise ValueError("native strength runtime contract keys changed")
    timeout = raw.get("match_timeout_sec")
    controls = raw.get("decision_controls")
    if not isinstance(controls, Mapping):
        raise ValueError("native strength decision controls must be an object")
    expected = native_strength_runtime_contract(
        timeout,  # type: ignore[arg-type]
        trace_decisions=controls.get("trace_decisions"),  # type: ignore[arg-type]
        force_hand=controls.get("force_hand"),  # type: ignore[arg-type]
        force_decision=controls.get("force_decision"),  # type: ignore[arg-type]
        force_action=controls.get("force_action"),  # type: ignore[arg-type]
    )
    if dict(raw) != expected:
        raise ValueError("native strength runtime contract changed")
    if require_default_timeout and timeout != DEFAULT_MATCH_TIMEOUT_SEC:
        raise ValueError("native strength match timeout differs from the frozen default")
    if (
        require_default_timeout
        and controls != native_strength_runtime_contract()["decision_controls"]
    ):
        raise ValueError("native strength decision controls differ from frozen defaults")
    return expected


__all__ = [
    "DEFAULT_MATCH_TIMEOUT_SEC",
    "RUNTIME_CONTRACT_SCHEMA",
    "RUNTIME_ENVIRONMENT_OVERRIDES",
    "native_strength_runtime_contract",
    "validate_native_strength_runtime_contract",
]
