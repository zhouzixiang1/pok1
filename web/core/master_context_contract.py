"""System-owned handoff for scheduler evidence consumed by Master planning.

The orchestrator LLM sees this evidence in its own context and historically had
to copy it back into ``run_master`` arguments.  That made the outer model an
unnecessary, lossy authority: v146 turned a schema error into an instruction
that directly contradicted the reference-card registry.  The scheduler now
persists the exact evidence bundle and Master reloads it from the checkpoint.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


MASTER_CONTEXT_SCHEMA_VERSION = "master-context-v1"
MASTER_CONTEXT_IDENTITY_FIELDS = ("next_v", "source_v")
MASTER_CONTEXT_FIELDS = (
    "stagnation_info",
    "match_analysis",
    "performance_verification",
)


def _payload(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": context.get("schema_version"),
        **{
            field: context.get(field)
            for field in (*MASTER_CONTEXT_IDENTITY_FIELDS, *MASTER_CONTEXT_FIELDS)
        },
    }


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_master_context(
    *,
    next_v: int,
    source_v: int,
    stagnation_info: str = "",
    match_analysis: str = "",
    performance_verification: str = "",
) -> dict[str, Any]:
    payload = {
        "schema_version": MASTER_CONTEXT_SCHEMA_VERSION,
        "next_v": int(next_v),
        "source_v": int(source_v),
        "stagnation_info": str(stagnation_info or ""),
        "match_analysis": str(match_analysis or ""),
        "performance_verification": str(performance_verification or ""),
    }
    return {**payload, "context_digest": _digest(payload)}


def validate_master_context(
    context: dict[str, Any] | None,
    *,
    next_v: int | None = None,
    source_v: int | None = None,
) -> list[str]:
    if not isinstance(context, dict):
        return ["master_context_missing_or_not_object"]
    errors: list[str] = []
    expected_keys = {
        "schema_version",
        "context_digest",
        *MASTER_CONTEXT_IDENTITY_FIELDS,
        *MASTER_CONTEXT_FIELDS,
    }
    unknown = sorted(set(context).difference(expected_keys))
    missing = sorted(expected_keys.difference(context))
    if unknown:
        errors.append(f"master_context_unknown_fields:{unknown}")
    if missing:
        errors.append(f"master_context_missing_fields:{missing}")
    if context.get("schema_version") != MASTER_CONTEXT_SCHEMA_VERSION:
        errors.append(
            "master_context_schema_mismatch:"
            f"expected={MASTER_CONTEXT_SCHEMA_VERSION}:"
            f"actual={context.get('schema_version')!r}"
        )
    for field in MASTER_CONTEXT_IDENTITY_FIELDS:
        value = context.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"master_context_{field}_not_integer")
    for field in MASTER_CONTEXT_FIELDS:
        if not isinstance(context.get(field), str):
            errors.append(f"master_context_{field}_not_string")
    if not errors:
        expected_digest = _digest(_payload(context))
        if context.get("context_digest") != expected_digest:
            errors.append("master_context_digest_mismatch")
    if next_v is not None and context.get("next_v") != int(next_v):
        errors.append(
            f"master_context_next_v_mismatch:expected={int(next_v)}:"
            f"actual={context.get('next_v')!r}"
        )
    if source_v is not None and context.get("source_v") != int(source_v):
        errors.append(
            f"master_context_source_v_mismatch:expected={int(source_v)}:"
            f"actual={context.get('source_v')!r}"
        )
    return errors
