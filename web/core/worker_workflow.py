"""Durable Worker activity domain and immutable candidate artifact store."""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid
from typing import Any

from bot_artifact import artifact_manifest, canonical_digest, hash_path
from workflow_kernel import (
    EffectLease,
    WorkflowEvent,
    WorkflowStore,
    WorkflowBusy,
    canonical_json,
    content_digest,
    reduce_events,
)


WORKER_WORKFLOW_DEFINITION_VERSION = 3
WORKER_ENVELOPE_SCHEMA_VERSION = 3
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_MATERIALIZATION_JOURNAL_SCHEMA_VERSION = 3
_MATERIALIZATION_RECEIPT_SCHEMA_VERSION = 1
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


@dataclass(frozen=True)
class MaterializationReceipt:
    """Durable result of one canonical namespace CAS.

    ``installed`` is true only when this operation moved its requested bytes
    into the canonical path.  Callers must not roll back a checkpoint CAS when
    it is false: the same output was already owned by another operation.
    Retired trees are deliberately preserved under the ignored artifact store;
    recursive deletion is not part of the publication transaction.
    """

    operation_id: str
    operation: str
    digest: str
    installed: bool
    receipt_digest: str
    retained_path: str = ""
    retained_digest: str = ""


def workflow_run_id(checkpoint: dict[str, Any]) -> str:
    workflow_id = str(checkpoint.get("workflow_run_id") or "").strip()
    if workflow_id:
        return workflow_id
    run_id = str(checkpoint.get("run_id") or "").strip()
    if run_id:
        return run_id
    return (
        f"{int(checkpoint.get('next_v'))}#"
        f"{int(checkpoint.get('generation_attempt') or 0)}"
    )


def _without_digest(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "envelope_digest"}


def build_worker_envelope(
    *,
    checkpoint: dict[str, Any],
    kind: str,
    source_stage: str,
    prepared_artifact_hash: str,
    prepared_snapshot_hash: str,
    source_artifact_hash: str,
    tasks: list[dict[str, Any]],
    reviewer_feedback: str,
    worker_template_hash: str,
    work_item: dict[str, Any] | None,
    backend_contract: dict[str, Any],
    precommit_rework_count: int,
    official_rework_count: int,
    projection_plan: dict[str, Any] | None = None,
    audit_context: dict[str, Any] | None = None,
    execution_policy: dict[str, Any] | None = None,
    checkpoint_contract: dict[str, Any] | None = None,
    worker_failure_count: int | None = None,
    projection_preimage_artifact_hash: str | None = None,
    projection_preimage_snapshot_hash: str | None = None,
) -> dict[str, Any]:
    frozen_checkpoint_contract = checkpoint_contract or {
        "workflow_run_id": workflow_run_id(checkpoint),
        "checkpoint_revision": int(checkpoint.get("checkpoint_revision") or 0),
        "checkpoint_stage": str(checkpoint.get("stage") or ""),
    }
    envelope = {
        "schema_version": WORKER_ENVELOPE_SCHEMA_VERSION,
        "definition_version": WORKER_WORKFLOW_DEFINITION_VERSION,
        "run_id": workflow_run_id(checkpoint),
        "next_v": int(checkpoint.get("next_v")),
        "source_v": int(checkpoint.get("source_v")),
        "kind": str(kind),
        "source_stage": str(source_stage),
        "prepared_artifact_hash": str(prepared_artifact_hash),
        "prepared_snapshot_hash": str(prepared_snapshot_hash),
        "projection_preimage_artifact_hash": str(
            projection_preimage_artifact_hash or prepared_artifact_hash
        ),
        "projection_preimage_snapshot_hash": str(
            projection_preimage_snapshot_hash or prepared_snapshot_hash
        ),
        "source_artifact_hash": str(source_artifact_hash),
        "tasks": json.loads(canonical_json(tasks)),
        "reviewer_feedback": str(reviewer_feedback or ""),
        "worker_template_hash": str(worker_template_hash),
        "work_item": json.loads(canonical_json(work_item or {})),
        "backend_contract": json.loads(canonical_json(backend_contract)),
        "precommit_rework_count": int(precommit_rework_count),
        "official_rework_count": int(official_rework_count),
        "worker_failure_count": int(
            checkpoint.get("worker_failure_count") or 0
            if worker_failure_count is None
            else worker_failure_count
        ),
        "projection_plan": json.loads(
            canonical_json(
                projection_plan
                if projection_plan is not None
                else checkpoint.get("master_plan") or {}
            )
        ),
        "audit_context": json.loads(canonical_json(audit_context or {})),
        "execution_policy": json.loads(canonical_json(execution_policy or {})),
        "checkpoint_contract": json.loads(
            canonical_json(frozen_checkpoint_contract)
        ),
    }
    envelope["envelope_digest"] = content_digest(envelope)
    return envelope


def validate_worker_envelope(envelope: Any) -> list[str]:
    if not isinstance(envelope, dict):
        return ["worker_envelope_not_object"]
    errors = []
    if envelope.get("schema_version") != WORKER_ENVELOPE_SCHEMA_VERSION:
        errors.append("worker_envelope_schema_version_mismatch")
    if envelope.get("definition_version") != WORKER_WORKFLOW_DEFINITION_VERSION:
        errors.append("worker_workflow_definition_version_mismatch")
    for field in (
        "run_id",
        "kind",
        "source_stage",
        "prepared_artifact_hash",
        "prepared_snapshot_hash",
        "projection_preimage_artifact_hash",
        "projection_preimage_snapshot_hash",
        "source_artifact_hash",
        "worker_template_hash",
        "envelope_digest",
    ):
        if not str(envelope.get(field) or "").strip():
            errors.append(f"worker_envelope_{field}_missing")
    for field in (
        "prepared_artifact_hash",
        "prepared_snapshot_hash",
        "projection_preimage_artifact_hash",
        "projection_preimage_snapshot_hash",
        "source_artifact_hash",
        "worker_template_hash",
        "envelope_digest",
    ):
        value = str(envelope.get(field) or "")
        if value and (len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)):
            errors.append(f"worker_envelope_{field}_invalid")
    if (
        envelope.get("projection_preimage_artifact_hash")
        != envelope.get("projection_preimage_snapshot_hash")
    ):
        errors.append("worker_envelope_projection_preimage_snapshot_mismatch")
    if not isinstance(envelope.get("tasks"), list) or not envelope.get("tasks"):
        errors.append("worker_envelope_tasks_missing")
    if "worker_execution_context" in envelope:
        errors.append("worker_envelope_legacy_prompt_context_forbidden")
    if not isinstance(envelope.get("work_item"), dict):
        errors.append("worker_envelope_work_item_invalid")
    for field in (
        "projection_plan",
        "audit_context",
        "execution_policy",
        "checkpoint_contract",
    ):
        if not isinstance(envelope.get(field), dict):
            errors.append(f"worker_envelope_{field}_invalid")
    checkpoint_contract = envelope.get("checkpoint_contract") or {}
    if not str(checkpoint_contract.get("workflow_run_id") or "").strip():
        errors.append("worker_envelope_checkpoint_workflow_id_missing")
    if not str(checkpoint_contract.get("checkpoint_stage") or "").strip():
        errors.append("worker_envelope_checkpoint_stage_missing")
    try:
        if int(checkpoint_contract.get("checkpoint_revision")) < 0:
            errors.append("worker_envelope_checkpoint_revision_invalid")
    except (TypeError, ValueError):
        errors.append("worker_envelope_checkpoint_revision_invalid")
    expected = content_digest(_without_digest(envelope))
    if envelope.get("envelope_digest") != expected:
        errors.append("worker_envelope_digest_mismatch")
    return errors


def initial_worker_state(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "status": "idle",
        "cycle": 0,
        "envelope": None,
        "repair_prepared_count": 0,
        "effect_id": "",
        "attempt": 0,
        "semantic_attempt": 0,
        "max_attempts": 0,
        "failure_class": "",
        "availability": None,
        "output_artifact_hash": "",
        "output_snapshot_hash": "",
        "projected_stage": "",
        "projection": None,
        "failure_projection": None,
        "abandon_reason": "",
        "last_seq": 0,
    }


def reduce_worker_event(
    state: dict[str, Any],
    event: WorkflowEvent,
) -> dict[str, Any]:
    """Pure Worker event reducer; it performs no external reads."""
    result = {**state, "last_seq": event.seq}
    if state.get("status") == "abandoned":
        return result
    payload = event.payload
    if event.event_type == "WorkerPrepared":
        result.update({
            "status": "prepared",
            "envelope": payload["envelope"],
            "repair_prepared_count": int(state["repair_prepared_count"]) + 1,
            "effect_id": str(payload["effect_id"]),
            "attempt": 0,
            "semantic_attempt": 0,
            "max_attempts": int(payload.get("max_attempts") or 3),
            "failure_class": "",
            "availability": None,
        })
    elif event.event_type == "WorkerCycleOpened":
        result.update({
            "status": "idle",
            "cycle": int(state.get("cycle") or 0) + 1,
            "envelope": None,
            "effect_id": "",
            "attempt": 0,
            "semantic_attempt": 0,
            "failure_class": "",
            "output_artifact_hash": "",
            "output_snapshot_hash": "",
            "projected_stage": "",
            "projection": None,
            "failure_projection": None,
            "abandon_reason": "",
        })
    elif event.event_type == "WorkerSuperseded":
        result.update({
            "status": "completed",
            "attempt": 0,
            "failure_class": "semantic",
            "projected_stage": str(payload.get("stage") or "direction_audited"),
            "projection": None,
            "failure_projection": None,
        })
    elif event.event_type == "EffectRequested" and payload.get("kind") in {
        "worker_llm",
        "system_blueprint",
    }:
        result.update({
            "status": "requested",
            "effect_id": str(payload["effect_id"]),
            "max_attempts": int(payload.get("max_attempts") or result["max_attempts"]),
        })
    elif event.event_type == "EffectFailed" and payload.get("effect_id") == result["effect_id"]:
        result.update({
            "status": "retry_wait" if payload.get("retryable") else "exhausted",
            "attempt": int(payload.get("attempt") or result["attempt"]),
            "failure_class": "infrastructure",
        })
    elif event.event_type == "EffectDeferred" and payload.get("effect_id") == result["effect_id"]:
        result.update({
            "status": "availability_deferred",
            "attempt": int(payload.get("restored_attempt") or 0),
            "failure_class": "availability",
            "availability": (payload.get("metadata") or {}).get(
                "availability"
            ) or {},
        })
    elif event.event_type == "EffectResumed" and payload.get("effect_id") == result["effect_id"]:
        result.update({
            "status": "requested",
            "attempt": int(payload.get("attempt") or 0),
            "failure_class": "",
            "availability": None,
        })
    elif event.event_type == "WorkerSemanticFailed":
        result.update({
            "status": "semantic_ready",
            "attempt": 0,
            "semantic_attempt": int(payload.get("semantic_attempt") or 0),
            "effect_id": str(payload["next_effect_id"]),
            "failure_class": "semantic",
            "failure_projection": payload.get("projection") or {},
        })
    elif event.event_type == "WorkerFailureProjected":
        result.update({
            "status": "completed",
            "failure_projection": None,
            "projected_stage": str(payload.get("stage") or "repair_planned"),
        })
    elif event.event_type == "WorkerOutputReady":
        result.update({
            "status": "output_ready",
            "attempt": int(payload.get("attempt") or result["attempt"]),
            "output_artifact_hash": str(payload["artifact_hash"]),
            "output_snapshot_hash": str(payload["snapshot_hash"]),
            "failure_class": "",
            "projection": payload.get("projection") or {},
        })
    elif event.event_type == "WorkerProjected":
        result.update({
            "status": "completed",
            "projected_stage": str(payload.get("stage") or "workers_done"),
        })
    elif event.event_type == "WorkerAbandoned":
        result.update({
            "status": "abandoned",
            "abandon_reason": str(payload.get("reason") or "worker_abandoned"),
        })
    return result


def next_worker_command(state: dict[str, Any]) -> dict[str, Any]:
    """Return the sole legal next command derived from replayed history."""
    status = state.get("status")
    if status == "idle":
        return {"command": "prepare"}
    if status == "prepared":
        return {
            "command": "request_or_claim_worker",
            "effect_id": state.get("effect_id"),
            "attempt": int(state.get("attempt") or 0) + 1,
        }
    if status in {"requested", "retry_wait"}:
        return {
            "command": "claim_worker",
            "effect_id": state.get("effect_id"),
            "attempt": int(state.get("attempt") or 0) + 1,
        }
    if status == "availability_deferred":
        return {
            "command": "wait_for_llm_availability",
            "effect_id": state.get("effect_id"),
            "attempt": int(state.get("attempt") or 0),
            "availability": state.get("availability") or {},
        }
    if status == "output_ready":
        return {
            "command": "project_output",
            "artifact_hash": state.get("output_artifact_hash"),
        }
    if status == "semantic_ready":
        return {
            "command": "project_failure",
            "projection": state.get("failure_projection") or {},
        }
    if status == "exhausted":
        return {"command": "abandon"}
    if status == "completed":
        return {"command": "none"}
    if status == "abandoned":
        return {
            "command": "reconcile_abandon",
            "reason": str(state.get("abandon_reason") or "worker_abandoned"),
        }
    return {"command": "recover", "status": status}


def replay_worker(store: WorkflowStore, run_id: str) -> dict[str, Any]:
    events = store.events(run_id)
    allowed = {
        "WorkerPrepared",
        "WorkerCycleOpened",
        "WorkerSuperseded",
        "EffectRequested",
        "EffectFailed",
        "EffectDeferred",
        "EffectResumed",
        "EffectCompleted",
        "WorkerSemanticFailed",
        "WorkerFailureProjected",
        "WorkerOutputReady",
        "WorkerProjected",
        "WorkerAbandoned",
    }
    for event in events:
        if event.schema_version != 1 or event.event_type not in allowed:
            raise RuntimeError(
                f"unsupported Worker history event: "
                f"{event.event_type}@{event.schema_version}"
            )
        if event.event_type == "WorkerPrepared":
            envelope_errors = validate_worker_envelope(
                event.payload.get("envelope")
            )
            if envelope_errors:
                raise RuntimeError(
                    "invalid WorkerPrepared envelope: "
                    + "; ".join(envelope_errors)
                )
        if event.event_type in {"WorkerOutputReady", "WorkerSemanticFailed"}:
            projection = event.payload.get("projection")
            if (
                not isinstance(projection, dict)
                or projection.get("schema_version") != 1
            ):
                raise RuntimeError(
                    f"unsupported {event.event_type} projection schema"
                )
    return reduce_events(
        initial_worker_state(run_id),
        events,
        reduce_worker_event,
    )


class WorkerArtifactStore:
    """Content-addressed immutable snapshots for Worker input/output trees."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".tmp").mkdir(parents=True, exist_ok=True)
        (self.root / ".materializations").mkdir(parents=True, exist_ok=True)
        (self.root / ".materialization_receipts").mkdir(parents=True, exist_ok=True)
        (self.root / ".retained_projections").mkdir(parents=True, exist_ok=True)
        (self.root / "workspaces").mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _copy_manifest(source: Path, destination: Path, manifest: dict[str, Any]) -> None:
        destination.mkdir(parents=True, exist_ok=False)
        for entry in manifest.get("entries") or []:
            rel = str(entry.get("path") or "")
            if rel == ".":
                continue
            target = destination / rel
            if entry.get("type") == "directory":
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(source / rel, "rb") as reader, open(target, "xb") as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())

    @staticmethod
    def _fsync_tree(root: Path) -> None:
        directories = [root]
        directories.extend(path for path in root.rglob("*") if path.is_dir())
        for directory in reversed(directories):
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def capture(self, source: str | Path) -> str:
        source_path = Path(source)
        manifest = artifact_manifest(source_path)
        digest = canonical_digest(manifest)
        destination = self.root / digest
        if destination.exists():
            if hash_path(destination) != digest:
                raise RuntimeError(f"corrupt immutable Worker artifact: {digest}")
            return digest
        temporary = self.root / ".tmp" / f"{digest}.{uuid.uuid4().hex}"
        try:
            self._copy_manifest(source_path, temporary, manifest)
            if hash_path(temporary) != digest:
                raise RuntimeError("Worker artifact changed while being captured")
            self._fsync_tree(temporary)
            try:
                os.replace(temporary, destination)
                self._fsync_directory(self.root)
            except OSError:
                if not destination.exists() or hash_path(destination) != digest:
                    raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return digest

    def path_for(self, digest: str) -> Path:
        path = self.root / str(digest)
        if not path.is_dir() or hash_path(path) != str(digest):
            raise RuntimeError(f"Worker artifact unavailable or corrupt: {digest}")
        return path

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _journal_path(self, destination: Path) -> Path:
        key = content_digest(str(destination.resolve()))
        return self.root / ".materializations" / f"{key}.json"

    def _write_journal(self, journal: Path, payload: dict[str, Any]) -> None:
        temporary = journal.with_suffix(f".tmp-{uuid.uuid4().hex}")
        with open(temporary, "x", encoding="utf-8") as writer:
            writer.write(canonical_json(payload))
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, journal)
        self._fsync_directory(journal.parent)

    def _remove_journal(self, journal: Path) -> None:
        journal.unlink(missing_ok=True)
        self._fsync_directory(journal.parent)

    @staticmethod
    def _renameat2(source: Path, destination: Path, flags: int) -> None:
        """Use Linux atomic rename flags or fail closed.

        A check followed by ``os.replace`` is not a destination CAS: another
        writer can rebuild the canonical directory in between and be silently
        deleted.  ``RENAME_EXCHANGE`` lets us inspect the exact tree displaced
        by the atomic swap, while ``RENAME_NOREPLACE`` owns an absent target
        without clobbering it.
        """

        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise RuntimeError("atomic renameat2 is unavailable")
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100,
            os.fsencode(source),
            -100,
            os.fsencode(destination),
            int(flags),
        )
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))

    @staticmethod
    def _tree_digest(path: Path) -> str | None:
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError(f"artifact projection target is not a directory: {path}")
        return hash_path(path)

    def _receipt_path(self, operation_id: str) -> Path:
        if not _OPERATION_ID_RE.fullmatch(str(operation_id)):
            raise RuntimeError("invalid Worker materialization operation id")
        return self.root / ".materialization_receipts" / f"{operation_id}.json"

    def _retained_path(self, operation_id: str) -> Path:
        if not _OPERATION_ID_RE.fullmatch(str(operation_id)):
            raise RuntimeError("invalid Worker materialization operation id")
        return self.root / ".retained_projections" / operation_id

    def _load_completion_receipt(
        self,
        operation_id: str,
    ) -> tuple[MaterializationReceipt, dict[str, Any]] | None:
        receipt_path = self._receipt_path(operation_id)
        if not receipt_path.exists():
            return None
        try:
            body = json.loads(receipt_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(
                "Worker materialization completion receipt is unreadable"
            ) from exc
        if (
            not isinstance(body, dict)
            or body.get("schema_version")
            != _MATERIALIZATION_RECEIPT_SCHEMA_VERSION
            or body.get("operation_id") != operation_id
        ):
            raise RuntimeError("Worker materialization completion receipt is invalid")
        claimed = str(body.get("receipt_digest") or "")
        unsigned = {
            key: value for key, value in body.items() if key != "receipt_digest"
        }
        if claimed != content_digest(unsigned):
            raise RuntimeError(
                "Worker materialization completion receipt digest mismatch"
            )
        return MaterializationReceipt(
            operation_id=operation_id,
            operation=str(body.get("operation") or ""),
            digest=str(body.get("digest") or ""),
            installed=body.get("installed") is True,
            receipt_digest=claimed,
            retained_path=str(body.get("retained_path") or ""),
            retained_digest=str(body.get("retained_digest") or ""),
        ), body

    def _write_completion_receipt(
        self,
        payload: dict[str, Any],
        *,
        installed: bool,
        retained_digest: str | None,
    ) -> MaterializationReceipt:
        operation_id = str(payload["operation_id"])
        retained = Path(str(payload["retained"]))
        body = {
            "schema_version": _MATERIALIZATION_RECEIPT_SCHEMA_VERSION,
            "operation_id": operation_id,
            "operation": str(payload["operation"]),
            "destination": str(payload["destination"]),
            "digest": str(payload["digest"]),
            "expected_destination_digest": payload.get(
                "expected_destination_digest"
            ),
            "installed": bool(installed),
            "retained_path": str(retained) if retained_digest else "",
            "retained_digest": str(retained_digest or ""),
        }
        body["receipt_digest"] = content_digest(body)
        receipt_path = self._receipt_path(operation_id)
        if receipt_path.exists():
            loaded = self._load_completion_receipt(operation_id)
            assert loaded is not None
            _existing_receipt, existing = loaded
            if existing != body:
                raise RuntimeError(
                    "Worker materialization completion receipt conflicts"
                )
        else:
            temporary = receipt_path.with_suffix(f".tmp-{uuid.uuid4().hex}")
            try:
                with open(temporary, "x", encoding="utf-8") as writer:
                    writer.write(canonical_json(body))
                    writer.flush()
                    os.fsync(writer.fileno())
                os.link(temporary, receipt_path)
                self._fsync_directory(receipt_path.parent)
            finally:
                temporary.unlink(missing_ok=True)
        return MaterializationReceipt(
            operation_id=operation_id,
            operation=str(body["operation"]),
            digest=str(body["digest"]),
            installed=bool(body["installed"]),
            receipt_digest=str(body["receipt_digest"]),
            retained_path=str(body["retained_path"]),
            retained_digest=str(body["retained_digest"]),
        )

    def _move_to_retained(self, source: Path, retained: Path) -> str | None:
        """Atomically retire a tree; never recursively delete it.

        The retained namespace lives under the ignored artifact store on the
        same filesystem.  If either path was concurrently changed, both trees
        remain available for operator reconciliation.
        """

        source_digest = self._tree_digest(source)
        retained_digest = self._tree_digest(retained)
        if retained_digest is not None:
            if source_digest is not None:
                raise RuntimeError(
                    "Worker projection has both prepared and retained trees"
                )
            return retained_digest
        if source_digest is None:
            return None
        try:
            self._renameat2(source, retained, _RENAME_NOREPLACE)
        except OSError as exc:
            if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                raise RuntimeError(
                    "Worker retained projection appeared concurrently"
                ) from exc
            raise
        self._fsync_directory(source.parent)
        if retained.parent != source.parent:
            self._fsync_directory(retained.parent)
        return self._tree_digest(retained)

    def _complete_materialization(
        self,
        *,
        destination: Path,
        prepared: Path,
        retained: Path,
        digest: str,
        expected_digest: str | None,
    ) -> tuple[bool, str | None]:
        current = self._tree_digest(destination)
        prepared_digest = self._tree_digest(prepared)
        retained_digest = self._tree_digest(retained)
        if prepared_digest is not None and retained_digest is not None:
            raise RuntimeError(
                "Worker projection has both prepared and retained trees"
            )

        if current == digest:
            evidence_digest = retained_digest
            if prepared_digest is not None:
                evidence_digest = self._move_to_retained(prepared, retained)
            if evidence_digest == digest:
                # The canonical output predated this operation.  Preserve the
                # redundant prepared copy and report a durable no-op.
                return False, evidence_digest
            if expected_digest is None and evidence_digest is None:
                # RENAME_NOREPLACE completed before the crash.
                return True, None
            if expected_digest is not None and evidence_digest == expected_digest:
                # RENAME_EXCHANGE completed and the displaced preimage is now
                # durably retained.
                return True, evidence_digest
            raise RuntimeError(
                "Worker materialization recovery lacks exact retention evidence:"
                f"expected={expected_digest}:retained={evidence_digest}"
            )

        if prepared_digest != digest:
            raise RuntimeError("prepared Worker projection artifact changed")
        if current != expected_digest:
            raise RuntimeError(
                "Worker artifact destination CAS mismatch:"
                f"expected={expected_digest}:actual={current}"
            )
        if expected_digest is None:
            try:
                self._renameat2(prepared, destination, _RENAME_NOREPLACE)
            except OSError as exc:
                if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise RuntimeError(
                        "Worker artifact destination appeared during absent-target CAS"
                    ) from exc
                raise
            retained_digest = None
        else:
            self._renameat2(prepared, destination, _RENAME_EXCHANGE)
            self._fsync_directory(destination.parent)
            displaced = self._move_to_retained(prepared, retained)
            if displaced != expected_digest:
                # Never perform a blind second EXCHANGE.  A concurrent writer
                # may already own the canonical name; both byte trees and the
                # active journal remain intact for operator reconciliation.
                raise RuntimeError(
                    "Worker artifact destination changed during atomic CAS:"
                    f"expected={expected_digest}:displaced={displaced}"
                )
            retained_digest = displaced
        self._fsync_directory(destination.parent)
        if self._tree_digest(destination) != digest:
            raise RuntimeError("Worker artifact projection hash mismatch")
        return True, retained_digest

    def _finish_materialization(
        self,
        journal: Path,
        payload: dict[str, Any],
        *,
        installed: bool,
        retained_digest: str | None,
    ) -> MaterializationReceipt:
        receipt = self._write_completion_receipt(
            payload,
            installed=installed,
            retained_digest=retained_digest,
        )
        self._remove_journal(journal)
        return receipt

    def _recover_materialization(
        self,
        destination: Path,
    ) -> MaterializationReceipt | None:
        """Finish an interrupted atomic CAS without deleting any byte tree."""
        journal = self._journal_path(destination)
        if not journal.exists():
            return None
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
            if payload.get("schema_version") != _MATERIALIZATION_JOURNAL_SCHEMA_VERSION:
                raise RuntimeError("Worker materialization journal schema mismatch")
            if Path(payload["destination"]) != destination:
                raise RuntimeError("Worker materialization journal destination mismatch")
            operation = str(payload.get("operation") or "")
            operation_id = str(payload.get("operation_id") or "")
            digest = str(payload["digest"])
            prepared_value = str(payload.get("prepared") or "")
            prepared = Path(prepared_value) if prepared_value else None
            retained = Path(str(payload["retained"]))
            expected_digest = payload.get("expected_destination_digest")
            if expected_digest is not None:
                expected_digest = str(expected_digest)
            expected_parent = destination.parent.resolve()
            expected_retained_parent = (
                self.root / ".retained_projections"
            ).resolve()
            prepared_invalid = bool(
                operation == "materialize"
                and (
                    prepared is None
                    or prepared.parent.resolve() != expected_parent
                    or not prepared.name.startswith(
                        f".{destination.name}.workflow-materialize-"
                    )
                )
            )
            if (
                operation not in {"materialize", "remove"}
                or not operation_id
                or retained.parent.resolve() != expected_retained_parent
                or retained.name != operation_id
                or prepared_invalid
                or (operation == "remove" and prepared is not None)
            ):
                raise RuntimeError("Worker materialization journal path escaped")
        except Exception as exc:
            raise RuntimeError("corrupt Worker materialization journal") from exc

        if operation == "materialize":
            assert prepared is not None
            installed, retained_digest = self._complete_materialization(
                destination=destination,
                prepared=prepared,
                retained=retained,
                digest=digest,
                expected_digest=expected_digest,
            )
            return self._finish_materialization(
                journal,
                payload,
                installed=installed,
                retained_digest=retained_digest,
            )

        current = self._tree_digest(destination)
        removed = self._tree_digest(retained)
        if current is None and removed == digest:
            return self._finish_materialization(
                journal,
                payload,
                installed=True,
                retained_digest=removed,
            )
        if current != digest or removed is not None:
            raise RuntimeError(
                "Worker artifact removal CAS mismatch:"
                f"expected={digest}:actual={current}:removed={removed}"
            )
        self._renameat2(destination, retained, _RENAME_NOREPLACE)
        self._fsync_directory(destination.parent)
        self._fsync_directory(retained.parent)
        removed = self._tree_digest(retained)
        if removed != digest:
            raise RuntimeError("Worker artifact changed during removal CAS")
        return self._finish_materialization(
            journal,
            payload,
            installed=True,
            retained_digest=removed,
        )

    def materialize(
        self,
        digest: str,
        destination: str | Path,
        *,
        expected_destination_digest: str | None,
        operation_id: str | None = None,
    ) -> MaterializationReceipt:
        source = self.path_for(digest)
        destination = Path(destination)
        parent = destination.parent
        parent.mkdir(parents=True, exist_ok=True)
        recovered = self._recover_materialization(destination)
        if (
            recovered is not None
            and recovered.operation == "materialize"
            and recovered.digest == str(digest)
            and self._tree_digest(destination) == str(digest)
        ):
            return recovered
        requested_operation_id = str(operation_id or "")
        if requested_operation_id:
            loaded = self._load_completion_receipt(requested_operation_id)
            if loaded is not None:
                receipt, body = loaded
                if (
                    receipt.operation != "materialize"
                    or receipt.digest != str(digest)
                    or Path(str(body.get("destination") or "")) != destination
                    or self._tree_digest(destination) != str(digest)
                ):
                    raise RuntimeError(
                        "Worker materialization operation receipt scope mismatch"
                    )
                return receipt
        # A prepared tree whose journal never became durable is intentionally
        # left untouched.  Its name is not authority to delete it; a separate
        # receipt-aware janitor may reclaim proven orphans later.
        operation_id = requested_operation_id or uuid.uuid4().hex
        prepared = parent / (
            f".{destination.name}.workflow-materialize-{operation_id}"
        )
        retained = self._retained_path(operation_id)
        journal = self._journal_path(destination)
        shutil.copytree(source, prepared)
        if hash_path(prepared) != digest:
            raise RuntimeError("materialized Worker artifact hash mismatch")
        self._fsync_tree(prepared)
        journal_payload = {
            "schema_version": _MATERIALIZATION_JOURNAL_SCHEMA_VERSION,
            "operation_id": operation_id,
            "operation": "materialize",
            "destination": str(destination),
            "digest": str(digest),
            "prepared": str(prepared),
            "retained": str(retained),
            "expected_destination_digest": expected_destination_digest,
        }
        self._write_journal(journal, journal_payload)
        installed, retained_digest = self._complete_materialization(
            destination=destination,
            prepared=prepared,
            retained=retained,
            digest=str(digest),
            expected_digest=expected_destination_digest,
        )
        return self._finish_materialization(
            journal,
            journal_payload,
            installed=installed,
            retained_digest=retained_digest,
        )

    def remove_if_matches(
        self,
        destination: str | Path,
        expected_digest: str,
    ) -> MaterializationReceipt:
        """Atomically retire exactly ``expected_digest`` and never another tree."""

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        recovered = self._recover_materialization(destination)
        if (
            recovered is not None
            and recovered.operation == "remove"
            and recovered.digest == str(expected_digest)
            and self._tree_digest(destination) is None
        ):
            return recovered
        current = self._tree_digest(destination)
        if current != str(expected_digest):
            raise RuntimeError(
                "Worker artifact removal destination CAS mismatch:"
                f"expected={expected_digest}:actual={current}"
            )
        operation_id = uuid.uuid4().hex
        retained = self._retained_path(operation_id)
        journal = self._journal_path(destination)
        self._write_journal(journal, {
            "schema_version": _MATERIALIZATION_JOURNAL_SCHEMA_VERSION,
            "operation_id": operation_id,
            "operation": "remove",
            "destination": str(destination),
            "digest": str(expected_digest),
            "prepared": "",
            "retained": str(retained),
            "expected_destination_digest": str(expected_digest),
        })
        receipt = self._recover_materialization(destination)
        if receipt is None:
            raise RuntimeError("Worker removal recovery produced no receipt")
        return receipt

    def workspace_for(self, lease: EffectLease, input_digest: str) -> Path:
        """Materialize one immutable input into a lease-epoch private tree."""
        key = content_digest(
            {
                "effect_id": lease.effect_id,
                "lease_epoch": lease.lease_epoch,
                "input_digest": input_digest,
            }
        )
        workspace = self.root / "workspaces" / key
        if workspace.exists():
            if hash_path(workspace) != input_digest:
                raise RuntimeError("Worker lease workspace is corrupt")
            return workspace
        temporary = self.root / ".tmp" / f"workspace-{key}.{uuid.uuid4().hex}"
        shutil.copytree(self.path_for(input_digest), temporary)
        if hash_path(temporary) != input_digest:
            shutil.rmtree(temporary, ignore_errors=True)
            raise RuntimeError("Worker lease workspace input hash mismatch")
        try:
            os.replace(temporary, workspace)
        except OSError:
            if not workspace.is_dir() or hash_path(workspace) != input_digest:
                raise
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        return workspace

    def preparation_workspace(
        self,
        *,
        run_id: str,
        cycle: int,
        input_digest: str,
        preparation_digest: str,
    ) -> Path:
        """Create a clean private tree for deterministic repair preparation."""
        key = content_digest({
            "run_id": run_id,
            "cycle": int(cycle),
            "input_digest": input_digest,
            "preparation_digest": preparation_digest,
        })
        workspace = self.root / "workspaces" / f"prepare-{key}"
        # An interrupted preparation is never trusted as input. Rebuild it from
        # the immutable base receipt; only WorkerPrepared publishes authority.
        shutil.rmtree(workspace, ignore_errors=True)
        temporary = self.root / ".tmp" / f"prepare-{key}.{uuid.uuid4().hex}"
        shutil.copytree(self.path_for(input_digest), temporary)
        if hash_path(temporary) != input_digest:
            shutil.rmtree(temporary, ignore_errors=True)
            raise RuntimeError("repair preparation base hash mismatch")
        os.replace(temporary, workspace)
        return workspace

    def discard_workspace(self, workspace: str | Path) -> None:
        workspace = Path(workspace)
        expected_parent = (self.root / "workspaces").resolve()
        if workspace.parent.resolve() != expected_parent:
            raise RuntimeError("refusing to discard a non-Worker workspace")
        shutil.rmtree(workspace, ignore_errors=True)


@dataclass
class WorkerWorkflow:
    store: WorkflowStore
    artifacts: WorkerArtifactStore
    run_id: str

    @classmethod
    def for_checkpoint(cls, checkpoint: dict[str, Any]) -> "WorkerWorkflow":
        from evolution_infra import RESULTS_DIR

        root = Path(RESULTS_DIR) / "workflow"
        store = WorkflowStore(root / "events.sqlite3")
        run_id = workflow_run_id(checkpoint)
        store.ensure_instance(
            run_id,
            definition_version=WORKER_WORKFLOW_DEFINITION_VERSION,
        )
        return cls(
            store=store,
            artifacts=WorkerArtifactStore(root / "artifacts"),
            run_id=run_id,
        )

    def state(self) -> dict[str, Any]:
        return replay_worker(self.store, self.run_id)

    def prepare(
        self,
        envelope: dict[str, Any],
        *,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        errors = validate_worker_envelope(envelope)
        if errors:
            raise ValueError("; ".join(errors))
        state = self.state()
        if state["status"] != "idle":
            if state.get("envelope", {}).get("envelope_digest") != envelope["envelope_digest"]:
                raise RuntimeError("active Worker envelope differs from prepared input")
            return state
        effect_id = (
            f"worker:{self.run_id}:cycle-{int(state.get('cycle') or 0)}:"
            f"{envelope['envelope_digest'][:16]}"
        )
        cycle = int(state.get("cycle") or 0)
        self.store.append_event(
            self.run_id,
            "WorkerPrepared",
            {
                "envelope": envelope,
                "effect_id": effect_id,
                "max_attempts": int(max_attempts),
            },
            causation_id=(
                f"worker-prepared:{self.run_id}:cycle-{cycle}:"
                f"{envelope['envelope_digest']}"
            ),
            expected_version=int(state["last_seq"]),
        )
        return self.state()

    def request_or_claim(
        self,
        *,
        owner: str,
        lease_seconds: float,
    ) -> EffectLease:
        state = self.state()
        if state.get("status") not in {"prepared", "requested", "retry_wait"}:
            raise RuntimeError(
                f"cannot claim Worker effect from {state.get('status')}"
            )
        envelope = state.get("envelope") or {}
        effect_id = str(state.get("effect_id") or "")
        expected_kind = (
            "system_blueprint"
            if (envelope.get("execution_policy") or {}).get("executor")
            == "system_policy_bootstrap_v1"
            else "worker_llm"
        )
        effect = self.store.effect(effect_id)
        if not effect:
            self.store.request_effect(
                run_id=self.run_id,
                effect_id=effect_id,
                kind=expected_kind,
                input_payload=envelope,
                causation_id=f"worker-effect-requested:{effect_id}",
                max_attempts=int(state.get("max_attempts") or 3),
                expected_version=int(state["last_seq"]),
            )
        elif effect.get("kind") != expected_kind:
            raise RuntimeError(
                "active Worker effect kind differs from frozen executor policy"
            )
        return self.store.claim_effect(
            effect_id,
            owner=owner,
            lease_seconds=lease_seconds,
        )

    def infrastructure_failed(self, lease: EffectLease, issues: list[str]) -> dict[str, Any]:
        error = "; ".join(str(item)[:500] for item in issues[:10])
        self.store.fail_effect(
            lease.effect_id,
            lease_epoch=lease.lease_epoch,
            error=error,
            retryable=True,
            causation_id=(
                f"worker-infra-failed:{lease.effect_id}:{lease.lease_epoch}"
            ),
        )
        return self.state()

    def availability_deferred(
        self,
        lease: EffectLease,
        availability: dict[str, Any],
    ) -> dict[str, Any]:
        """Release a Worker lease for provider availability, without an attempt."""
        frozen = json.loads(canonical_json(availability or {}))
        reason = (
            f"{frozen.get('category') or 'llm_availability'}: "
            f"{frozen.get('summary') or 'provider unavailable'}"
        )
        self.store.defer_effect(
            lease.effect_id,
            lease_epoch=lease.lease_epoch,
            reason=reason,
            metadata={"availability": frozen},
            causation_id=(
                f"worker-availability-deferred:{lease.effect_id}:"
                f"{lease.lease_epoch}"
            ),
        )
        return self.state()

    def resume_availability_deferred(self) -> dict[str, Any]:
        state = self.state()
        if state.get("status") != "availability_deferred":
            raise RuntimeError(
                f"cannot resume Worker availability from {state.get('status')}"
            )
        effect_id = str(state.get("effect_id") or "")
        availability_digest = str(
            (state.get("availability") or {}).get("evidence_digest") or ""
        )
        self.store.resume_effect(
            effect_id,
            causation_id=(
                f"worker-availability-resumed:{effect_id}:"
                f"{availability_digest or 'unclassified'}"
            ),
        )
        return self.state()

    def execution_failed(
        self,
        lease: EffectLease,
        issues: list[str],
        *,
        retryable: bool,
    ) -> dict[str, Any]:
        error = "; ".join(str(item)[:500] for item in issues[:10])
        self.store.fail_effect(
            lease.effect_id,
            lease_epoch=lease.lease_epoch,
            error=error,
            retryable=retryable,
            causation_id=(
                f"worker-execution-failed:{lease.effect_id}:{lease.lease_epoch}:"
                f"{int(bool(retryable))}"
            ),
        )
        return self.state()

    def semantic_failed(
        self,
        lease: EffectLease,
        evidence: dict[str, Any],
        *,
        projection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(projection, dict)
            or projection.get("schema_version") != 1
        ):
            raise ValueError(
                "Worker semantic projection must use schema_version=1"
            )
        state = self.state()
        semantic_attempt = int(state.get("semantic_attempt") or 0) + 1
        next_effect_id = (
            f"worker:{self.run_id}:cycle-{int(state.get('cycle') or 0)}:"
            f"{state['envelope']['envelope_digest'][:16]}:semantic-{semantic_attempt}"
        )
        completion = self.store.complete_effect(
            lease.effect_id,
            lease_epoch=lease.lease_epoch,
            completion_id=f"worker-semantic:{lease.effect_id}:{lease.lease_epoch}",
            result_payload={
                "outcome": "semantic_failure",
                "evidence": json.loads(canonical_json(evidence)),
            },
            causation_id=f"worker-semantic-effect-completed:{lease.effect_id}:{lease.lease_epoch}",
            followup_events=[{
                "event_type": "WorkerSemanticFailed",
                "payload": {
                    "attempt": lease.attempt,
                    "semantic_attempt": semantic_attempt,
                    "next_effect_id": next_effect_id,
                    "evidence": json.loads(canonical_json(evidence)),
                    "projection": json.loads(canonical_json(projection)),
                },
                "causation_id": (
                    f"worker-semantic-failed:{lease.effect_id}:{lease.lease_epoch}"
                ),
            }],
        )
        if not completion.get("accepted"):
            raise RuntimeError("semantic Worker result lost its fenced lease")
        return self.state()

    def output_ready(
        self,
        lease: EffectLease,
        *,
        artifact_hash: str,
        snapshot_hash: str,
        projection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(projection, dict)
            or projection.get("schema_version") != 1
        ):
            raise ValueError(
                "Worker output projection must use schema_version=1"
            )
        completion = self.store.complete_effect(
            lease.effect_id,
            lease_epoch=lease.lease_epoch,
            completion_id=f"worker-output:{lease.effect_id}:{lease.lease_epoch}",
            result_payload={
                "artifact_hash": artifact_hash,
                "snapshot_hash": snapshot_hash,
                "projection": json.loads(canonical_json(projection)),
            },
            causation_id=f"worker-effect-completed:{lease.effect_id}:{lease.lease_epoch}",
            followup_events=[{
                "event_type": "WorkerOutputReady",
                "payload": {
                    "attempt": lease.attempt,
                    "artifact_hash": artifact_hash,
                    "snapshot_hash": snapshot_hash,
                    "projection": json.loads(canonical_json(projection)),
                },
                "causation_id": (
                    f"worker-output-ready:{lease.effect_id}:{lease.lease_epoch}"
                ),
            }],
        )
        if not completion.get("accepted"):
            raise RuntimeError("Worker output completion lost its fenced lease")
        return self.state()

    def projected(self, stage: str = "workers_done") -> dict[str, Any]:
        state = self.state()
        if state.get("status") != "output_ready":
            raise RuntimeError(
                f"cannot project Worker output from {state.get('status')}"
            )
        self.store.append_event_and_set_status(
            self.run_id,
            event_type="WorkerProjected",
            payload={
                "stage": stage,
                "artifact_hash": state.get("output_artifact_hash"),
            },
            causation_id=(
                f"worker-projected:{self.run_id}:cycle-{state.get('cycle', 0)}:"
                f"{state.get('output_artifact_hash')}:{stage}"
            ),
            expected_version=int(state["last_seq"]),
            status="completed",
        )
        return self.state()

    def open_cycle(self, reason: str) -> dict[str, Any]:
        state = self.state()
        if state["status"] != "completed":
            raise RuntimeError(
                f"cannot open Worker cycle from {state['status']}"
            )
        self.store.append_event_and_set_status(
            self.run_id,
            event_type="WorkerCycleOpened",
            payload={"reason": str(reason)[:1000]},
            causation_id=(
                f"worker-cycle-opened:{self.run_id}:"
                f"{state.get('cycle', 0) + 1}:"
                f"{content_digest(reason)}"
            ),
            expected_version=int(state["last_seq"]),
            status="running",
        )
        return self.state()

    def supersede(
        self,
        reason: str,
        evidence: dict[str, Any],
        *,
        stage: str = "direction_audited",
    ) -> dict[str, Any]:
        state = self.state()
        if state.get("status") != "semantic_ready" or state.get("failure_class") != "semantic":
            raise RuntimeError(
                f"cannot supersede Worker cycle from {state.get('status')}"
            )
        self.store.append_event_and_set_status(
            self.run_id,
            event_type="WorkerSuperseded",
            payload={
                "reason": str(reason)[:1000],
                "evidence": json.loads(canonical_json(evidence)),
                "stage": str(stage),
            },
            causation_id=(
                f"worker-superseded:{self.run_id}:"
                f"{state.get('cycle', 0)}:"
                f"{content_digest({'reason': reason, 'evidence': evidence})}"
            ),
            expected_version=int(state["last_seq"]),
            status="completed",
        )
        return self.state()

    def failure_projected(self, stage: str = "repair_planned") -> dict[str, Any]:
        state = self.state()
        if state.get("status") != "semantic_ready":
            raise RuntimeError(
                f"cannot confirm Worker failure projection from {state.get('status')}"
            )
        self.store.append_event_and_set_status(
            self.run_id,
            event_type="WorkerFailureProjected",
            payload={"stage": str(stage)},
            causation_id=(
                f"worker-failure-projected:{self.run_id}:"
                f"cycle-{state.get('cycle', 0)}:semantic-{state.get('semantic_attempt', 0)}"
            ),
            expected_version=int(state["last_seq"]),
            status="completed",
        )
        return self.state()

    def abandon(self, reason: str) -> dict[str, Any]:
        state = self.state()
        if state["status"] != "abandoned":
            self.store.terminal_transition(
                self.run_id,
                event_type="WorkerAbandoned",
                payload={"reason": str(reason)[:1000]},
                causation_id=(
                    f"worker-abandoned:{self.run_id}:"
                    f"cycle-{state.get('cycle', 0)}:{content_digest(reason)}"
                ),
                expected_version=int(state["last_seq"]),
                status="abandoned",
            )
        return self.state()


def serialized_worker_command(function):
    """Serialize one execute_workers command per generation across processes."""
    @wraps(function)
    async def wrapped(args):
        try:
            from evolution_infra import read_pipeline_checkpoint

            checkpoint = read_pipeline_checkpoint() or {}
            if not checkpoint.get("next_v") or not checkpoint.get("source_v"):
                return await function(args)
            workflow = WorkerWorkflow.for_checkpoint(checkpoint)
            with workflow.store.command_lock(workflow.run_id):
                return await function(args)
        except WorkflowBusy:
            from tool_helpers import _json_tool_result

            return _json_tool_result({
                "error": "WORKER_COMMAND_BUSY",
                "failure_class": "infrastructure",
                "action": "retry_same_tool",
                "directive": (
                    "Another process owns the fenced Worker command for this "
                    "generation. Retry execute_workers without editing the candidate."
                ),
            })

    return wrapped
