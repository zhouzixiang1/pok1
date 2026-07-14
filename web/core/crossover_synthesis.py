"""Durable, fenced LLM synthesis for crossover preparation.

The crossover projection journal protects the canonical bot directory.  This
module protects the expensive operation which precedes that projection: the
LLM edit of an isolated Parent-A-derived workspace.  A complete invocation is
written to :class:`workflow_kernel.WorkflowStore` before the provider is
called, and only the current lease epoch may publish the resulting immutable
artifact.

Completed effects are deliberately *not* treated as a gate pass.  Replay only
materializes the accepted LLM snapshot into a new private workspace; callers
must rerun every deterministic crossover gate before canonical projection.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid
from typing import Any

from bot_artifact import hash_path
from workflow_kernel import (
    EffectLease,
    WorkflowBusy,
    WorkflowConflict,
    canonical_json,
    content_digest,
)


CROSSOVER_SYNTHESIS_SCHEMA_VERSION = 1
CROSSOVER_SYNTHESIS_KIND = "crossover_llm_synthesis"
CROSSOVER_SYNTHESIS_LEASE_SECONDS = 3600.0
CROSSOVER_SYNTHESIS_MAX_LEASE_ATTEMPTS = 16
_OWNER_PREFIX = "crossover-synthesis-owner-v1"


def _frozen(value: Any) -> Any:
    return json.loads(canonical_json(value))


def build_synthesis_input(
    *,
    run_id: str,
    prompt: str,
    parent_a_v: int,
    parent_b_v: int,
    target_v: int,
    attempt: int,
    checkpoint: dict[str, Any],
    checkpoint_digest: str,
    parent_a_artifact_hash: str,
    parent_b_artifact_hash: str,
    input_snapshot_hash: str,
    compatibility_receipt: dict[str, Any],
    capability_context: dict[str, Any],
    architecture_policy: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Return stable effect/invocation ids and their complete frozen input."""
    semantic_attempt = int(attempt)
    if semantic_attempt < 1:
        raise ValueError("crossover synthesis attempt must be positive")
    base = {
        "schema_version": CROSSOVER_SYNTHESIS_SCHEMA_VERSION,
        "workflow_run_id": str(run_id),
        "prompt": str(prompt),
        "parents": {
            "parent_a_v": int(parent_a_v),
            "parent_b_v": int(parent_b_v),
            "parent_a_artifact_hash": str(parent_a_artifact_hash),
            "parent_b_artifact_hash": str(parent_b_artifact_hash),
        },
        "target_v": int(target_v),
        "attempt": semantic_attempt,
        "checkpoint": _frozen(checkpoint),
        "checkpoint_digest": str(checkpoint_digest),
        "input_snapshot_hash": str(input_snapshot_hash),
        "compatibility_receipt": _frozen(compatibility_receipt),
        "capability_context": _frozen(capability_context),
        "architecture_policy": _frozen(architecture_policy),
        "execution": {
            "tools": ["Bash", "Read", "Edit"],
            "write_scope": "isolated_crossover_workspace",
        },
    }
    invocation_id = f"crossover-invocation:{content_digest(base)}"
    payload = {**base, "invocation_id": invocation_id}
    effect_id = f"crossover-synthesis:{run_id}:attempt-{semantic_attempt}"
    return effect_id, invocation_id, payload


def ensure_synthesis_effect(
    *,
    store,
    run_id: str,
    effect_id: str,
    input_payload: dict[str, Any],
    definition_version: int,
) -> dict[str, Any]:
    """Durably request one immutable synthesis effect, idempotently."""
    store.ensure_instance(
        run_id,
        definition_version=int(definition_version),
    )
    # request_effect performs the authoritative full-input equality check.  Its
    # existing-row fast path is a raw SQLite row, so always reread through the
    # decoded public accessor before returning to the domain layer.
    store.request_effect(
        run_id=run_id,
        effect_id=effect_id,
        kind=CROSSOVER_SYNTHESIS_KIND,
        input_payload=input_payload,
        causation_id=f"crossover-synthesis-requested:{effect_id}",
        max_attempts=CROSSOVER_SYNTHESIS_MAX_LEASE_ATTEMPTS,
    )
    return store.effect(effect_id)


def claim_synthesis_effect(
    *,
    store,
    effect_id: str,
    invocation_id: str,
    lease_seconds: float = CROSSOVER_SYNTHESIS_LEASE_SECONDS,
) -> EffectLease:
    """Claim the only currently valid provider-call lease.

    A process crash would otherwise strand a one-hour provider lease.  Owners
    include Linux PID start ticks, so a later process may immediately fence a
    *provably dead* local owner (including PID reuse) while unknown or live
    owners remain protected until normal expiry.
    """
    owner = _lease_owner(invocation_id)
    duration = float(lease_seconds)
    try:
        return store.claim_effect(
            effect_id,
            owner=owner,
            lease_seconds=duration,
        )
    except WorkflowBusy:
        current = store.effect(effect_id)
        prior_owner = str(current.get("lease_owner") or "")
        lease_until = float(current.get("lease_until") or 0.0)
        if (
            current.get("status") != "running"
            or not _recognized_owner_is_dead(prior_owner)
            or lease_until <= 0
        ):
            raise
        actual_now = time.time()
        fenced_now = max(actual_now, lease_until + 0.001)
        # claim_effect accepts an injected clock.  Keep the resulting lease end
        # at roughly actual_now+duration instead of accidentally extending it
        # by the abandoned lease's remaining lifetime.
        adjusted_duration = max(0.001, actual_now + duration - fenced_now)
        return store.claim_effect(
            effect_id,
            owner=owner,
            lease_seconds=adjusted_duration,
            now=fenced_now,
        )


def _process_start_ticks(pid: int) -> str:
    try:
        # The comm field may contain spaces and parentheses.  Everything after
        # the final ')' starts at stat field 3; starttime is field 22.
        stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        tail = stat.rsplit(")", 1)[1].strip().split()
        return str(tail[19])
    except Exception:
        return ""


def _lease_owner(invocation_id: str) -> str:
    pid = os.getpid()
    start_ticks = _process_start_ticks(pid)
    if not start_ticks:
        # An unrecognized owner is intentionally not eligible for early steal.
        return f"{invocation_id}:opaque-owner-{uuid.uuid4().hex}"
    return (
        f"{_OWNER_PREFIX}:pid={pid}:start={start_ticks}:"
        f"nonce={uuid.uuid4().hex}"
    )


def _recognized_owner_is_dead(owner: str) -> bool:
    parts = str(owner).split(":")
    if len(parts) != 4 or parts[0] != _OWNER_PREFIX:
        return False
    try:
        pid = int(parts[1].split("=", 1)[1])
        expected_start = parts[2].split("=", 1)[1]
    except (IndexError, ValueError):
        return False
    current_start = _process_start_ticks(pid)
    return not current_start or current_start != expected_start


def _validated_completed_artifact(effect: dict[str, Any], artifact_store) -> str:
    if effect.get("status") != "completed":
        raise WorkflowConflict("crossover synthesis effect is not completed")
    input_payload = effect.get("input_payload") or {}
    result = effect.get("result_payload") or {}
    if (
        result.get("schema_version") != CROSSOVER_SYNTHESIS_SCHEMA_VERSION
        or result.get("invocation_id") != input_payload.get("invocation_id")
        or result.get("input_digest") != effect.get("input_digest")
    ):
        raise WorkflowConflict("crossover synthesis completion binding mismatch")
    artifact_hash = str(result.get("output_artifact_hash") or "")
    if len(artifact_hash) != 64:
        raise WorkflowConflict("crossover synthesis output artifact is missing")
    artifact_store.path_for(artifact_hash)
    return artifact_hash


def materialize_completed_effect(
    *,
    effect: dict[str, Any],
    workspace: str | Path,
    artifact_store,
) -> dict[str, Any]:
    """Restore the accepted snapshot; this intentionally performs no gates."""
    output_hash = _validated_completed_artifact(effect, artifact_store)
    destination = Path(workspace)
    current_hash = hash_path(destination)
    artifact_store.materialize(
        output_hash,
        destination,
        expected_destination_digest=current_hash,
    )
    return {
        "effect_id": str(effect["effect_id"]),
        "invocation_id": str((effect.get("input_payload") or {})["invocation_id"]),
        "input_digest": str(effect["input_digest"]),
        "output_artifact_hash": output_hash,
        "replayed": True,
    }


def complete_synthesis_effect(
    *,
    store,
    artifact_store,
    lease: EffectLease,
    invocation_id: str,
    workspace: str | Path,
) -> dict[str, Any]:
    """Capture provider output first, then accept it through the lease fence."""
    output_hash = artifact_store.capture(workspace)
    completion = store.complete_effect(
        lease.effect_id,
        lease_epoch=lease.lease_epoch,
        completion_id=(
            f"crossover-synthesis-completed:{lease.effect_id}:"
            f"epoch-{lease.lease_epoch}"
        ),
        result_payload={
            "schema_version": CROSSOVER_SYNTHESIS_SCHEMA_VERSION,
            "invocation_id": str(invocation_id),
            "input_digest": str(lease.input_digest),
            "output_artifact_hash": output_hash,
        },
        causation_id=(
            f"crossover-synthesis-effect-completed:{lease.effect_id}:"
            f"epoch-{lease.lease_epoch}"
        ),
    )
    if completion.get("accepted"):
        return {
            "effect_id": lease.effect_id,
            "invocation_id": str(invocation_id),
            "input_digest": str(lease.input_digest),
            "output_artifact_hash": output_hash,
            "replayed": False,
        }

    # At-least-once provider calls are allowed after an expired lease, but the
    # stale output is never authoritative.  If the competing lease already won,
    # return its immutable artifact; otherwise make the caller stop immediately.
    current = store.effect(lease.effect_id)
    if current.get("status") == "completed":
        return materialize_completed_effect(
            effect=current,
            workspace=workspace,
            artifact_store=artifact_store,
        )
    raise WorkflowBusy(
        f"crossover synthesis completion lost lease: {lease.effect_id}"
    )


def synthesis_receipt(effect: dict[str, Any], artifact_store) -> dict[str, Any]:
    """Return the content bindings safe to embed in the projection receipt."""
    output_hash = _validated_completed_artifact(effect, artifact_store)
    input_payload = effect.get("input_payload") or {}
    return {
        "schema_version": CROSSOVER_SYNTHESIS_SCHEMA_VERSION,
        "effect_id": str(effect["effect_id"]),
        "invocation_id": str(input_payload["invocation_id"]),
        "input_digest": str(effect["input_digest"]),
        "input_snapshot_hash": str(input_payload["input_snapshot_hash"]),
        "checkpoint_digest": str(input_payload["checkpoint_digest"]),
        "parent_a_artifact_hash": str(
            (input_payload.get("parents") or {})["parent_a_artifact_hash"]
        ),
        "parent_b_artifact_hash": str(
            (input_payload.get("parents") or {})["parent_b_artifact_hash"]
        ),
        "llm_output_artifact_hash": output_hash,
        "attempt": int(input_payload["attempt"]),
        "compatibility_receipt": _frozen(
            input_payload.get("compatibility_receipt") or {}
        ),
    }


__all__ = [
    "CROSSOVER_SYNTHESIS_KIND",
    "CROSSOVER_SYNTHESIS_SCHEMA_VERSION",
    "build_synthesis_input",
    "claim_synthesis_effect",
    "complete_synthesis_effect",
    "ensure_synthesis_effect",
    "materialize_completed_effect",
    "synthesis_receipt",
]
