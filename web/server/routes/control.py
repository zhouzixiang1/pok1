"""Control Panel endpoints — manual orchestrator tool triggering and state management."""

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WEB_DIR = PROJECT_ROOT / "web"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
sys.path.insert(0, str(WEB_DIR / "core"))

from server.state import app_state

router = APIRouter(prefix="/api/control", tags=["control"])


class ModeRequest(BaseModel):
    mode: str  # orchestrator | classic | manual


class ConfigRequest(BaseModel):
    model_config = {"strict": True}
    mode: str | None = None
    daemon_enabled: bool | None = None
    daemon_workers: int | None = None
    daemon_pairs: int | None = None

    @property
    def safe_updates(self) -> dict:
        """Filter out None values."""
        result = {}
        if self.mode is not None:
            result["mode"] = self.mode
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
    return app_state.update_config(**updates)


@router.post("/mode")
async def set_mode(req: ModeRequest):
    if req.mode not in ("orchestrator", "classic", "manual"):
        return {"error": "Invalid mode. Must be: orchestrator, classic, or manual"}
    app_state.set_mode(req.mode)
    return {"mode": req.mode}


@router.get("/status")
async def control_status():
    return app_state.to_dict()


@router.get("/decisions")
async def get_decisions(limit: int = 50):
    state = app_state.to_dict()
    decisions = state.get("decisions", [])
    return decisions[-limit:]


@router.post("/start")
async def start_evolution():
    if app_state.running:
        return {"error": "Evolution is already running"}

    from server.app import web_ui
    config = app_state.get_config()
    mode = config["mode"]

    app_state.set_running(True)

    if mode == "orchestrator":
        from orchestrator import orchestrator_loop
        asyncio.create_task(orchestrator_loop(web_ui, no_daemon=not config["daemon_enabled"]))
    elif mode == "classic":
        from evolution_core import main_loop
        asyncio.create_task(main_loop(web_ui, is_text_ui=False, no_daemon=not config["daemon_enabled"]))
    else:
        if config["daemon_enabled"]:
            from evolution_core import start_daemon
            start_daemon(workers=config["daemon_workers"], pairs=config["daemon_pairs"])
        app_state.set_running(False)
        return {"status": "daemon_started", "mode": mode}

    return {"status": "started", "mode": mode}


@router.post("/stop")
async def stop_evolution():
    app_state.set_running(False)
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
        return {"error": f"Unknown tool: {tool_name}. Available: {list(tools.keys())}"}

    try:
        result = await tools[tool_name]((req.args if req else None) or {})
        text = ""
        if isinstance(result, dict):
            content = result.get("content", [])
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text += item.get("text", "")

        app_state.add_decision(tool_name, text[:200])

        return {"tool": tool_name, "result": text}
    except Exception as e:
        return {"tool": tool_name, "error": str(e)}


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
        try:
            from evolution_core import stop_daemon
            stop_daemon()
        except Exception:
            pass
        await asyncio.sleep(2)

    loop = asyncio.get_event_loop()
    from reset import reset_evolution
    result = await loop.run_in_executor(None, reset_evolution)

    # Git commit
    import subprocess
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
    mode = config["mode"]
    app_state.set_running(True)

    from server.app import web_ui
    if mode == "orchestrator":
        from orchestrator import orchestrator_loop
        asyncio.create_task(orchestrator_loop(web_ui, no_daemon=not config["daemon_enabled"]))
        web_ui.log_history("Evolution reset complete. Orchestrator restarted.", "success")
    elif mode == "classic":
        from evolution_core import main_loop
        asyncio.create_task(main_loop(web_ui, is_text_ui=False, no_daemon=not config["daemon_enabled"]))
        web_ui.log_history("Evolution reset complete. Classic loop restarted.", "success")
    else:
        app_state.set_running(False)
        web_ui.log_history("Evolution reset complete. Manual mode — start daemon manually.", "info")

    return {"status": "reset_complete", "details": result}
