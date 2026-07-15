"""Evolution SSE stream and state endpoints."""

import asyncio
import json

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
    try:
        from epoch_authority import epoch_stream_authority_digest

        stream_authority_digest = epoch_stream_authority_digest(epoch)
    except Exception:
        stream_authority_digest = None
    return {
        "evaluation_epoch": epoch.get("evaluation_epoch", "national_tcp_policy_v1"),
        "epoch_state": epoch.get("state", "epoch_authority_unavailable"),
        "epoch_initialized": bool(epoch.get("initialized")),
        "epoch_reset_receipt_digest": epoch.get("reset_receipt_digest"),
        "stream_authority_digest": stream_authority_digest,
        "current_v": int(epoch.get("current_v") or 0),
        "next_v": int(epoch.get("next_v") or 0),
    }


def _handoff_projection(epoch: dict) -> dict:
    from server.routes._helpers import post_publication_handoff_projection

    return post_publication_handoff_projection(
        enabled=bool(epoch.get("initialized"))
    )


def _stable_stream_projection(max_attempts: int = 3):
    """Bracket handoff with one stable epoch so events never mix identities.

    A handoff revision is expected to advance while Archivist is running.  If
    the complete epoch is stable, the observed handoff projection is safe even
    if a later revision lands immediately afterwards.  Only an unstable epoch
    returns a ``None`` authority digest and may trigger the irreversible
    browser epoch fence.
    """

    from epoch_authority import epoch_stream_authority_digest
    from server.routes._helpers import stable_epoch_handoff_sample

    epoch, handoff, stable = stable_epoch_handoff_sample(
        _epoch_projection,
        _handoff_projection,
        max_attempts=max_attempts,
    )
    return (
        epoch,
        handoff,
        epoch_stream_authority_digest(epoch) if stable else None,
    )


@router.get("/evolution/stream")
async def evolution_stream(request: Request):
    """SSE endpoint for real-time evolution events."""
    from sse_starlette.sse import EventSourceResponse
    from server.app import broadcaster

    bound_before_sample = broadcaster.authority_identity()
    epoch, connection_handoff, connection_authority_digest = (
        _stable_stream_projection()
    )
    expected_authority_digest = str(
        request.query_params.get("authority") or ""
    )
    expected_identity_valid = bool(
        len(expected_authority_digest) == 64
        and all(char in "0123456789abcdef" for char in expected_authority_digest)
    )
    # Bind the process ring to the canonical observation even when this
    # particular browser arrived with a stale/malformed expectation. It never
    # subscribes below, but it also cannot leave future events in an old ring.
    binding_current = False
    if connection_authority_digest is not None or not epoch.get("initialized"):
        binding_current = broadcaster.compare_and_bind_authority(
            connection_authority_digest,
            expected_identity=bound_before_sample,
        )
    if (
        connection_authority_digest is None
        or not binding_current
        or not expected_identity_valid
        or expected_authority_digest != connection_authority_digest
    ):
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
                "data": json.dumps({
                    "reason": (
                        "stream_authority_mismatch"
                        if connection_authority_digest is not None
                        else "policy_epoch_not_initialized"
                    )
                }),
            }

        return EventSourceResponse(blocked_stream())

    try:
        cid, queue = broadcaster.add_client(connection_authority_digest)
    except ValueError:
        # Another request observed a different receipt between projection and
        # subscription. Reopen authority and fail closed instead of replaying a
        # ring owned by either snapshot.
        live_epoch = _epoch_projection()

        async def moved_stream():
            yield {
                "event": "epoch_blocked",
                "data": json.dumps(_epoch_metadata(live_epoch)),
            }

        return EventSourceResponse(moved_stream())

    async def generate():
        try:
            initial_epoch, initial_handoff, initial_digest = (
                _stable_stream_projection()
            )
            if initial_digest != connection_authority_digest:
                if initial_digest is not None or not initial_epoch.get("initialized"):
                    broadcaster.compare_and_bind_authority(
                        initial_digest,
                        expected_identity=connection_authority_digest,
                    )
                yield {
                    "event": "epoch_blocked",
                    "data": json.dumps(_epoch_metadata(initial_epoch)),
                }
                return
            connection_handoff = initial_handoff
            yield {
                "event": "post_publication_handoff",
                "data": json.dumps({
                    **connection_handoff,
                    "stream_authority_digest": connection_authority_digest,
                }),
            }
            last_handoff_digest = connection_handoff["projection_digest"]
            while True:
                # Cooperative disconnect check: closes the half-open/proxy
                # case that sse-starlette's internal _listen_for_disconnect
                # cannot detect. Race with sse-starlette's own receive() is
                # benign — both paths lead to cleanup.
                if await request.is_disconnected():
                    break
                live_epoch, live_handoff, live_authority_digest = (
                    _stable_stream_projection()
                )
                if (
                    live_authority_digest is None
                    or live_authority_digest != connection_authority_digest
                ):
                    if live_authority_digest is not None or not live_epoch.get("initialized"):
                        broadcaster.compare_and_bind_authority(
                            live_authority_digest,
                            expected_identity=connection_authority_digest,
                        )
                    yield {
                        "event": "epoch_blocked",
                        "data": json.dumps(_epoch_metadata(live_epoch)),
                    }
                    break
                if live_handoff.get("projection_digest") != last_handoff_digest:
                    last_handoff_digest = live_handoff["projection_digest"]
                    yield {
                        "event": "post_publication_handoff",
                        "data": json.dumps({
                            **live_handoff,
                            "stream_authority_digest": connection_authority_digest,
                        }),
                    }
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5)
                    # Publication can move while this coroutine is blocked in
                    # queue.get(). Reopen authority after wakeup and discard
                    # the queued row if it belonged to the preceding replay
                    # identity; otherwise one new-generation event could flash
                    # in an old controller immediately before the fence.
                    delivery_epoch, _delivery_handoff, delivery_authority_digest = (
                        _stable_stream_projection()
                    )
                    if delivery_authority_digest != connection_authority_digest:
                        if delivery_authority_digest is not None or not delivery_epoch.get("initialized"):
                            broadcaster.compare_and_bind_authority(
                                delivery_authority_digest,
                                expected_identity=connection_authority_digest,
                            )
                        yield {
                            "event": "epoch_blocked",
                            "data": json.dumps(_epoch_metadata(delivery_epoch)),
                        }
                        break
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

    epoch, handoff, stable_authority_digest = _stable_stream_projection()
    if epoch.get("initialized") and stable_authority_digest is None:
        epoch = {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "epoch_authority_unavailable",
            "initialized": False,
            "reset_receipt_valid": False,
            "reset_receipt_digest": None,
            "current_v": 0,
            "next_v": 0,
            "active_bots": [],
            "active_generation": None,
        }
        handoff = _handoff_projection(epoch)
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
            "pipeline_checkpoint_revision": None,
            "grand_cost_total": 0.0,
            "gen_cost_total": 0.0,
            "generation_cost_identity": None,
            "generation_cost_policy": None,
            "post_publication_handoff": handoff,
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
    state["pipeline_checkpoint_revision"] = (
        checkpoint.get("checkpoint_revision") if checkpoint else None
    )
    state["post_publication_handoff"] = handoff
    if checkpoint is None and handoff.get("status") != "none":
        state["pipeline_stage"] = "post_publication_handoff"
    return state
