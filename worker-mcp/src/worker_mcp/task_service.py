"""Durable task application service and recovery-aware execution pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from datetime import datetime
import os
from pathlib import Path, PurePosixPath
import socket
from typing import Any
import uuid

from .agent_executor import (
    AgentCancelled,
    AgentExecutionError,
    AgentTimedOut,
    BaseAgentExecutor,
    executor_for_config,
)
from .audit_log import AuditLogger
from .config import WorkerConfig
from .idempotency import request_fingerprint
from .persistence import ClaimConflict, IdempotencyConflict, Persistence
from .queue import AsyncFileLock, ConcurrencyController, LockUnavailable, TaskQueue
from .result_normalizer import (
    normalize_failure,
    normalize_success,
    redact_sensitive_text,
)
from .schemas import (
    CancelResponse,
    ListTasksRequest,
    ListTasksResponse,
    StatusResponse,
    SubmitResponse,
    TaskEnvelope,
    TaskResult,
    TaskStatus,
    TaskSummary,
    TaskType,
)
from .state_machine import is_terminal
from .worktree import WorktreeError, WorktreeManager, WorktreeSnapshot


def _iter_original_strings(value: Any) -> Iterator[str]:
    """Yield raw strings from a model-dumped Python container graph.

    JSON serialization is deliberately not involved: quotes, backslashes, and
    control characters are escaped by JSON and therefore cannot be compared
    safely with the original credential.  ``model_dump(mode="python")`` emits
    ordinary containers today, but the walker also handles mapping keys and
    the common sequence/set containers defensively.  Container identities are
    fenced so an unexpected cyclic object cannot make validation loop forever.
    """

    pending = [value]
    seen_containers: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            yield current
            continue
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            for key, item in current.items():
                pending.append(key)
                pending.append(item)
            continue
        if isinstance(current, (list, tuple, set, frozenset)):
            identity = id(current)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            pending.extend(current)


class TaskService:
    def __init__(
        self,
        config: WorkerConfig,
        *,
        executor: BaseAgentExecutor | None = None,
        additional_redaction_secrets: tuple[str, ...] = (),
    ):
        self.config = config
        self.config.prepare_directories()
        self.persistence = Persistence(config.state_dir / "tasks.sqlite3")
        self.worktrees = WorktreeManager(config)
        self.executor = executor or executor_for_config(config)
        self.concurrency = ConcurrencyController(config)
        credential = os.environ.get(config.gateway.auth_token_env, "")
        self._redaction_secrets = tuple(
            dict.fromkeys(
                secret
                for secret in (credential, *additional_redaction_secrets)
                if secret
            )
        )
        self.audit = AuditLogger(
            config.state_dir / "logs" / "worker-mcp.jsonl",
            max_bytes=config.logging.max_bytes,
            backup_count=config.logging.backup_count,
            secrets=self._redaction_secrets,
        )
        workers = min(
            config.limits.max_subprocesses,
            config.limits.global_read_tasks + config.limits.global_write_tasks,
        )
        self.queue = TaskQueue(self._execute_task, workers=workers)
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"
        self._instance_lock = AsyncFileLock(
            config.state_dir / "locks" / "service-instance.lock"
        )
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        try:
            await self._instance_lock.acquire(wait=False)
        except LockUnavailable as exc:
            raise RuntimeError(
                "another Worker MCP service already owns this state_dir"
            ) from exc
        try:
            await self.queue.start()
            await self._recover()
        except BaseException:
            for event in self._cancel_events.values():
                event.set()
            await self.queue.stop()
            self._instance_lock.release()
            raise
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            self.audit.close()
            return
        for event in self._cancel_events.values():
            event.set()
        try:
            await self.queue.stop()
        finally:
            self._instance_lock.release()
            self.audit.close()
            self._started = False

    @staticmethod
    def _path_under(child: str, parent: str) -> bool:
        child_path = PurePosixPath(child)
        parent_path = PurePosixPath(parent)
        return child_path == parent_path or parent_path in child_path.parents

    def _validate_scope_contract(self, request: TaskEnvelope) -> None:
        request_values = _iter_original_strings(request.model_dump(mode="python"))
        if any(
            secret in value
            for value in request_values
            for secret in self._redaction_secrets
        ):
            raise ValueError("task envelope contains the dedicated Worker credential")
        mandatory = self.config.mandatory_forbidden_paths
        for allowed in request.allowed_paths:
            for forbidden in mandatory:
                if self._path_under(allowed, forbidden):
                    raise ValueError(
                        f"allowed path is system-forbidden: {allowed}"
                    )
        if request.execution.max_turns > self.config.limits.max_turns:
            raise ValueError("task max_turns exceeds server policy")
        if request.execution.timeout_sec > self.config.limits.max_task_timeout_sec:
            raise ValueError("task timeout exceeds server policy")

    async def submit(self, request: TaskEnvelope) -> SubmitResponse:
        canonical = await asyncio.to_thread(self.worktrees.validate_request, request)
        self._validate_scope_contract(canonical)
        fingerprint = request_fingerprint(canonical)
        row, replay = self.persistence.create_or_get(canonical, fingerprint)
        task_id = row["task_id"]
        status = TaskStatus(row["status"])
        if status is TaskStatus.ACCEPTED:
            row = self.persistence.transition(
                task_id,
                TaskStatus.QUEUED,
                phase="queued",
                reason="accepted task enqueued",
                progress_summary="Task is queued for an isolated executor",
            )
            status = TaskStatus(row["status"])
        if status is TaskStatus.QUEUED:
            await self.queue.enqueue(task_id)
        self.audit.log(
            "task.submit",
            timestamp=datetime.now().astimezone().isoformat(),
            trace_id=(
                redact_sensitive_text(canonical.trace_id)
                if canonical.trace_id
                else None
            ),
            task_id=task_id,
            idempotency_key=redact_sensitive_text(canonical.idempotency_key),
            repository=canonical.repo,
            base_commit=canonical.base_commit,
            state=status.value,
            idempotent_replay=replay,
        )
        return SubmitResponse(
            task_id=task_id,
            status=status,
            idempotent_replay=replay,
        )

    def status(self, task_id: str) -> StatusResponse:
        row = self.persistence.get_task(task_id)
        return StatusResponse(
            task_id=task_id,
            status=TaskStatus(row["status"]),
            phase=row["phase"],
            attempt=int(row["attempt"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            progress_summary=row["progress_summary"],
            worktree_path=row["worktree_path"],
        )

    def result(self, task_id: str) -> TaskResult:
        row = self.persistence.get_task(task_id)
        if not row["result_json"]:
            raise ValueError(
                f"task result is not available while status={row['status']}"
            )
        return TaskResult.model_validate_json(row["result_json"])

    async def cancel(self, task_id: str) -> CancelResponse:
        failure = normalize_failure(
            task_id=task_id,
            summary="Task cancelled before execution",
            worktree_path=None,
            secrets=self._redaction_secrets,
        )
        row, cancelled_before_claim = self.persistence.cancel_or_request(
            task_id,
            pre_execution_result_json=failure.model_dump_json(),
        )
        if not cancelled_before_claim and not is_terminal(TaskStatus(row["status"])):
            self._cancel_events.setdefault(task_id, asyncio.Event()).set()
            for _ in range(50):
                await asyncio.sleep(0.1)
                row = self.persistence.get_task(task_id)
                if is_terminal(TaskStatus(row["status"])):
                    break
        self.audit.log(
            "task.cancel",
            timestamp=datetime.now().astimezone().isoformat(),
            task_id=task_id,
            state=row["status"],
            worktree_path=row["worktree_path"],
        )
        return CancelResponse(
            task_id=task_id,
            status=TaskStatus(row["status"]),
            worktree_path=row["worktree_path"],
            progress_summary=row["progress_summary"],
        )

    def list(self, filters: ListTasksRequest) -> ListTasksResponse:
        repo = None
        if filters.repo:
            repo = str(self.worktrees.canonical_repo(filters.repo))
        rows = self.persistence.list_tasks(
            status=filters.status,
            repo=repo,
            task_type=filters.task_type,
            since=filters.since,
            limit=filters.limit,
        )
        summaries: list[TaskSummary] = []
        for row in rows:
            request = TaskEnvelope.model_validate_json(row["request_json"])
            summaries.append(
                TaskSummary(
                    task_id=row["task_id"],
                    status=TaskStatus(row["status"]),
                    task_type=TaskType(row["task_type"]),
                    repo=row["repository"],
                    goal=request.goal,
                    attempt=int(row["attempt"]),
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                )
            )
        return ListTasksResponse(tasks=summaries)

    async def _recover(self) -> None:
        for row in self.persistence.incomplete_tasks():
            task_id = row["task_id"]
            request = TaskEnvelope.model_validate_json(row["request_json"])
            status = TaskStatus(row["status"])
            try:
                self._validate_scope_contract(request)
                canonical = await asyncio.to_thread(
                    self.worktrees.validate_request, request
                )
                if canonical != request:
                    raise WorktreeError(
                        "durable task identity is no longer canonical"
                    )
            except (ValueError, WorktreeError):
                # Do not inspect or serialize a quarantined worktree: content
                # created under the stale contract may contain the same secret.
                # The owner-locked path remains available for operator review.
                await self._finish_abnormal(
                    task_id,
                    request,
                    TaskStatus.NEEDS_REVIEW,
                    (
                        "Persisted task violates the current safety contract; "
                        "operator review is required"
                    ),
                    None,
                    recovery=True,
                )
                continue
            if row["cancel_requested"]:
                snapshot = await self._safe_snapshot(row["worktree_path"])
                uncertain_worktree = bool(row["worktree_path"]) and snapshot is None
                target = (
                    TaskStatus.NEEDS_REVIEW
                    if uncertain_worktree or (snapshot and snapshot.dirty)
                    else TaskStatus.CANCELLED
                )
                await self._finish_abnormal(
                    task_id,
                    request,
                    target,
                    (
                        "Cancelled task has worktree changes or unverifiable evidence; "
                        "Codex review required"
                        if target is TaskStatus.NEEDS_REVIEW
                        else "Task was cancelled during service recovery"
                    ),
                    snapshot,
                )
                continue
            if status is TaskStatus.QUEUED:
                await self.queue.enqueue(task_id)
                continue
            if status is TaskStatus.ACCEPTED:
                self.persistence.transition(
                    task_id,
                    TaskStatus.QUEUED,
                    phase="queued",
                    reason="recovered accepted task",
                    progress_summary="Accepted task recovered and queued",
                    recovery=True,
                    clear_lease=True,
                )
                await self.queue.enqueue(task_id)
                continue
            snapshot = await self._safe_snapshot(row["worktree_path"])
            unverifiable_worktree = bool(row["worktree_path"]) and snapshot is None
            retries_allowed = self.config.limits.read_retry_count
            if (
                request.execution.read_only
                and int(row["attempt"]) <= retries_allowed
                and not unverifiable_worktree
                and not (snapshot and snapshot.dirty)
            ):
                self.persistence.transition(
                    task_id,
                    TaskStatus.QUEUED,
                    phase="queued",
                    reason="safe read-only task recovered after interruption",
                    progress_summary="Read-only task recovered for its single safe retry",
                    recovery=True,
                    clear_lease=True,
                )
                await self.queue.enqueue(task_id)
                continue
            target = (
                TaskStatus.NEEDS_REVIEW
                if unverifiable_worktree or (snapshot and snapshot.dirty)
                else TaskStatus.FAILED
            )
            await self._finish_abnormal(
                task_id,
                request,
                target,
                "Interrupted write task requires review"
                if target is TaskStatus.NEEDS_REVIEW
                else "Interrupted task is not safe to replay",
                snapshot,
            )

    async def _safe_snapshot(self, value: str | None) -> WorktreeSnapshot | None:
        if not value:
            return None
        path = Path(value)
        if not path.exists():
            return None
        try:
            return await asyncio.to_thread(self.worktrees.snapshot, path)
        except Exception:
            return None

    async def _finish_abnormal(
        self,
        task_id: str,
        request: TaskEnvelope,
        target: TaskStatus,
        summary: str,
        snapshot: WorktreeSnapshot | None,
        *,
        error: Exception | None = None,
        recovery: bool = False,
    ) -> None:
        row = self.persistence.get_task(task_id)
        partial = target is TaskStatus.NEEDS_REVIEW
        result = normalize_failure(
            task_id=task_id,
            summary=summary,
            worktree_path=row["worktree_path"],
            snapshot=snapshot,
            partial=partial,
            secrets=self._redaction_secrets,
        )
        self.persistence.save_result(task_id, result.model_dump_json())
        self.persistence.transition(
            task_id,
            target,
            phase=target.value,
            reason=summary,
            progress_summary=summary,
            recovery=recovery,
            clear_lease=True,
            error_class=type(error).__name__ if error else None,
            error_message=(
                redact_sensitive_text(
                    str(error), secrets=self._redaction_secrets
                )
                if error
                else None
            ),
        )
        self.audit.log(
            "task.terminal",
            timestamp=datetime.now().astimezone().isoformat(),
            trace_id=(
                redact_sensitive_text(
                    request.trace_id, secrets=self._redaction_secrets
                )
                if request.trace_id
                else None
            ),
            task_id=task_id,
            repository=request.repo,
            base_commit=request.base_commit,
            worktree_path=row["worktree_path"],
            state=target.value,
            phase=target.value,
            attempt=row["attempt"],
            files_changed=result.files_changed,
            error_class=type(error).__name__ if error else None,
            error_message=result.summary if error else None,
        )

    async def _execute_task(self, task_id: str) -> None:
        try:
            row = self.persistence.claim_task(task_id, self._owner)
        except ClaimConflict:
            return
        request = self.persistence.request_for(task_id)
        cancel_event = self._cancel_events.setdefault(task_id, asyncio.Event())
        worktree: Path | None = None
        snapshot: WorktreeSnapshot | None = None
        try:
            async with self.concurrency.slot(request):
                current = self.persistence.get_task(task_id)
                if current["cancel_requested"] or cancel_event.is_set():
                    raise AgentCancelled("cancelled before worktree preparation")
                # Persist the deterministic worktree intention before any Git
                # side effect. A hard crash after `worktree add`/marker creation
                # can then be recovered or quarantined by the exact path instead
                # of leaving an owner-marked orphan invisible to SQLite.
                worktree = self.worktrees.path_for(
                    Path(request.repo), request.base_commit, task_id
                ).resolve()
                self.persistence.update_worktree(task_id, str(worktree))
                prepared = await asyncio.to_thread(
                    self.worktrees.prepare, request, task_id
                )
                if prepared.resolve() != worktree:
                    raise RuntimeError(
                        "prepared worktree path does not match durable intention"
                    )
                current = self.persistence.get_task(task_id)
                if current["cancel_requested"] or cancel_event.is_set():
                    raise AgentCancelled("cancelled during worktree preparation")
                self.persistence.transition(
                    task_id,
                    TaskStatus.RUNNING,
                    phase="running",
                    reason="isolated worktree prepared",
                    progress_summary="Claude Agent SDK Worker is running in isolation",
                )
                execution = await self.executor.run(request, worktree, cancel_event)
                current = self.persistence.get_task(task_id)
                if current["cancel_requested"] or cancel_event.is_set():
                    raise AgentCancelled("cancelled after Worker execution")
                self.persistence.transition(
                    task_id,
                    TaskStatus.VERIFYING,
                    phase="verifying",
                    reason="Agent SDK returned structured output",
                    progress_summary="Verifying actual Git diff and tool evidence",
                )
                snapshot = await asyncio.to_thread(self.worktrees.snapshot, worktree)
                violations = await asyncio.to_thread(
                    self.worktrees.verify_changed_scope, request, snapshot
                )
                if snapshot.truncated:
                    violations.append(
                        "worktree evidence exceeded configured resource limits"
                    )
                if snapshot.ignored_files:
                    violations.append(
                        "worktree contains ignored residue: "
                        + ", ".join(snapshot.ignored_files[:20])
                    )
                if request.execution.read_only and snapshot.dirty:
                    violations.append("read-only task produced a worktree diff")
                if (
                    request.execution.read_only
                    and not execution.audit.get("files_read")
                ):
                    violations.append(
                        "read-only task produced no successful file-read evidence"
                    )
                if (
                    not request.execution.read_only
                    and request.task_type in {TaskType.PATCH, TaskType.REFACTOR, TaskType.DOCUMENT}
                    and not snapshot.dirty
                ):
                    violations.append("write task completed without a Git diff")
                if violations:
                    target = (
                        TaskStatus.NEEDS_REVIEW
                        if snapshot.dirty
                        else TaskStatus.FAILED
                    )
                    await self._finish_abnormal(
                        task_id,
                        request,
                        target,
                        "Verification rejected Worker output: " + "; ".join(violations),
                        snapshot,
                    )
                    return
                current = self.persistence.get_task(task_id)
                if current["cancel_requested"] or cancel_event.is_set():
                    raise AgentCancelled("cancelled during Worker verification")
                result = normalize_success(
                    task_id=task_id,
                    execution=execution,
                    snapshot=snapshot,
                    secrets=self._redaction_secrets,
                )
                self.persistence.save_result(task_id, result.model_dump_json())
                final = self.persistence.transition(
                    task_id,
                    TaskStatus.SUCCEEDED,
                    phase="succeeded",
                    reason="structured result and actual worktree evidence verified",
                    progress_summary="Task succeeded and is ready for Codex review",
                    clear_lease=True,
                )
                self.audit.log(
                    "task.terminal",
                    timestamp=datetime.now().astimezone().isoformat(),
                    trace_id=(
                        redact_sensitive_text(request.trace_id)
                        if request.trace_id
                        else None
                    ),
                    task_id=task_id,
                    idempotency_key=redact_sensitive_text(
                        request.idempotency_key,
                        secrets=execution.redaction_secrets,
                    ),
                    repository=request.repo,
                    base_commit=request.base_commit,
                    worktree_path=str(worktree),
                    state=TaskStatus.SUCCEEDED.value,
                    phase="succeeded",
                    attempt=final["attempt"],
                    sdk_session_id=result.metrics.session_id,
                    tool_calls=len(result.commands_executed),
                    commands=[
                        item.model_dump(mode="json")
                        for item in result.commands_executed
                    ],
                    files_changed=result.files_changed,
                    tests=[item.model_dump() for item in result.tests],
                    duration_ms=execution.duration_ms,
                    turn_count=execution.turns,
                )
        except AgentCancelled as exc:
            snapshot = snapshot or await self._safe_snapshot(str(worktree) if worktree else None)
            unverifiable_worktree = worktree is not None and snapshot is None
            target = (
                TaskStatus.NEEDS_REVIEW
                if unverifiable_worktree or (snapshot and snapshot.dirty)
                else TaskStatus.CANCELLED
            )
            await self._finish_abnormal(
                task_id,
                request,
                target,
                "Cancelled task left changes for Codex review"
                if target is TaskStatus.NEEDS_REVIEW
                else "Task cancelled",
                snapshot,
                error=exc,
            )
        except AgentTimedOut as exc:
            snapshot = snapshot or await self._safe_snapshot(str(worktree) if worktree else None)
            unverifiable_worktree = worktree is not None and snapshot is None
            target = (
                TaskStatus.NEEDS_REVIEW
                if unverifiable_worktree or (snapshot and snapshot.dirty)
                else TaskStatus.TIMED_OUT
            )
            await self._finish_abnormal(
                task_id,
                request,
                target,
                "Timed-out task left changes for Codex review"
                if target is TaskStatus.NEEDS_REVIEW
                else "Task timed out",
                snapshot,
                error=exc,
            )
        except Exception as exc:
            snapshot = snapshot or await self._safe_snapshot(str(worktree) if worktree else None)
            row = self.persistence.get_task(task_id)
            retryable = bool(getattr(exc, "retryable", False))
            unverifiable_worktree = worktree is not None and snapshot is None
            if (
                request.execution.read_only
                and retryable
                and int(row["attempt"]) <= self.config.limits.read_retry_count
                and not unverifiable_worktree
                and not (snapshot and snapshot.dirty)
            ):
                self.persistence.transition(
                    task_id,
                    TaskStatus.QUEUED,
                    phase="queued",
                    reason=f"safe read-only retry after {type(exc).__name__}",
                    progress_summary="Read-only task queued for its single automatic retry",
                    recovery=True,
                    clear_lease=True,
                    error_class=type(exc).__name__,
                    error_message=redact_sensitive_text(
                        str(exc), secrets=self._redaction_secrets
                    ),
                )
                await self.queue.enqueue(task_id)
            else:
                target = (
                    TaskStatus.NEEDS_REVIEW
                    if unverifiable_worktree or (snapshot and snapshot.dirty)
                    else TaskStatus.FAILED
                )
                await self._finish_abnormal(
                    task_id,
                    request,
                    target,
                    "Worker failed after producing changes; Codex review required"
                    if target is TaskStatus.NEEDS_REVIEW
                    else "Worker execution failed",
                    snapshot,
                    error=exc,
                )
        finally:
            self._cancel_events.pop(task_id, None)
