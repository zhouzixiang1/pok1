from __future__ import annotations

import asyncio

import pytest

from conftest import run_git
from worker_mcp.agent_executor import (
    AgentCancelled,
    AgentExecution,
    AgentExecutionError,
    AgentTimedOut,
    MockAgentExecutor,
)
from worker_mcp.persistence import IdempotencyConflict
from worker_mcp.schemas import (
    ExecutionProfile,
    TaskEnvelope,
    TaskStatus,
    TaskType,
    WorkerReportedResult,
)
from worker_mcp.task_service import TaskService


def request(git_repo, *, key="service-test-0001", read_only=True, task_type=TaskType.ANALYZE):
    return TaskEnvelope(
        goal="inspect or patch source",
        context="integration test",
        repo=str(git_repo),
        base_commit=run_git(git_repo, "rev-parse", "HEAD"),
        allowed_paths=["src", "tests"],
        forbidden_paths=["archive"],
        constraints=[],
        acceptance_criteria=["return evidence"],
        execution=ExecutionProfile(
            read_only=read_only, use_worktree=True, max_turns=4, timeout_sec=30
        ),
        idempotency_key=key,
        task_type=task_type,
    )


async def wait_terminal(service: TaskService, task_id: str, timeout=10):
    async with asyncio.timeout(timeout):
        while True:
            status = service.status(task_id)
            if status.status in {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.TIMED_OUT,
                TaskStatus.NEEDS_REVIEW,
            }:
                return status
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_read_task_async_success_and_strict_idempotency(worker_config, git_repo):
    service = TaskService(worker_config, executor=MockAgentExecutor())
    await service.start()
    try:
        first = await service.submit(request(git_repo))
        replay = await service.submit(request(git_repo))
        assert replay.task_id == first.task_id and replay.idempotent_replay
        status = await wait_terminal(service, first.task_id)
        assert status.status is TaskStatus.SUCCEEDED and status.attempt == 1
        result = service.result(first.task_id)
        assert result.status.value == "succeeded"
        assert result.files_changed == []
        with pytest.raises(IdempotencyConflict):
            await service.submit(
                request(git_repo).model_copy(update={"goal": "different request"})
            )
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_write_task_returns_actual_diff_without_touching_primary(worker_config, git_repo):
    primary_before = run_git(git_repo, "status", "--porcelain")

    async def patch(request, worktree, calls, cancel_event):
        assert calls == 1 and not cancel_event.is_set()
        (worktree / "src" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
        return AgentExecution(
            reported=WorkerReportedResult(
                summary="updated value", acceptance_result="patch generated"
            ),
            audit={
                "files_read": [str(worktree / "src" / "module.py")],
                "commands": [
                    {
                        "command": "git diff --check",
                        "exit_code": 0,
                        "duration_ms": 1,
                        "allowed": True,
                    }
                ],
                "denied": [],
            },
            session_id="mock-write",
            turns=2,
            duration_ms=5,
        )

    service = TaskService(worker_config, executor=MockAgentExecutor(patch))
    await service.start()
    try:
        submitted = await service.submit(
            request(
                git_repo,
                key="service-write-0001",
                read_only=False,
                task_type=TaskType.PATCH,
            )
        )
        status = await wait_terminal(service, submitted.task_id)
        assert status.status is TaskStatus.SUCCEEDED
        result = service.result(submitted.task_id)
        assert result.files_changed == ["src/module.py"]
        assert "VALUE = 2" in result.diff
        assert run_git(git_repo, "status", "--porcelain") == primary_before
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_read_task_retries_once_after_executor_failure(worker_config, git_repo):
    async def flaky(request, worktree, calls, cancel_event):
        if calls == 1:
            raise AgentExecutionError("simulated SDK crash")
        return AgentExecution(
            reported=WorkerReportedResult(summary="recovered", acceptance_result="ok"),
            audit={"files_read": [], "commands": [], "denied": []},
            session_id="retry",
            turns=1,
            duration_ms=1,
        )

    executor = MockAgentExecutor(flaky)
    service = TaskService(worker_config, executor=executor)
    await service.start()
    try:
        submitted = await service.submit(
            request(git_repo, key="service-retry-0001")
        )
        status = await wait_terminal(service, submitted.task_id)
        assert status.status is TaskStatus.SUCCEEDED
        assert status.attempt == 2 and executor.calls == 2
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_cancelled_dirty_write_enters_needs_review(worker_config, git_repo):
    async def blocking(request, worktree, calls, cancel_event):
        (worktree / "src" / "module.py").write_text("VALUE = 9\n", encoding="utf-8")
        await cancel_event.wait()
        raise AgentCancelled("cancelled")

    service = TaskService(worker_config, executor=MockAgentExecutor(blocking))
    await service.start()
    try:
        submitted = await service.submit(
            request(
                git_repo,
                key="service-cancel-0001",
                read_only=False,
                task_type=TaskType.PATCH,
            )
        )
        async with asyncio.timeout(5):
            while service.status(submitted.task_id).status is not TaskStatus.RUNNING:
                await asyncio.sleep(0.05)
        cancelled = await service.cancel(submitted.task_id)
        assert cancelled.status is TaskStatus.NEEDS_REVIEW
        result = service.result(submitted.task_id)
        assert result.status.value == "partial"
        assert result.files_changed == ["src/module.py"]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_timeout_without_diff_is_timed_out(worker_config, git_repo):
    async def timed(request, worktree, calls, cancel_event):
        raise AgentTimedOut("simulated timeout")

    service = TaskService(worker_config, executor=MockAgentExecutor(timed))
    await service.start()
    try:
        submitted = await service.submit(
            request(git_repo, key="service-timeout-0001")
        )
        status = await wait_terminal(service, submitted.task_id)
        assert status.status is TaskStatus.TIMED_OUT
    finally:
        await service.stop()
