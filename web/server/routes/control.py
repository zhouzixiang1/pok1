"""Control Panel endpoints — manual orchestrator tool triggering and state management."""

import asyncio
import json
import sys
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WEB_DIR = PROJECT_ROOT / "web"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
sys.path.insert(0, str(WEB_DIR / "core"))

from server.state import app_state

router = APIRouter(prefix="/api/control", tags=["control"])


async def _run_with_cleanup(coro):
    """Run an evolution coroutine, ensuring app_state.running is cleared on exit."""
    try:
        await coro
    finally:
        app_state.set_running(False)


class ConfigRequest(BaseModel):
    model_config = {"strict": True}
    daemon_enabled: bool | None = None
    daemon_workers: int | None = None
    daemon_pairs: int | None = None

    @property
    def safe_updates(self) -> dict:
        """Filter out None values."""
        result = {}
        if self.daemon_enabled is not None:
            result["daemon_enabled"] = self.daemon_enabled
        if self.daemon_workers is not None:
            result["daemon_workers"] = self.daemon_workers
        if self.daemon_pairs is not None:
            result["daemon_pairs"] = self.daemon_pairs
        return result


class ToolRequest(BaseModel):
    tool_name: str = ""
    args: dict = {}


_tool_map: dict[str, Any] | None = None


def _get_tool_map() -> dict[str, Any]:
    global _tool_map
    if _tool_map is None:
        from tools import all_tools
        _tool_map = {t.name: t.handler for t in all_tools}
    return _tool_map


@router.get("/config")
async def get_config():
    return app_state.get_config()


@router.put("/config")
async def set_config(req: ConfigRequest):
    updates = req.safe_updates
    if not updates:
        return app_state.get_config()
    was_enabled = app_state.daemon_enabled
    result = app_state.update_config(**updates)
    if "daemon_enabled" in updates and updates["daemon_enabled"] != was_enabled:
        if updates["daemon_enabled"]:
            from evolution_core import start_daemon
            start_daemon(workers=app_state.daemon_workers, pairs=app_state.daemon_pairs)
        else:
            from evolution_core import stop_daemon
            stop_daemon()
    return result


@router.get("/status")
async def control_status():
    return app_state.to_dict()


@router.get("/decisions")
async def get_decisions(limit: int = 50):
    state = app_state.to_dict()
    decisions = state.get("decisions", [])
    if limit <= 0:
        return []
    return decisions[-limit:]


@router.post("/start")
async def start_evolution():
    if not app_state.try_set_running(True):
        raise HTTPException(status_code=409, detail="Evolution is already running")

    from server.app import web_ui
    web_ui._broadcaster.clear()
    config = app_state.get_config()

    from shutdown_manager import ShutdownManager
    shutdown_mgr = ShutdownManager(grace_period=15.0)
    app_state.set_shutdown_mgr(shutdown_mgr)

    from orchestrator import orchestrator_loop
    task = asyncio.create_task(_run_with_cleanup(orchestrator_loop(
        web_ui, shutdown_mgr=shutdown_mgr, no_daemon=not config["daemon_enabled"],
        daemon_workers=config["daemon_workers"], daemon_pairs=config["daemon_pairs"])))
    app_state.set_task(task)

    return {"status": "started", "mode": "orchestrator"}


@router.post("/stop")
async def stop_evolution():
    app_state.request_shutdown()
    task = app_state.stop_running()
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    try:
        from evolution_core import stop_daemon
        stop_daemon()
    except Exception:
        pass
    return {"status": "stopped"}


@router.post("/tool/{tool_name}")
async def call_tool(tool_name: str, req: ToolRequest = Body(default=None)):
    tools = _get_tool_map()
    if tool_name not in tools:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {tool_name}. Available: {list(tools.keys())}")

    try:
        result = await tools[tool_name]((req.args if req else None) or {})
        text = ""
        if isinstance(result, dict):
            content = result.get("content", [])
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text += item.get("text", "")

        app_state.add_decision(tool_name, text[:200])

        # Post-tool state sync
        if tool_name == "start_daemon":
            app_state.update_config(daemon_enabled=True)
        elif tool_name == "stop_daemon":
            app_state.update_config(daemon_enabled=False)

        return {"tool": tool_name, "result": text}
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing parameter: {e}")
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid parameter: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tools")
async def list_tools():
    tools = _get_tool_map()
    return {"tools": list(tools.keys())}


# ── Orchestrator Session Management ──

ORCHESTRATOR_SESSION_FILE = PROJECT_ROOT / "web" / "core" / "results" / "orchestrator_session.json"


@router.get("/orchestrator/session")
async def get_orchestrator_session():
    """Return current Orchestrator session ID (if any)."""
    if not ORCHESTRATOR_SESSION_FILE.exists():
        return {"session_id": None, "active": False}
    try:
        import json as _json
        data = _json.loads(ORCHESTRATOR_SESSION_FILE.read_text())
        session_id = data.get("session_id")
        return {"session_id": session_id, "active": bool(session_id)}
    except Exception:
        return {"session_id": None, "active": False}


@router.delete("/orchestrator/session")
async def clear_orchestrator_session():
    """Delete the Orchestrator session file — forces a fresh conversation on next startup."""
    existed = ORCHESTRATOR_SESSION_FILE.exists()
    ORCHESTRATOR_SESSION_FILE.unlink(missing_ok=True)
    return {"cleared": existed, "message": "Session reset. Next Orchestrator start will begin a new conversation."}


# ── Evolution Reset ──

@router.post("/reset")
async def reset_evolution_endpoint():
    """Reset evolution to baseline (v1-v6), then auto-restart."""
    if app_state.running:
        app_state.set_running(False)
        task = None
        with app_state._lock:
            task = app_state._evolution_task
            app_state._evolution_task = None
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        try:
            from evolution_core import stop_daemon
            stop_daemon()
        except Exception:
            pass

    loop = asyncio.get_running_loop()
    from reset import reset_evolution
    result = await loop.run_in_executor(None, reset_evolution)

    # Git commit
    try:
        subprocess.run(["git", "add", "-A"], cwd=str(PROJECT_ROOT), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "chore: reset evolution to baseline (v1-v6)"],
            cwd=str(PROJECT_ROOT), check=True, capture_output=True,
        )
    except subprocess.CalledProcessError:
        pass

    # Auto-restart
    config = app_state.get_config()

    from server.app import web_ui
    web_ui._broadcaster.clear()
    from orchestrator import orchestrator_loop
    from shutdown_manager import ShutdownManager

    if not app_state.try_set_running(True):
        return {"status": "reset_complete", "warning": "Orchestrator already running — restart skipped"}

    shutdown_mgr = ShutdownManager(grace_period=15.0)
    app_state.set_shutdown_mgr(shutdown_mgr)

    task = asyncio.create_task(_run_with_cleanup(orchestrator_loop(
        web_ui, shutdown_mgr=shutdown_mgr, no_daemon=not config["daemon_enabled"],
        daemon_workers=config["daemon_workers"], daemon_pairs=config["daemon_pairs"])))
    app_state.set_task(task)
    web_ui.log_history("Evolution reset complete. Orchestrator restarted.", "success")

    return {"status": "reset_complete", "details": result}
