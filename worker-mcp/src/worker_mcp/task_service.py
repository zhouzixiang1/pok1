"""Durable task application service and recovery-aware execution pipeline."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path, PurePosixPath
import socket
from typing import Any

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
from .queue import ConcurrencyController, TaskQueue
from .result_normalizer import normalize_failure, normalize_success
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
from .worktree import WorktreeManager, WorktreeSnapshot


class TaskService:
    def __init__(
        self,
        config: WorkerConfig,
        *,
        executor: BaseAgentExecutor | None = None,
    ):
        self.config = config
        self.config.prepare_directories()
        self.persistence = Persistence(config.state_dir / "tasks.sqlite3")
        self.worktrees = WorktreeManager(config)
        self.executor = executor or executor_for_config(config)
        self.concurrency = ConcurrencyController(config)
        self.audit = AuditLogger(
            config.state_dir / "logs" / "worker-mcp.jsonl",
            max_bytes=config.logging.max_bytes,
            backup_count=config.logging.backup_count,
        )
        workers = min(
            config.limits.max_subprocesses,
            config.limits.global_read_tasks + config.limits.global_write_tasks,
        )
        self.queue = TaskQueue(self._execute_task, workers=workers)
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._owner = f"{socket.gethostname()}:{id(self)}"
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        await self.queue.start()
        await self._recover()
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            self.audit.close()
            return
        for event in self._cancel_events.values():
            event.set()
        await self.queue.stop()
        self.audit.close()
        self._started = False

    @staticmethod
    def _path_under(child: str, parent: str) -> bool:
        child_path = PurePosixPath(child)
        parent_path = PurePosixPath(parent)
        return child_path == parent_path or parent_path in child_path.parents

    def _validate_scope_contract(self, request: TaskEnvelope) -> None:
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
            trace_id=canonical.trace_id,
            task_id=task_id,
            idempotency_key=canonical.idempotency_key,
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
        row = self.persistence.get_task(task_id)
        status = TaskStatus(row["status"])
        if is_terminal(status):
            return CancelResponse(
                task_id=task_id,
                status=status,
                worktree_path=row["worktree_path"],
                progress_summary=row["progress_summary"],
            )
        self.persistence.request_cancel(task_id)
        if status in {TaskStatus.ACCEPTED, TaskStatus.QUEUED}:
            failure = normalize_failure(
                task_id=task_id,
                summary="Task cancelled before execution",
                worktree_path=row["worktree_path"],
            )
            self.persistence.save_result(task_id, failure.model_dump_json())
            row = self.persistence.transition(
                task_id,
                TaskStatus.CANCELLED,
                phase="cancelled",
                reason="cancel requested before executor claim",
                progress_summary="Task was cancelled before execution",
                clear_lease=True,
            )
        else:
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
            if row["cancel_requested"]:
                snapshot = await self._safe_snapshot(row["worktree_path"])
                await self._finish_abnormal(
                    task_id,
                    request,
                    TaskStatus.CANCELLED,
                    "Task was cancelled during service recovery",
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
            retries_allowed = self.config.limits.read_retry_count
            if (
                request.execution.read_only
                and int(row["attempt"]) <= retries_allowed
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
                if snapshot and snapshot.dirty
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
    ) -> None:
        row = self.persistence.get_task(task_id)
        partial = target is TaskStatus.NEEDS_REVIEW
        result = normalize_failure(
            task_id=task_id,
            summary=summary,
            worktree_path=row["worktree_path"],
            snapshot=snapshot,
            partial=partial,
        )
        self.persistence.save_result(task_id, result.model_dump_json())
        self.persistence.transition(
            task_id,
            target,
            phase=target.value,
            reason=summary,
            progress_summary=summary,
            clear_lease=True,
            error_class=type(error).__name__ if error else None,
            error_message=str(error) if error else None,
        )
        self.audit.log(
            "task.terminal",
            timestamp=datetime.now().astimezone().isoformat(),
            trace_id=request.trace_id,
            task_id=task_id,
            repository=request.repo,
            base_commit=request.base_commit,
            worktree_path=row["worktree_path"],
            state=target.value,
            phase=target.value,
            attempt=row["attempt"],
            files_changed=list(snapshot.changed_files) if snapshot else [],
            error_class=type(error).__name__ if error else None,
            error_message=str(error) if error else None,
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
                worktree = await asyncio.to_thread(
                    self.worktrees.prepare, request, task_id
                )
                self.persistence.update_worktree(task_id, str(worktree))
                self.persistence.transition(
                    task_id,
                    TaskStatus.RUNNING,
                    phase="running",
                    reason="isolated worktree prepared",
                    progress_summary="Claude Agent SDK Worker is running in isolation",
                )
                execution = await self.executor.run(request, worktree, cancel_event)
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
                if request.execution.read_only and snapshot.dirty:
                    violations.append("read-only task produced a worktree diff")
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
                result = normalize_success(
                    task_id=task_id,
                    execution=execution,
                    snapshot=snapshot,
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
                    trace_id=request.trace_id,
                    task_id=task_id,
                    idempotency_key=request.idempotency_key,
                    repository=request.repo,
                    base_commit=request.base_commit,
                    worktree_path=str(worktree),
                    state=TaskStatus.SUCCEEDED.value,
                    phase="succeeded",
                    attempt=final["attempt"],
                    sdk_session_id=execution.session_id,
                    tool_calls=len(execution.audit.get("commands", [])),
                    commands=execution.audit.get("commands", []),
                    files_changed=list(snapshot.changed_files),
                    tests=[item.model_dump() for item in result.tests],
                    duration_ms=execution.duration_ms,
                    turn_count=execution.turns,
                )
        except AgentCancelled as exc:
            snapshot = snapshot or await self._safe_snapshot(str(worktree) if worktree else None)
            target = (
                TaskStatus.NEEDS_REVIEW
                if snapshot and snapshot.dirty
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
            target = (
                TaskStatus.NEEDS_REVIEW
                if snapshot and snapshot.dirty
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
            if (
                request.execution.read_only
                and retryable
                and int(row["attempt"]) <= self.config.limits.read_retry_count
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
                    error_message=str(exc),
                )
                await self.queue.enqueue(task_id)
            else:
                target = (
                    TaskStatus.NEEDS_REVIEW
                    if snapshot and snapshot.dirty
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
