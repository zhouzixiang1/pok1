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
sys.path.insert(0, str(WEB_DIR / "core"))

from server.state import app_state

router = APIRouter(prefix="/api/control", tags=["control"])
_last_status_sync_correction: tuple | None = None
_SENSITIVE_ARG_MARKERS = ("password", "token", "secret", "key", "credential", "cookie", "auth")


def _control_log(event_type: str, severity: str, message: str, data: dict | None = None) -> None:
    """Best-effort structured logging for control-plane actions."""
    try:
        from system_log import log_system_event
        log_system_event(event_type, severity, message, data or {})
    except Exception:
        pass


def _summarize_tool_args(args: dict | None) -> dict:
    """Return a compact, non-secret argument summary for system events."""
    if not isinstance(args, dict):
        return {"arg_type": type(args).__name__}
    summary = {}
    for key, value in args.items():
        key_s = str(key)
        key_l = key_s.lower()
        if any(marker in key_l for marker in _SENSITIVE_ARG_MARKERS):
            summary[key_s] = "<redacted>"
        elif isinstance(value, (int, float, bool)) or value is None:
            summary[key_s] = value
        elif isinstance(value, str):
            summary[key_s] = value if len(value) <= 160 else value[:157] + "..."
        elif isinstance(value, list):
            summary[key_s] = {"type": "list", "len": len(value)}
        elif isinstance(value, dict):
            summary[key_s] = {"type": "dict", "keys": sorted(map(str, value.keys()))[:12]}
        else:
            summary[key_s] = {"type": type(value).__name__}
    return summary


def _sync_evolution_fields(state: dict) -> dict:
    """Overlay cheap authoritative evolution fields for status reads.

    AppState is initialized from the latest completed tag, but the active
    pipeline may skip ahead because bare commits, abandoned versions, or a
    resume checkpoint reserve a later target. Keep /api/control/status aligned
    with the files the orchestrator actually uses, without mutating AppState
    from a read-only endpoint.
    """
    global _last_status_sync_correction
    before = (
        state.get("current_v"),
        state.get("next_v"),
        state.get("generation_count"),
    )
    try:
        from evolution_core import (
            find_current_v,
            find_max_committed_v,
            read_pipeline_checkpoint,
        )

        current_v = int(find_current_v())
        max_committed_v = int(find_max_committed_v())
        checkpoint = read_pipeline_checkpoint() or {}
        active_generation = None

        if checkpoint.get("next_v") is not None and checkpoint.get("stage") not in (None, "archived"):
            next_v = int(checkpoint["next_v"])
            generation_attempt = int(checkpoint.get("generation_attempt") or 0)
            audit_attempt = int(checkpoint.get("audit_attempt") or 0)
            precommit_attempt = int(checkpoint.get("precommit_attempt") or 0)
            active_generation = {
                "next_v": next_v,
                "source_v": checkpoint.get("source_v"),
                "stage": checkpoint.get("stage"),
                "run_id": checkpoint.get("run_id") or f"{next_v}#{generation_attempt}",
                "attempt": {
                    "generation": generation_attempt,
                    "audit": audit_attempt,
                    "precommit": precommit_attempt,
                },
            }
        else:
            next_v = max(current_v, max_committed_v) + 1

        state["current_v"] = current_v
        state["next_v"] = next_v
        state["generation_count"] = current_v
        state["active_generation"] = active_generation

        after = (current_v, next_v, current_v)
        if before != after:
            key = (before, after, active_generation["stage"] if active_generation else None)
            if key != _last_status_sync_correction:
                _last_status_sync_correction = key
                try:
                    from system_log import log_system_event
                    log_system_event(
                        "control.status_sync_corrected", "info",
                        f"Control status corrected from {before} to {after}",
                        {
                            "before": before,
                            "after": after,
                            "active_generation": active_generation,
                            "max_committed_v": max_committed_v,
                        },
                    )
                except Exception:
                    pass
    except Exception as exc:
        state["status_sync_error"] = str(exc)[:200]
    return state


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
    return _sync_evolution_fields(app_state.to_dict())


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
            await asyncio.wait_for(task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5)
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
    args = (req.args if req else None) or {}
    arg_summary = _summarize_tool_args(args)
    if tool_name not in tools:
        _control_log(
            "control.tool_unknown", "warn",
            f"Unknown control tool requested: {tool_name}",
            {"tool": tool_name, "args": arg_summary, "available_count": len(tools)},
        )
        raise HTTPException(status_code=404, detail=f"Unknown tool: {tool_name}. Available: {list(tools.keys())}")

    _control_log(
        "control.tool_requested", "info",
        f"Control tool requested: {tool_name}",
        {"tool": tool_name, "args": arg_summary},
    )
    try:
        result = await tools[tool_name](args)
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

        _control_log(
            "control.tool_succeeded", "success",
            f"Control tool succeeded: {tool_name}",
            {"tool": tool_name, "result_chars": len(text), "decision_preview": text[:200]},
        )
        return {"tool": tool_name, "result": text}
    except KeyError as e:
        _control_log(
            "control.tool_failed", "error",
            f"Control tool failed: {tool_name} missing parameter {e}",
            {"tool": tool_name, "error_type": "KeyError", "error": str(e), "args": arg_summary},
        )
        raise HTTPException(status_code=400, detail=f"Missing parameter: {e}")
    except (TypeError, ValueError) as e:
        _control_log(
            "control.tool_failed", "error",
            f"Control tool failed: {tool_name} invalid parameter",
            {"tool": tool_name, "error_type": type(e).__name__, "error": str(e), "args": arg_summary},
        )
        raise HTTPException(status_code=400, detail=f"Invalid parameter: {e}")
    except Exception as e:
        _control_log(
            "control.tool_failed", "error",
            f"Control tool failed: {tool_name}",
            {"tool": tool_name, "error_type": type(e).__name__, "error": str(e), "args": arg_summary},
        )
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
    try:
        from system_log import log_system_event
        log_system_event(
            "control.session_cleared", "warn",
            "Control API cleared orchestrator session",
            {"existed": existed},
        )
    except Exception:
        pass
    return {"cleared": existed, "message": "Session reset. Next Orchestrator start will begin a new conversation."}


# ── Evolution Reset ──

@router.post("/reset")
async def reset_evolution_endpoint():
    """Reset evolution to baseline (v1-v6), then auto-restart."""
    if app_state.running:
        app_state.request_shutdown()
        task = app_state.stop_running()
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5)
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
