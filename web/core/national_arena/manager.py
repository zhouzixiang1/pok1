"""Long-lived national Web Arena manager.

Arena matches are local diagnostics and presentation artifacts.  They never
issue official certificates and never write ratings; official compliance is
owned exclusively by the Windows EXE certification pipeline.
"""

from __future__ import annotations

import asyncio
import copy
import contextlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid
from typing import Any, Awaitable, Callable, TextIO, TypeVar

from bot_namespace import (
    FIRST_STRICT_POLICY_VERSION,
    ROLE_RATING_POOL,
    bot_name,
    parse_bot_version,
    resolve_national_bot_spec,
    version_sort_key,
)
from evolution_infra import get_published_active_bots_read_only
from national_arena.models import (
    ACTIVE_ARENA_STATES,
    ARENA_SCHEMA_VERSION,
    ArenaSession,
    utc_now,
)
from national_arena.storage import ArenaStore
from national_arena.sandbox import (
    ArenaSandboxError,
    ArenaSandboxUnavailable,
    SandboxCapability,
    launch_sandboxed_bot,
    remove_sealed_artifacts,
    require_managed_sandbox,
    seal_bot_artifact,
)
from managed_bot_executor import EndpointLease
from national_game_runtime import NationalTCPGameEngine
from national_native import check_native_contract, resolve_bot
from national_runtime_authority import current_system_native_runtime_errors
from sever.server.transport import NationalProtocolError, NationalTCPClient
from runtime_capacity import RuntimeCapacityLease, try_acquire_match_slots
from official_platform_resource import (
    OfficialPlatformLease,
    try_acquire_official_platform,
)

# The official_platform_harness module was removed with the EXE certification
# system (Phases 3-4).  Only the default arena TCP port and the cross-process
# lock path are still needed here, so they are defined locally.
DEFAULT_PORT = 10001


class OfficialPlatformConfig:
    """Minimal config retained for the arena port-fence lease.

    The full EXE harness config (exe_path, wineprefix, timeouts, ...) is gone
    with the certification system; only ``lock_path`` is consumed by the arena
    resource lease.
    """

    def __init__(self) -> None:
        import os
        from pathlib import Path
        self.lock_path = Path(os.environ.get(
            "POK_OFFICIAL_LOCK_PATH",
            "/tmp/pok_official_platform.lock",
        ))


class ArenaError(RuntimeError):
    pass


class ArenaNotFound(ArenaError):
    pass


class ArenaConflict(ArenaError):
    pass


class ArenaInfrastructureError(ArenaError):
    pass


StorageResultT = TypeVar("StorageResultT")
RUNTIME_SHUTDOWN_TIMEOUT_SEC = 30.0
DEFAULT_CAPACITY_WAIT_SECONDS = 30.0
CHILD_SHUTDOWN_TIMEOUT_SEC = 5.0


@dataclass
class _ManagedProcess:
    seat: str
    label: str
    process: subprocess.Popen
    pgid: int
    stdout_handle: TextIO
    stderr_handle: TextIO


@dataclass(frozen=True)
class _CleanupOutcome:
    clean: bool
    pending: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class _ArenaRuntime:
    session_id: str
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    connection_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    event_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    clients: list[NationalTCPClient] = field(default_factory=list)
    clients_by_seat: dict[str, NationalTCPClient] = field(default_factory=dict)
    peers: list[str] = field(default_factory=list)
    peers_by_seat: dict[str, str] = field(default_factory=dict)
    processes: list[_ManagedProcess] = field(default_factory=list)
    wire_queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=32_768)
    )
    wire_writer_task: asyncio.Task | None = None
    wire_executor: ThreadPoolExecutor | None = None
    wire_sequence: int = 0
    wire_dropped: int = 0
    wire_writer_error: str | None = None
    launch_labels: list[str] = field(default_factory=list)
    server: asyncio.AbstractServer | None = None
    servers: dict[str, asyncio.AbstractServer] = field(default_factory=dict)
    engine: Any = None
    task: asyncio.Task | None = None
    child_tasks: set[asyncio.Task] = field(default_factory=set)
    cleanup_future: asyncio.Future[_CleanupOutcome] | None = None
    stop_request_task: asyncio.Task | None = None
    startup_error: str | None = None
    stop_requested: bool = False
    sandbox_capability: SandboxCapability | None = None
    sandbox_root: Path | None = None
    capacity_lease: RuntimeCapacityLease | None = None
    official_platform_lease: OfficialPlatformLease | None = None


class NationalArenaManager:
    def __init__(
        self,
        store: ArenaStore | None = None,
        *,
        epoch_authority: dict[str, Any] | None = None,
    ) -> None:
        self.store = store or ArenaStore()
        self._sessions: dict[str, ArenaSession] = {}
        self._runtimes: dict[str, _ArenaRuntime] = {}
        self._conditions: dict[str, asyncio.Condition] = {}
        self._event_locks: dict[str, asyncio.Lock] = {}
        self._manager_lock = asyncio.Lock()
        self._started = False
        self._storage_executor: ThreadPoolExecutor | None = None
        self._bot_catalog_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])
        self._unstarted_stop_tasks: dict[str, asyncio.Task] = {}
        self._epoch_binding = (
            self.build_epoch_binding(epoch_authority)
            if epoch_authority is not None
            else None
        )

    @staticmethod
    def build_epoch_binding(authority: dict[str, Any]) -> dict[str, Any]:
        """Compile a stable identity for the strict epoch authority root.

        The reset receipt is the preferred root.  A clean clone may instead be
        initialized solely by an eligible strict publication, in which case
        the immutable strict namespace root is used.  Workflow ID is retained
        as diagnostic provenance but deliberately excluded from the
        epoch-root digest so historical sessions remain visible across normal
        generation transitions in the same strict epoch.
        """

        if not isinstance(authority, dict) or not authority.get("initialized"):
            raise ArenaConflict("national Arena requires an initialized policy epoch")
        evaluation_epoch = str(authority.get("evaluation_epoch") or "")
        if not evaluation_epoch:
            raise ArenaConflict("national Arena epoch authority is missing evaluation_epoch")

        reset_digest = str(authority.get("reset_receipt_digest") or "")
        if reset_digest and not re.fullmatch(r"[0-9a-f]{64}", reset_digest):
            raise ArenaConflict(
                "national Arena reset receipt identity is malformed"
            )
        strict_bots = sorted(
            {
                str(name)
                for name in (authority.get("strict_published_bots") or [])
                if str(name)
            },
            key=version_sort_key,
        )
        if reset_digest:
            root_kind = "policy_epoch_reset_receipt"
            root_value = reset_digest
        elif strict_bots:
            # The eligible publication set can be pruned as the active pool
            # advances.  Bind to the immutable strict epoch namespace rather
            # than whichever eligible directory happens to be oldest today.
            root_kind = "strict_publication_epoch_root"
            root_value = (
                f"{evaluation_epoch}:{bot_name(FIRST_STRICT_POLICY_VERSION)}"
            )
        else:
            raise ArenaConflict("national Arena epoch authority has no durable root")

        identity_payload = {
            "evaluation_epoch": evaluation_epoch,
            "root_kind": root_kind,
            "root_value": root_value,
        }
        identity = hashlib.sha256(
            json.dumps(
                identity_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        active_generation = authority.get("active_generation") or {}
        workflow_run_id = (
            str(active_generation.get("workflow_run_id"))
            if isinstance(active_generation, dict)
            and active_generation.get("workflow_run_id")
            else None
        )
        return {
            **identity_payload,
            "epoch_authority_identity": identity,
            "reset_receipt_digest": reset_digest or None,
            "epoch_authority_state": str(authority.get("state") or ""),
            "workflow_run_id": workflow_run_id,
        }

    @classmethod
    def session_epoch_fields(cls, authority: dict[str, Any]) -> dict[str, Any]:
        binding = cls.build_epoch_binding(authority)
        return {
            "evaluation_epoch": binding["evaluation_epoch"],
            "epoch_authority_identity": binding["epoch_authority_identity"],
            "epoch_reset_receipt_digest": binding["reset_receipt_digest"],
            "epoch_authority_state": binding["epoch_authority_state"],
            "workflow_run_id": binding["workflow_run_id"],
        }

    def accepts_epoch_authority(self, authority: dict[str, Any]) -> bool:
        if self._epoch_binding is None:
            return False
        try:
            current = self.build_epoch_binding(authority)
        except ArenaError:
            return False
        return (
            current["epoch_authority_identity"]
            == self._epoch_binding["epoch_authority_identity"]
            and current["reset_receipt_digest"]
            == self._epoch_binding["reset_receipt_digest"]
        )

    @property
    def started(self) -> bool:
        return self._started

    def _session_matches_epoch(self, session: ArenaSession) -> bool:
        binding = self._epoch_binding
        return bool(
            binding
            and session.schema_version == ARENA_SCHEMA_VERSION
            and session.evaluation_epoch == binding["evaluation_epoch"]
            and session.epoch_authority_identity
            == binding["epoch_authority_identity"]
            and session.epoch_reset_receipt_digest
            == binding["reset_receipt_digest"]
        )

    async def startup(
        self,
        *,
        epoch_authority: dict[str, Any] | None = None,
    ) -> None:
        if self._started:
            return
        if epoch_authority is None:
            from epoch_authority import require_policy_epoch_initialized

            epoch_authority = require_policy_epoch_initialized(
                "national_arena.startup"
            )
        binding = self.build_epoch_binding(epoch_authority)
        if self._epoch_binding is not None and (
            self._epoch_binding["epoch_authority_identity"]
            != binding["epoch_authority_identity"]
            or self._epoch_binding["reset_receipt_digest"]
            != binding["reset_receipt_digest"]
        ):
            raise ArenaConflict("national Arena manager epoch authority changed")
        self._epoch_binding = binding
        try:
            self.store.acquire_owner()
        except RuntimeError as exc:
            raise ArenaConflict(str(exc)) from exc
        self._storage_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="arena-storage",
        )
        try:
            discovered = await self._run_storage(self.store.list_sessions)
            # Discovery itself is read-only.  Only sessions with an exact
            # strict epoch-root binding may proceed to event locks, high-water
            # recovery, process cleanup, or any other per-session write.
            sessions = [
                session for session in discovered
                if self._session_matches_epoch(session)
            ]
            self._sessions = {session.session_id: session for session in sessions}
            self._started = True
            for session in sessions:
                session.last_event_id = max(
                    session.last_event_id,
                    await self._run_storage(
                        self.store.event_high_watermark,
                        session.session_id,
                    ),
                )
                self._conditions.setdefault(session.session_id, asyncio.Condition())
                self._event_locks.setdefault(session.session_id, asyncio.Lock())
                if session.status in ACTIVE_ARENA_STATES or session.managed_processes:
                    previous_status = session.status
                    process_cleanup = await self._reap_persisted_processes(session)
                    residual_cleanup: list[str] = []
                    sandbox_root = self.store.session_dir(session.session_id) / "sandbox"
                    if not session.managed_processes and (
                        sandbox_root.exists() or sandbox_root.is_symlink()
                    ):
                        try:
                            remove_sealed_artifacts(sandbox_root)
                            process_cleanup.append({
                                "resource": "sandbox_tree",
                                "action": "removed",
                            })
                        except Exception as exc:
                            residual_cleanup.append(
                                f"sandbox_tree:{type(exc).__name__}:{str(exc)[:160]}"
                            )
                    if session.managed_processes or residual_cleanup:
                        session.status = "quarantined"
                        session.finished_at = None
                        session.cleanup_completed = False
                        session.resource_fence_held = True
                        session.quarantine_reason = (
                            "persisted runtime resources could not be reaped"
                        )
                        session.failure_reason = session.quarantine_reason
                        await self._emit(
                            session,
                            "session_quarantined",
                            {
                                "reason": session.quarantine_reason,
                                "previous_status": previous_status,
                                "managed_process_cleanup": process_cleanup,
                                "pending_resources": residual_cleanup,
                                "resource_fence_held": True,
                            },
                        )
                    else:
                        session.status = "failed"
                        session.finished_at = utc_now()
                        session.cleanup_completed = True
                        session.resource_fence_held = False
                        session.failure_reason = (
                            "web_process_restarted"
                            if previous_status in ACTIVE_ARENA_STATES
                            else "terminal_session_had_unfinished_process_cleanup"
                        )
                        await self._emit(
                            session,
                            "session_failed",
                            {
                                "reason": session.failure_reason,
                                "previous_status": previous_status,
                                "managed_process_cleanup": process_cleanup,
                                "cleanup_completed": True,
                            },
                        )
        except BaseException:
            self._started = False
            self._sessions.clear()
            self._conditions.clear()
            self._event_locks.clear()
            executor, self._storage_executor = self._storage_executor, None
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            self.store.release_owner()
            raise

    async def shutdown(self) -> None:
        try:
            for session_id in list(self._runtimes):
                with contextlib.suppress(ArenaError):
                    await self.stop_session(session_id, reason="web_shutdown")
        finally:
            self._started = False
            self.store.release_owner()
            executor, self._storage_executor = self._storage_executor, None
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

    def _require_started(self) -> None:
        if not self._started:
            raise ArenaError("arena manager is not started")

    async def _run_storage(
        self,
        function: Callable[..., StorageResultT],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> StorageResultT:
        executor = self._storage_executor
        if executor is None:
            raise ArenaError("arena storage executor is not running")
        invocation = partial(function, *args, **kwargs)
        concurrent = executor.submit(invocation)
        try:
            # Polling avoids coupling correctness to the event loop's
            # cross-thread wakeup pipe, which can be delayed during shutdown.
            while not concurrent.done():
                await asyncio.sleep(0.001)
        except asyncio.CancelledError:
            concurrent.cancel()
            raise
        return concurrent.result()

    async def create_session(
        self,
        *,
        mode: str,
        host: str = "127.0.0.1",
        port: int | None = None,
        hands: int = 70,
        action_timeout_seconds: float = 60.0,
        official_action_delay: float = 0.30,
        capacity_wait_seconds: float = DEFAULT_CAPACITY_WAIT_SECONDS,
        top_bot: str | None = None,
        bottom_bot: str | None = None,
    ) -> dict[str, Any]:
        self._require_started()
        if mode not in {"external_tcp", "managed_bots"}:
            raise ArenaError("mode must be external_tcp or managed_bots")
        try:
            host_address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ArenaError("host must be an IP address") from exc
        if mode == "managed_bots":
            if not host_address.is_loopback:
                raise ArenaError("managed_bots host must be loopback")
            host = "127.0.0.1"
        selected_port = (
            0 if mode == "managed_bots" else DEFAULT_PORT
        ) if port is None else int(port)
        if not 0 <= selected_port <= 65_535:
            raise ArenaError("port must be between 0 and 65535")
        if not 1 <= int(hands) <= 70:
            raise ArenaError("hands must be between 1 and 70")
        if not 0.05 <= float(action_timeout_seconds) <= 60.0:
            raise ArenaError("action timeout must be between 0.05 and 60 seconds")
        if not 0.0 <= float(official_action_delay) <= 5.0:
            raise ArenaError("action delay must be between 0 and 5 seconds")
        if not 0.05 <= float(capacity_wait_seconds) <= 300.0:
            raise ArenaError("capacity wait must be between 0.05 and 300 seconds")

        certification: dict[str, Any] = {}
        if mode == "managed_bots":
            if not top_bot or not bottom_bot:
                raise ArenaError("managed_bots requires top_bot and bottom_bot")
            launchable = {row["id"]: row for row in self.list_launchable_bots()}
            missing = [name for name in (top_bot, bottom_bot) if name not in launchable]
            if missing:
                raise ArenaError(
                    "managed bot is not active/native/official-eligible: "
                    + ", ".join(missing)
                )
            certification = {
                "top": launchable[top_bot]["certification"],
                "bottom": launchable[bottom_bot]["certification"],
                "note": "display_only_snapshot; EXE certification remains authoritative",
            }
            managed_bot_identities = {
                "top": launchable[top_bot]["artifact_identity"],
                "bottom": launchable[bottom_bot]["artifact_identity"],
            }
        else:
            managed_bot_identities = {}

        session_id = f"arena_{time.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
        session = ArenaSession(
            session_id=session_id,
            mode=mode,
            host=host,
            port=selected_port,
            requested_port=selected_port,
            hands_total=int(hands),
            action_timeout_seconds=float(action_timeout_seconds),
            official_action_delay=float(official_action_delay),
            capacity_wait_seconds=float(capacity_wait_seconds),
            top_bot=top_bot,
            bottom_bot=bottom_bot,
            official_certification=certification,
            managed_bot_identities=managed_bot_identities,
            evaluation_epoch=str(self._epoch_binding["evaluation_epoch"]),
            epoch_authority_identity=str(
                self._epoch_binding["epoch_authority_identity"]
            ),
            epoch_reset_receipt_digest=self._epoch_binding[
                "reset_receipt_digest"
            ],
            epoch_authority_state=str(
                self._epoch_binding["epoch_authority_state"]
            ),
            workflow_run_id=self._epoch_binding["workflow_run_id"],
        )
        async with self._manager_lock:
            await self._run_storage(self.store.create_session, session)
            self._sessions[session_id] = session
            self._conditions[session_id] = asyncio.Condition()
            self._event_locks[session_id] = asyncio.Lock()
        await self._emit(session, "session_created", {
            "mode": mode,
            "result_authority": session.result_authority,
            "official_exe_certification": False,
        })
        return session.to_dict()

    def list_sessions(self) -> list[dict[str, Any]]:
        self._require_started()
        return [
            session.to_dict()
            for session in sorted(
                self._sessions.values(),
                key=lambda item: item.created_at,
                reverse=True,
            )
        ]

    def get_session(self, session_id: str) -> dict[str, Any]:
        self._require_started()
        session = self._sessions.get(session_id)
        if session is None:
            raise ArenaNotFound(f"arena session not found: {session_id}")
        return session.to_dict()

    async def start_session(self, session_id: str) -> dict[str, Any]:
        self._require_started()
        initial = self._sessions.get(session_id)
        if initial is None:
            raise ArenaNotFound(f"arena session not found: {session_id}")
        active_fence = next(
            (
                item.session_id
                for item in self._sessions.values()
                if item.session_id != session_id and item.active
            ),
            None,
        )
        if active_fence is not None:
            raise ArenaConflict(f"another arena session is active: {active_fence}")
        sandbox_capability: SandboxCapability | None = None
        if initial.mode == "managed_bots":
            if initial.status != "created":
                raise ArenaConflict(
                    f"session cannot start from status {initial.status}"
                )
            self._revalidate_managed_bot_identities(initial)
            try:
                sandbox_capability = await self._run_storage(
                    require_managed_sandbox
                )
            except ArenaSandboxUnavailable as exc:
                raise ArenaInfrastructureError(str(exc)) from exc
        async with self._manager_lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ArenaNotFound(f"arena session not found: {session_id}")
            if session.status != "created":
                raise ArenaConflict(f"session cannot start from status {session.status}")
            if session.mode == "managed_bots":
                self._revalidate_managed_bot_identities(session)
                if sandbox_capability is None:
                    raise ArenaInfrastructureError(
                        "arena_sandbox_capability_missing; managed execution has no fallback"
                    )
            active = [
                item.session_id
                for item in self._sessions.values()
                if item.session_id != session_id and item.active
            ]
            if active:
                raise ArenaConflict(f"another arena session is active: {active[0]}")
            session.status = "starting"
            await self._emit(session, "session_starting", {
                "mode": session.mode,
                "host": session.host,
                "port": session.requested_port,
            })
            runtime = _ArenaRuntime(session_id=session_id)
            runtime.condition = self._conditions[session_id]
            runtime.event_lock = self._event_locks[session_id]
            runtime.cleanup_future = asyncio.get_running_loop().create_future()
            runtime.sandbox_capability = sandbox_capability
            self._runtimes[session_id] = runtime
            runtime.task = asyncio.create_task(
                self._run_session(session, runtime),
                name=f"national-arena-{session_id}",
            )
        if session.mode == "managed_bots":
            return session.to_dict()
        try:
            await asyncio.wait_for(runtime.ready.wait(), timeout=10.0)
        except asyncio.TimeoutError as exc:
            await self.stop_session(session_id, reason="listener_start_timeout")
            raise ArenaError("arena listener did not become ready") from exc
        if runtime.startup_error:
            if runtime.task is not None and not runtime.task.done():
                await asyncio.shield(runtime.task)
            raise ArenaError(runtime.startup_error)
        return session.to_dict()

    async def stop_session(self, session_id: str, *, reason: str = "operator_stop") -> dict[str, Any]:
        self._require_started()
        async with self._manager_lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ArenaNotFound(f"arena session not found: {session_id}")
            if session.terminal or session.status == "quarantined":
                return session.to_dict()
            runtime = self._runtimes.get(session_id)
            if runtime is None:
                stop_task = self._unstarted_stop_tasks.get(session_id)
                if stop_task is None:
                    stop_task = asyncio.create_task(
                        self._stop_unstarted_session(session, reason),
                        name=f"national-arena-stop-{session_id}",
                    )
                    self._unstarted_stop_tasks[session_id] = stop_task
                cleanup_waiter: asyncio.Future | asyncio.Task = stop_task
            else:
                if runtime.cleanup_future is None:
                    raise ArenaError("arena runtime cleanup future is not initialized")
                runtime_task_done = bool(runtime.task is None or runtime.task.done())
                if (
                    runtime.stop_request_task is None
                    and session.status != "finalizing"
                    and not runtime_task_done
                ):
                    runtime.stop_requested = True
                    runtime.stop_request_task = asyncio.create_task(
                        self._request_runtime_stop(session, runtime, reason),
                        name=f"national-arena-stop-request-{session_id}",
                    )
                cleanup_waiter = runtime.cleanup_future
        try:
            await asyncio.wait_for(
                asyncio.shield(cleanup_waiter),
                timeout=RUNTIME_SHUTDOWN_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError as exc:
            raise ArenaError(
                f"arena runtime cleanup exceeded {RUNTIME_SHUTDOWN_TIMEOUT_SEC:.0f}s"
            ) from exc
        return session.to_dict()

    async def _stop_unstarted_session(
        self,
        session: ArenaSession,
        reason: str,
    ) -> None:
        try:
            session.status = "stopping"
            await self._emit(session, "session_stopping", {"reason": reason})
            session.status = "stopped"
            session.finished_at = utc_now()
            session.cleanup_completed = True
            session.resource_fence_held = False
            await self._emit(session, "session_stopped", {
                "reason": reason,
                "cleanup_completed": True,
            })
        finally:
            self._unstarted_stop_tasks.pop(session.session_id, None)

    async def _request_runtime_stop(
        self,
        session: ArenaSession,
        runtime: _ArenaRuntime,
        reason: str,
    ) -> None:
        if session.status not in {"finalizing", "quarantined"} and not session.terminal:
            session.status = "stopping"
            try:
                await self._emit(session, "session_stopping", {"reason": reason})
            except Exception as exc:
                runtime.startup_error = (
                    f"stop_event_persistence_failed:{type(exc).__name__}:{str(exc)[:160]}"
                )
        task = runtime.task
        if (
            session.status == "stopping"
            and task is not None
            and task is not asyncio.current_task()
            and not task.done()
        ):
            task.cancel()

    def list_launchable_bots(self, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        """Return only evolution-active bots already eligible outside Arena."""

        cached_at, cached_rows = self._bot_catalog_cache
        if not force_refresh and cached_rows and time.monotonic() - cached_at < 15.0:
            return copy.deepcopy(cached_rows)
        rows: list[dict[str, Any]] = []
        for name in get_published_active_bots_read_only():
            try:
                label, path = resolve_bot(name)
                errors = [
                    *current_system_native_runtime_errors(path),
                    *check_native_contract(path),
                ]
            except Exception:
                continue
            if errors:
                continue
            certification = self._certification_snapshot(path)
            if not certification.get("arena_launch_eligible"):
                continue
            rows.append({
                "id": label,
                "version": parse_bot_version(label),
                "display_name": label,
                "launchable": True,
                "native_contract": "passed",
                "certification": certification,
                "artifact_identity": certification["artifact_identity"],
                "result_authority": "diagnostic_only",
                "selection_authority": "official_windows_exe",
            })
        rows = sorted(rows, key=lambda row: version_sort_key(row["id"]), reverse=True)
        self._bot_catalog_cache = (time.monotonic(), copy.deepcopy(rows))
        return rows

    @staticmethod
    def _certification_snapshot(path: Path) -> dict[str, Any]:
        # The official EXE certification system was removed (Phases 3-4).  Arena
        # launch eligibility is now derived from the published native policy
        # artifact identity (resolve_national_bot_spec) rather than from a
        # signed certificate.  The field names are retained for API stability.
        try:
            spec = resolve_national_bot_spec(
                path,
                ROLE_RATING_POOL,
                repo_root=Path(__file__).resolve().parents[3],
            )
            if not spec.eligible:
                raise ArenaError(
                    "bot is not a strict published policy artifact: "
                    + ", ".join(spec.issues or ["ineligible"])
                )
            artifact = spec.publication_identity
            artifact_identity = {
                key: artifact.get(key)
                for key in (
                    "label",
                    "artifact_hash",
                    "tag",
                    "tag_object",
                    "commit_oid",
                    "current_tree_oid",
                )
            }
            return {
                "status": "native",
                "mode": "native",
                "official_full_certified": True,
                "official_exe_passed": True,
                "arena_launch_eligible": True,
                "eligibility_basis": "native_published",
                "authority": "native",
                "artifact_identity": artifact_identity,
            }
        except Exception as exc:
            return {
                "status": "unavailable",
                "official_full_certified": False,
                "official_exe_passed": False,
                "arena_launch_eligible": False,
                "eligibility_basis": "ineligible",
                "authority": "native",
                "artifact_identity": {},
                "error": f"{type(exc).__name__}: {str(exc)[:160]}",
            }

    def _revalidate_managed_bot_identities(self, session: ArenaSession) -> None:
        launchable = {
            row["id"]: row
            for row in self.list_launchable_bots(force_refresh=True)
        }
        for seat, bot_name in (("top", session.top_bot), ("bottom", session.bottom_bot)):
            row = launchable.get(str(bot_name))
            if row is None:
                raise ArenaConflict(
                    f"managed bot is no longer active/native/official-eligible: {bot_name}"
                )
            expected = session.managed_bot_identities.get(seat) or {}
            current = row.get("artifact_identity") or {}
            if not expected or current != expected:
                raise ArenaConflict(
                    f"managed bot publication identity changed after session creation: {bot_name}"
                )

    async def read_events(
        self,
        session_id: str,
        *,
        after_event_id: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        if session_id not in self._sessions:
            raise ArenaNotFound(f"arena session not found: {session_id}")
        return await self._run_storage(
            self.store.read_events,
            session_id,
            after_event_id=after_event_id,
            limit=limit,
        )

    async def wait_for_events(
        self,
        session_id: str,
        *,
        after_event_id: int,
        timeout: float = 15.0,
    ) -> list[dict[str, Any]]:
        condition = self._conditions.get(session_id)
        if condition is None:
            raise ArenaNotFound(f"arena session not found: {session_id}")
        try:
            async with condition:
                rows = await self.read_events(
                    session_id,
                    after_event_id=after_event_id,
                )
                if rows:
                    return rows
                await asyncio.wait_for(condition.wait(), timeout=max(0.1, timeout))
        except asyncio.TimeoutError:
            return []
        return await self.read_events(session_id, after_event_id=after_event_id)

    def artifact_path(self, session_id: str, artifact_key: str) -> Path:
        session = self._sessions.get(session_id)
        if session is None:
            raise ArenaNotFound(f"arena session not found: {session_id}")
        filename = session.artifacts.get(artifact_key)
        if not filename:
            raise ArenaNotFound(f"arena artifact not available: {artifact_key}")
        path = self.store.artifact_path(session_id, filename)
        if not path.is_file():
            raise ArenaNotFound(f"arena artifact missing: {artifact_key}")
        return path

    async def _run_session(self, session: ArenaSession, runtime: _ArenaRuntime) -> None:
        terminal_status = "failed"
        terminal_event = "session_failed"
        terminal_payload: dict[str, Any] = {"reason": "arena_session_failed"}
        try:
            if session.mode == "managed_bots":
                await self._emit(session, "runtime_capacity_waiting", {
                    "required_match_slots": 2,
                    "timeout_seconds": session.capacity_wait_seconds,
                })
                runtime.capacity_lease = await self._acquire_capacity(session)
                await self._emit(session, "runtime_capacity_acquired", {
                    "match_slots": runtime.capacity_lease.slots,
                })
            if int(session.requested_port or 0) == DEFAULT_PORT:
                self._claim_official_platform_resource(session, runtime)
                await self._emit(session, "official_platform_resource_acquired", {
                    "port": session.requested_port,
                    "purpose": "local_arena_diagnostic",
                    "official_certification": False,
                })
            session.artifacts["wire"] = "wire.jsonl"
            runtime.wire_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"arena-wire-{session.session_id[-8:]}",
            )
            runtime.wire_writer_task = self._create_child_task(
                runtime,
                self._wire_writer(session, runtime),
                name=f"national-arena-wire-{session.session_id}",
            )
            await self._open_listener(session, runtime)
            if session.mode == "managed_bots":
                await self._launch_managed_bots(session, runtime)
                runtime.ready.set()
                await self._wait_for_managed_connections(session, runtime)
            else:
                runtime.ready.set()
                await runtime.connected.wait()
            listener_pending = await self._close_servers(runtime)
            if listener_pending:
                raise ArenaError(
                    "arena listeners did not close: " + ", ".join(listener_pending)
                )
            ordered_clients, names = await self._handshake(session, runtime)
            session.top_player_name, session.bottom_player_name = names
            session.status = "ready"
            await self._emit(session, "players_ready", {"names": names})
            session.status = "running"
            session.started_at = utc_now()
            await self._emit(session, "match_started", {
                "hands_total": session.hands_total,
                "names": names,
                "result_authority": "diagnostic_only",
                "official_exe_certification": False,
            })

            events: list[dict[str, Any]] = []
            engine = NationalTCPGameEngine(
                ordered_clients,
                events,
                action_timeout_sec=session.action_timeout_seconds,
                event_sink=lambda event: self._on_engine_event(session, event),
            )
            runtime.engine = engine
            match_task = self._create_child_task(
                runtime,
                engine.run_limited_match(names[0], names[1], session.hands_total),
                name=f"national-arena-match-{session.session_id}",
            )
            if runtime.processes:
                monitor = self._create_child_task(
                    runtime,
                    self._wait_for_process_exit(runtime),
                    name=f"national-arena-process-monitor-{session.session_id}",
                )
                done, _pending = await asyncio.wait(
                    {match_task, monitor},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if match_task in done:
                    await match_task
                    runtime.child_tasks.discard(match_task)
                    pending = await self._cancel_and_await_tasks(runtime, {monitor})
                    if pending:
                        raise ArenaError(
                            "managed process monitor did not stop: " + ", ".join(pending)
                        )
                else:
                    seat, returncode = monitor.result()
                    runtime.child_tasks.discard(monitor)
                    await self._cancel_and_await_tasks(runtime, {match_task})
                    raise ArenaError(
                        f"managed bot exited before match completion: {seat} rc={returncode}"
                    )
            else:
                await match_task
                runtime.child_tasks.discard(match_task)

            await self._export_thp(session, engine, partial=False)
            if session.top_total_earnings > session.bottom_total_earnings:
                session.winner = session.top_player_name
            elif session.bottom_total_earnings > session.top_total_earnings:
                session.winner = session.bottom_player_name
            else:
                session.winner = "tie"
            terminal_status = "finished"
            terminal_event = "match_finished"
            terminal_payload = {
                "hands_completed": session.hands_completed,
                "total_earnings": [
                    session.top_total_earnings,
                    session.bottom_total_earnings,
                ],
                "winner": session.winner,
                "thp_artifact": "thp",
                "official_exe_certification": False,
            }
        except asyncio.CancelledError:
            if runtime.stop_requested:
                terminal_status = "stopped"
                terminal_event = "session_stopped"
                terminal_payload = {"reason": "operator_or_shutdown"}
            else:
                terminal_payload = {"reason": "arena_task_cancelled"}
        except ArenaInfrastructureError as exc:
            runtime.startup_error = f"infrastructure_failure: {str(exc)[:360]}"
            terminal_payload = {"reason": runtime.startup_error}
        except ArenaSandboxError as exc:
            runtime.startup_error = f"infrastructure_failure: {str(exc)[:360]}"
            terminal_payload = {"reason": runtime.startup_error}
        except Exception as exc:
            runtime.startup_error = f"{type(exc).__name__}: {str(exc)[:400]}"
            terminal_payload = {"reason": runtime.startup_error}
        finally:
            runtime.ready.set()
            session.status = "finalizing"
            session.finished_at = None
            finalizing_error = ""
            try:
                await self._emit(session, "session_finalizing", {
                    "target_status": terminal_status,
                    "pending_managed_processes": len(session.managed_processes),
                })
            except Exception as exc:
                finalizing_error = f"event_flush:{type(exc).__name__}:{str(exc)[:200]}"
            try:
                cleanup_outcome = await self._cleanup_runtime(session, runtime)
            except BaseException as exc:
                cleanup_outcome = _CleanupOutcome(
                    clean=False,
                    pending=(
                        f"cleanup_exception:{type(exc).__name__}:{str(exc)[:200]}",
                    ),
                )
            if cleanup_outcome is None:
                # Test and extension hooks written against schema v1 returned
                # no value after completing cleanup.
                cleanup_outcome = _CleanupOutcome(clean=True)
            if finalizing_error:
                terminal_status = "failed"
                terminal_event = "session_failed"
                terminal_payload = {
                    "reason": finalizing_error,
                    "original_outcome": terminal_payload,
                }
            if cleanup_outcome.clean:
                session.status = terminal_status
                session.finished_at = utc_now()
                session.cleanup_completed = True
                session.resource_fence_held = False
                session.quarantine_reason = None
                if terminal_status == "failed":
                    session.failure_reason = str(
                        terminal_payload.get("reason") or "arena_session_failed"
                    )[:500]
                terminal_payload = {
                    **terminal_payload,
                    "cleanup_completed": True,
                    "resource_fence_held": False,
                    "wire_log_complete": session.wire_log_complete,
                }
            else:
                original_outcome = terminal_payload
                quarantine_reason = (
                    "runtime cleanup did not reach zero: "
                    + ", ".join(cleanup_outcome.pending)
                )[:500]
                session.status = "quarantined"
                session.finished_at = None
                session.cleanup_completed = False
                session.resource_fence_held = True
                session.quarantine_reason = quarantine_reason
                session.failure_reason = quarantine_reason
                terminal_status = "quarantined"
                terminal_event = "session_quarantined"
                terminal_payload = {
                    "reason": quarantine_reason,
                    "pending_resources": list(cleanup_outcome.pending),
                    "cleanup_details": cleanup_outcome.details,
                    "original_outcome": original_outcome,
                    "cleanup_completed": False,
                    "resource_fence_held": True,
                    "wire_log_complete": session.wire_log_complete,
                }
            try:
                await self._emit(session, terminal_event, terminal_payload)
            except Exception as exc:
                runtime.startup_error = runtime.startup_error or (
                    f"terminal_event_persistence_failed:{type(exc).__name__}:{str(exc)[:160]}"
                )
            finally:
                if cleanup_outcome.clean:
                    self._runtimes.pop(session.session_id, None)
                cleanup_future = runtime.cleanup_future
                if cleanup_future is not None and not cleanup_future.done():
                    cleanup_future.set_result(cleanup_outcome)
            self._system_event(
                "arena.match_finished" if terminal_status == "finished" else "arena.session_terminated",
                (
                    "success"
                    if terminal_status == "finished"
                    else "error"
                    if terminal_status in {"failed", "quarantined"}
                    else "info"
                ),
                f"National Arena {session.session_id} ended as {terminal_status}",
                session,
            )

    @staticmethod
    def _create_child_task(
        runtime: _ArenaRuntime,
        awaitable: Awaitable[Any],
        *,
        name: str,
    ) -> asyncio.Task:
        task = asyncio.create_task(awaitable, name=name)
        runtime.child_tasks.add(task)
        return task

    @staticmethod
    async def _cancel_and_await_tasks(
        runtime: _ArenaRuntime,
        tasks: set[asyncio.Task] | list[asyncio.Task] | tuple[asyncio.Task, ...],
        *,
        timeout: float = CHILD_SHUTDOWN_TIMEOUT_SEC,
    ) -> tuple[str, ...]:
        current = asyncio.current_task()
        candidates = {
            task for task in tasks if task is not None and task is not current
        }
        for task in candidates:
            if not task.done():
                task.cancel()
        if not candidates:
            return ()
        done, pending = await asyncio.wait(candidates, timeout=max(0.01, timeout))
        if done:
            await asyncio.gather(*done, return_exceptions=True)
            runtime.child_tasks.difference_update(done)
        return tuple(sorted(task.get_name() for task in pending))

    async def _wait_for_managed_connections(
        self,
        session: ArenaSession,
        runtime: _ArenaRuntime,
    ) -> None:
        connection_waiter = self._create_child_task(
            runtime,
            runtime.connected.wait(),
            name=f"national-arena-connect-wait-{session.session_id}",
        )
        process_monitor = self._create_child_task(
            runtime,
            self._wait_for_process_exit(runtime),
            name=f"national-arena-startup-monitor-{session.session_id}",
        )
        done, _pending = await asyncio.wait(
            {connection_waiter, process_monitor},
            timeout=30.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if process_monitor in done:
            seat, returncode = process_monitor.result()
            runtime.child_tasks.discard(process_monitor)
            await self._cancel_and_await_tasks(runtime, {connection_waiter})
            raise ArenaInfrastructureError(
                "managed sandbox process exited before its seat endpoint connected: "
                f"{seat} rc={returncode}"
            )
        if connection_waiter in done:
            await connection_waiter
            runtime.child_tasks.discard(connection_waiter)
            pending = await self._cancel_and_await_tasks(runtime, {process_monitor})
            if pending:
                raise ArenaError(
                    "managed startup monitor did not stop: " + ", ".join(pending)
                )
            return
        await self._cancel_and_await_tasks(
            runtime,
            {connection_waiter, process_monitor},
        )
        raise ArenaInfrastructureError(
            "managed bots did not connect to both seat-bound loopback endpoints within 30s"
        )

    @staticmethod
    async def _close_servers(runtime: _ArenaRuntime) -> tuple[str, ...]:
        servers = dict(runtime.servers)
        if runtime.server is not None:
            servers.setdefault("external", runtime.server)
        for server in servers.values():
            server.close()
        pending: list[str] = []
        for label, server in servers.items():
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=2.0)
            except (asyncio.TimeoutError, OSError, RuntimeError):
                pending.append(f"listener:{label}")
        if not pending:
            runtime.servers.clear()
            runtime.server = None
        return tuple(pending)

    async def _open_listener(self, session: ArenaSession, runtime: _ArenaRuntime) -> None:
        if session.mode == "managed_bots":
            await self._open_managed_listeners(session, runtime)
            return

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            if task is not None:
                runtime.child_tasks.add(task)
            try:
                await self._accept_client(session, runtime, reader, writer, seat=None)
            finally:
                if task is not None:
                    runtime.child_tasks.discard(task)

        try:
            runtime.server = await asyncio.start_server(handle, session.host, session.port)
        except OSError as exc:
            raise ArenaError(f"cannot bind {session.host}:{session.port}: {exc}") from exc
        runtime.servers["external"] = runtime.server
        socket_info = runtime.server.sockets[0].getsockname()
        session.port = int(socket_info[1])
        session.status = "waiting_for_players"
        await self._emit(session, "server_listening", {
            "host": session.host,
            "port": session.port,
            "mode": session.mode,
            "seat_binding": "connection_order",
        })

    async def _open_managed_listeners(
        self,
        session: ArenaSession,
        runtime: _ArenaRuntime,
    ) -> None:
        if session.host != "127.0.0.1":
            raise ArenaInfrastructureError("managed Arena listener must use 127.0.0.1")

        def handler_for(seat: str):
            async def handle(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                task = asyncio.current_task()
                if task is not None:
                    runtime.child_tasks.add(task)
                try:
                    await self._accept_client(
                        session,
                        runtime,
                        reader,
                        writer,
                        seat=seat,
                    )
                finally:
                    if task is not None:
                        runtime.child_tasks.discard(task)

            return handle

        requested_top_port = int(session.requested_port or 0)
        try:
            top_server = await asyncio.start_server(
                handler_for("top"),
                "127.0.0.1",
                requested_top_port,
            )
            runtime.servers["top"] = top_server
            bottom_server = await asyncio.start_server(
                handler_for("bottom"),
                "127.0.0.1",
                0,
            )
            runtime.servers["bottom"] = bottom_server
        except OSError as exc:
            await self._close_servers(runtime)
            raise ArenaInfrastructureError(
                f"cannot bind managed loopback seat listeners: {exc}"
            ) from exc

        top_port = int(top_server.sockets[0].getsockname()[1])
        bottom_port = int(bottom_server.sockets[0].getsockname()[1])
        if top_port == bottom_port:
            await self._close_servers(runtime)
            raise ArenaInfrastructureError("managed seat listeners resolved to one endpoint")
        session.port = top_port
        session.managed_endpoints = {
            "top": {"host": "127.0.0.1", "port": top_port, "seat": "top"},
            "bottom": {
                "host": "127.0.0.1",
                "port": bottom_port,
                "seat": "bottom",
            },
        }
        session.status = "waiting_for_players"
        await self._emit(session, "server_listening", {
            "host": "127.0.0.1",
            "port": top_port,
            "mode": session.mode,
            "seat_binding": "dedicated_loopback_endpoints",
            "managed_endpoints": copy.deepcopy(session.managed_endpoints),
        })

    async def _accept_client(
        self,
        session: ArenaSession,
        runtime: _ArenaRuntime,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        seat: str | None,
    ) -> None:
        peer_info = writer.get_extra_info("peername")
        peer = str(peer_info)
        if session.mode == "managed_bots":
            try:
                peer_host = str(peer_info[0])
                peer_is_loopback = ipaddress.ip_address(peer_host).is_loopback
            except (TypeError, ValueError, IndexError):
                peer_is_loopback = False
            if not peer_is_loopback:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                await self._emit(session, "connection_rejected", {
                    "peer": peer,
                    "seat": seat,
                    "reason": "managed_connections_must_be_loopback",
                })
                return

        async with runtime.connection_lock:
            assigned_seat = seat
            if assigned_seat is None:
                assigned_seat = next(
                    (candidate for candidate in ("top", "bottom")
                     if candidate not in runtime.clients_by_seat),
                    None,
                )
            if assigned_seat is None or assigned_seat in runtime.clients_by_seat:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                await self._emit(session, "connection_rejected", {
                    "peer": peer,
                    "seat": assigned_seat,
                    "reason": "seat_endpoint_already_occupied",
                })
                return
            player_idx = 0 if assigned_seat == "top" else 1

            def wire_sink(
                event: dict[str, Any],
                idx: int = player_idx,
                address: str = peer,
            ) -> None:
                runtime.wire_sequence += 1
                row = {
                    "sequence": runtime.wire_sequence,
                    "session_id": session.session_id,
                    "player_idx": idx,
                    "seat": assigned_seat,
                    "peer": address,
                    **event,
                }
                try:
                    runtime.wire_queue.put_nowait(row)
                except asyncio.QueueFull:
                    runtime.wire_dropped += 1
                    session.wire_log_complete = False

            idle_flush = 0.003 if session.mode == "managed_bots" else 0.03
            client = NationalTCPClient(
                reader,
                writer,
                idle_flush_sec=idle_flush,
                wire_sink=wire_sink,
            )
            runtime.clients_by_seat[assigned_seat] = client
            runtime.peers_by_seat[assigned_seat] = peer
            runtime.clients = [
                runtime.clients_by_seat[candidate]
                for candidate in ("top", "bottom")
                if candidate in runtime.clients_by_seat
            ]
            runtime.peers = [
                runtime.peers_by_seat[candidate]
                for candidate in ("top", "bottom")
                if candidate in runtime.peers_by_seat
            ]
            session.connected_players = len(runtime.clients_by_seat)
            await self._emit(session, "player_connected", {
                "player_idx": player_idx,
                "seat": assigned_seat,
                "peer": peer,
                "connected_players": session.connected_players,
                "seat_authority": "listener_endpoint",
            })
            if len(runtime.clients_by_seat) == 2:
                runtime.connected.set()

    async def _handshake(
        self,
        session: ArenaSession,
        runtime: _ArenaRuntime,
    ) -> tuple[list[NationalTCPClient], list[str]]:
        try:
            ordered_clients = [
                runtime.clients_by_seat[seat] for seat in ("top", "bottom")
            ]
        except KeyError as exc:
            raise ArenaError("both seat-bound clients are required before handshake") from exc
        await asyncio.gather(*(client.send_message("name") for client in ordered_clients))
        try:
            raw_names = await asyncio.gather(*(
                client.recv_name(max(1.0, min(30.0, session.action_timeout_seconds)))
                for client in ordered_clients
            ))
        except NationalProtocolError as exc:
            raise ArenaError(f"team name handshake failed: {exc}") from exc
        if not all(raw_names):
            raise ArenaError("team name handshake timed out")
        reported_names = [str(name) for name in raw_names]
        if session.mode == "managed_bots":
            if len(runtime.launch_labels) != 2:
                raise ArenaError("managed launch labels are not initialized")
            names = list(runtime.launch_labels)
        else:
            names = reported_names
        for index, (client, name, reported_name) in enumerate(zip(
            ordered_clients,
            names,
            reported_names,
        )):
            client.name = name
            await self._emit(session, "player_named", {
                "player_idx": index,
                "seat": "top" if index == 0 else "bottom",
                "name": name,
                "reported_name": reported_name,
                "name_authority": (
                    "managed_launch_plan"
                    if session.mode == "managed_bots"
                    else "external_client"
                ),
            })
        return ordered_clients, names

    async def _launch_managed_bots(
        self,
        session: ArenaSession,
        runtime: _ArenaRuntime,
    ) -> None:
        capability = runtime.sandbox_capability
        if capability is None:
            raise ArenaInfrastructureError(
                "managed sandbox capability was not retained for launch"
            )
        bot_names = [str(session.top_bot), str(session.bottom_bot)]
        labels = list(bot_names)
        if labels[0] == labels[1]:
            labels = [f"{labels[0]}_TOP", f"{labels[1]}_BOTTOM"]
        runtime.launch_labels = labels
        hard_deadline = max(0.05, min(55.0, session.action_timeout_seconds - 0.25))

        sandbox_root = self.store.session_dir(session.session_id) / "sandbox"
        if sandbox_root.exists() or sandbox_root.is_symlink():
            raise ArenaInfrastructureError(
                f"managed sandbox root already exists: {sandbox_root}"
            )
        sandbox_root.mkdir(mode=0o700)
        runtime.sandbox_root = sandbox_root
        sealed_by_seat: dict[str, tuple[Path, Any, dict[str, Any]]] = {}
        for index, (seat, bot_name, label) in enumerate(zip(
            ("top", "bottom"), bot_names, labels
        )):
            _resolved_label, bot_dir = resolve_bot(bot_name)
            errors = [
                *current_system_native_runtime_errors(bot_dir),
                *check_native_contract(bot_dir),
            ]
            if errors:
                raise ArenaError(f"{bot_name} native contract failed: {errors[0]}")
            expected_identity = session.managed_bot_identities.get(seat) or {}
            current_identity = (
                self._certification_snapshot(bot_dir).get("artifact_identity") or {}
            )
            if not expected_identity or current_identity != expected_identity:
                raise ArenaError(
                    f"{bot_name} publication identity changed before launch"
                )
            expected_hash = str(expected_identity.get("artifact_hash") or "")
            sealed = seal_bot_artifact(
                bot_dir,
                sandbox_root / seat / "bot",
                expected_hash=expected_hash,
            )
            sealed_by_seat[seat] = (bot_dir, sealed, expected_identity)
            await self._emit(session, "bot_artifact_sealed", {
                "seat": seat,
                "bot": bot_name,
                "artifact_hash": sealed.artifact_hash,
                "manifest_digest": sealed.manifest_digest,
                "content_bound_copy": True,
                "source_mounted": False,
            })

        for index, (seat, bot_name, label) in enumerate(zip(
            ("top", "bottom"), bot_names, labels
        )):
            _bot_dir, sealed, expected_identity = sealed_by_seat[seat]
            endpoint = session.managed_endpoints.get(seat) or {}
            endpoint_host = str(endpoint.get("host") or "")
            endpoint_port = int(endpoint.get("port") or 0)
            stdout_path = self.store.artifact_path(
                session.session_id,
                f"{seat}.stdout.log",
                create_parent=True,
            )
            stderr_path = self.store.artifact_path(
                session.session_id,
                f"{seat}.stderr.log",
                create_parent=True,
            )
            session.artifacts[f"{seat}_stdout"] = stdout_path.name
            session.artifacts[f"{seat}_stderr"] = stderr_path.name
            stdout_handle = stdout_path.open("w", encoding="utf-8", buffering=1)
            stderr_handle = stderr_path.open("w", encoding="utf-8", buffering=1)
            try:
                with EndpointLease.connect(
                    endpoint_host,
                    endpoint_port,
                    timeout=5.0,
                ) as endpoint_lease:
                    managed = launch_sandboxed_bot(
                        sealed,
                        capability,
                        endpoint_lease,
                        name=label,
                        seat="upper" if seat == "top" else "lower",
                        session_id=session.session_id,
                        action_delay=session.official_action_delay,
                        hard_deadline=hard_deadline,
                        refinement_budget=max(0.04, hard_deadline - 0.10),
                        baseline_target=min(0.25, hard_deadline * 0.25),
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_handle,
                        stderr=stderr_handle,
                        start_new_session=True,
                    )
                process = managed.process
                session.sandbox_profile = (
                    "central-managed-executor:"
                    f"{managed.isolation.policy_sha256[:16]}"
                )
            except Exception as exc:
                stdout_handle.close()
                stderr_handle.close()
                raise ArenaInfrastructureError(
                    f"managed sandbox process launch failed: {type(exc).__name__}: {exc}"
                ) from exc
            try:
                pgid = os.getpgid(process.pid)
            except ProcessLookupError:
                pgid = process.pid
            runtime.processes.append(_ManagedProcess(
                seat=seat,
                label=label,
                process=process,
                pgid=pgid,
                stdout_handle=stdout_handle,
                stderr_handle=stderr_handle,
            ))
            process_record = {
                "seat": seat,
                "label": label,
                "pid": process.pid,
                "pgid": pgid,
                "start_ticks": self._proc_start_ticks(process.pid),
                "session_marker": session.session_id,
                "started_at": utc_now(),
                "artifact_hash": expected_identity.get("artifact_hash"),
                "sandbox_profile": session.sandbox_profile,
            }
            session.managed_processes.append(process_record)
            await self._emit(session, "bot_process_started", {
                "seat": seat,
                "bot": bot_name,
                "label": label,
                "pid": process.pid,
                "entry": "/bot/national_bot.py",
                "adapter_used": False,
                "official_action_delay": session.official_action_delay,
                "launch_index": index,
                "endpoint": copy.deepcopy(endpoint),
                "seat_authority": "listener_endpoint",
                "sandbox_profile": session.sandbox_profile,
                "repository_mounted": False,
                "writable_host_binds": [],
                "process_identity": process_record,
            })

    async def _wait_for_process_exit(self, runtime: _ArenaRuntime) -> tuple[str, int]:
        while True:
            for managed in runtime.processes:
                returncode = managed.process.poll()
                if returncode is not None:
                    return managed.seat, int(returncode)
            await asyncio.sleep(0.1)

    async def _on_engine_event(
        self,
        session: ArenaSession,
        event: dict[str, Any],
    ) -> None:
        raw_type = str(event.get("type") or "engine_event")
        payload = {key: value for key, value in event.items() if key != "type"}
        mapped_type = {
            "hand_start": "hand_started",
            "cards_dealt": "hole_cards_dealt",
            "stage": "street_started",
            "action_requested": "action_requested",
            "action": "player_action",
            "settle": "hand_finished",
            "match_end": "engine_match_summary",
        }.get(raw_type, raw_type)
        if raw_type == "action":
            action = str(event.get("action") or "")
            player_idx = int(event.get("player_idx", 0) or 0)
            if action == "timeout":
                session.timeouts[player_idx] += 1
                mapped_type = "timeout"
            elif action.startswith("illegal:") or action.startswith("protocol_"):
                session.illegal_actions[player_idx] += 1
                mapped_type = "illegal_action"
        elif raw_type == "settle":
            session.hands_completed = max(
                session.hands_completed,
                int(event.get("hand", 0) or 0),
            )
            earnings = event.get("earnings") or [0, 0]
            session.top_total_earnings += int(earnings[0])
            session.bottom_total_earnings += int(earnings[1])
        elif raw_type == "match_end":
            totals = event.get("total_earnings") or [0, 0]
            session.top_total_earnings = int(totals[0])
            session.bottom_total_earnings = int(totals[1])
        await self._emit(session, mapped_type, payload)

    async def _emit(
        self,
        session: ArenaSession,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        lock = self._event_locks.setdefault(session.session_id, asyncio.Lock())
        async with lock:
            session.last_event_id += 1
            event = {
                "event_id": session.last_event_id,
                "session_id": session.session_id,
                "type": event_type,
                "timestamp": utc_now(),
                "hand_no": int(payload.get("hand", session.hands_completed) or 0),
                "payload": payload,
            }
            await self._run_storage(
                self.store.append_event_and_session,
                session,
                event,
            )
        condition = self._conditions.setdefault(session.session_id, asyncio.Condition())
        async with condition:
            condition.notify_all()
        return event

    async def _cleanup_runtime(
        self,
        session: ArenaSession,
        runtime: _ArenaRuntime,
    ) -> _CleanupOutcome:
        pending: list[str] = list(await self._close_servers(runtime))
        details: dict[str, Any] = {}

        protocol_tasks = {
            task
            for task in runtime.child_tasks
            if task is not runtime.wire_writer_task
        }
        pending.extend(await self._cancel_and_await_tasks(runtime, protocol_tasks))
        for seat, client in list(runtime.clients_by_seat.items()):
            try:
                await client.close(timeout=1.0)
            except Exception as exc:
                pending.append(f"client:{seat}:{type(exc).__name__}")
        session.connected_players = 0
        if (
            runtime.engine is not None
            and "thp" not in session.artifacts
            and getattr(runtime.engine.recorder, "records", None)
        ):
            try:
                await self._export_thp(session, runtime.engine, partial=True)
            except Exception as exc:
                details["partial_thp_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

        wire_task = runtime.wire_writer_task
        if wire_task is not None and not wire_task.done():
            try:
                await asyncio.wait_for(runtime.wire_queue.put(None), timeout=5.0)
                await asyncio.wait_for(asyncio.shield(wire_task), timeout=5.0)
            except asyncio.TimeoutError:
                runtime.wire_writer_error = (
                    runtime.wire_writer_error or "wire_writer_shutdown_timeout"
                )
                pending.extend(
                    await self._cancel_and_await_tasks(runtime, {wire_task})
                )
        if wire_task is not None and wire_task.done():
            await asyncio.gather(wire_task, return_exceptions=True)
            runtime.child_tasks.discard(wire_task)
        wire_pending = bool(wire_task is not None and not wire_task.done())
        if wire_pending and not any(item.startswith("national-arena-wire-") for item in pending):
            pending.append("wire_writer")
        if runtime.wire_executor is not None and not wire_pending:
            runtime.wire_executor.shutdown(wait=True, cancel_futures=False)
            runtime.wire_executor = None
        if runtime.wire_dropped or runtime.wire_writer_error:
            session.wire_log_complete = False
            with contextlib.suppress(Exception):
                await self._emit(session, "wire_log_incomplete", {
                    "dropped_records": runtime.wire_dropped,
                    "writer_error": runtime.wire_writer_error,
                    "reason": (
                        "wire_writer_failed"
                        if runtime.wire_writer_error
                        else "bounded_wire_queue_saturated"
                    ),
                })
        surviving_processes: list[_ManagedProcess] = []
        for managed in runtime.processes:
            process_pending = await self._terminate_managed_process(session, managed)
            if process_pending:
                pending.append(process_pending)
                surviving_processes.append(managed)
        runtime.processes = surviving_processes

        if not surviving_processes and runtime.sandbox_root is not None:
            try:
                remove_sealed_artifacts(runtime.sandbox_root)
                runtime.sandbox_root = None
            except Exception as exc:
                pending.append(f"sandbox_tree:{type(exc).__name__}")
                details["sandbox_cleanup_error"] = str(exc)[:200]

        clean = not pending
        if clean:
            if runtime.official_platform_lease is not None:
                runtime.official_platform_lease.release()
                runtime.official_platform_lease = None
            if runtime.capacity_lease is not None:
                runtime.capacity_lease.release()
                runtime.capacity_lease = None
        details.update({
            "capacity_fence_held": runtime.capacity_lease is not None,
            "official_port_fence_held": runtime.official_platform_lease is not None,
            "managed_processes_remaining": len(runtime.processes),
            "child_tasks_remaining": sum(
                1 for task in runtime.child_tasks if not task.done()
            ),
        })
        return _CleanupOutcome(
            clean=clean,
            pending=tuple(dict.fromkeys(pending)),
            details=details,
        )

    async def _terminate_managed_process(
        self,
        session: ArenaSession,
        managed: _ManagedProcess,
    ) -> str | None:
        process = managed.process
        pgid = int(managed.pgid)
        if self._process_group_alive(pgid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGTERM)
            deadline = asyncio.get_running_loop().time() + 2.0
            while (
                self._process_group_alive(pgid)
                and asyncio.get_running_loop().time() < deadline
            ):
                process.poll()
                await asyncio.sleep(0.05)
            if self._process_group_alive(pgid):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pgid, signal.SIGKILL)
                deadline = asyncio.get_running_loop().time() + 2.0
                while (
                    self._process_group_alive(pgid)
                    and asyncio.get_running_loop().time() < deadline
                ):
                    process.poll()
                    await asyncio.sleep(0.05)
        process.poll()
        for handle in (managed.stdout_handle, managed.stderr_handle):
            with contextlib.suppress(Exception):
                handle.close()
        alive = self._process_group_alive(pgid)
        if not alive:
            session.managed_processes = [
                item
                for item in session.managed_processes
                if int(item.get("pid", -1)) != int(process.pid)
            ]
        with contextlib.suppress(Exception):
            await self._emit(session, "bot_process_exited", {
                "seat": managed.seat,
                "label": managed.label,
                "returncode": process.returncode,
                "process_group_alive": alive,
            })
        return f"process_group:{managed.label}:{pgid}" if alive else None

    async def _wire_writer(
        self,
        session: ArenaSession,
        runtime: _ArenaRuntime,
    ) -> None:
        try:
            if runtime.wire_executor is None:
                raise RuntimeError("wire executor is not initialized")
            stopping = False
            while not stopping:
                item = await runtime.wire_queue.get()
                if item is None:
                    runtime.wire_queue.task_done()
                    break
                batch = [item]
                runtime.wire_queue.task_done()
                while len(batch) < 128:
                    try:
                        item = runtime.wire_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    runtime.wire_queue.task_done()
                    if item is None:
                        stopping = True
                        break
                    batch.append(item)
                concurrent = runtime.wire_executor.submit(
                    self.store.append_wire_batch,
                    session.session_id,
                    batch,
                )
                while not concurrent.done():
                    await asyncio.sleep(0.001)
                concurrent.result()
        except Exception as exc:
            runtime.wire_writer_error = f"{type(exc).__name__}: {str(exc)[:240]}"

    async def _acquire_capacity(
        self,
        session: ArenaSession,
    ) -> RuntimeCapacityLease:
        owner = f"arena:{session.session_id}"
        deadline = time.monotonic() + max(0.05, session.capacity_wait_seconds)
        while True:
            lease = try_acquire_match_slots(owner, count=2)
            if lease is not None:
                return lease
            if time.monotonic() >= deadline:
                raise ArenaInfrastructureError(
                    "runtime_capacity_timeout: "
                    f"2 slots unavailable after {session.capacity_wait_seconds:g}s"
                )
            await asyncio.sleep(0.2)

    @staticmethod
    def _claim_official_platform_resource(
        session: ArenaSession,
        runtime: _ArenaRuntime,
    ) -> None:
        config = OfficialPlatformConfig()
        lease = try_acquire_official_platform(
            config.lock_path,
            owner=f"arena:{session.session_id}",
        )
        if lease is None:
            raise ArenaConflict(
                "another Arena session currently owns TCP port 10001"
            )
        # The official EXE certification job queue was removed (Phases 3-4);
        # there are no pending cert jobs to defer to, so the lease is held
        # directly by this arena session.
        runtime.official_platform_lease = lease

    def read_wire(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if session_id not in self._sessions:
            raise ArenaNotFound(f"arena session not found: {session_id}")
        return self.store.read_wire(
            session_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    async def read_wire_async(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if session_id not in self._sessions:
            raise ArenaNotFound(f"arena session not found: {session_id}")
        return await self._run_storage(
            self.store.read_wire,
            session_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    async def _export_thp(
        self,
        session: ArenaSession,
        engine: NationalTCPGameEngine,
        *,
        partial: bool,
    ) -> None:
        filename = "partial.thp.txt" if partial else "match.thp.txt"
        artifact_key = "partial_thp" if partial else "thp"
        path = self.store.artifact_path(
            session.session_id,
            filename,
            create_parent=True,
        )
        await self._run_storage(
            engine.recorder.export_file,
            str(path),
            "National Web Arena - Diagnostic Only",
            (
                "Local Diagnostic Partial - Not EXE Evidence"
                if partial
                else "Local Diagnostic - Not EXE Evidence"
            ),
        )
        session.artifacts[artifact_key] = path.name
        await self._emit(session, "partial_thp_written" if partial else "thp_written", {
            "artifact": artifact_key,
            "filename": path.name,
            "encoding": "gb2312",
            "partial": partial,
        })

    @staticmethod
    def _proc_start_ticks(pid: int) -> int | None:
        try:
            _prefix, _separator, suffix = Path(f"/proc/{int(pid)}/stat").read_text().rpartition(")")
            fields = suffix.strip().split()
            return int(fields[19])
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def _process_group_alive(pgid: int) -> bool:
        if int(pgid) <= 1:
            return False
        try:
            os.killpg(int(pgid), 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    @classmethod
    def _marked_process_group_members(cls, pgid: int, marker: str) -> list[int]:
        members: list[int] = []
        proc_root = Path("/proc")
        for path in proc_root.iterdir():
            if not path.name.isdigit():
                continue
            pid = int(path.name)
            try:
                _prefix, _separator, suffix = (path / "stat").read_text().rpartition(")")
                fields = suffix.strip().split()
                process_group = int(fields[2])
            except (OSError, ValueError, IndexError):
                continue
            if process_group == int(pgid) and cls._proc_has_session_marker(pid, marker):
                members.append(pid)
        return members

    @staticmethod
    def _proc_has_session_marker(pid: int, marker: str) -> bool:
        try:
            entries = Path(f"/proc/{int(pid)}/environ").read_bytes().split(b"\0")
        except OSError:
            return False
        expected = {
            f"POK_MANAGED_PROCESS_OWNER={marker}".encode("utf-8"),
            f"POK_ARENA_SESSION_ID={marker}".encode("utf-8"),
        }
        return any(item in entries for item in expected)

    async def _reap_persisted_processes(
        self,
        session: ArenaSession,
    ) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        for record in list(session.managed_processes):
            pid = int(record.get("pid", 0) or 0)
            pgid = int(record.get("pgid", 0) or 0)
            expected_ticks = record.get("start_ticks")
            actual_ticks = self._proc_start_ticks(pid) if pid > 1 else None
            marked_members = self._marked_process_group_members(
                pgid,
                session.session_id,
            )
            identity_matches = bool(
                pid > 1
                and pgid > 1
                and (
                    (
                        actual_ticks is not None
                        and actual_ticks == expected_ticks
                        and self._proc_has_session_marker(pid, session.session_id)
                    )
                    or marked_members
                )
            )
            outcome = {
                "pid": pid,
                "pgid": pgid,
                "identity_matches": identity_matches,
                "marked_members": marked_members,
                "action": "none",
            }
            if identity_matches:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pgid, signal.SIGTERM)
                    outcome["action"] = "sigterm"
                for _ in range(40):
                    if not self._marked_process_group_members(pgid, session.session_id):
                        break
                    await asyncio.sleep(0.05)
                if self._marked_process_group_members(pgid, session.session_id):
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(pgid, signal.SIGKILL)
                        outcome["action"] = "sigkill"
                    for _ in range(40):
                        if not self._marked_process_group_members(pgid, session.session_id):
                            break
                        await asyncio.sleep(0.05)
                if self._marked_process_group_members(pgid, session.session_id):
                    outcome["action"] = "unresolved"
                    unresolved.append(record)
            elif actual_ticks is None:
                outcome["action"] = "already_exited"
            else:
                outcome["action"] = "identity_mismatch_not_killed"
                unresolved.append(record)
            outcomes.append(outcome)
        session.managed_processes = unresolved
        return outcomes

    @staticmethod
    def _system_event(
        event_type: str,
        severity: str,
        message: str,
        session: ArenaSession,
    ) -> None:
        try:
            from system_log import log_system_event

            log_system_event(event_type, severity, message, {
                "session_id": session.session_id,
                "mode": session.mode,
                "status": session.status,
                "result_authority": "diagnostic_only",
                "affects_glicko": False,
                "official_exe_certification": False,
            })
        except Exception:
            pass
