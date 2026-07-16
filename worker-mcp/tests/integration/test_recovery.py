from __future__ import annotations

import asyncio

import pytest

from conftest import run_git
from worker_mcp.agent_executor import AgentCancelled, MockAgentExecutor
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
async def test_dirty_cancel_requested_before_restart_stays_needs_review(
    worker_config, git_repo
):
    task_id, worktree = interrupted_running(
        worker_config,
        make_request(
            git_repo,
            read_only=False,
            key="recovery-dirty-cancel-0001",
        ),
    )
    (worktree / "src" / "module.py").write_text("VALUE = 11\n", encoding="utf-8")
    store = Persistence(worker_config.state_dir / "tasks.sqlite3")
    row, terminal = store.cancel_or_request(
        task_id, pre_execution_result_json="{}"
    )
    assert not terminal and row["cancel_requested"] == 1

    service = TaskService(worker_config, executor=MockAgentExecutor())
    await service.start()
    try:
        assert service.status(task_id).status is TaskStatus.NEEDS_REVIEW
        result = service.result(task_id)
        assert result.status.value == "partial"
        assert result.files_changed == ["src/module.py"]
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


@pytest.mark.asyncio
async def test_unverifiable_interrupted_worktree_never_retries(
    worker_config, git_repo, monkeypatch
):
    task_id, _ = interrupted_running(
        worker_config,
        make_request(
            git_repo,
            read_only=True,
            key="recovery-unverifiable-read-0001",
        ),
    )
    executor = MockAgentExecutor()
    service = TaskService(worker_config, executor=executor)

    def snapshot_failure(_path):
        raise RuntimeError("snapshot evidence unavailable")

    monkeypatch.setattr(service.worktrees, "snapshot", snapshot_failure)
    await service.start()
    try:
        assert service.status(task_id).status is TaskStatus.NEEDS_REVIEW
        assert executor.calls == 0
        assert service.result(task_id).status.value == "partial"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_durable_worktree_intention_quarantines_half_created_path(
    worker_config, git_repo
):
    manager = WorktreeManager(worker_config)
    request = manager.validate_request(
        make_request(
            git_repo,
            read_only=False,
            key="recovery-half-created-worktree-0001",
        )
    )
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
    store.claim_task(task_id, "crashed-preparer")
    expected = manager.path_for(
        git_repo.resolve(), request.base_commit, task_id
    ).resolve()
    store.update_worktree(task_id, str(expected))
    expected.mkdir(parents=True)

    executor = MockAgentExecutor()
    service = TaskService(worker_config, executor=executor)
    await service.start()
    try:
        assert service.status(task_id).status is TaskStatus.NEEDS_REVIEW
        assert service.status(task_id).worktree_path == str(expected)
        assert executor.calls == 0
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_second_live_service_fails_closed_without_recovering_active_task(
    worker_config, git_repo
):
    running = asyncio.Event()

    async def blocking(request, worktree, calls, cancel_event):
        running.set()
        await cancel_event.wait()
        raise AgentCancelled("test service stopped")

    first_executor = MockAgentExecutor(blocking)
    first = TaskService(worker_config, executor=first_executor)
    await first.start()
    second = TaskService(worker_config, executor=MockAgentExecutor())
    try:
        submitted = await first.submit(
            make_request(
                git_repo,
                read_only=True,
                key="recovery-live-owner-0001",
            )
        )
        await asyncio.wait_for(running.wait(), timeout=5)
        with pytest.raises(RuntimeError, match="already owns this state_dir"):
            await second.start()
        row = first.persistence.get_task(submitted.task_id)
        assert row["status"] == TaskStatus.RUNNING.value
        assert int(row["attempt"]) == 1
        assert first_executor.calls == 1
    finally:
        await second.stop()
        await first.stop()
