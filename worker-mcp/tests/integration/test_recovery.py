from __future__ import annotations

import asyncio

import pytest

from conftest import run_git
from worker_mcp.agent_executor import MockAgentExecutor
from worker_mcp.idempotency import request_fingerprint
from worker_mcp.persistence import Persistence
from worker_mcp.schemas import ExecutionProfile, TaskEnvelope, TaskStatus, TaskType
from worker_mcp.task_service import TaskService
from worker_mcp.worktree import WorktreeManager


def make_request(git_repo, *, read_only, key):
    return TaskEnvelope(
        goal="recover task",
        context="",
        repo=str(git_repo),
        base_commit=run_git(git_repo, "rev-parse", "HEAD"),
        allowed_paths=["src"],
        forbidden_paths=["archive"],
        constraints=[],
        acceptance_criteria=[],
        execution=ExecutionProfile(read_only=read_only, timeout_sec=30, max_turns=4),
        idempotency_key=key,
        task_type=TaskType.ANALYZE if read_only else TaskType.PATCH,
    )


def interrupted_running(worker_config, request):
    manager = WorktreeManager(worker_config)
    request = manager.validate_request(request)
    store = Persistence(worker_config.state_dir / "tasks.sqlite3")
    row, _ = store.create_or_get(request, request_fingerprint(request))
    task_id = row["task_id"]
    store.transition(
        task_id,
        TaskStatus.QUEUED,
        phase="queued",
        reason="test",
        progress_summary="queued",
    )
    store.claim_task(task_id, "dead-process")
    worktree = manager.prepare(request, task_id)
    store.update_worktree(task_id, str(worktree))
    store.transition(
        task_id,
        TaskStatus.RUNNING,
        phase="running",
        reason="test interruption",
        progress_summary="running",
    )
    return task_id, worktree


@pytest.mark.asyncio
async def test_dirty_interrupted_write_recovers_to_needs_review(worker_config, git_repo):
    task_id, worktree = interrupted_running(
        worker_config,
        make_request(git_repo, read_only=False, key="recovery-write-0001"),
    )
    (worktree / "src" / "module.py").write_text("VALUE = 7\n", encoding="utf-8")
    service = TaskService(worker_config, executor=MockAgentExecutor())
    await service.start()
    try:
        assert service.status(task_id).status is TaskStatus.NEEDS_REVIEW
        assert service.result(task_id).files_changed == ["src/module.py"]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_clean_interrupted_read_gets_single_safe_retry(worker_config, git_repo):
    task_id, _ = interrupted_running(
        worker_config,
        make_request(git_repo, read_only=True, key="recovery-read-0001"),
    )
    executor = MockAgentExecutor()
    service = TaskService(worker_config, executor=executor)
    await service.start()
    try:
        async with asyncio.timeout(10):
            while service.status(task_id).status not in {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
            }:
                await asyncio.sleep(0.05)
        assert service.status(task_id).status is TaskStatus.SUCCEEDED
        assert service.status(task_id).attempt == 2
    finally:
        await service.stop()
