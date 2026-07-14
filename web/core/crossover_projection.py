"""Crash-safe publication of an isolated crossover candidate.

The crossover LLM works only in a private workspace.  This module is the sole
canonical projection boundary: a durable intent is fsynced before candidate
bytes move, then the candidate and semantic checkpoint are reconciled as one
recoverable effect.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid
from typing import Any

from workflow_kernel import canonical_json, content_digest


CROSSOVER_PROJECTION_SCHEMA_VERSION = 2
_PROJECTION_RECEIPT_SCHEMA_VERSION = 1
_PRE_WORKER_COMMITTED_STAGES = frozenset({
    "crossover_running",
    "prepared",
    "direction_audited",
    "master_planned",
})
_DOWNSTREAM_COMMITTED_STAGES = frozenset({
    "workers_done",
    "quality_failed",
    "quality_passed",
    "reviewed",
    "critic_checked",
    "precommit_failed",
    "repair_planned",
    "rework_running",
    "verified",
    "official_bootstrap_required",
    "official_certifying",
    "official_failed",
    "official_inconclusive",
    "archived",
})


def checkpoint_digest(checkpoint: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            checkpoint or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def projection_failure(component: str, issue: str, **extra) -> dict[str, Any]:
    payload = {
        "success": False,
        "failure_class": "infrastructure",
        "outcome": "infrastructure_failure",
        "component": str(component),
        "infrastructure_failures": [{
            "component": str(component),
            "failure_class": "projection_infrastructure",
            "issues": [str(issue)],
        }],
    }
    payload.update(extra)
    return payload


def target_identity(target_dir: str | Path) -> dict[str, Any]:
    from bot_artifact import hash_path

    target = Path(target_dir)
    if not target.exists():
        return {"exists": False, "artifact_hash": ""}
    if not target.is_dir():
        raise RuntimeError(f"crossover target is not a directory: {target}")
    return {"exists": True, "artifact_hash": hash_path(target)}


def _journal_path(workflow_store, run_id: str) -> Path:
    root = Path(workflow_store.path).parent / "crossover_projections"
    key = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()
    return root / f"{key}.json"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _intent_with_digest(payload: dict[str, Any]) -> dict[str, Any]:
    frozen = json.loads(canonical_json(payload))
    frozen["intent_digest"] = content_digest(frozen)
    return frozen


def _write_intent(path: Path, payload: dict[str, Any]) -> None:
    """Publish a complete no-clobber intent after fsyncing its bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    encoded = canonical_json(payload).encode("utf-8")
    try:
        with open(temporary, "xb") as writer:
            writer.write(encoded)
            writer.flush()
            os.fsync(writer.fileno())
        os.link(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _load_intent(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("crossover projection intent is unreadable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("crossover projection intent is not an object")
    if payload.get("schema_version") != CROSSOVER_PROJECTION_SCHEMA_VERSION:
        raise RuntimeError("crossover projection intent schema mismatch")
    claimed = str(payload.get("intent_digest") or "")
    body = {key: value for key, value in payload.items() if key != "intent_digest"}
    if claimed != content_digest(body):
        raise RuntimeError("crossover projection intent digest mismatch")
    return payload


def _remove_intent(path: Path) -> None:
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _checkpoint_is_committed(checkpoint: Any, intent: dict[str, Any]) -> bool:
    if not isinstance(checkpoint, dict):
        return False
    workflow_id = str(
        checkpoint.get("workflow_run_id") or checkpoint.get("run_id") or ""
    )
    receipt = (
        (checkpoint.get("audit_context") or {}).get("crossover")
        if isinstance(checkpoint.get("audit_context"), dict)
        else None
    )
    projection_receipt = (
        receipt.get("projection") if isinstance(receipt, dict) else None
    )
    expected_projection_receipt = intent.get("projection_receipt")
    stage = str(checkpoint.get("stage") or "")
    committed_revision = int(
        (expected_projection_receipt or {}).get("committed_revision") or -1
    )
    expected_semantics = (
        (expected_projection_receipt or {}).get("crossover_semantics") or {}
    )
    return bool(
        workflow_id == str(intent.get("workflow_run_id") or "")
        and checkpoint.get("next_v") == intent.get("target_v")
        and checkpoint.get("source_v") == intent.get("parent_a_v")
        and checkpoint.get("parent2_v") == intent.get("parent_b_v")
        and stage in (_PRE_WORKER_COMMITTED_STAGES | _DOWNSTREAM_COMMITTED_STAGES)
        and int(checkpoint.get("checkpoint_revision") or -1)
        >= committed_revision
        and isinstance(receipt, dict)
        and receipt.get("parent_a") == intent.get("parent_a_v")
        and receipt.get("parent_b") == intent.get("parent_b_v")
        and receipt.get("isolated_output_artifact_hash")
        == intent.get("output_artifact_hash")
        and receipt.get("architecture_policy_digest")
        == intent.get("architecture_policy_digest")
        and isinstance(projection_receipt, dict)
        and projection_receipt == expected_projection_receipt
        and projection_receipt.get("schema_version")
        == _PROJECTION_RECEIPT_SCHEMA_VERSION
        and projection_receipt.get("projection_id")
        == intent.get("projection_id")
        and isinstance(expected_semantics, dict)
        and all(receipt.get(key) == value for key, value in expected_semantics.items())
    )


def _write_bound_checkpoint(intent: dict[str, Any]) -> bool:
    from evolution_infra import write_pipeline_checkpoint

    return bool(write_pipeline_checkpoint(
        int(intent["target_v"]),
        int(intent["parent_a_v"]),
        "crossover_running",
        parent2_v=int(intent["parent_b_v"]),
        touch_stage_timestamp=True,
        audit_context={"crossover": dict(intent["crossover_receipt"])},
        expected_checkpoint_revision=int(intent["expected_checkpoint_revision"]),
        expected_checkpoint_stage=str(intent["expected_checkpoint_stage"]),
        expected_workflow_run_id=str(intent["workflow_run_id"]),
    ))


def _restore_preimage(intent, target_dir: Path, artifact_store) -> None:
    if intent["entry_target_identity"]["exists"]:
        artifact_store.materialize(
            str(intent["preimage_artifact_hash"]),
            target_dir,
            expected_destination_digest=str(intent["output_artifact_hash"]),
        )
    else:
        artifact_store.remove_if_matches(
            target_dir,
            str(intent["output_artifact_hash"]),
        )


def _validate_intent_scope(
    intent: dict[str, Any],
    *,
    run_id: str,
    target_dir: Path,
    parent_a_v: int,
    parent_b_v: int,
    target_v: int,
) -> None:
    expected = {
        "workflow_run_id": str(run_id),
        "parent_a_v": int(parent_a_v),
        "parent_b_v": int(parent_b_v),
        "target_v": int(target_v),
        "target_path": str(target_dir.resolve()),
    }
    for field, value in expected.items():
        if intent.get(field) != value:
            raise RuntimeError(f"crossover projection intent {field} mismatch")


def recover_crossover_projection(
    *,
    entry_checkpoint: dict[str, Any],
    target_dir: str | Path,
    parent_a_v: int,
    parent_b_v: int,
    target_v: int,
    workflow_store,
    artifact_store,
) -> bool | dict[str, Any] | None:
    """Finish or roll back a projection interrupted by process death."""
    from evolution_infra import read_pipeline_checkpoint
    from worker_workflow import workflow_run_id

    run_id = workflow_run_id(entry_checkpoint)
    journal = _journal_path(workflow_store, run_id)
    if not journal.exists():
        return None
    target = Path(target_dir)
    try:
        intent = _load_intent(journal)
        _validate_intent_scope(
            intent,
            run_id=run_id,
            target_dir=target,
            parent_a_v=parent_a_v,
            parent_b_v=parent_b_v,
            target_v=target_v,
        )
    except Exception as exc:
        return projection_failure(
            "crossover_projection_recovery",
            f"invalid_projection_intent:{type(exc).__name__}:{str(exc)[:240]}",
        )

    with workflow_store.command_lock(run_id, blocking=True):
        current_checkpoint = read_pipeline_checkpoint() or {}
        try:
            current_target = target_identity(target)
        except Exception as exc:
            return projection_failure(
                "crossover_projection_recovery",
                f"target_identity_error:{type(exc).__name__}:{str(exc)[:240]}",
            )
        output_identity = {
            "exists": True,
            "artifact_hash": intent["output_artifact_hash"],
        }
        if _checkpoint_is_committed(current_checkpoint, intent):
            stage = str(current_checkpoint.get("stage") or "")
            if (
                stage in _PRE_WORKER_COMMITTED_STAGES
                and current_target != output_identity
            ):
                return projection_failure(
                    "crossover_projection_recovery",
                    "committed_checkpoint_candidate_mismatch",
                    current_target_identity=current_target,
                )
            _remove_intent(journal)
            if stage == "crossover_running":
                return True
            return {
                "success": False,
                "absorbed": True,
                "action": "follow_checkpoint",
                "projection_recovery": "committed_downstream_intent_absorbed",
                "checkpoint_stage": stage,
            }

        expected_checkpoint = (
            checkpoint_digest(current_checkpoint)
            == intent["expected_checkpoint_digest"]
        )
        allowed_preimages = [intent["entry_target_identity"], output_identity]
        interrupted_old_move = bool(
            intent["entry_target_identity"]["exists"]
            and current_target == {"exists": False, "artifact_hash": ""}
            and expected_checkpoint
        )
        if current_target not in allowed_preimages and not interrupted_old_move:
            return projection_failure(
                "crossover_projection_recovery",
                "canonical_target_changed_while_projection_pending",
                current_target_identity=current_target,
            )
        if not expected_checkpoint:
            if current_target == output_identity:
                install_receipt = artifact_store.materialize(
                    str(intent["output_artifact_hash"]),
                    target,
                    expected_destination_digest=(
                        str(intent["entry_target_identity"]["artifact_hash"])
                        if intent["entry_target_identity"]["exists"]
                        else None
                    ),
                    operation_id=str(intent["install_operation_id"]),
                )
                if install_receipt.installed:
                    _restore_preimage(intent, target, artifact_store)
            _remove_intent(journal)
            return projection_failure(
                "crossover_projection_conflict",
                "checkpoint_changed_while_projection_pending",
            )

        try:
            install_receipt = artifact_store.materialize(
                str(intent["output_artifact_hash"]),
                target,
                expected_destination_digest=(
                    str(current_target["artifact_hash"])
                    if current_target["exists"]
                    else None
                ),
                operation_id=str(intent["install_operation_id"]),
            )
            checkpoint_ok = _write_bound_checkpoint(intent)
        except BaseException:
            # Process death and infrastructure exceptions leave the durable
            # intent in place.  A later invocation re-enters this reducer.
            raise
        latest_checkpoint = read_pipeline_checkpoint() or {}
        if checkpoint_ok or _checkpoint_is_committed(latest_checkpoint, intent):
            if target_identity(target) != output_identity:
                return projection_failure(
                    "crossover_projection_recovery",
                    "candidate_changed_after_checkpoint_commit",
                )
            _remove_intent(journal)
            return True
        if target_identity(target) != output_identity:
            return projection_failure(
                "crossover_projection_recovery",
                "candidate_changed_after_recovery_projection",
            )
        if install_receipt.installed:
            _restore_preimage(intent, target, artifact_store)
        _remove_intent(journal)
        return projection_failure(
            "crossover_projection_conflict", "checkpoint_cas_refused"
        )


def absorb_committed_crossover_projection(
    *,
    checkpoint: dict[str, Any],
    target_dir: str | Path,
    workflow_store,
) -> dict[str, Any] | None:
    """Clear a stale intent already proven by a causal checkpoint receipt.

    This reducer is safe to run before normal tool route checks.  It never
    projects or rolls back candidate bytes.  Before Workers, the canonical
    child must still equal the crossover output; at ``workers_done`` and later
    legitimate Worker changes are expected and only the stale intent is
    absorbed.
    """

    if not isinstance(checkpoint, dict):
        return None
    run_id = str(
        checkpoint.get("workflow_run_id") or checkpoint.get("run_id") or ""
    )
    if not run_id:
        return None
    journal = _journal_path(workflow_store, run_id)
    if not journal.exists():
        return None
    try:
        intent = _load_intent(journal)
        _validate_intent_scope(
            intent,
            run_id=run_id,
            target_dir=Path(target_dir),
            parent_a_v=int(checkpoint.get("source_v")),
            parent_b_v=int(checkpoint.get("parent2_v")),
            target_v=int(checkpoint.get("next_v")),
        )
    except Exception as exc:
        return projection_failure(
            "crossover_projection_absorber",
            f"invalid_projection_intent:{type(exc).__name__}:{str(exc)[:240]}",
        )
    if not _checkpoint_is_committed(checkpoint, intent):
        return None
    stage = str(checkpoint.get("stage") or "")
    with workflow_store.command_lock(run_id, blocking=True):
        if stage in _PRE_WORKER_COMMITTED_STAGES:
            expected = {
                "exists": True,
                "artifact_hash": str(intent["output_artifact_hash"]),
            }
            try:
                actual = target_identity(target_dir)
            except Exception as exc:
                return projection_failure(
                    "crossover_projection_absorber",
                    f"target_identity_error:{type(exc).__name__}:{str(exc)[:240]}",
                )
            if actual != expected:
                return projection_failure(
                    "crossover_projection_absorber",
                    "committed_checkpoint_candidate_mismatch",
                    current_target_identity=actual,
                )
        _remove_intent(journal)
    return {
        "success": True,
        "absorbed": True,
        "projection_recovery": "committed_intent_absorbed_pre_dispatch",
        "checkpoint_stage": stage,
    }


def completed_crossover_projection(
    *,
    checkpoint: dict[str, Any],
    target_dir: str | Path,
    parent_a_v: int,
    parent_b_v: int,
    target_v: int,
    architecture_policy: dict[str, Any] | None,
) -> bool | dict[str, Any] | None:
    """Return an idempotent success for a previously committed projection."""
    if checkpoint.get("stage") != "crossover_running":
        return None
    receipt = (checkpoint.get("audit_context") or {}).get("crossover") or {}
    projection_receipt = (
        receipt.get("projection") if isinstance(receipt, dict) else None
    )
    expected_policy = str((architecture_policy or {}).get("policy_digest") or "")
    output_hash = str(receipt.get("isolated_output_artifact_hash") or "")
    valid = bool(
        checkpoint.get("next_v") == int(target_v)
        and checkpoint.get("source_v") == int(parent_a_v)
        and checkpoint.get("parent2_v") == int(parent_b_v)
        and receipt.get("parent_a") == int(parent_a_v)
        and receipt.get("parent_b") == int(parent_b_v)
        and receipt.get("architecture_policy_digest") == expected_policy
        and isinstance(projection_receipt, dict)
        and projection_receipt.get("schema_version")
        == _PROJECTION_RECEIPT_SCHEMA_VERSION
        and projection_receipt.get("workflow_run_id")
        == str(checkpoint.get("workflow_run_id") or checkpoint.get("run_id") or "")
        and projection_receipt.get("parent_a_v") == int(parent_a_v)
        and projection_receipt.get("parent_b_v") == int(parent_b_v)
        and projection_receipt.get("target_v") == int(target_v)
        and projection_receipt.get("output_artifact_hash") == output_hash
        and isinstance(projection_receipt.get("crossover_semantics"), dict)
        and all(
            receipt.get(key) == value
            for key, value in projection_receipt[
                "crossover_semantics"
            ].items()
        )
        and int(checkpoint.get("checkpoint_revision") or -1)
        >= int(projection_receipt.get("committed_revision") or -1)
        and projection_receipt.get("projection_id")
        == content_digest({
            key: value
            for key, value in projection_receipt.items()
            if key != "projection_id"
        })
        and len(output_hash) == 64
        and target_identity(target_dir)
        == {"exists": True, "artifact_hash": output_hash}
    )
    if valid:
        return True
    return projection_failure(
        "crossover_projection_contract",
        "crossover_running_receipt_or_candidate_mismatch",
    )


def project_crossover_candidate(
    *,
    workspace,
    target_dir,
    parent_a_v,
    parent_b_v,
    target_v,
    attempt,
    compatibility,
    architecture_policy,
    synthesis_receipt=None,
    entry_checkpoint,
    entry_target_identity,
    preimage_artifact_hash,
    workflow_store,
    artifact_store,
):
    """Publish output under a durable intent and checkpoint CAS."""
    from evolution_infra import read_pipeline_checkpoint
    from worker_workflow import workflow_run_id

    output_artifact_hash = artifact_store.capture(workspace)
    run_id = workflow_run_id(entry_checkpoint)
    expected_digest = checkpoint_digest(entry_checkpoint)
    expected_revision = int(entry_checkpoint.get("checkpoint_revision") or 0)
    expected_stage = str(entry_checkpoint.get("stage") or "")
    target = Path(target_dir)
    receipt = {
        "parent_a": int(parent_a_v),
        "parent_b": int(parent_b_v),
        "attempt": int(attempt) + 1,
        "compatibility": compatibility or {},
        "synthesis_effect": synthesis_receipt or {},
        "architecture_policy_digest": str(
            (architecture_policy or {}).get("policy_digest") or ""
        ),
        "isolated_output_artifact_hash": output_artifact_hash,
    }
    projection_contract = {
        "schema_version": _PROJECTION_RECEIPT_SCHEMA_VERSION,
        "workflow_run_id": run_id,
        "parent_a_v": int(parent_a_v),
        "parent_b_v": int(parent_b_v),
        "target_v": int(target_v),
        "target_path": str(target.resolve()),
        "expected_checkpoint_digest": expected_digest,
        "expected_checkpoint_revision": expected_revision,
        "expected_checkpoint_stage": expected_stage,
        "committed_revision": expected_revision + 1,
        "entry_target_identity": entry_target_identity,
        "preimage_artifact_hash": str(preimage_artifact_hash or ""),
        "output_artifact_hash": output_artifact_hash,
        "architecture_policy_digest": receipt["architecture_policy_digest"],
        "crossover_semantics": json.loads(canonical_json(receipt)),
    }
    projection_id = content_digest(projection_contract)
    install_operation_id = f"crossover-install-{projection_id}"
    projection_receipt = {
        **projection_contract,
        "projection_id": projection_id,
    }
    receipt["projection"] = projection_receipt
    intent = _intent_with_digest({
        "schema_version": CROSSOVER_PROJECTION_SCHEMA_VERSION,
        "workflow_run_id": run_id,
        "parent_a_v": int(parent_a_v),
        "parent_b_v": int(parent_b_v),
        "target_v": int(target_v),
        "target_path": str(target.resolve()),
        "expected_checkpoint_digest": expected_digest,
        "expected_checkpoint_revision": expected_revision,
        "expected_checkpoint_stage": expected_stage,
        "entry_target_identity": entry_target_identity,
        "preimage_artifact_hash": str(preimage_artifact_hash or ""),
        "output_artifact_hash": output_artifact_hash,
        "architecture_policy_digest": receipt["architecture_policy_digest"],
        "projection_id": projection_id,
        "projection_receipt": projection_receipt,
        "install_operation_id": install_operation_id,
        "crossover_receipt": receipt,
    })
    journal = _journal_path(workflow_store, run_id)

    with workflow_store.command_lock(run_id, blocking=True):
        current_checkpoint = read_pipeline_checkpoint() or {}
        if checkpoint_digest(current_checkpoint) != expected_digest:
            return projection_failure(
                "crossover_projection_conflict",
                "checkpoint_changed_before_projection",
                expected_checkpoint_revision=expected_revision,
                current_checkpoint_revision=int(
                    current_checkpoint.get("checkpoint_revision") or 0
                ),
            )
        try:
            current_target_identity = target_identity(target)
        except Exception as exc:
            return projection_failure(
                "crossover_projection_conflict",
                f"target_identity_error:{type(exc).__name__}:{str(exc)[:240]}",
            )
        if current_target_identity != entry_target_identity:
            return projection_failure(
                "crossover_projection_conflict",
                "canonical_target_changed_before_projection",
                expected_target_identity=entry_target_identity,
                current_target_identity=current_target_identity,
            )
        if journal.exists():
            return projection_failure(
                "crossover_projection_recovery",
                "pending_projection_intent_requires_recovery",
            )
        _write_intent(journal, intent)

        try:
            install_receipt = artifact_store.materialize(
                output_artifact_hash,
                target,
                expected_destination_digest=(
                    str(entry_target_identity["artifact_hash"])
                    if entry_target_identity["exists"]
                    else None
                ),
                operation_id=install_operation_id,
            )
            checkpoint_ok = _write_bound_checkpoint(intent)
        except BaseException:
            raise
        latest_checkpoint = read_pipeline_checkpoint() or {}
        if checkpoint_ok or _checkpoint_is_committed(latest_checkpoint, intent):
            if target_identity(target) != {
                "exists": True,
                "artifact_hash": output_artifact_hash,
            }:
                return projection_failure(
                    "crossover_projection_recovery",
                    "candidate_changed_after_checkpoint_commit",
                )
            _remove_intent(journal)
            return True

        try:
            projected_identity = target_identity(target)
            if projected_identity != {
                "exists": True,
                "artifact_hash": output_artifact_hash,
            }:
                raise RuntimeError(
                    "canonical target changed after projection; refusing rollback"
                )
            if install_receipt.installed:
                _restore_preimage(intent, target, artifact_store)
            _remove_intent(journal)
        except Exception as rollback_exc:
            return projection_failure(
                "crossover_projection_rollback",
                "checkpoint_projection_failed_and_preimage_restore_failed:"
                f"{type(rollback_exc).__name__}:{str(rollback_exc)[:240]}",
            )
        return projection_failure(
            "crossover_projection_conflict", "checkpoint_cas_refused"
        )


__all__ = [
    "absorb_committed_crossover_projection",
    "checkpoint_digest",
    "completed_crossover_projection",
    "project_crossover_candidate",
    "projection_failure",
    "recover_crossover_projection",
    "target_identity",
]
