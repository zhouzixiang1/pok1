"""Durable Worker activity domain and immutable candidate artifact store."""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import json
import os
from pathlib import Path
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


WORKER_WORKFLOW_DEFINITION_VERSION = 1
WORKER_ENVELOPE_SCHEMA_VERSION = 1


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
    worker_execution_context: dict[str, Any],
    work_item: dict[str, Any] | None,
    backend_contract: dict[str, Any],
    precommit_rework_count: int,
    official_rework_count: int,
    projection_plan: dict[str, Any] | None = None,
    audit_context: dict[str, Any] | None = None,
    execution_policy: dict[str, Any] | None = None,
    checkpoint_contract: dict[str, Any] | None = None,
    worker_failure_count: int | None = None,
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
        "source_artifact_hash": str(source_artifact_hash),
        "tasks": json.loads(canonical_json(tasks)),
        "reviewer_feedback": str(reviewer_feedback or ""),
        "worker_template_hash": str(worker_template_hash),
        "worker_execution_context": json.loads(
            canonical_json(worker_execution_context)
        ),
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
        "source_artifact_hash",
        "worker_template_hash",
        "envelope_digest",
    ):
        if not str(envelope.get(field) or "").strip():
            errors.append(f"worker_envelope_{field}_missing")
    for field in (
        "prepared_artifact_hash",
        "prepared_snapshot_hash",
        "source_artifact_hash",
        "worker_template_hash",
        "envelope_digest",
    ):
        value = str(envelope.get(field) or "")
        if value and (len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)):
            errors.append(f"worker_envelope_{field}_invalid")
    if not isinstance(envelope.get("tasks"), list) or not envelope.get("tasks"):
        errors.append("worker_envelope_tasks_missing")
    if not isinstance(envelope.get("worker_execution_context"), dict):
        errors.append("worker_envelope_prompt_context_invalid")
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
    elif event.event_type == "EffectRequested" and payload.get("kind") == "worker_llm":
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

    def _recover_materialization(self, destination: Path) -> None:
        """Finish or roll back an interrupted two-rename projection."""
        journal = self._journal_path(destination)
        if not journal.exists():
            return
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
            if payload.get("schema_version") != 1:
                raise RuntimeError("Worker materialization journal schema mismatch")
            if Path(payload["destination"]) != destination:
                raise RuntimeError("Worker materialization journal destination mismatch")
            digest = str(payload["digest"])
            prepared = Path(payload["prepared"])
            backup = Path(payload["backup"])
            expected_parent = destination.parent.resolve()
            if (
                prepared.parent.resolve() != expected_parent
                or backup.parent.resolve() != expected_parent
                or not prepared.name.startswith(
                    f".{destination.name}.workflow-new-"
                )
                or not backup.name.startswith(
                    f".{destination.name}.workflow-old-"
                )
            ):
                raise RuntimeError("Worker materialization journal path escaped")
        except Exception as exc:
            raise RuntimeError("corrupt Worker materialization journal") from exc

        destination_matches = (
            destination.is_dir() and hash_path(destination) == digest
        )
        if destination_matches:
            shutil.rmtree(prepared, ignore_errors=True)
            shutil.rmtree(backup, ignore_errors=True)
            self._remove_journal(journal)
            return

        if prepared.is_dir() and hash_path(prepared) == digest:
            if destination.exists():
                if backup.exists():
                    shutil.rmtree(destination, ignore_errors=True)
                else:
                    os.replace(destination, backup)
            os.replace(prepared, destination)
            self._fsync_directory(destination.parent)
            if hash_path(destination) != digest:
                raise RuntimeError("recovered Worker artifact hash mismatch")
            shutil.rmtree(backup, ignore_errors=True)
            self._remove_journal(journal)
            return

        if not destination.exists() and backup.exists():
            os.replace(backup, destination)
            self._fsync_directory(destination.parent)
        raise RuntimeError(
            "interrupted Worker projection lacked its immutable prepared tree"
        )

    def materialize(self, digest: str, destination: str | Path) -> None:
        source = self.path_for(digest)
        destination = Path(destination)
        parent = destination.parent
        parent.mkdir(parents=True, exist_ok=True)
        self._recover_materialization(destination)
        # The first journal fsync can itself fail after the prepared copy exists.
        # With the per-generation actor lock there is no concurrent projection,
        # so journal-less siblings are orphaned crash debris and safe to remove.
        if not self._journal_path(destination).exists():
            for pattern in (
                f".{destination.name}.workflow-new-*",
                f".{destination.name}.workflow-old-*",
            ):
                for orphan in parent.glob(pattern):
                    shutil.rmtree(orphan, ignore_errors=True)
        token = uuid.uuid4().hex
        prepared = parent / f".{destination.name}.workflow-new-{token}"
        backup = parent / f".{destination.name}.workflow-old-{token}"
        journal = self._journal_path(destination)
        shutil.copytree(source, prepared)
        if hash_path(prepared) != digest:
            shutil.rmtree(prepared, ignore_errors=True)
            raise RuntimeError("materialized Worker artifact hash mismatch")
        self._fsync_tree(prepared)
        journal_payload = {
            "schema_version": 1,
            "destination": str(destination),
            "digest": str(digest),
            "prepared": str(prepared),
            "backup": str(backup),
            "phase": "prepared",
        }
        self._write_journal(journal, journal_payload)
        try:
            if destination.exists():
                os.replace(destination, backup)
                self._fsync_directory(parent)
            journal_payload["phase"] = "old_moved"
            self._write_journal(journal, journal_payload)
            os.replace(prepared, destination)
            self._fsync_directory(parent)
            journal_payload["phase"] = "new_moved"
            self._write_journal(journal, journal_payload)
        except BaseException:
            raise
        if hash_path(destination) != digest:
            raise RuntimeError("Worker artifact projection hash mismatch")
        shutil.rmtree(backup, ignore_errors=True)
        shutil.rmtree(prepared, ignore_errors=True)
        self._remove_journal(journal)

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
        effect = self.store.effect(effect_id)
        if not effect:
            self.store.request_effect(
                run_id=self.run_id,
                effect_id=effect_id,
                kind="worker_llm",
                input_payload=envelope,
                causation_id=f"worker-effect-requested:{effect_id}",
                max_attempts=int(state.get("max_attempts") or 3),
                expected_version=int(state["last_seq"]),
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
