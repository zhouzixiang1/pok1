"""Bounded task queue, repository semaphores, and cross-process path locks."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import fcntl
import hashlib
import os
from pathlib import Path
from typing import Awaitable, Callable

from .config import WorkerConfig
from .schemas import TaskEnvelope


class AsyncFileLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    async def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                await asyncio.sleep(0.1)
            except BaseException:
                os.close(self.fd)
                self.fd = None
                raise

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None


class ConcurrencyController:
    def __init__(self, config: WorkerConfig):
        limits = config.limits
        self._global_read = asyncio.Semaphore(limits.global_read_tasks)
        self._global_write = asyncio.Semaphore(limits.global_write_tasks)
        self._processes = asyncio.Semaphore(limits.max_subprocesses)
        self._repo_read_limit = limits.repository_read_tasks
        self._repo_write_limit = limits.repository_write_tasks
        self._repo_read: dict[str, asyncio.Semaphore] = {}
        self._repo_write: dict[str, asyncio.Semaphore] = {}
        self._map_lock = asyncio.Lock()
        self._lock_root = config.state_dir / "locks"

    async def _repo_semaphore(self, repo: str, *, read_only: bool) -> asyncio.Semaphore:
        async with self._map_lock:
            mapping = self._repo_read if read_only else self._repo_write
            limit = self._repo_read_limit if read_only else self._repo_write_limit
            return mapping.setdefault(repo, asyncio.Semaphore(limit))

    def _path_lock_paths(self, request: TaskEnvelope) -> list[Path]:
        if request.execution.read_only:
            return []
        repo_hash = hashlib.sha256(request.repo.encode("utf-8")).hexdigest()[:16]
        paths = []
        for scope in sorted(request.allowed_paths):
            scope_hash = hashlib.sha256(scope.encode("utf-8")).hexdigest()
            paths.append(self._lock_root / repo_hash / f"{scope_hash}.lock")
        return paths

    @asynccontextmanager
    async def slot(self, request: TaskEnvelope):
        global_sem = self._global_read if request.execution.read_only else self._global_write
        repo_sem = await self._repo_semaphore(request.repo, read_only=request.execution.read_only)
        locks = [AsyncFileLock(path) for path in self._path_lock_paths(request)]
        async with global_sem, repo_sem, self._processes:
            try:
                for lock in locks:
                    await lock.acquire()
                yield
            finally:
                for lock in reversed(locks):
                    lock.release()


class TaskQueue:
    def __init__(
        self,
        worker: Callable[[str], Awaitable[None]],
        *,
        workers: int,
    ):
        self._worker_callback = worker
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._workers_count = workers
        self._tasks: list[asyncio.Task[None]] = []
        self._enqueued: set[str] = set()
        self._lock = asyncio.Lock()
        self._stopping = False

    @property
    def running(self) -> bool:
        return bool(self._tasks) and all(not task.done() for task in self._tasks)

    async def start(self) -> None:
        if self._tasks:
            return
        self._stopping = False
        self._tasks = [
            asyncio.create_task(self._run(index), name=f"worker-mcp-queue-{index}")
            for index in range(self._workers_count)
        ]

    async def stop(self) -> None:
        if not self._tasks:
            return
        self._stopping = True
        while True:
            try:
                queued = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if queued is not None:
                self._enqueued.discard(queued)
            self._queue.task_done()
        for _ in self._tasks:
            await self._queue.put(None)
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def enqueue(self, task_id: str) -> bool:
        async with self._lock:
            if self._stopping:
                return False
            if task_id in self._enqueued:
                return False
            self._enqueued.add(task_id)
            await self._queue.put(task_id)
            return True

    async def _run(self, _index: int) -> None:
        while True:
            task_id = await self._queue.get()
            if task_id is None:
                self._queue.task_done()
                return
            async with self._lock:
                self._enqueued.discard(task_id)
                stopping = self._stopping
            try:
                if not stopping:
                    await self._worker_callback(task_id)
            except Exception:
                # TaskService owns durable error transitions. A callback bug must
                # not kill the shared dispatcher or consume future tasks.
                pass
            finally:
                self._queue.task_done()
