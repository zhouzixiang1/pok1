"""FastAPI control and observation surface for the local national Web Arena."""

from __future__ import annotations

import json
import hmac
import ipaddress
import os
from typing import Literal
from urllib.parse import urlsplit

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


def _manager(request: Request) -> NationalArenaManager:
    manager = getattr(request.app.state, "national_arena_manager", None)
    if not isinstance(manager, NationalArenaManager):
        raise HTTPException(status_code=503, detail="National Arena manager is not running")
    return manager


def _raise_http(exc: ArenaError) -> None:
    if isinstance(exc, ArenaNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ArenaConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ArenaInfrastructureError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


def _require_control(request: Request) -> None:
    configured = os.environ.get("POK_ARENA_CONTROL_TOKEN", "")
    supplied = request.headers.get("x-arena-token", "")
    if configured:
        if not supplied or not hmac.compare_digest(configured, supplied):
            raise HTTPException(status_code=403, detail="invalid Arena control token")
        return
    client_host = request.client.host if request.client else ""
    try:
        loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise HTTPException(
            status_code=403,
            detail="remote Arena mutation requires POK_ARENA_CONTROL_TOKEN",
        )
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    if origin:
        if urlsplit(origin).netloc == host:
            return
        raise HTTPException(status_code=403, detail="cross-origin Arena mutation rejected")
    return


@router.get("/bots")
async def list_arena_bots(request: Request):
    manager = _manager(request)
    try:
        return {
            "bots": manager.list_launchable_bots(),
            "selection_contract": "active_tagged_native_and_official_eligible",
            "result_authority": "diagnostic_only",
            "selection_authority": "official_windows_exe",
        }
    except ArenaError as exc:
        _raise_http(exc)


@router.get("/health")
async def arena_health(request: Request):
    manager = _manager(request)
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
        "result_authority": "diagnostic_only",
        "official_exe_certification": False,
        "compliance_oracle": "official_windows_exe",
        "can_certify": False,
        "resource_fence_held": resource_fence_held,
        "mutation_auth": (
            "control_token" if os.environ.get("POK_ARENA_CONTROL_TOKEN") else "loopback_only"
        ),
    }


@router.get("/sessions")
async def list_arena_sessions(request: Request):
    manager = _manager(request)
    try:
        return {"sessions": manager.list_sessions()}
    except ArenaError as exc:
        _raise_http(exc)


@router.post("/sessions", status_code=201)
async def create_arena_session(payload: ArenaCreateRequest, request: Request):
    _require_control(request)
    manager = _manager(request)
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
    manager = _manager(request)
    try:
        return manager.get_session(session_id)
    except ArenaError as exc:
        _raise_http(exc)


@router.post("/sessions/{session_id}/start")
async def start_arena_session(session_id: str, request: Request):
    _require_control(request)
    manager = _manager(request)
    try:
        return await manager.start_session(session_id)
    except ArenaError as exc:
        _raise_http(exc)


@router.post("/sessions/{session_id}/stop")
async def stop_arena_session(session_id: str, request: Request):
    _require_control(request)
    manager = _manager(request)
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
    manager = _manager(request)
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
        }
    except ArenaError as exc:
        _raise_http(exc)


@router.get("/sessions/{session_id}/events")
async def arena_event_stream(
    session_id: str,
    request: Request,
    after_event_id: int = Query(default=0, ge=0),
):
    manager = _manager(request)
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

    return EventSourceResponse(generate())


@router.get("/sessions/{session_id}/wire/history")
async def arena_wire_history(
    session_id: str,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=1000, ge=1, le=5000),
):
    manager = _manager(request)
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
        }
    except ArenaError as exc:
        _raise_http(exc)


@router.get("/sessions/{session_id}/thp")
async def download_arena_thp(session_id: str, request: Request):
    manager = _manager(request)
    try:
        path = manager.artifact_path(session_id, "thp")
    except ArenaError as exc:
        _raise_http(exc)
    return FileResponse(
        path,
        media_type="text/plain; charset=gb2312",
        filename=f"{session_id}.txt",
    )


@router.get("/sessions/{session_id}/artifacts/{artifact_key}")
async def download_arena_artifact(
    session_id: str,
    artifact_key: str,
    request: Request,
):
    manager = _manager(request)
    try:
        path = manager.artifact_path(session_id, artifact_key)
    except ArenaError as exc:
        _raise_http(exc)
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=path.name)
