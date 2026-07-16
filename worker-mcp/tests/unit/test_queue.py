from __future__ import annotations

import asyncio

import pytest

from conftest import run_git
from worker_mcp.queue import AsyncFileLock, ConcurrencyController, LockUnavailable, TaskQueue
from worker_mcp.schemas import ExecutionProfile, TaskEnvelope, TaskType


@pytest.mark.asyncio
async def test_file_lock_serializes_across_instances(tmp_path):
    first = AsyncFileLock(tmp_path / "scope.lock")
    second = AsyncFileLock(tmp_path / "scope.lock")
    await first.acquire()
    acquired = asyncio.Event()

    async def take_second():
        await second.acquire()
        acquired.set()

    task = asyncio.create_task(take_second())
    await asyncio.sleep(0.15)
    assert not acquired.is_set()
    first.release()
    await asyncio.wait_for(acquired.wait(), timeout=2)
    second.release()
    await task


@pytest.mark.asyncio
async def test_file_lock_can_fail_closed_without_waiting(tmp_path):
    first = AsyncFileLock(tmp_path / "singleton.lock")
    second = AsyncFileLock(tmp_path / "singleton.lock")
    await first.acquire()
    try:
        with pytest.raises(LockUnavailable):
            await second.acquire(wait=False)
    finally:
        first.release()


def write_request(git_repo, allowed_paths):
    return TaskEnvelope(
        goal="patch bounded scope",
        context="queue lock test",
        repo=str(git_repo),
        base_commit=run_git(git_repo, "rev-parse", "HEAD"),
        allowed_paths=allowed_paths,
        forbidden_paths=["archive"],
        constraints=[],
        acceptance_criteria=[],
        execution=ExecutionProfile(read_only=False, max_turns=4, timeout_sec=30),
        idempotency_key="queue-write-lock-0001",
        task_type=TaskType.PATCH,
    )


@pytest.mark.asyncio
async def test_parent_and_child_write_scopes_serialize_across_controllers(
    worker_config, git_repo
):
    first = ConcurrencyController(worker_config)
    second = ConcurrencyController(worker_config)
    parent = write_request(git_repo, ["src"])
    child = write_request(git_repo, ["src/nested"])
    entered = asyncio.Event()

    async def enter_child():
        async with second.slot(child):
            entered.set()

    async with first.slot(parent):
        task = asyncio.create_task(enter_child())
        await asyncio.sleep(0.15)
        assert not entered.is_set()
    await asyncio.wait_for(entered.wait(), timeout=2)
    await task


@pytest.mark.asyncio
async def test_queue_deduplicates_enqueued_task_ids():
    calls = []

    async def worker(task_id):
        calls.append(task_id)

    queue = TaskQueue(worker, workers=1)
    await queue.start()
    try:
        assert await queue.enqueue("one")
        assert not await queue.enqueue("one")
        async with asyncio.timeout(2):
            while calls != ["one"]:
                await asyncio.sleep(0.01)
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_queue_stop_does_not_start_waiting_tasks():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def worker(task_id):
        calls.append(task_id)
        started.set()
        await release.wait()

    queue = TaskQueue(worker, workers=1)
    await queue.start()
    await queue.enqueue("running")
    await asyncio.wait_for(started.wait(), timeout=2)
    await queue.enqueue("must-remain-persisted")
    stopping = asyncio.create_task(queue.stop())
    await asyncio.sleep(0.1)
    release.set()
    await stopping
    assert calls == ["running"]
