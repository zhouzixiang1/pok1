"""Read-only status plus explicit, authenticated operator controls."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WEB_DIR = PROJECT_ROOT / "web"
RESULTS_DIR = WEB_DIR / "core" / "results"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(WEB_DIR / "core"))

from server.state import app_state
from server.operator_control import CONTROL_TOKEN_HEADER, require_operator_mutation

router = APIRouter(prefix="/api/control", tags=["control"])

# This is deliberately an explicit HTTP capability registry, not a projection
# of the Orchestrator's MCP ``all_tools`` registry.  Adding an MCP tool can
# therefore never make it remotely callable through the dashboard.
_CONTROL_CAPABILITIES = (
    {"id": "read_status", "method": "GET", "path": "/api/control/status", "mutation": False},
    {"id": "read_health", "method": "GET", "path": "/api/control/health", "mutation": False},
    {"id": "read_config", "method": "GET", "path": "/api/control/config", "mutation": False},
    {"id": "read_decisions", "method": "GET", "path": "/api/control/decisions", "mutation": False},
    {
        "id": "read_orchestrator_session",
        "method": "GET",
        "path": "/api/control/orchestrator/session",
        "mutation": False,
    },
    {
        "id": "start_evolution",
        "method": "POST",
        "path": "/api/control/start",
        "mutation": True,
        "requires_epoch": True,
    },
    {
        "id": "stop_evolution",
        "method": "POST",
        "path": "/api/control/stop",
        "mutation": True,
        "requires_epoch": False,
    },
    {
        "id": "update_config",
        "method": "PUT",
        "path": "/api/control/config",
        "mutation": True,
        "requires_epoch": True,
    },
    {
        "id": "clear_orchestrator_session",
        "method": "DELETE",
        "path": "/api/control/orchestrator/session",
        "mutation": True,
        "requires_epoch": True,
    },
)


def _epoch_access_state(operation: str) -> dict:
    """Return canonical launch state, failing closed on authority errors."""

    try:
        from epoch_authority import require_policy_epoch_initialized

        return require_policy_epoch_initialized(operation)
    except Exception as exc:
        return getattr(exc, "state", None) or {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "epoch_authority_unavailable",
            "initialized": False,
            "operator_action": "inspect_epoch_authority",
            "operator_command": None,
        }


def _require_initialized_epoch(operation: str) -> dict:
    """Translate the canonical epoch launch guard into an HTTP 409.

    This helper intentionally does not persist an event on denial because the
    canonical event ledger still belongs to the retired epoch.
    """

    state = _epoch_access_state(operation)
    if not state.get("initialized"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "policy_epoch_not_initialized",
                "operation": operation,
                "epoch": state,
            },
        ) from None
    return state


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _read_pid_info(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "pid": None, "alive": False}
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    data: dict[str, Any] = {"exists": True, "raw_format": "json"}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            data.update(parsed)
        else:
            data["pid"] = parsed
            data["raw_format"] = "scalar"
    except Exception:
        data["raw_format"] = "plain"
        try:
            data["pid"] = int(raw)
        except Exception:
            data["pid"] = None
            data["parse_error"] = raw[:120]
    try:
        data["pid"] = int(data.get("pid")) if data.get("pid") is not None else None
    except Exception:
        data["pid"] = None
    data["alive"] = _pid_alive(data.get("pid"))
    hb = data.get("last_heartbeat")
    if hb is not None:
        try:
            data["heartbeat_age_sec"] = round(time.time() - float(hb), 2)
        except Exception:
            data["heartbeat_age_sec"] = None
    return data


def _read_pipeline_health(status: dict) -> dict:
    """Project pipeline health only from canonical strict epoch status.

    The old implementation reopened ``pipeline_state.json`` and rendered raw
    fields even when that file belonged to the retired epoch.  The status
    projection has already validated the strict checkpoint envelope; health
    must not invent a second checkpoint reader or expose unbound state.
    """

    authority = "strict_epoch_projection"
    if status.get("status_sync_error"):
        return {
            "exists": False,
            "stage": None,
            "authority": authority,
            "error": "canonical_epoch_projection_unavailable",
        }

    active = status.get("active_generation")
    ignored = status.get("ignored_checkpoint")
    if not isinstance(active, dict):
        return {
            "exists": False,
            "stage": None,
            "authority": authority,
            "epoch_state": status.get("epoch_state"),
            "blocked": not bool(status.get("epoch_initialized")),
            "ignored_checkpoint": ignored if isinstance(ignored, dict) else None,
        }

    # Only an initialized projection with a validated active generation may
    # reopen the live checkpoint for recovery diagnostics.  Revalidate the
    # bytes read now so a concurrent replacement cannot inherit the earlier
    # projection's authority.
    if not status.get("epoch_initialized"):
        return {
            "exists": False,
            "stage": None,
            "authority": authority,
            "epoch_state": status.get("epoch_state"),
            "blocked": True,
            "error": "active_generation_without_initialized_epoch",
        }
    try:
        from checkpoint_schema import (
            checkpoint_epoch_errors,
            live_policy_epoch_reset_receipt_errors,
        )
        from evolution_infra import PROJECT_ROOT as CORE_PROJECT_ROOT
        from evolution_infra import read_pipeline_checkpoint
        from pipeline_recovery import checkpoint_recovery_diagnostics

        checkpoint = read_pipeline_checkpoint()
        issues = checkpoint_epoch_errors(checkpoint)
        if not issues:
            issues.extend(
                live_policy_epoch_reset_receipt_errors(
                    checkpoint,
                    project_root=CORE_PROJECT_ROOT,
                )
            )
        expected = (
            active.get("next_v"),
            active.get("stage"),
            active.get("workflow_run_id"),
        )
        observed = (
            checkpoint.get("next_v") if isinstance(checkpoint, dict) else None,
            checkpoint.get("stage") if isinstance(checkpoint, dict) else None,
            checkpoint.get("workflow_run_id") if isinstance(checkpoint, dict) else None,
        )
        if issues or observed != expected:
            return {
                "exists": True,
                "stage": active.get("stage"),
                "authority": authority,
                "epoch_state": status.get("epoch_state"),
                "error": "strict_checkpoint_revalidation_failed",
                "issues": list(dict.fromkeys(map(str, issues))),
                "identity_changed": observed != expected,
            }
        recovery = checkpoint_recovery_diagnostics(checkpoint)
    except Exception as exc:
        return {
            "exists": True,
            "stage": active.get("stage"),
            "authority": authority,
            "epoch_state": status.get("epoch_state"),
            "error": f"strict_checkpoint_diagnostic_failed:{type(exc).__name__}",
        }

    attempt = active.get("attempt") if isinstance(active.get("attempt"), dict) else {}
    snapshot = {
        "exists": True,
        "authority": authority,
        "epoch_state": status.get("epoch_state"),
        "run_id": active.get("run_id"),
        "workflow_run_id": active.get("workflow_run_id"),
        "stage": active.get("stage"),
        "next_v": active.get("next_v"),
        "source_v": active.get("source_v"),
        "generation_attempt": attempt.get("generation"),
        "audit_attempt": attempt.get("audit"),
        "precommit_attempt": attempt.get("precommit"),
        "ignored_checkpoint": None,
        "recovery": recovery,
    }
    now = time.time()
    for source_key, target_key in (
        ("last_stage_change_ts", "last_stage_age_sec"),
        ("last_update_ts", "last_update_age_sec"),
    ):
        value = checkpoint.get(source_key)
        if value is not None:
            try:
                snapshot[target_key] = round(now - float(value), 2)
            except (TypeError, ValueError):
                snapshot[target_key] = None
    return snapshot


def _daemon_health_snapshot() -> dict:
    data = _read_pid_info(RESULTS_DIR / ".daemon_pid")
    try:
        from elo_daemon import HEARTBEAT_STALE_SEC
    except Exception:
        HEARTBEAT_STALE_SEC = 120
    data["heartbeat_stale_sec"] = HEARTBEAT_STALE_SEC
    hb_age = data.get("heartbeat_age_sec")
    data["heartbeat_stale"] = (
        hb_age is not None and hb_age > HEARTBEAT_STALE_SEC
    )
    return data


def _health_summary(status: dict) -> dict:
    task = app_state.task_snapshot()
    daemon = _daemon_health_snapshot()
    pipeline = _read_pipeline_health(status)
    issues = []
    if not status.get("running"):
        issues.append("evolution_not_running")
    if status.get("running") and (not task.get("present") or task.get("done")):
        issues.append("orchestrator_task_not_active")
    if status.get("running") and status.get("daemon_enabled"):
        if not daemon.get("alive"):
            issues.append("daemon_dead")
        elif daemon.get("heartbeat_stale"):
            issues.append("daemon_heartbeat_stale")
    if status.get("active_generation") and not pipeline.get("exists"):
        issues.append("active_generation_without_checkpoint")
    if pipeline.get("error"):
        issues.append("pipeline_checkpoint_unreadable")
    recovery = pipeline.get("recovery") if isinstance(pipeline.get("recovery"), dict) else {}
    for issue in recovery.get("issues") or []:
        issues.append(f"pipeline_{issue}")

    if not status.get("running"):
        overall = "stopped"
    elif issues:
        overall = "degraded"
    else:
        overall = "healthy"
    return {
        "overall": overall,
        "issues": issues,
        "status": status,
        "running": status.get("running"),
        "active_generation": status.get("active_generation"),
        "task": task,
        "daemon": daemon,
        "pipeline": pipeline,
        "checked_at": time.time(),
    }


def _sync_evolution_fields(state: dict) -> dict:
    """Overlay cheap authoritative evolution fields for status reads.

    AppState is initialized from the latest completed tag, but the active
    pipeline may skip ahead because bare commits, abandoned versions, or a
    resume checkpoint reserve a later target. Keep /api/control/status aligned
    with the files the orchestrator actually uses, without mutating AppState
    from a read-only endpoint.
    """
    try:
        from epoch_authority import (
            strict_epoch_projection,
            unpublished_candidate_versions,
        )

        epoch = strict_epoch_projection()
        current_v = int(epoch["current_v"])
        next_v = int(epoch["next_v"])
        generation_count = int(epoch["strict_generation_count"])
        active_generation = epoch["active_generation"]

        state["current_v"] = current_v
        state["next_v"] = next_v
        state["generation_count"] = generation_count
        state["active_generation"] = active_generation
        state["evaluation_epoch"] = epoch["evaluation_epoch"]
        state["epoch_state"] = epoch["state"]
        state["epoch_initialized"] = bool(epoch["initialized"])
        state["version_authority_high_water"] = int(
            epoch["version_authority_high_water"]
        )
        state["strict_generation_count"] = generation_count
        state["strict_published_versions"] = epoch["strict_published_versions"]
        state["active_bots"] = epoch["active_bots"]
        state["reset_receipt_valid"] = bool(epoch["reset_receipt_valid"])
        state["reset_receipt_issues"] = epoch["reset_receipt_issues"]
        state["operator_action"] = epoch["operator_action"]
        state["operator_command"] = epoch["operator_command"]
        state["ignored_checkpoint"] = epoch["ignored_checkpoint"]
        state["unpublished_candidate_versions"] = unpublished_candidate_versions()
    except Exception as exc:
        # Never fall back to AppState's bootstrap counters: those values may
        # have been initialized from a retired checkpoint or abandoned bot
        # directory.  Return a complete, explicitly unavailable projection so
        # browser clients can render a recovery state without guessing fields
        # or accidentally presenting stale version authority.
        state.update({
            "current_v": 0,
            "next_v": 0,
            "generation_count": 0,
            "active_generation": None,
            "evaluation_epoch": "national_tcp_policy_v1",
            "epoch_state": "epoch_authority_unavailable",
            "epoch_initialized": False,
            "version_authority_high_water": 0,
            "strict_generation_count": 0,
            "strict_published_versions": [],
            "active_bots": [],
            "reset_receipt_valid": False,
            "reset_receipt_issues": ["canonical_epoch_projection_unavailable"],
            "operator_action": "inspect_epoch_authority",
            "operator_command": None,
            "ignored_checkpoint": None,
            "unpublished_candidate_versions": [],
        })
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


@router.get("/config")
async def get_config():
    return app_state.get_config()


@router.put("/config")
async def set_config(req: ConfigRequest, request: Request):
    require_operator_mutation(request, operation="control_config_update")
    updates = req.safe_updates
    if not updates:
        return app_state.get_config()
    _require_initialized_epoch("control_config_update")
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


@router.get("/health")
async def control_health():
    """Return a single read-only health snapshot for observers/supervisors."""
    status = _sync_evolution_fields(app_state.to_dict())
    return _health_summary(status)


@router.get("/decisions")
async def get_decisions(limit: int = 50):
    state = app_state.to_dict()
    decisions = state.get("decisions", [])
    if limit <= 0:
        return []
    return decisions[-limit:]


@router.post("/start")
async def start_evolution(request: Request):
    require_operator_mutation(request, operation="control_start_evolution")
    _require_initialized_epoch("control_start_evolution")
    if not app_state.try_set_running(True):
        raise HTTPException(status_code=409, detail="Evolution is already running")

    from server.app import web_ui
    web_ui._broadcaster.clear()
    config = app_state.get_config()

    from shutdown_manager import ShutdownManager
    shutdown_mgr = ShutdownManager(grace_period=15.0)
    app_state.set_shutdown_mgr(shutdown_mgr)
    try:
        from llm_query import set_shutdown_manager
        set_shutdown_manager(shutdown_mgr)
    except Exception:
        pass

    from orchestrator import orchestrator_loop
    task = asyncio.create_task(_run_with_cleanup(orchestrator_loop(
        web_ui, shutdown_mgr=shutdown_mgr, no_daemon=not config["daemon_enabled"],
        daemon_workers=config["daemon_workers"], daemon_pairs=config["daemon_pairs"])))
    app_state.set_task(task)

    return {"status": "started", "mode": "orchestrator"}


@router.post("/stop")
async def stop_evolution(request: Request):
    require_operator_mutation(request, operation="control_stop_evolution")
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


@router.post("/tool/{tool_name}", status_code=410)
async def retired_tool_executor(tool_name: str):
    """Permanently retire the old arbitrary MCP-over-HTTP dispatcher."""

    raise HTTPException(
        status_code=410,
        detail={
            "code": "control_tool_executor_retired",
            "tool": tool_name,
            "message": "Use explicit read-only APIs or operator control endpoints.",
        },
    )


@router.get("/tools")
async def list_tools():
    epoch = _epoch_access_state("control_tools_catalog")
    initialized = bool(epoch.get("initialized"))
    capabilities = []
    for definition in _CONTROL_CAPABILITIES:
        capability = dict(definition)
        requires_epoch = bool(capability.pop("requires_epoch", False))
        enabled = not requires_epoch or initialized
        capability["enabled"] = enabled
        capability["blocked_reason"] = (
            None if enabled else "policy_epoch_not_initialized"
        )
        capabilities.append(capability)
    enabled = [item["id"] for item in capabilities if item["enabled"]]
    blocked = [item["id"] for item in capabilities if not item["enabled"]]
    return {
        "capabilities": capabilities,
        # Transitional aliases are capability IDs, never MCP tool names.
        "tools": [item["id"] for item in capabilities],
        "enabled_tools": enabled,
        "blocked_tools": blocked,
        "epoch_initialized": initialized,
        "epoch_state": epoch.get("state"),
        "operator_action": epoch.get("operator_action"),
        "operator_auth_required": True,
        "operator_auth_modes": ["loopback_same_origin", "control_token"],
        "operator_token_configured": bool(os.environ.get("POK_CONTROL_TOKEN")),
        "operator_token_header": CONTROL_TOKEN_HEADER,
    }


# ── Orchestrator Session Management ──

ORCHESTRATOR_SESSION_FILE = PROJECT_ROOT / "web" / "core" / "results" / "orchestrator_session.json"


@router.get("/orchestrator/session")
async def get_orchestrator_session():
    """Return current Orchestrator session ID (if any)."""
    epoch = _epoch_access_state("control_orchestrator_session_read")
    if not epoch.get("initialized"):
        # A retired session id is not resumable authority and must never be
        # rendered as active before the reset archives it.
        return {
            "session_id": None,
            "active": False,
            "blocked": True,
            "epoch_state": epoch.get("state"),
            "operator_action": epoch.get("operator_action"),
        }
    if not ORCHESTRATOR_SESSION_FILE.exists():
        return {"session_id": None, "active": False, "blocked": False}
    try:
        import json as _json
        data = _json.loads(ORCHESTRATOR_SESSION_FILE.read_text())
        session_id = data.get("session_id")
        return {
            "session_id": session_id,
            "active": bool(session_id),
            "blocked": False,
        }
    except Exception:
        return {"session_id": None, "active": False, "blocked": False}


@router.delete("/orchestrator/session")
async def clear_orchestrator_session(request: Request):
    """Delete the Orchestrator session file — forces a fresh conversation on next startup."""
    require_operator_mutation(
        request,
        operation="control_orchestrator_session_clear",
    )
    _require_initialized_epoch("control_orchestrator_session_clear")
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
