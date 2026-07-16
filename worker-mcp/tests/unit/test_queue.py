from __future__ import annotations

import asyncio

import pytest

from worker_mcp.queue import AsyncFileLock, TaskQueue


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
