"""Cross-process exclusive lease for the official EXE host/port resource."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import time
from typing import TextIO


@dataclass
class OfficialPlatformLease:
    path: Path
    owner: str
    handle: TextIO | None

    def release(self) -> None:
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "OfficialPlatformLease":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def try_acquire_official_platform(
    path: str | Path,
    *,
    owner: str,
) -> OfficialPlatformLease | None:
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(
        f"owner={owner} pid={os.getpid()} acquired_at={time.time():.6f}\n"
    )
    handle.flush()
    os.fsync(handle.fileno())
    return OfficialPlatformLease(path=lock_path, owner=owner, handle=handle)


def acquire_official_platform(
    path: str | Path,
    *,
    owner: str,
    timeout: float,
    poll_interval: float = 0.5,
) -> OfficialPlatformLease:
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        lease = try_acquire_official_platform(path, owner=owner)
        if lease is not None:
            return lease
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"official_platform_lock_timeout: {Path(path)} after {timeout:g}s"
            )
        time.sleep(max(0.01, float(poll_interval)))


def official_platform_busy(path: str | Path) -> bool:
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
