"""Unified FastAPI backend — imports from web/core modules."""

import asyncio
import os
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
    async def _stop_orchestrator():
        """Cancel orchestrator task with reduced timeout."""
        task = app_state.stop_running()
        if task and not task.done():
            shutdown_mgr.request_shutdown()
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
        if orchestrator_owned:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        _stop_orchestrator(),
                        _stop_daemon_async(),
                        return_exceptions=True,
                    ),
                    timeout=18,
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        if arena_started:
            await arena_manager.shutdown()
        if view_only:
            web_ui.log_history("Dashboard stopped.", "info")
        elif orchestrator_owned:
            web_ui.log_history("Evolution stopped.", "info")


app = FastAPI(title="Poker Evolution Unified API", version="1.0", lifespan=lifespan)

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
from server.routes.certification import router as certification_router
from server.routes.pipeline import router as pipeline_router
from server.routes.prompts import router as prompts_router
from server.routes.data_stream import router as data_stream_router
from server.routes.national_arena import router as national_arena_router

app.include_router(ratings_router)
app.include_router(matches_router)
app.include_router(evolution_router)
app.include_router(logs_router)
app.include_router(control_router)
app.include_router(bots_router)
app.include_router(certification_router)
app.include_router(pipeline_router)
app.include_router(prompts_router)
app.include_router(data_stream_router)
app.include_router(national_arena_router)

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
