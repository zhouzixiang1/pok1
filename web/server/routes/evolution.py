"""Evolution SSE stream and state endpoints."""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api", tags=["evolution"])


def _epoch_projection() -> dict:
    """Return a complete read-only epoch view without consulting old state."""

    try:
        from epoch_authority import strict_epoch_projection

        return strict_epoch_projection()
    except Exception as exc:
        return {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "epoch_authority_unavailable",
            "initialized": False,
            "reset_receipt_valid": False,
            "reset_receipt_digest": None,
            "reset_receipt_issues": [
                f"canonical_epoch_projection_unavailable:{type(exc).__name__}"
            ],
            "current_v": 0,
            "next_v": 0,
            "active_bots": [],
            "active_generation": None,
        }


def _epoch_metadata(epoch: dict) -> dict:
    return {
        "evaluation_epoch": epoch.get("evaluation_epoch", "national_tcp_policy_v1"),
        "epoch_state": epoch.get("state", "epoch_authority_unavailable"),
        "epoch_initialized": bool(epoch.get("initialized")),
        "epoch_reset_receipt_digest": epoch.get("reset_receipt_digest"),
        "current_v": int(epoch.get("current_v") or 0),
        "next_v": int(epoch.get("next_v") or 0),
    }


@router.get("/evolution/stream")
async def evolution_stream(request: Request):
    """SSE endpoint for real-time evolution events."""
    from sse_starlette.sse import EventSourceResponse
    from server.app import broadcaster

    epoch = _epoch_projection()
    if not epoch.get("initialized"):
        # Do not subscribe to or replay the in-memory ring while its strict
        # epoch authority is unavailable.  This is especially important for a
        # dashboard process started against pre-reset v155 debris.
        async def blocked_stream():
            yield {
                "event": "epoch_blocked",
                "data": json.dumps(_epoch_metadata(epoch)),
            }
            yield {
                "event": "stream_closed",
                "data": json.dumps({"reason": "policy_epoch_not_initialized"}),
            }

        return EventSourceResponse(blocked_stream())

    cid, queue = broadcaster.add_client()
    connection_receipt_digest = epoch.get("reset_receipt_digest")

    async def generate():
        try:
            while True:
                # Cooperative disconnect check: closes the half-open/proxy
                # case that sse-starlette's internal _listen_for_disconnect
                # cannot detect. Race with sse-starlette's own receive() is
                # benign — both paths lead to cleanup.
                if await request.is_disconnected():
                    break
                live_epoch = _epoch_projection()
                if (
                    not live_epoch.get("initialized")
                    or not connection_receipt_digest
                    or live_epoch.get("reset_receipt_digest")
                    != connection_receipt_digest
                ):
                    yield {
                        "event": "epoch_blocked",
                        "data": json.dumps(_epoch_metadata(live_epoch)),
                    }
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
    from evolution_infra import PIPELINE_STATE_FILE, RESULTS_DIR
    from server.routes._helpers import (
        load_strict_pipeline_checkpoint,
        load_strict_strength_snapshot,
    )

    epoch = _epoch_projection()
    state = dict(web_ui.get_state())
    state.update(_epoch_metadata(epoch))
    if not epoch.get("initialized"):
        # WebUI is process memory, not version or result authority.  Its cost
        # counters and status may have been loaded before the reset receipt was
        # validated, so expose only a typed stopped projection here.
        state.update({
            "status": f"Stopped: {epoch.get('state', 'epoch_authority_unavailable')}",
            "is_working": False,
            "running": False,
            "metrics": {},
            "ratings": [],
            "active_bots": [],
            "pipeline_stage": None,
            "grand_cost_total": 0.0,
            "gen_cost_total": 0.0,
            "generation_cost_identity": None,
            "generation_cost_policy": None,
        })
        return state

    strength = load_strict_strength_snapshot(RESULTS_DIR)
    checkpoint = load_strict_pipeline_checkpoint(RESULTS_DIR, PIPELINE_STATE_FILE)
    if strength.get("available") is True:
        state["ratings"] = list(strength.get("selection_rows") or [])
        state["active_bots"] = list(strength.get("active_bots") or [])
    else:
        state["ratings"] = []
        state["active_bots"] = []
    state["pipeline_stage"] = checkpoint.get("stage") if checkpoint else None
    return state
