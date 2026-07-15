"""Global state for the unified web app."""

import asyncio
import json
import os
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shutdown_manager import ShutdownManager


# Mirrors daemon_management.MAX_SAFE_DAEMON_WORKERS. Kept here (not imported)
# to avoid a web/server -> web/core import cycle at module load. The cap
# prevents OOM-kills: each mirror battle forks two bot subprocesses, so peak
# memory scales ~3x per worker (2026-06-16 rc=-9 storm at 28 workers).
_MAX_SAFE_DAEMON_WORKERS = 12


def _default_daemon_workers() -> int:
    """Default daemon workers = CPU cores * 7/8, clamped to [1, _MAX_SAFE_DAEMON_WORKERS]."""
    return max(
        1,
        min(
            _MAX_SAFE_DAEMON_WORKERS,
            int((os.cpu_count() or 1) * 28 / 32),
        ),
    )


class AppState:
    def __init__(self, config_file=None):
        self._lock = threading.RLock()
        # Configuration persistence may fsync.  Serialize writers separately
        # so ordinary status/config readers never block on disk I/O while
        # holding the in-memory state lock.
        self._config_transaction_lock = threading.Lock()
        self._config_file = config_file or Path(__file__).resolve().parents[1] / "core" / "results" / "app_config.json"
        self.mode: str = "orchestrator"
        self.running: bool = False  # Coarse-grained loop control: True = orchestrator loop is active, False = stopped or idle
        self.daemon_enabled: bool = True
        self.daemon_workers: int = _default_daemon_workers()
        self.daemon_pairs: int = 5
        self.current_v: int = 0
        self.next_v: int = 0
        self.generation_count: int = 0
        self.decisions: list = []
        self._evolution_task: asyncio.Task | None = None
        self._runtime_owner_id: str | None = None
        self._shutdown_mgr: "ShutdownManager | None" = None
        self._shutdown_owner_id: str | None = None
        self._load_config()

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "mode": self.mode,
                "running": self.running,
                "daemon_enabled": self.daemon_enabled,
                "daemon_workers": self.daemon_workers,
                "daemon_pairs": self.daemon_pairs,
                "current_v": self.current_v,
                "next_v": self.next_v,
                "generation_count": self.generation_count,
                "decisions": self.decisions[-50:],
            }

    def get_config(self) -> dict:
        with self._lock:
            return {
                "mode": self.mode,
                "daemon_enabled": self.daemon_enabled,
                "daemon_workers": self.daemon_workers,
                "daemon_pairs": self.daemon_pairs,
            }

    def update_config(self, **kwargs) -> dict:
        with self._config_transaction_lock:
            with self._lock:
                prospective = self._config_payload_locked()
                prospective.update(kwargs)
                prospective = self._validated_config(prospective)
            # Persist outside ``_lock``.  A failed write leaves memory
            # unchanged, while the writer lock prevents an override/update
            # from racing the disk-to-memory publish boundary.
            self._write_config_atomic(prospective)
            with self._lock:
                self._set_config_locked(prospective)
                return {
                    "mode": self.mode,
                    **self._config_payload_locked(),
                }

    def override_runtime_config(self, **kwargs) -> dict:
        """Apply process-local CLI overrides without changing persisted user config."""
        with self._config_transaction_lock:
            with self._lock:
                self._apply_config_locked(kwargs)
                return {
                    "mode": self.mode,
                    **self._config_payload_locked(),
                }

    def _apply_config_locked(self, updates: dict):
        prospective = self._config_payload_locked()
        prospective.update(updates)
        self._set_config_locked(self._validated_config(prospective))

    @staticmethod
    def _validated_config(config: dict) -> dict:
        allowed = {"daemon_enabled", "daemon_workers", "daemon_pairs"}
        unknown = sorted(set(config) - allowed)
        if unknown:
            raise ValueError(f"unknown runtime configuration fields: {unknown}")
        enabled = config.get("daemon_enabled")
        workers = config.get("daemon_workers")
        pairs = config.get("daemon_pairs")
        if not isinstance(enabled, bool):
            raise ValueError("daemon_enabled must be boolean")
        if (
            not isinstance(workers, int)
            or isinstance(workers, bool)
            or not 1 <= workers <= _MAX_SAFE_DAEMON_WORKERS
        ):
            raise ValueError(
                f"daemon_workers must be an integer in [1, {_MAX_SAFE_DAEMON_WORKERS}]"
            )
        if (
            not isinstance(pairs, int)
            or isinstance(pairs, bool)
            or not 1 <= pairs <= 20
        ):
            raise ValueError("daemon_pairs must be an integer in [1, 20]")
        return {
            "daemon_enabled": enabled,
            "daemon_workers": workers,
            "daemon_pairs": pairs,
        }

    def _config_payload_locked(self) -> dict:
        return {
            "daemon_enabled": self.daemon_enabled,
            "daemon_workers": self.daemon_workers,
            "daemon_pairs": self.daemon_pairs,
        }

    def _set_config_locked(self, config: dict) -> None:
        self.daemon_enabled = bool(config["daemon_enabled"])
        self.daemon_workers = int(config["daemon_workers"])
        self.daemon_pairs = int(config["daemon_pairs"])

    def set_running(self, running: bool):
        with self._lock:
            self.running = bool(running)
            if running:
                if self._runtime_owner_id is None:
                    self._runtime_owner_id = uuid.uuid4().hex
            elif self._evolution_task is None or self._evolution_task.done():
                self._evolution_task = None
                self._runtime_owner_id = None
                self._shutdown_mgr = None
                self._shutdown_owner_id = None

    def begin_runtime_owner(self) -> str | None:
        """Reserve the sole orchestrator owner without replacing live work."""

        with self._lock:
            task = self._evolution_task
            if self.running or (task is not None and not task.done()):
                return None
            if task is not None and task.done():
                self._evolution_task = None
            owner_id = uuid.uuid4().hex
            self._runtime_owner_id = owner_id
            self._shutdown_mgr = None
            self._shutdown_owner_id = None
            self.running = True
            return owner_id

    def runtime_owner_id(self) -> str | None:
        with self._lock:
            return self._runtime_owner_id

    def try_set_running(self, running: bool) -> bool:
        if running:
            return self.begin_runtime_owner() is not None
        with self._lock:
            if not self.running:
                return False
        self.set_running(False)
        return True

    def stop_running(self):
        """Mark stopped and return the owner task without clearing it early.

        A cancelled task may take time to finish its cleanup.  Keeping the
        exact handle and owner fenced until its wrapper exits prevents a new
        start from overlapping it and prevents its late ``finally`` block from
        mutating a later owner.
        """
        with self._lock:
            self.running = False
            task = self._evolution_task
            if task is None or task.done():
                self._evolution_task = None
                self._runtime_owner_id = None
                self._shutdown_mgr = None
                self._shutdown_owner_id = None
            return task

    def _load_config(self):
        try:
            from evolution_infra import locked_file
            if self._config_file.exists():
                lock_file = self._config_file.with_name(
                    f".{self._config_file.name}.lock"
                )
                with locked_file(lock_file, "a+"):
                    data = json.loads(self._config_file.read_text(encoding="utf-8"))
                candidate = self._config_payload_locked()
                if isinstance(data, dict):
                    candidate.update({
                        key: data[key]
                        for key in ("daemon_enabled", "daemon_workers", "daemon_pairs")
                        if key in data
                    })
                self._set_config_locked(self._validated_config(candidate))
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            pass

    def _save_config(self):
        """Compatibility writer used by tests and maintenance callers."""

        with self._config_transaction_lock:
            with self._lock:
                payload = self._config_payload_locked()
            self._write_config_atomic(payload)

    def _write_config_atomic(self, payload: dict) -> None:
        """Durably publish one exact config or raise without changing memory."""

        payload = self._validated_config(dict(payload))
        from evolution_infra import locked_file

        self._config_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self._config_file.with_name(f".{self._config_file.name}.lock")
        tmp = self._config_file.with_name(
            f".{self._config_file.name}.{uuid.uuid4().hex}.tmp"
        )
        rollback_tmp = self._config_file.with_name(
            f".{self._config_file.name}.{uuid.uuid4().hex}.rollback.tmp"
        )
        try:
            with locked_file(lock_file, "a+"):
                try:
                    previous_bytes = self._config_file.read_bytes()
                    previous_exists = True
                except FileNotFoundError:
                    previous_bytes = b""
                    previous_exists = False
                with open(tmp, "x", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self._config_file)
                try:
                    self._fsync_config_directory()
                except Exception as publish_exc:
                    # The rename happened but its directory sync failed. Restore
                    # the prior bytes before reporting failure so memory and the
                    # visible file still describe one transaction outcome.
                    try:
                        if previous_exists:
                            with open(rollback_tmp, "xb") as handle:
                                handle.write(previous_bytes)
                                handle.flush()
                                os.fsync(handle.fileno())
                            os.replace(rollback_tmp, self._config_file)
                        else:
                            self._config_file.unlink(missing_ok=True)
                        self._fsync_config_directory()
                    except Exception as rollback_exc:
                        raise RuntimeError(
                            "runtime config publish and rollback both failed: "
                            f"publish={type(publish_exc).__name__}; "
                            f"rollback={type(rollback_exc).__name__}"
                        ) from publish_exc
                    raise
        finally:
            tmp.unlink(missing_ok=True)
            rollback_tmp.unlink(missing_ok=True)

    def _fsync_config_directory(self) -> None:
        directory_fd = os.open(
            self._config_file.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def bootstrap(self, current_v: int):
        with self._lock:
            self.current_v = current_v
            self.next_v = current_v + 1
            self.generation_count = current_v

    def set_generation(self, current_v: int, next_v: int):
        with self._lock:
            self.current_v = current_v
            self.next_v = next_v
            self.generation_count += 1

    def set_task(
        self,
        task: asyncio.Task,
        *,
        owner_id: str | None = None,
    ) -> str:
        with self._lock:
            if owner_id is None:
                owner_id = self._runtime_owner_id or uuid.uuid4().hex
            if (
                self._runtime_owner_id is not None
                and self._runtime_owner_id != owner_id
            ):
                raise RuntimeError("evolution task owner fencing conflict")
            if (
                self._evolution_task is not None
                and self._evolution_task is not task
                and not self._evolution_task.done()
            ):
                raise RuntimeError("evolution task ownership conflict")
            self._runtime_owner_id = owner_id
            self._evolution_task = task
            return owner_id

    def clear_task_if(
        self,
        task: asyncio.Task | None,
        *,
        owner_id: str | None = None,
    ) -> None:
        with self._lock:
            if (
                task is not None
                and (
                    self._evolution_task is task
                    or (
                        self._evolution_task is None
                        and owner_id is not None
                        and self._runtime_owner_id == owner_id
                    )
                )
                and (owner_id is None or self._runtime_owner_id == owner_id)
            ):
                self.running = False
                self._evolution_task = None
                self._runtime_owner_id = None
                if owner_id is None or self._shutdown_owner_id == owner_id:
                    self._shutdown_mgr = None
                    self._shutdown_owner_id = None

    def abort_runtime_owner(self, owner_id: str | None) -> bool:
        """Release a reservation only when no live task was attached."""

        with self._lock:
            if owner_id is None or self._runtime_owner_id != owner_id:
                return False
            task = self._evolution_task
            if task is not None and not task.done():
                return False
            self.running = False
            self._evolution_task = None
            self._runtime_owner_id = None
            self._shutdown_mgr = None
            self._shutdown_owner_id = None
            return True

    def task_snapshot(self) -> dict:
        with self._lock:
            task = self._evolution_task
            shutdown_requested = bool(
                self._shutdown_mgr and self._shutdown_mgr.is_shutting_down
            )
            if task is None:
                return {
                    "present": False,
                    "done": None,
                    "cancelled": None,
                    "shutdown_requested": shutdown_requested,
                    "owner_id": self._runtime_owner_id,
                }
            return {
                "present": True,
                "done": task.done(),
                "cancelled": task.cancelled() if task.done() else False,
                "shutdown_requested": shutdown_requested,
                "owner_id": self._runtime_owner_id,
            }

    def cancel_task(self):
        with self._lock:
            if self._evolution_task and not self._evolution_task.done():
                self._evolution_task.cancel()

    def set_shutdown_mgr(
        self,
        mgr: "ShutdownManager",
        *,
        owner_id: str | None = None,
    ):
        with self._lock:
            if owner_id is not None and self._runtime_owner_id != owner_id:
                raise RuntimeError("shutdown manager owner fencing conflict")
            self._shutdown_mgr = mgr
            self._shutdown_owner_id = owner_id or self._runtime_owner_id

    def request_shutdown(self):
        with self._lock:
            if self._shutdown_mgr:
                self._shutdown_mgr.request_shutdown()

    def add_decision(self, tool_name: str, result_summary: str):
        import time
        with self._lock:
            self.decisions.append({
                "tool": tool_name,
                "summary": result_summary[:200],
                "ts": time.time(),
            })
            if len(self.decisions) > 100:
                self.decisions = self.decisions[-100:]


app_state = AppState()


async def run_evolution_task(coro, *, owner_id: str | None = None):
    """Run the single owned evolution coroutine and clear its running flag.

    The orchestrator has several legitimate early-return paths (for example a
    rejected cost policy).  Keeping this ownership cleanup outside the
    orchestrator makes both lifespan startup and the explicit control route
    publish the same stopped state when any of those paths completes.
    """

    owner_task = asyncio.current_task()
    captured_owner_id = owner_id or app_state.runtime_owner_id()
    try:
        return await coro
    finally:
        app_state.clear_task_if(
            owner_task,
            owner_id=captured_owner_id,
        )
