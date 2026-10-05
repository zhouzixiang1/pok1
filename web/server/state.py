"""Global state for the unified web app."""

import asyncio
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from shutdown_manager import ShutdownManager


# Mirrors daemon_management.MAX_SAFE_DAEMON_WORKERS. Kept here (not imported)
# to avoid a web/server -> web/core import cycle at module load. The cap
# prevents OOM-kills: each mirror battle forks two bot subprocesses, so peak
# memory scales ~3x per worker (2026-06-16 rc=-9 storm at 28 workers).
_MAX_SAFE_DAEMON_WORKERS = 12
# Mirrors the national rating-match cap enforced by elo_daemon and the active
# workflow profile.  This is an evaluation-sample budget, not strength proof.
MAX_DAEMON_PAIRS = 8


def _default_daemon_workers() -> int:
    """Default daemon workers.

    Priority: ``POK_DAEMON_WORKERS`` env override, else CPU-based default
    (cores * 7/8, clamped to [1, _MAX_SAFE_DAEMON_WORKERS]). A persisted
    ``app_config.json`` value still wins over both — see ``AppState._load_config``.
    """
    env_workers = _env_int_in_range(
        "POK_DAEMON_WORKERS", 1, _MAX_SAFE_DAEMON_WORKERS
    )
    if env_workers is not None:
        return env_workers
    return max(
        1,
        min(
            _MAX_SAFE_DAEMON_WORKERS,
            int((os.cpu_count() or 1) * 28 / 32),
        ),
    )


def _default_daemon_pairs() -> int:
    """Default daemon pairs (70-hand matches per scheduled pairing).

    Priority: ``POK_DAEMON_PAIRS`` env override, else the canonical 5-match
    sample. A persisted ``app_config.json`` value still wins over both.
    """
    env_pairs = _env_int_in_range("POK_DAEMON_PAIRS", 1, MAX_DAEMON_PAIRS)
    if env_pairs is not None:
        return env_pairs
    return 5


def _env_int_in_range(name: str, lo: int, hi: int) -> int | None:
    """Read a positive integer env var clamped to [lo, hi]; None if unset/invalid."""
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(value, bool) or not lo <= value <= hi:
        return None
    return value


# --- P2 (2026-10-05): crash-revival supervisor around run_evolution_task ------
#
# The 06:12 v511 orchestrator crash (``KeyError('bot_name(next_v)')``) ended
# the one-shot evolution task for ~2h while the service kept burning saturator
# tokens: nothing re-entered ``orchestrator_loop`` after its crash branch
# returned ``terminal_outcome == -1.0``.  The supervisor re-enters the loop
# ONLY for that crash sentinel; every other terminal (operator stop, cost
# policy, manual pause, recovery blocked, LLM-availability stop) stays
# stopped.  Backoff is bounded (30s doubling to a 30min cap), a sliding
# window rate-limits the burst (5 restarts / 30min; beyond that a terminal
# ``operator_action_required`` event replaces the storm), and a stable run
# (>= 1h) resets the counters.

_ORCHESTRATOR_CRASH_OUTCOME = -1.0
# P2 (2026-10-05): ORCH_LLM_AVAILABILITY_BLOCKED_COST (orchestrator.py:98,
# -99995.0) is auto-restartable double insurance.  F9 evidence: the waitable
# 1302 pause path exited the loop silently up to four times in one day
# (16:29-18:08: 70min zero-flow, ~38M tokens) and only a manual start or a
# deploy restart recovered it.  The primary fix is the bounded re-query loop
# in orchestrator_abandon_and_cost; this classification guarantees the
# supervisor revives the pipeline even if that loop ever leaks the sentinel
# again.  Mirrored here (not imported) to keep web/server free of a
# web/core import at module load, exactly like _ORCHESTRATOR_CRASH_OUTCOME.
_ORCHESTRATOR_LLM_BLOCK_OUTCOME = -99995.0


def _env_float_in_range(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not lo <= value <= hi:
        return default
    return value


def _restart_initial_backoff_seconds() -> float:
    return _env_float_in_range(
        "POK_ORCHESTRATOR_RESTART_INITIAL_BACKOFF_SEC", 30.0, 0.0, 86400.0
    )


def _restart_max_backoff_seconds() -> float:
    return _env_float_in_range(
        "POK_ORCHESTRATOR_RESTART_MAX_BACKOFF_SEC", 1800.0, 1.0, 86400.0
    )


def _restart_max_burst() -> int:
    raw = os.environ.get("POK_ORCHESTRATOR_RESTART_MAX_BURST")
    try:
        value = int(raw) if raw else 5
    except (TypeError, ValueError):
        value = 5
    return max(1, value)


def _restart_window_seconds() -> float:
    return _env_float_in_range(
        "POK_ORCHESTRATOR_RESTART_WINDOW_SEC", 1800.0, 1.0, 86400.0
    )


def _restart_stable_run_seconds() -> float:
    return _env_float_in_range(
        "POK_ORCHESTRATOR_RESTART_STABLE_RUN_SEC", 3600.0, 0.0, 86400.0
    )


async def _restart_backoff_sleep(seconds: float) -> None:
    """Patchable sleep seam so tests never wait a real backoff."""

    await asyncio.sleep(seconds)


def _is_crash_outcome(result: object) -> bool:
    return (
        isinstance(result, (int, float))
        and not isinstance(result, bool)
        and float(result) in (
            _ORCHESTRATOR_CRASH_OUTCOME,
            _ORCHESTRATOR_LLM_BLOCK_OUTCOME,
        )
    )


def _active_pipeline_stage_label() -> str:
    """Best-effort stage label from the primary checkpoint (never raises)."""

    try:
        from evolution_infra import pipeline_state_path

        data = json.loads(
            Path(pipeline_state_path()).read_text(encoding="utf-8")
        )
        if isinstance(data, dict):
            return str(data.get("stage") or "")
    except Exception:
        pass
    return ""


def _emit_orchestrator_restart_event(
    *,
    status: str,
    attempt: int,
    backoff_s: float,
    last_error: str,
    stage: str,
    owner_id: str | None,
    operator_action_required: bool = False,
) -> None:
    try:
        from system_log import log_system_event

        log_system_event(
            "pipeline.orchestrator_auto_restart",
            "error" if operator_action_required else "warn",
            (
                "Orchestrator auto-restart rate-limited; operator action required"
                if operator_action_required
                else f"Orchestrator crashed; auto-restarting in {backoff_s:.0f}s"
            ),
            {
                "status": status,
                "attempt": attempt,
                "backoff_s": backoff_s,
                "last_error": last_error,
                "stage": stage,
                "owner_id": owner_id,
                "operator_action_required": operator_action_required,
            },
        )
    except Exception:
        pass


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
        self.daemon_pairs: int = _default_daemon_pairs()
        self.current_v: int = 0
        self.next_v: int = 0
        self.generation_count: int = 0
        self.decisions: list = []
        self._evolution_task: asyncio.Task | None = None
        self._runtime_owner_id: str | None = None
        self._task_lifecycle_revision = 0
        self._task_snapshot_listeners: list[Callable[[dict], None]] = []
        self._shutdown_mgr: "ShutdownManager | None" = None
        self._shutdown_owner_id: str | None = None
        # P7 (2026-10-05): pipeline liveness heartbeat for the LLM saturator
        # gate. Updated once per orchestrator cycle (and once per watchdog
        # tick while the loop task is alive); read by saturator_may_launch to
        # park background burns when the pipeline is dead/stopped.
        self._pipeline_heartbeat_monotonic: float | None = None
        # P2 (2026-10-05): last orchestrator crash note for restart events.
        self._last_orchestrator_crash: dict | None = None
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
            or not 1 <= pairs <= MAX_DAEMON_PAIRS
        ):
            raise ValueError(
                f"daemon_pairs must be an integer in [1, {MAX_DAEMON_PAIRS}]"
            )
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
            before = self._task_snapshot_locked()
            self.running = bool(running)
            if running:
                if self._runtime_owner_id is None:
                    self._runtime_owner_id = uuid.uuid4().hex
            elif self._evolution_task is None or self._evolution_task.done():
                self._evolution_task = None
                self._runtime_owner_id = None
                self._shutdown_mgr = None
                self._shutdown_owner_id = None
            after, changed = self._advance_task_lifecycle_locked(before)
        if changed:
            self._notify_task_snapshot(after)

    def begin_runtime_owner(self) -> str | None:
        """Reserve the sole orchestrator owner without replacing live work."""

        with self._lock:
            task = self._evolution_task
            if self.running or (task is not None and not task.done()):
                return None
            before = self._task_snapshot_locked()
            if task is not None and task.done():
                self._evolution_task = None
            owner_id = uuid.uuid4().hex
            self._runtime_owner_id = owner_id
            self._shutdown_mgr = None
            self._shutdown_owner_id = None
            self.running = True
            snapshot, changed = self._advance_task_lifecycle_locked(before)
        if changed:
            self._notify_task_snapshot(snapshot)
        return owner_id

    def runtime_owner_id(self) -> str | None:
        with self._lock:
            return self._runtime_owner_id

    def shutdown_requested(self) -> bool:
        """True when the live shutdown manager already requested a stop."""

        with self._lock:
            mgr = self._shutdown_mgr
        if mgr is None:
            return False
        try:
            return bool(mgr.is_shutting_down)
        except Exception:
            return False

    def note_pipeline_heartbeat(self) -> None:
        """Record one pipeline-liveness beat (P7 saturator gate)."""

        with self._lock:
            self._pipeline_heartbeat_monotonic = time.monotonic()

    def pipeline_heartbeat_age_seconds(
        self, *, now: float | None = None
    ) -> float | None:
        """Seconds since the last heartbeat; ``None`` when never recorded."""

        with self._lock:
            beat = self._pipeline_heartbeat_monotonic
        if beat is None:
            return None
        return max(0.0, (now if now is not None else time.monotonic()) - beat)

    def note_orchestrator_crash(self, error: object) -> None:
        """Stash the crash detail the restart events republish (P2)."""

        with self._lock:
            self._last_orchestrator_crash = {
                "error": str(error)[:500],
                "at": time.time(),
            }

    def last_orchestrator_crash(self) -> dict | None:
        with self._lock:
            return dict(self._last_orchestrator_crash) if self._last_orchestrator_crash else None

    def try_set_running(self, running: bool) -> bool:
        if running:
            return self.begin_runtime_owner() is not None
        with self._lock:
            if not self.running:
                return False
        self.set_running(False)
        return True

    def stop_running(self, *, owner_id: str | None = None):
        """Mark stopped and return the owner task without clearing it early.

        A cancelled task may take time to finish its cleanup.  Keeping the
        exact handle and owner fenced until its wrapper exits prevents a new
        start from overlapping it and prevents its late ``finally`` block from
        mutating a later owner.
        """
        with self._lock:
            if owner_id is not None and self._runtime_owner_id != owner_id:
                return None
            before = self._task_snapshot_locked()
            self.running = False
            task = self._evolution_task
            if task is None or task.done():
                self._evolution_task = None
                self._runtime_owner_id = None
                self._shutdown_mgr = None
                self._shutdown_owner_id = None
            after, changed = self._advance_task_lifecycle_locked(before)
        if changed:
            self._notify_task_snapshot(after)
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
            before = self._task_snapshot_locked()
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
            after, changed = self._advance_task_lifecycle_locked(before)
        if changed:
            self._notify_task_snapshot(after)
        return owner_id

    def clear_task_if(
        self,
        task: asyncio.Task | None,
        *,
        owner_id: str | None = None,
    ) -> None:
        with self._lock:
            before = self._task_snapshot_locked()
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
            after, changed = self._advance_task_lifecycle_locked(before)
        if changed:
            self._notify_task_snapshot(after)

    def abort_runtime_owner(self, owner_id: str | None) -> bool:
        """Release a reservation only when no live task was attached."""

        with self._lock:
            if owner_id is None or self._runtime_owner_id != owner_id:
                return False
            task = self._evolution_task
            if task is not None and not task.done():
                return False
            before = self._task_snapshot_locked()
            self.running = False
            self._evolution_task = None
            self._runtime_owner_id = None
            self._shutdown_mgr = None
            self._shutdown_owner_id = None
            snapshot, changed = self._advance_task_lifecycle_locked(before)
        if changed:
            self._notify_task_snapshot(snapshot)
        return True

    def add_task_snapshot_listener(self, listener: Callable[[dict], None]) -> None:
        """Register a best-effort observer for live task ownership changes."""

        with self._lock:
            self._task_snapshot_listeners.append(listener)

    def _notify_task_snapshot(self, snapshot: dict) -> None:
        """Notify outside the state lock so observers cannot block ownership CAS."""

        with self._lock:
            listeners = tuple(self._task_snapshot_listeners)
        for listener in listeners:
            try:
                listener(dict(snapshot))
            except Exception:
                # Status delivery is advisory only; it must not alter the
                # task-owner transition or make a running task unsafe.
                continue

    def _advance_task_lifecycle_locked(self, before: dict) -> tuple[dict, bool]:
        """Advance the monotonic owner epoch only for visible lifecycle changes."""

        after = self._task_snapshot_locked()
        if after == before:
            return after, False
        self._task_lifecycle_revision += 1
        return self._task_snapshot_locked(), True

    def _task_snapshot_locked(self) -> dict:
        task = self._evolution_task
        shutdown_requested = bool(
            self._shutdown_mgr
            and self._shutdown_owner_id == self._runtime_owner_id
            and self._shutdown_mgr.is_shutting_down
        )
        if task is None:
            return {
                "present": False,
                "done": None,
                "cancelled": None,
                "shutdown_requested": shutdown_requested,
                "status_eligible": False,
                "owner_id": self._runtime_owner_id,
                "lifecycle_revision": self._task_lifecycle_revision,
            }
        done = bool(task.done())
        cancelled = getattr(task, "cancelled", False)
        cancelled_value = (
            bool(cancelled()) if callable(cancelled) else bool(cancelled)
        ) if done else False
        return {
            "present": True,
            "done": done,
            "cancelled": cancelled_value,
            "shutdown_requested": shutdown_requested,
            "status_eligible": bool(
                self.running and not done and not shutdown_requested
            ),
            "owner_id": self._runtime_owner_id,
            "lifecycle_revision": self._task_lifecycle_revision,
        }

    def task_snapshot(self) -> dict:
        with self._lock:
            return self._task_snapshot_locked()

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
        snapshot = None
        changed = False
        with self._lock:
            # Shutdown is an owner-scoped capability.  An unowned lifespan or
            # late startup attempt must never replace the manager used by an
            # already-running orchestrator.
            if (
                not isinstance(owner_id, str)
                or not owner_id
                or self._runtime_owner_id != owner_id
                or not self.running
            ):
                raise RuntimeError("shutdown manager owner fencing conflict")
            if self._shutdown_mgr is not None and self._shutdown_mgr is not mgr:
                raise RuntimeError("shutdown manager replacement conflict")
            before = self._task_snapshot_locked()
            self._shutdown_mgr = mgr
            self._shutdown_owner_id = owner_id
            listener = getattr(mgr, "add_shutdown_listener", None)
            if callable(listener):
                listener(
                    lambda: self._on_shutdown_requested(
                        mgr,
                        owner_id,
                    )
                )
            # A signal can race manager construction/binding.  If it has
            # already arrived, the new visible shutdown state still receives a
            # monotonic revision even though no listener can replay the edge.
            snapshot, changed = self._advance_task_lifecycle_locked(before)
        if changed:
            self._notify_task_snapshot(snapshot)

    def _on_shutdown_requested(
        self,
        mgr: "ShutdownManager",
        owner_id: str,
    ) -> None:
        """Publish a fenced same-owner shutdown lifecycle edge exactly once."""

        with self._lock:
            if (
                self._shutdown_mgr is not mgr
                or self._shutdown_owner_id != owner_id
                or self._runtime_owner_id != owner_id
            ):
                return
            # ShutdownManager invokes listeners only on its first request.
            # The manager state has already changed, so increment directly
            # rather than comparing two post-edge snapshots.
            self._task_lifecycle_revision += 1
            snapshot = self._task_snapshot_locked()
        self._notify_task_snapshot(snapshot)

    def request_shutdown(self, *, owner_id: str | None = None) -> bool:
        with self._lock:
            if owner_id is not None and self._runtime_owner_id != owner_id:
                return False
            mgr = self._shutdown_mgr
        if mgr:
            return bool(mgr.request_shutdown())
        return False

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


async def run_evolution_task(coro, *, owner_id: str | None = None, restart_factory=None):
    """Run the single owned evolution coroutine and clear its running flag.

    The orchestrator has several legitimate early-return paths (for example a
    rejected cost policy).  Keeping this ownership cleanup outside the
    orchestrator makes both lifespan startup and the explicit control route
    publish the same stopped state when any of those paths completes.

    P2 (2026-10-05): with a ``restart_factory`` (both production call sites
    pass one), a CRASH outcome — exactly ``orchestrator_loop``'s crash-branch
    sentinel ``terminal_outcome == -1.0`` — re-enters the loop through the
    factory after a bounded backoff, subject to a sliding-window rate limit
    (default 5 restarts / 30min). Every other terminal outcome (operator
    stop, cost policy, manual pause, recovery blocked, LLM-availability
    stop), cancellation, owner drift, or an in-flight shutdown stays stopped,
    and ownership cleanup runs exactly once at the true end.
    """

    owner_task = asyncio.current_task()
    captured_owner_id = owner_id or app_state.runtime_owner_id()
    try:
        result = await coro
        if restart_factory is not None and _is_crash_outcome(result):
            result = await _supervise_orchestrator_crash_revival(
                result,
                owner_task=owner_task,
                captured_owner_id=captured_owner_id,
                restart_factory=restart_factory,
            )
        return result
    finally:
        try:
            from llm_query import set_shutdown_manager

            set_shutdown_manager(None, owner_id=captured_owner_id)
        finally:
            app_state.clear_task_if(
                owner_task,
                owner_id=captured_owner_id,
            )


async def _supervise_orchestrator_crash_revival(
    result,
    *,
    owner_task,
    captured_owner_id: str | None,
    restart_factory,
):
    """Re-enter the orchestrator loop after crash outcomes, rate-limited."""

    initial_backoff = _restart_initial_backoff_seconds()
    max_backoff = max(initial_backoff, _restart_max_backoff_seconds())
    burst_limit = _restart_max_burst()
    window_seconds = _restart_window_seconds()
    stable_run_seconds = _restart_stable_run_seconds()

    backoff_s = initial_backoff
    restarts: list[float] = []
    attempt = 0

    def _last_error() -> str:
        note = app_state.last_orchestrator_crash() or {}
        return str(note.get("error") or "")

    def _still_owned() -> bool:
        if app_state.runtime_owner_id() != captured_owner_id:
            return False
        return not app_state.shutdown_requested()

    while True:
        now = time.monotonic()
        restarts = [stamp for stamp in restarts if now - stamp < window_seconds]
        if not _still_owned():
            # Review follow-up: owner drift / shutdown is a fencing outcome,
            # not a restart-storm stop — emit its own non-terminal status so
            # the rate-limited operator-action event is never misleading.
            _emit_orchestrator_restart_event(
                status="owner_lost_no_restart",
                attempt=attempt,
                backoff_s=0.0,
                last_error=_last_error(),
                stage=_active_pipeline_stage_label(),
                owner_id=captured_owner_id,
            )
            return result
        if len(restarts) >= burst_limit:
            _emit_orchestrator_restart_event(
                status="restart_rate_limited",
                attempt=attempt,
                backoff_s=0.0,
                last_error=_last_error(),
                stage=_active_pipeline_stage_label(),
                owner_id=captured_owner_id,
                operator_action_required=True,
            )
            return result

        attempt += 1
        _emit_orchestrator_restart_event(
            status="scheduled",
            attempt=attempt,
            backoff_s=backoff_s,
            last_error=_last_error(),
            stage=_active_pipeline_stage_label(),
            owner_id=captured_owner_id,
        )
        restarts.append(now)
        await _restart_backoff_sleep(backoff_s)
        if not _still_owned():
            return result

        # The crashed loop's finally cleared the running flag while this
        # supervisor task stayed alive; re-arm it so health/UI track the
        # revived loop (guarded by the ownership check above).
        try:
            app_state.set_running(True)
        except Exception:
            pass

        loop_started = time.monotonic()
        result = await restart_factory()
        if not _is_crash_outcome(result):
            return result
        if time.monotonic() - loop_started >= stable_run_seconds:
            # Stable run before the crash: counters and backoff reset.
            backoff_s = initial_backoff
            restarts = []
        else:
            backoff_s = min(backoff_s * 2, max_backoff)
