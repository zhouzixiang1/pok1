"""Unified FastAPI backend — imports from web/core modules."""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = PROJECT_ROOT / "web"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
sys.path.insert(0, str(WEB_DIR / "core"))

from web_ui import EventBroadcaster, WebUI
from server.state import app_state
from system_log import set_ui as _set_system_log_ui

broadcaster = EventBroadcaster(buffer_size=500)
web_ui = WebUI(broadcaster)
_set_system_log_ui(web_ui)

_evolution_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _evolution_task

    from evolution_infra import find_current_v
    app_state.bootstrap(find_current_v())

    config = app_state.get_config()
    daemon_enabled = config["daemon_enabled"]

    # Install signal handlers for graceful shutdown
    from shutdown_manager import ShutdownManager
    shutdown_mgr = ShutdownManager(grace_period=15.0)
    loop = asyncio.get_running_loop()
    shutdown_mgr.install_signal_handlers(loop)

    app_state.set_running(True)
    try:
        from orchestrator import orchestrator_loop
        _evolution_task = asyncio.create_task(orchestrator_loop(
            web_ui, shutdown_mgr=shutdown_mgr,
            no_daemon=not daemon_enabled,
            daemon_workers=config["daemon_workers"], daemon_pairs=config["daemon_pairs"]))
        app_state.set_task(_evolution_task)
        web_ui.log_history("🔥 Orchestrator started (LLM-driven mode)", "success")
    except Exception:
        app_state.set_running(False)
        raise

    yield

    # On shutdown: signal orchestrator to stop, wait briefly
    if _evolution_task and not _evolution_task.done():
        shutdown_mgr.request_shutdown()
        try:
            await asyncio.wait_for(_evolution_task, timeout=20)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            _evolution_task.cancel()
            try:
                await asyncio.wait_for(_evolution_task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    try:
        from evolution_infra import stop_daemon
        stop_daemon()
    except Exception:
        pass
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
from server.routes.pipeline import router as pipeline_router
from server.routes.prompts import router as prompts_router
from server.routes.data_stream import router as data_stream_router

app.include_router(ratings_router)
app.include_router(matches_router)
app.include_router(evolution_router)
app.include_router(logs_router)
app.include_router(control_router)
app.include_router(bots_router)
app.include_router(pipeline_router)
app.include_router(prompts_router)
app.include_router(data_stream_router)

# ── Static files (production build) ──
STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        return FileResponse(STATIC_DIR / "index.html")
