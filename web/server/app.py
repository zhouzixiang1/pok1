"""Unified FastAPI backend — imports from web/core modules."""

import asyncio
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = PROJECT_ROOT / "web"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(WEB_DIR / "core"))

from web_ui import EventBroadcaster, WebUI
from server.state import app_state, run_evolution_task
from system_log import set_ui as _set_system_log_ui
from national_arena.manager import NationalArenaManager
from blocking_runtime import run_blocking_isolated

broadcaster = EventBroadcaster(buffer_size=500)
web_ui = WebUI(broadcaster)
_set_system_log_ui(web_ui)
arena_manager = NationalArenaManager()


def register_lifespan_runtime_owner(owner_id: str | None) -> None:
    """Mark one owner as created by this FastAPI lifespan/control plane.

    ``AppState`` is intentionally owner-agnostic so its fencing remains usable
    in tests and standalone control code.  The web lifespan separately tracks
    the owners it created, preventing shutdown from taking an unrelated
    pre-existing in-process owner while still covering a later ``/start``.
    """

    if isinstance(owner_id, str) and owner_id:
        app.state.evolution_lifespan_owned_owners.add(owner_id)


def unregister_lifespan_runtime_owner(owner_id: str | None) -> None:
    if isinstance(owner_id, str) and owner_id:
        app.state.evolution_lifespan_owned_owners.discard(owner_id)


def _publish_task_owner(snapshot: dict) -> None:
    """Invalidate browser-only status text on every owner lifecycle edge.

    This is intentionally a separate SSE event rather than a WebUI ``status``
    phrase.  It carries no workflow evidence, but lets a connected browser
    immediately reject a status stamped by a replaced owner instead of waiting
    for its periodic control-health poll.  ``EventBroadcaster`` drops it until
    the strict epoch authority is bound, and the route rechecks the snapshot at
    delivery time before forwarding replay or queued rows.
    """

    owner_id = snapshot.get("owner_id")
    owner_valid = isinstance(owner_id, str) and re.fullmatch(
        r"[0-9a-f]{32}", owner_id
    ) is not None
    present = snapshot.get("present") is True
    done = snapshot.get("done")
    shutdown_requested = snapshot.get("shutdown_requested")
    status_eligible = snapshot.get("status_eligible")
    lifecycle_revision = snapshot.get("lifecycle_revision")
    revision_valid = (
        type(lifecycle_revision) is int and lifecycle_revision >= 0
    )
    if (
        present
        and isinstance(done, bool)
        and isinstance(shutdown_requested, bool)
        and isinstance(status_eligible, bool)
        and owner_valid
        and revision_valid
        and (
            status_eligible is False
            or (done is False and shutdown_requested is False)
        )
    ):
        payload = {
            "present": True,
            "done": done,
            "shutdown_requested": shutdown_requested,
            "status_eligible": status_eligible,
            "owner_id": owner_id,
            "lifecycle_revision": lifecycle_revision,
        }
    elif (
        not present
        and done is None
        and isinstance(shutdown_requested, bool)
        and status_eligible is False
        and revision_valid
    ):
        payload = {
            "present": False,
            "done": None,
            "shutdown_requested": shutdown_requested,
            "status_eligible": False,
            "owner_id": owner_id if owner_valid else None,
            "lifecycle_revision": lifecycle_revision,
        }
    else:
        # Do not invent a revision-zero task_owner row.  A browser with a
        # later high-water would correctly reject it and could retain stale
        # text.  This distinct typed invalidator means "no trustworthy task
        # authority exists"; clients clear transient status without advancing
        # their lifecycle counter, so a later legitimate same-revision owner
        # projection can still recover.
        broadcaster.broadcast(
            "task_authority_lost",
            {"reason": "task_snapshot_projection_invalid"},
        )
        return
    broadcaster.broadcast("task_owner", payload)


app_state.add_task_snapshot_listener(_publish_task_owner)

from logging_config import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):

    from evolution_infra import find_current_v
    configure_logging(broadcaster=broadcaster)
    app_state.bootstrap(find_current_v())

    config = app_state.get_config()
    daemon_enabled = config["daemon_enabled"]
    from stability_observation import bind_runtime_configuration

    bind_runtime_configuration(config)
    view_only = os.environ.get("POK_WEB_VIEW_ONLY") == "1"
    epoch_launch_state = None
    epoch_launch_allowed = False
    try:
        from epoch_authority import (
            epoch_stream_authority_digest,
            require_policy_epoch_initialized,
            strict_epoch_projection,
        )

        epoch_launch_state = require_policy_epoch_initialized("web_lifespan")
        try:
            projection = strict_epoch_projection()
            if projection.get("initialized"):
                epoch_launch_state = projection
        except Exception:
            # The initialization guard remains authoritative.  Failure to
            # obtain optional workflow provenance must not manufacture a
            # second epoch interpretation.
            pass
        epoch_launch_allowed = True
    except Exception as exc:
        # The typed exception carries the canonical, read-only projection.  Do
        # not emit a structured event here: results/ still belongs to the
        # retired epoch and must remain untouched until the reset is executed.
        epoch_launch_state = getattr(exc, "state", None) or {
            "state": "epoch_authority_unavailable",
            "operator_action": "inspect_epoch_authority",
            "operator_command": None,
        }
    try:
        launch_authority_digest = epoch_stream_authority_digest(epoch_launch_state)
    except (NameError, TypeError, ValueError):
        launch_authority_digest = None
    broadcaster.bind_authority(launch_authority_digest)

    # Let uvicorn own signal handling — its handle_exit sets should_exit,
    # which triggers sse-starlette shutdown → lifespan shutdown below.
    from shutdown_manager import ShutdownManager
    shutdown_mgr = ShutdownManager(grace_period=15.0)
    # LLM saturator: an independent background workload that keeps free LLM
    # permits filled with deep analysis sessions, decoupled from the (bursty,
    # stall-prone) pipeline. Runs regardless of epoch/orchestrator launch state
    # so LLM consumption does not depend on the pipeline being healthy. Gated by
    # POK_LLM_SATURATOR_ENABLED; cancelled in the finally below.
    saturator_task = None
    try:
        from llm_saturator import run_llm_saturator, SATURATOR_ENABLED
        if SATURATOR_ENABLED:
            saturator_task = asyncio.create_task(run_llm_saturator(shutdown_mgr))
            app.state.saturator_task = saturator_task
    except Exception as _sat_exc:
        import logging as _sat_log
        _sat_log.getLogger("pok.saturator").warning(
            "LLM saturator launch failed: %s", _sat_exc)

    # Memory heartbeat: the process historically grows to its MemoryMax
    # ceiling over hours (2026-08-11 finding) with zero in-code
    # observability, so every growth cycle was a black box. A periodic
    # VmRSS/Threads line in the journal makes the trend and the pre-OOM
    # phase greppable without tracemalloc overhead. Cancelled in the finally
    # below alongside the saturator.
    async def _memory_heartbeat(interval_sec: float = 600.0) -> None:
        import logging as _mem_log

        heartbeat_log = _mem_log.getLogger("pok.memory")
        warn_mb = 1536
        while not (
            shutdown_mgr is not None
            and getattr(shutdown_mgr, "is_shutting_down", False)
        ):
            try:
                fields: dict[str, str] = {}
                with open("/proc/self/status", encoding="ascii") as fh:
                    for line in fh:
                        if ":" in line:
                            key, value = line.split(":", 1)
                            fields[key.strip()] = value.strip()
                rss_mb = int(fields.get("VmRSS", "0").split()[0]) // 1024
                threads = int(fields.get("Threads", "0"))
                log_at = (
                    heartbeat_log.warning if rss_mb > warn_mb
                    else heartbeat_log.info
                )
                log_at("memory heartbeat: rss_mb=%d threads=%d", rss_mb, threads)
            except Exception:
                pass
            for _ in range(int(interval_sec)):
                if (
                    shutdown_mgr is not None
                    and getattr(shutdown_mgr, "is_shutting_down", False)
                ):
                    break
                await asyncio.sleep(1.0)

    memory_task = asyncio.create_task(_memory_heartbeat())
    app.state.memory_heartbeat_task = memory_task
    app.state.national_arena_manager = arena_manager
    app.state.national_arena_epoch_authority = epoch_launch_state
    arena_started = False
    if epoch_launch_allowed:
        await arena_manager.startup(epoch_authority=epoch_launch_state)
        arena_started = True
    orchestrator_owned = False
    stability_launch_allowed = True
    launch_reservation = None

    def _live_runtime_owner_present() -> bool:
        return bool(
            app_state.runtime_owner_id()
            and app_state.to_dict().get("running") is True
        )

    # On shutdown: stop orchestrator + daemon in parallel for fast exit.
    async def _stop_orchestrator(owner_id: str):
        """Stop the current fenced owner, never the startup-time manager."""
        # A control-plane start can bind a successor manager after this
        # lifespan started.  Resolve through AppState immediately before the
        # edge so the current owner receives graceful cancellation.
        app_state.request_shutdown(owner_id=owner_id)
        if app_state.runtime_owner_id() != owner_id:
            return
        task = app_state.stop_running(owner_id=owner_id)
        if task and not task.done():
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=3)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

    async def _stop_daemon_async():
        """Stop daemon subprocess."""
        try:
            from daemon_management import _daemon_shutting_down
            import daemon_management
            daemon_management._daemon_shutting_down = True
        except Exception:
            pass
        try:
            from daemon_management import stop_daemon
            await run_blocking_isolated(
                stop_daemon,
                thread_name_prefix="daemon-shutdown",
            )
        except Exception:
            pass

    try:
        if view_only:
            web_ui.log_history(
                "Dashboard started in view-only mode; evolution loop is not running.",
                "info",
            )
        elif not epoch_launch_allowed:
            owner_retained = _live_runtime_owner_present()
            epoch_state = str(epoch_launch_state.get("state") or "reset_required")
            operator_command = epoch_launch_state.get("operator_command")
            message = (
                "Dashboard launch attempt retained the existing runtime owner; "
                if owner_retained
                else "Dashboard started with evolution stopped: "
            ) + f"policy epoch initialization is {epoch_state}."
            if operator_command:
                message += f" Run: {operator_command}"
            web_ui.log_history(message, "warn")
            if not owner_retained:
                web_ui.set_status(
                    f"Stopped: {epoch_state}",
                    is_working=False,
                )
        else:
            from server.routes.control import _reserve_runtime_launch_owner

            launch_reservation = await _reserve_runtime_launch_owner()
            if launch_reservation.get("acquired") is not True:
                stability_launch_allowed = False
                owner_retained = _live_runtime_owner_present()
                barrier = launch_reservation.get("barrier") or {}
                denial = str(
                    barrier.get("denial_code")
                    or launch_reservation.get("reason")
                    or "launch_authority_unavailable"
                )
                issues = list(barrier.get("issues") or [])
                launch_message = (
                    "Dashboard launch attempt retained the existing runtime owner: "
                    if owner_retained
                    else "Dashboard started with evolution stopped by the canonical "
                ) + f"launch barrier: {denial}"
                if issues:
                    launch_message += f" ({'; '.join(map(str, issues))})"
                web_ui.log_history(
                    launch_message,
                    "warn",
                )
                if not owner_retained:
                    web_ui.set_status(
                        f"Stopped: {denial}",
                        is_working=False,
                    )

        if (
            not view_only
            and epoch_launch_allowed
            and stability_launch_allowed
            and launch_reservation is not None
            and launch_reservation.get("acquired") is True
        ):
            try:
                from stability_observation import initialize_stability_observation

                observation = initialize_stability_observation(
                    "runtime_process_start"
                )
                web_ui.log_history(
                    "连续进化验收已启动："
                    f"{observation.get('count', 0)}/{observation.get('target', 10)}",
                    "info",
                )
            except Exception as exc:
                # Continuous-generation acceptance is part of the launch
                # contract.  If its durable state cannot be initialized, both
                # lifespan and POST start stay stopped instead of running an
                # evolution task whose required 10-generation observation can
                # never be proven.
                stability_launch_allowed = False
                try:
                    web_ui.log_history(
                        "连续进化验收状态无法初始化："
                        f"{type(exc).__name__}: {str(exc)[:160]}",
                        "error",
                    )
                    web_ui.set_status(
                        "Stopped: stability observation unavailable",
                        is_working=False,
                    )
                finally:
                    app_state.abort_runtime_owner(
                        launch_reservation.get("owner_id")
                    )

        if view_only or not epoch_launch_allowed or not stability_launch_allowed:
            pass
        else:
            owner_id = launch_reservation.get("owner_id")
            task = None
            try:
                from orchestrator import orchestrator_loop

                app_state.set_shutdown_mgr(
                    shutdown_mgr,
                    owner_id=owner_id,
                )
                try:
                    from llm_query import set_shutdown_manager

                    if not set_shutdown_manager(
                        shutdown_mgr,
                        owner_id=owner_id,
                    ):
                        raise RuntimeError(
                            "LLM shutdown manager owner fencing conflict"
                        )
                except Exception:
                    raise
                task = asyncio.create_task(run_evolution_task(orchestrator_loop(
                    web_ui,
                    shutdown_mgr=shutdown_mgr,
                    no_daemon=not daemon_enabled,
                    daemon_workers=config["daemon_workers"],
                    daemon_pairs=config["daemon_pairs"],
                ), owner_id=owner_id))
                app_state.set_task(task, owner_id=owner_id)
                register_lifespan_runtime_owner(owner_id)
                orchestrator_owned = True
                web_ui.log_history("🔥 Orchestrator started (LLM-driven mode)", "success")
            except BaseException:
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except BaseException:
                        pass
                try:
                    from llm_query import set_shutdown_manager

                    set_shutdown_manager(None, owner_id=owner_id)
                except Exception:
                    pass
                app_state.abort_runtime_owner(owner_id)
                raise
        yield
    finally:
        # Cancel the LLM saturator background task (independent of the
        # orchestrator owner logic below).
        _sat = getattr(app.state, "saturator_task", None)
        if _sat is not None and not _sat.done():
            _sat.cancel()
            try:
                await asyncio.wait_for(_sat, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        _mem = getattr(app.state, "memory_heartbeat_task", None)
        if _mem is not None and not _mem.done():
            _mem.cancel()
            try:
                await asyncio.wait_for(_mem, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        # ``AppState`` is process-local, so any live owner here was started by
        # this server (including a later /api/control/start).  Do not rely on
        # the initial auto-launch flag: it is stale after a control restart.
        current_runtime_owner = app_state.runtime_owner_id()
        runtime_owned_at_shutdown = bool(
            current_runtime_owner
            and current_runtime_owner in app.state.evolution_lifespan_owned_owners
        )
        if runtime_owned_at_shutdown:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        _stop_orchestrator(current_runtime_owner),
                        _stop_daemon_async(),
                        return_exceptions=True,
                    ),
                    timeout=18,
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            finally:
                unregister_lifespan_runtime_owner(current_runtime_owner)
        if arena_started:
            await arena_manager.shutdown()
        if view_only:
            web_ui.log_history("Dashboard stopped.", "info")
        elif runtime_owned_at_shutdown:
            web_ui.log_history("Evolution stopped.", "info")


app = FastAPI(title="Poker Evolution Unified API", version="1.0", lifespan=lifespan)
app.state.evolution_lifespan_owned_owners = set()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routes ──
from server.routes.ratings import router as ratings_router
from server.routes.matches import router as matches_router
from server.routes.evolution import router as evolution_router
from server.routes.logs import router as logs_router
from server.routes.control import router as control_router
from server.routes.bots import router as bots_router
from server.routes.pipeline import router as pipeline_router
from server.routes.prompts import router as prompts_router
from server.routes.data_stream import router as data_stream_router
from server.routes.national_arena import router as national_arena_router
from server.routes.llm_metrics import router as llm_metrics_router

app.include_router(ratings_router)
app.include_router(matches_router)
app.include_router(evolution_router)
app.include_router(logs_router)
app.include_router(control_router)
app.include_router(bots_router)
app.include_router(pipeline_router)
app.include_router(prompts_router)
app.include_router(data_stream_router)
app.include_router(national_arena_router)
app.include_router(llm_metrics_router)

def _install_static_spa_routes(target_app: FastAPI, static_dir: Path) -> None:
    """Serve the built React app without swallowing unknown API/static paths."""
    if not static_dir.is_dir():
        return

    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        target_app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @target_app.get("/favicon.png")
    async def serve_favicon():
        favicon = static_dir / "favicon.png"
        if not favicon.exists():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(favicon, media_type="image/png")

    @target_app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        if full_path.startswith("api/") or Path(full_path).suffix:
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(static_dir / "index.html")


# ── Static files (production build) ──
STATIC_DIR = Path(__file__).resolve().parent / "static"
_install_static_spa_routes(app, STATIC_DIR)
