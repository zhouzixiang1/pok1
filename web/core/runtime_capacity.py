"""Cross-process capacity leases for poker match workloads."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import TextIO


CAPACITY_ROOT_ENV = "POK_RUNTIME_CAPACITY_ROOT"
DEFAULT_CAPACITY_ROOT = (
    Path(tempfile.gettempdir()) / f"pok-runtime-capacity-{os.geteuid()}"
)
DEFAULT_MATCH_SLOTS = 12
DEFAULT_CAPACITY_WAIT_SECONDS = 300.0


def runtime_capacity_root(root: str | Path | None = None) -> Path:
    """Resolve the host-shared capacity root, never a checkout-local path."""
    if root is not None:
        return Path(root)
    configured = os.environ.get(CAPACITY_ROOT_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_CAPACITY_ROOT


def _prepare_capacity_root(root: str | Path | None) -> Path:
    directory = runtime_capacity_root(root)
    if directory.exists() and directory.is_symlink():
        raise RuntimeError(f"runtime capacity root must not be a symlink: {directory}")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"runtime capacity root is not a directory: {directory}")
    if info.st_uid != os.geteuid():
        raise RuntimeError(
            f"runtime capacity root is not owned by uid {os.geteuid()}: {directory}"
        )
    # The directory contains only advisory owner text, but a private mode also
    # prevents another local user from replacing lock files between opens.
    if stat.S_IMODE(info.st_mode) != 0o700:
        directory.chmod(0o700)
    return directory


def _open_slot(path: Path) -> TextIO:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        os.close(fd)
        raise RuntimeError(f"invalid runtime capacity slot: {path}")
    return os.fdopen(fd, "r+", encoding="utf-8")


@dataclass
class RuntimeCapacityLease:
    owner: str
    handles: list[TextIO]

    @property
    def slots(self) -> int:
        return len(self.handles)

    def release(self) -> None:
        handles, self.handles = self.handles, []
        for handle in handles:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def __enter__(self) -> "RuntimeCapacityLease":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def try_acquire_match_slots(
    owner: str,
    count: int,
    *,
    root: str | Path | None = None,
    total_slots: int = DEFAULT_MATCH_SLOTS,
) -> RuntimeCapacityLease | None:
    requested = max(1, int(count))
    total = max(1, int(total_slots))
    if requested > total:
        raise ValueError(
            f"requested runtime capacity {requested} exceeds total slots {total}"
        )
    directory = _prepare_capacity_root(root)
    handles: list[TextIO] = []
    for index in range(total):
        try:
            handle = _open_slot(directory / f"match-slot-{index:02d}.lock")
        except Exception:
            RuntimeCapacityLease(owner=owner, handles=handles).release()
            raise
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            continue
        handle.seek(0)
        handle.truncate()
        handle.write(f"owner={owner} pid={os.getpid()} acquired={time.time():.6f}\n")
        handle.flush()
        handles.append(handle)
        if len(handles) == requested:
            return RuntimeCapacityLease(owner=owner, handles=handles)
    RuntimeCapacityLease(owner=owner, handles=handles).release()
    return None


def acquire_match_slots(
    owner: str,
    count: int = 1,
    *,
    root: str | Path | None = None,
    total_slots: int = DEFAULT_MATCH_SLOTS,
    timeout: float | None = None,
    poll_interval: float = 0.1,
) -> RuntimeCapacityLease:
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    while True:
        lease = try_acquire_match_slots(
            owner,
            count,
            root=root,
            total_slots=total_slots,
        )
        if lease is not None:
            return lease
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"runtime capacity unavailable for {owner}")
        time.sleep(max(0.01, poll_interval))


async def acquire_match_slots_async(
    owner: str,
    count: int = 1,
    *,
    root: str | Path | None = None,
    total_slots: int = DEFAULT_MATCH_SLOTS,
    timeout: float | None = DEFAULT_CAPACITY_WAIT_SECONDS,
    poll_interval: float = 0.1,
) -> RuntimeCapacityLease:
    """Acquire a process-wide lease without blocking the asyncio event loop."""
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    while True:
        lease = try_acquire_match_slots(
            owner,
            count,
            root=root,
            total_slots=total_slots,
        )
        if lease is not None:
            return lease
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"runtime capacity unavailable for {owner}")
        await asyncio.sleep(max(0.01, poll_interval))
