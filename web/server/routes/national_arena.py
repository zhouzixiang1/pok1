"""FastAPI control and observation surface for the local national Web Arena."""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from national_arena.manager import (
    ArenaConflict,
    ArenaError,
    ArenaInfrastructureError,
    ArenaNotFound,
    NationalArenaManager,
)
from national_arena.models import ACTIVE_ARENA_STATES
from server.operator_control import require_operator_mutation


router = APIRouter(prefix="/api/national-arena", tags=["national-arena"])


class ArenaCreateRequest(BaseModel):
    mode: Literal["external_tcp", "managed_bots"]
    host: str = "127.0.0.1"
    port: int | None = Field(default=None, ge=0, le=65_535)
    hands: int = Field(default=70, ge=1, le=70)
    action_timeout_seconds: float = Field(default=60.0, ge=0.05, le=60.0)
    official_action_delay: float = Field(default=0.30, ge=0.0, le=5.0)
    capacity_wait_seconds: float = Field(default=30.0, ge=0.05, le=300.0)
    managed_port_override: bool = False
    top_bot: str | None = None
    bottom_bot: str | None = None


def _epoch_access_state(request: Request, operation: str) -> dict:
    """Return the canonical read-only epoch projection without side effects."""

    try:
        from epoch_authority import require_policy_epoch_initialized

        state = require_policy_epoch_initialized(operation)
    except Exception as exc:
        state = getattr(exc, "state", None) or {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "epoch_authority_unavailable",
            "initialized": False,
            "operator_action": "inspect_epoch_authority",
            "operator_command": None,
        }
    lifespan_state = getattr(
        request.app.state,
        "national_arena_epoch_authority",
        None,
    )
    if (
        isinstance(lifespan_state, dict)
        and lifespan_state.get("initialized")
        and state.get("initialized")
        and lifespan_state.get("evaluation_epoch") == state.get("evaluation_epoch")
        and lifespan_state.get("reset_receipt_digest")
        == state.get("reset_receipt_digest")
    ):
        # Lifespan may carry the richer strict_epoch_projection, including an
        # active workflow identity.  Initialization evidence still comes from
        # the live guard above.
        state = {**state, **lifespan_state}
    return state


def _epoch_metadata(state: dict) -> dict:
    return {
        "evaluation_epoch": state.get("evaluation_epoch", "national_tcp_policy_v1"),
        "epoch_state": state.get("state", "epoch_authority_unavailable"),
        "epoch_reset_receipt_digest": state.get("reset_receipt_digest"),
        "epoch_authority": state,
        "epoch_initialized": bool(state.get("initialized")),
        "result_authority": "diagnostic_only",
        "affects_glicko": False,
        "official_exe_certification": False,
        "can_certify": False,
    }


def _epoch_headers(state: dict) -> dict[str, str]:
    """Carry the canonical authority on successful artifact GETs."""

    return {
        "X-Pok-Evaluation-Epoch": str(state.get("evaluation_epoch") or ""),
        "X-Pok-Epoch-State": str(state.get("state") or ""),
        "X-Pok-Epoch-Initialized": (
            "true" if state.get("initialized") else "false"
        ),
        "X-Pok-Result-Authority": "diagnostic_only",
    }


def _manager(
    request: Request,
    *,
    epoch: dict,
    required: bool = True,
) -> NationalArenaManager | None:
    manager = getattr(request.app.state, "national_arena_manager", None)
    if (
        not epoch.get("initialized")
        or not isinstance(manager, NationalArenaManager)
        or not manager.started
        or not manager.accepts_epoch_authority(epoch)
    ):
        if not required:
            return None
        raise HTTPException(status_code=503, detail="National Arena manager is not running")
    return manager


def _require_initialized_epoch(request: Request, operation: str) -> dict:
    state = _epoch_access_state(request, operation)
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


def _raise_http(exc: ArenaError) -> None:
    if isinstance(exc, ArenaNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ArenaConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ArenaInfrastructureError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/bots")
async def list_arena_bots(request: Request):
    epoch = _epoch_access_state(request, "national_arena.list_bots")
    manager = _manager(request, epoch=epoch, required=False)
    if manager is None:
        return {
            "bots": [],
            "selection_contract": "strict_epoch_unavailable",
            "selection_authority": "official_windows_exe",
            **_epoch_metadata(epoch),
        }
    try:
        return {
            "bots": manager.list_launchable_bots(),
            "selection_contract": "active_tagged_native_and_official_eligible",
            "selection_authority": "official_windows_exe",
            **_epoch_metadata(epoch),
        }
    except ArenaError as exc:
        _raise_http(exc)


@router.get("/health")
async def arena_health(request: Request):
    epoch = _epoch_access_state(request, "national_arena.health")
    manager = _manager(request, epoch=epoch, required=False)
    if manager is None:
        return {
            "ok": False,
            "active_session": None,
            "accepting_new_session": False,
            "session_count": 0,
            "compliance_oracle": "official_windows_exe",
            "resource_fence_held": False,
            "mutation_auth": "operator_control",
            **_epoch_metadata(epoch),
        }
    sessions = manager.list_sessions()
    active = [
        item for item in sessions if item.get("status") in ACTIVE_ARENA_STATES
    ]
    resource_fence_held = bool(
        active and active[0].get("resource_fence_held")
    )
    return {
        "ok": not resource_fence_held,
        "active_session": active[0] if active else None,
        "accepting_new_session": not active,
        "session_count": len(sessions),
        "compliance_oracle": "official_windows_exe",
        "resource_fence_held": resource_fence_held,
        "mutation_auth": "operator_control",
        **_epoch_metadata(epoch),
    }


@router.get("/sessions")
async def list_arena_sessions(request: Request):
    epoch = _epoch_access_state(request, "national_arena.list_sessions")
    manager = _manager(request, epoch=epoch, required=False)
    if manager is None:
        return {"sessions": [], **_epoch_metadata(epoch)}
    try:
        return {"sessions": manager.list_sessions(), **_epoch_metadata(epoch)}
    except ArenaError as exc:
        _raise_http(exc)


@router.post("/sessions", status_code=201)
async def create_arena_session(payload: ArenaCreateRequest, request: Request):
    require_operator_mutation(request, operation="national_arena.create_session")
    epoch = _require_initialized_epoch(request, "national_arena.create_session")
    manager = _manager(request, epoch=epoch)
    try:
        values = payload.model_dump()
        managed_port_override = bool(values.pop("managed_port_override", False))
        if values.get("mode") == "managed_bots" and not managed_port_override:
            values["port"] = None
        return await manager.create_session(**values)
    except ArenaError as exc:
        _raise_http(exc)


@router.get("/sessions/{session_id}")
async def get_arena_session(session_id: str, request: Request):
    epoch = _epoch_access_state(request, "national_arena.get_session")
    manager = _manager(request, epoch=epoch, required=False)
    if manager is None:
        return {
            "session": None,
            "requested_session_id": session_id,
            **_epoch_metadata(epoch),
        }
    try:
        return {**manager.get_session(session_id), "epoch_authority": epoch}
    except ArenaError as exc:
        _raise_http(exc)


@router.post("/sessions/{session_id}/start")
async def start_arena_session(session_id: str, request: Request):
    require_operator_mutation(request, operation="national_arena.start_session")
    epoch = _require_initialized_epoch(request, "national_arena.start_session")
    manager = _manager(request, epoch=epoch)
    try:
        return await manager.start_session(session_id)
    except ArenaError as exc:
        _raise_http(exc)


@router.post("/sessions/{session_id}/stop")
async def stop_arena_session(session_id: str, request: Request):
    require_operator_mutation(request, operation="national_arena.stop_session")
    epoch = _require_initialized_epoch(request, "national_arena.stop_session")
    manager = _manager(request, epoch=epoch)
    try:
        return await manager.stop_session(session_id)
    except ArenaError as exc:
        _raise_http(exc)


@router.get("/sessions/{session_id}/events/history")
async def arena_event_history(
    session_id: str,
    request: Request,
    after_event_id: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=5000),
):
    epoch = _epoch_access_state(request, "national_arena.event_history")
    manager = _manager(request, epoch=epoch, required=False)
    if manager is None:
        return {
            "events": [],
            "after_event_id": after_event_id,
            "high_watermark": 0,
            "next_after_event_id": after_event_id,
            **_epoch_metadata(epoch),
        }
    try:
        rows = await manager.read_events(
            session_id,
            after_event_id=after_event_id,
            limit=limit,
        )
        snapshot = manager.get_session(session_id)
        return {
            "events": rows,
            "after_event_id": after_event_id,
            "high_watermark": int(snapshot.get("last_event_id", 0) or 0),
            "next_after_event_id": int(rows[-1]["event_id"]) if rows else after_event_id,
            **_epoch_metadata(epoch),
        }
    except ArenaError as exc:
        _raise_http(exc)


@router.get("/sessions/{session_id}/events")
async def arena_event_stream(
    session_id: str,
    request: Request,
    after_event_id: int = Query(default=0, ge=0),
):
    epoch = _epoch_access_state(request, "national_arena.event_stream")
    manager = _manager(request, epoch=epoch, required=False)
    if manager is None:
        async def blocked_stream():
            yield {
                "id": "0",
                "event": "epoch_blocked",
                "data": json.dumps({
                    "session": None,
                    **_epoch_metadata(epoch),
                }, ensure_ascii=False),
            }
            yield {
                "id": "0",
                "event": "stream_closed",
                "data": json.dumps({"status": "epoch_not_initialized"}),
            }

        return EventSourceResponse(
            blocked_stream(),
            headers=_epoch_headers(epoch),
        )
    try:
        manager.get_session(session_id)
    except ArenaError as exc:
        _raise_http(exc)

    header_id = request.headers.get("last-event-id")
    header_cursor = int(header_id) if header_id and header_id.isdigit() else 0
    cursor = max(after_event_id, header_cursor)
    reconnecting = cursor > 0

    async def generate():
        nonlocal cursor
        if not reconnecting:
            snapshot = manager.get_session(session_id)
            cursor = int(snapshot.get("last_event_id", 0) or 0)
            yield {
                "id": str(cursor),
                "event": "snapshot",
                "data": json.dumps({
                    "session": snapshot,
                    "result_authority": "diagnostic_only",
                    "official_exe_certification": False,
                    "compliance_oracle": "official_windows_exe",
                    "can_certify": False,
                    "epoch_authority": epoch,
                }, ensure_ascii=False),
            }
        while not await request.is_disconnected():
            current = manager.get_session(session_id)
            if bool(current.get("finished_at")) and cursor >= int(
                current.get("last_event_id", 0) or 0
            ):
                yield {
                    "id": str(cursor),
                    "event": "stream_closed",
                    "data": json.dumps({"status": current.get("status")}),
                }
                break
            rows = await manager.wait_for_events(
                session_id,
                after_event_id=cursor,
                timeout=15.0,
            )
            if not rows:
                yield {"event": "ping", "data": "{}"}
                continue
            for row in rows:
                event_id = int(row.get("event_id", 0) or 0)
                if event_id <= cursor:
                    continue
                cursor = event_id
                yield {
                    "id": str(event_id),
                    "event": str(row.get("type") or "arena_event"),
                    "data": json.dumps(row, ensure_ascii=False),
                }

    return EventSourceResponse(generate(), headers=_epoch_headers(epoch))


@router.get("/sessions/{session_id}/wire/history")
async def arena_wire_history(
    session_id: str,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=1000, ge=1, le=5000),
):
    epoch = _epoch_access_state(request, "national_arena.wire_history")
    manager = _manager(request, epoch=epoch, required=False)
    if manager is None:
        return {
            "records": [],
            "after_sequence": after_sequence,
            "complete": True,
            **_epoch_metadata(epoch),
        }
    try:
        rows = await manager.read_wire_async(
            session_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        session = manager.get_session(session_id)
        return {
            "records": rows,
            "after_sequence": after_sequence,
            "complete": bool(session.get("wire_log_complete", True)),
            **_epoch_metadata(epoch),
        }
    except ArenaError as exc:
        _raise_http(exc)


@router.get("/sessions/{session_id}/thp")
async def download_arena_thp(session_id: str, request: Request):
    epoch = _epoch_access_state(request, "national_arena.download_thp")
    manager = _manager(request, epoch=epoch, required=False)
    if manager is None:
        return {
            "artifact": None,
            "requested_session_id": session_id,
            **_epoch_metadata(epoch),
        }
    try:
        path = manager.artifact_path(session_id, "thp")
    except ArenaError as exc:
        _raise_http(exc)
    return FileResponse(
        path,
        media_type="text/plain; charset=gb2312",
        filename=f"{session_id}.txt",
        headers=_epoch_headers(epoch),
    )


@router.get("/sessions/{session_id}/artifacts/{artifact_key}")
async def download_arena_artifact(
    session_id: str,
    artifact_key: str,
    request: Request,
):
    epoch = _epoch_access_state(request, "national_arena.download_artifact")
    manager = _manager(request, epoch=epoch, required=False)
    if manager is None:
        return {
            "artifact": None,
            "artifact_key": artifact_key,
            "requested_session_id": session_id,
            **_epoch_metadata(epoch),
        }
    try:
        path = manager.artifact_path(session_id, artifact_key)
    except ArenaError as exc:
        _raise_http(exc)
    return FileResponse(
        path,
        media_type="text/plain; charset=utf-8",
        filename=path.name,
        headers=_epoch_headers(epoch),
    )
