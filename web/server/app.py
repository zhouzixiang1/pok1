"""Unified FastAPI backend — imports from web/core modules."""

import asyncio
import os
import sys
import threading
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

broadcaster = EventBroadcaster(buffer_size=500)
web_ui = WebUI(broadcaster)

_evolution_task: asyncio.Task | None = None
_daemon_monitor_stop: threading.Event | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _evolution_task, _daemon_monitor_stop

    config = app_state.get_config()
    mode = config["mode"]
    daemon_enabled = config["daemon_enabled"]

    if mode != "manual":
        if mode == "orchestrator":
            from orchestrator import orchestrator_loop
            _evolution_task = asyncio.create_task(orchestrator_loop(web_ui, no_daemon=not daemon_enabled))
            web_ui.log_history("🔥 Orchestrator started (LLM-driven mode)", "success")
        else:
            from evolution_core import (
                main_loop, start_daemon, daemon_monitor_thread,
                PROMPTS_DIR, RESULTS_DIR as EVO_RESULTS_DIR,
            )
            os.makedirs(PROMPTS_DIR, exist_ok=True)
            os.makedirs(EVO_RESULTS_DIR, exist_ok=True)

            if daemon_enabled:
                start_daemon(
                    workers=config["daemon_workers"],
                    pairs=config["daemon_pairs"],
                )
                _daemon_monitor_stop = threading.Event()
                monitor = threading.Thread(
                    target=daemon_monitor_thread,
                    args=(web_ui, _daemon_monitor_stop),
                    daemon=True,
                )
                monitor.start()

            _evolution_task = asyncio.create_task(
                main_loop(web_ui, is_text_ui=False, no_daemon=not daemon_enabled)
            )
            web_ui.log_history("Evolution started (classic mode)", "success")
    elif mode == "manual" and daemon_enabled:
        from evolution_core import start_daemon
        start_daemon(
            workers=config["daemon_workers"],
            pairs=config["daemon_pairs"],
        )
        web_ui.log_history("Manual mode: daemon started, no evolution loop.", "info")
    else:
        web_ui.log_history("Evolution disabled.", "info")

    yield

    if _evolution_task and not _evolution_task.done():
        _evolution_task.cancel()
        try:
            await asyncio.wait_for(_evolution_task, timeout=10)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    if _daemon_monitor_stop:
        _daemon_monitor_stop.set()

    try:
        from evolution_core import stop_daemon
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

app.include_router(ratings_router)
app.include_router(matches_router)
app.include_router(evolution_router)
app.include_router(logs_router)
app.include_router(control_router)
app.include_router(bots_router)
app.include_router(pipeline_router)
app.include_router(prompts_router)

# ── Static files (production build) ──
STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        return FileResponse(STATIC_DIR / "index.html")
