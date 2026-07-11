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
from server.state import app_state
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
    view_only = os.environ.get("POK_WEB_VIEW_ONLY") == "1"

    # Let uvicorn own signal handling — its handle_exit sets should_exit,
    # which triggers sse-starlette shutdown → lifespan shutdown below.
    from shutdown_manager import ShutdownManager
    shutdown_mgr = ShutdownManager(grace_period=15.0)
    app_state.set_shutdown_mgr(shutdown_mgr)
    await arena_manager.startup()
    app.state.national_arena_manager = arena_manager
    try:
        from llm_query import set_shutdown_manager
        set_shutdown_manager(shutdown_mgr)
    except Exception:
        pass

    orchestrator_owned = False

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
        elif not app_state.try_set_running(True):
            web_ui.log_history("Orchestrator already running", "warn")
        else:
            try:
                from orchestrator import orchestrator_loop

                task = asyncio.create_task(orchestrator_loop(
                    web_ui,
                    shutdown_mgr=shutdown_mgr,
                    no_daemon=not daemon_enabled,
                    daemon_workers=config["daemon_workers"],
                    daemon_pairs=config["daemon_pairs"],
                ))
                app_state.set_task(task)
                orchestrator_owned = True
                web_ui.log_history("🔥 Orchestrator started (LLM-driven mode)", "success")
            except Exception:
                app_state.set_running(False)
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
from server.routes.scheduler import router as scheduler_router
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
app.include_router(scheduler_router)
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
