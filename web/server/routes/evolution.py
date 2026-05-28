"""Evolution SSE stream and state endpoints."""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["evolution"])


@router.get("/evolution/stream")
async def evolution_stream():
    """SSE endpoint for real-time evolution events."""
    from sse_starlette.sse import EventSourceResponse
    from server.app import broadcaster

    cid, queue = broadcaster.add_client()

    async def generate():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield event
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        except asyncio.CancelledError:
            pass
        finally:
            broadcaster.remove_client(cid)

    return EventSourceResponse(generate())


@router.get("/evolution/state")
async def evolution_state():
    """Current state snapshot for initial load."""
    from server.app import web_ui
    return web_ui.get_state()
