"""Evolution SSE stream and state endpoints."""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api", tags=["evolution"])


@router.get("/evolution/stream")
async def evolution_stream(request: Request):
    """SSE endpoint for real-time evolution events."""
    from sse_starlette.sse import EventSourceResponse
    from server.app import broadcaster

    cid, queue = broadcaster.add_client()

    async def generate():
        try:
            while True:
                # Cooperative disconnect check: closes the half-open/proxy
                # case that sse-starlette's internal _listen_for_disconnect
                # cannot detect. Race with sse-starlette's own receive() is
                # benign — both paths lead to cleanup.
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5)
                    yield event
                except asyncio.TimeoutError:
                    # sse-starlette sends its own ping every 15s; no need
                    # to duplicate keep-alive from the generator.
                    continue
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
