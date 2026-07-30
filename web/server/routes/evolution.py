"""Evolution SSE stream and state endpoints.

Multi-slot authority (primary + draft ``active_generations``, Slice-2b
``pipeline_mode``, async certification / eval-wait blocks) is owned by
``GET /api/control/status`` and ``/health`` poll projections.  This SSE stream
still validates and forwards primary-slot ``status`` ring events only; clients
must not treat SSE as the multi-slot source of truth until an explicit
``active_generations`` event is added.
"""

import asyncio
import json
import math
import re
import time

from fastapi import APIRouter, Request
from blocking_runtime import run_blocking_isolated

router = APIRouter(prefix="/api", tags=["evolution"])

# WebUI status text is intentionally transient rather than checkpoint evidence.
# A long-lived process can retain a previous Master/Worker message after its
# checkpoint or owner task moved.  Keep the window short enough that a
# reconnect never turns an old ring-buffer row into a current workflow claim.
_TRANSIENT_STATUS_MAX_AGE_SEC = 30.0
_TRANSIENT_STATUS_FUTURE_SKEW_SEC = 5.0
_TASK_OWNER_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _task_is_active(task: object) -> bool:
    return bool(
        isinstance(task, dict)
        and task.get("present") is True
        and task.get("done") is False
        and task.get("shutdown_requested") is False
        and task.get("status_eligible") is True
    )


def _active_task_owner_id(task: object) -> str | None:
    """Return the exact active task owner, or no authority on any mismatch."""

    if not _task_is_active(task) or not isinstance(task, dict):
        return None
    owner_id = task.get("owner_id")
    if not isinstance(owner_id, str) or _TASK_OWNER_ID_RE.fullmatch(owner_id) is None:
        return None
    return owner_id


def _task_owner_projection(task: object) -> dict | None:
    """Return the typed task-owner lifecycle projection used by SSE.

    Status phrases may only be accepted while this projection identifies the
    same live owner.  A reservation (``present=False`` with a valid owner) is
    still useful: it immediately invalidates the prior task's status before a
    replacement coroutine is attached.  Any malformed state fails closed.
    """

    if not isinstance(task, dict):
        return None
    present = task.get("present")
    done = task.get("done")
    shutdown_requested = task.get("shutdown_requested")
    status_eligible = task.get("status_eligible")
    owner_id = task.get("owner_id")
    lifecycle_revision = task.get("lifecycle_revision")
    if (
        type(present) is not bool
        or (done is not None and type(done) is not bool)
        or type(shutdown_requested) is not bool
        or type(status_eligible) is not bool
    ):
        return None
    if type(lifecycle_revision) is not int or lifecycle_revision < 0:
        return None
    if owner_id is not None and (
        not isinstance(owner_id, str)
        or _TASK_OWNER_ID_RE.fullmatch(owner_id) is None
    ):
        return None
    if present is True and (type(done) is not bool or owner_id is None):
        return None
    if present is False and done is not None:
        return None
    # Only a live, non-stopping task may be eligible to own human status.
    # Project the boolean even for invalidating lifecycle rows so a browser
    # does not infer eligibility from an omitted field during an SSE/HTTP race.
    if status_eligible and not (
        present is True and done is False and shutdown_requested is False
    ):
        return None
    return {
        "present": present,
        "done": done,
        "shutdown_requested": shutdown_requested,
        "status_eligible": status_eligible,
        "owner_id": owner_id,
        "lifecycle_revision": lifecycle_revision,
    }


def _task_owner_event_is_current(event: object) -> bool:
    """Drop stale lifecycle rows from the process ring before browser delivery."""

    if not isinstance(event, dict) or event.get("event") != "task_owner":
        return True
    raw = event.get("data")
    if not isinstance(raw, str):
        return False
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    projection = _task_owner_projection(payload)
    return projection is not None and projection == _task_owner_projection(
        _live_task_snapshot()
    )


def _active_generation_identity(epoch: object) -> dict | None:
    """Return only the exact identity eligible to own transient status text."""

    if not isinstance(epoch, dict) or epoch.get("initialized") is not True:
        return None
    active = epoch.get("active_generation")
    if not isinstance(active, dict):
        return None
    run_id = active.get("run_id")
    workflow_run_id = active.get("workflow_run_id")
    revision = active.get("checkpoint_revision")
    stage = active.get("stage")
    if (
        not isinstance(run_id, str)
        or not run_id.strip()
        or not isinstance(workflow_run_id, str)
        or not workflow_run_id.strip()
        or type(revision) is not int
        or revision < 1
        or not isinstance(stage, str)
        or not stage.strip()
    ):
        return None
    return {
        "run_id": run_id,
        "workflow_run_id": workflow_run_id,
        "checkpoint_revision": revision,
        "stage": stage,
    }


def _current_transient_status(
    payload: object,
    epoch: object,
    *,
    task: object | None = None,
    now: float | None = None,
) -> dict | None:
    """Validate a WebUI status against canonical generation + live task.

    This boundary is used both by the JSON snapshot and SSE delivery.  It is
    deliberately strict: partial identity, stale timestamp, a stopped task,
    or any revision/stage mismatch suppresses the human text instead of
    allowing a stale "Master planning" claim to flash in a new generation.
    """

    if not isinstance(payload, dict):
        return None
    msg = payload.get("msg", payload.get("status"))
    is_working = payload.get("is_working")
    run_id = payload.get("run_id")
    workflow_run_id = payload.get("workflow_run_id")
    revision = payload.get("checkpoint_revision")
    stage = payload.get("stage")
    task_owner_id = payload.get("task_owner_id")
    task_lifecycle_revision = payload.get("task_lifecycle_revision")
    emitted_at = payload.get("emitted_at")
    expected = _active_generation_identity(epoch)
    live_owner_id = _active_task_owner_id(task)
    live_lifecycle_revision = (
        task.get("lifecycle_revision") if isinstance(task, dict) else None
    )
    if (
        not isinstance(msg, str)
        or not isinstance(is_working, bool)
        or expected is None
        or live_owner_id is None
        or run_id != expected["run_id"]
        or workflow_run_id != expected["workflow_run_id"]
        or type(revision) is not int
        or revision != expected["checkpoint_revision"]
        or stage != expected["stage"]
        or task_owner_id != live_owner_id
        or type(task_lifecycle_revision) is not int
        or task_lifecycle_revision != live_lifecycle_revision
        or isinstance(emitted_at, bool)
    ):
        return None
    try:
        emitted = float(emitted_at)
        observed_now = time.time() if now is None else float(now)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not math.isfinite(emitted)
        or not math.isfinite(observed_now)
        or emitted < 0.0
        or emitted > observed_now + _TRANSIENT_STATUS_FUTURE_SKEW_SEC
        or observed_now - emitted > _TRANSIENT_STATUS_MAX_AGE_SEC
    ):
        return None
    return {
        "msg": msg,
        "is_working": is_working,
        "run_id": run_id,
        "workflow_run_id": workflow_run_id,
        "checkpoint_revision": revision,
        "stage": stage,
        "task_owner_id": task_owner_id,
        "task_lifecycle_revision": task_lifecycle_revision,
        "emitted_at": emitted,
    }


def _live_task_snapshot() -> dict | None:
    """Read task ownership lazily so a status failure cannot affect runtime."""

    try:
        from server.state import app_state

        snapshot = app_state.task_snapshot()
    except Exception:
        return None
    return snapshot if isinstance(snapshot, dict) else None


def _status_event_is_current(event: object, epoch: object) -> bool:
    """Filter replay/live SSE status rows before a browser can consume them."""

    if not isinstance(event, dict) or event.get("event") != "status":
        return True
    raw = event.get("data")
    if not isinstance(raw, str):
        return False
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return _current_transient_status(
        payload,
        epoch,
        task=_live_task_snapshot(),
    ) is not None


def _epoch_projection() -> dict:
    """Return a complete read-only epoch view without consulting old state."""

    try:
        from epoch_authority import strict_epoch_projection

        return strict_epoch_projection(ledger_fresh=False)
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


async def _stable_stream_projection_async(max_attempts: int = 3):
    """Run complete Git/checkpoint authority proof outside the ASGI loop."""

    return await run_blocking_isolated(
        _stable_stream_projection,
        max_attempts,
        thread_name_prefix="evolution-stream-authority",
    )


@router.get("/evolution/stream")
async def evolution_stream(request: Request):
    """SSE endpoint for real-time evolution events."""
    from sse_starlette.sse import EventSourceResponse
    from server.app import broadcaster

    bound_before_sample = broadcaster.authority_identity()
    epoch, connection_handoff, connection_authority_digest = (
        await _stable_stream_projection_async()
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
        live_epoch, _live_handoff, _live_digest = (
            await _stable_stream_projection_async()
        )

        async def moved_stream():
            yield {
                "event": "epoch_blocked",
                "data": json.dumps(_epoch_metadata(live_epoch)),
            }

        return EventSourceResponse(moved_stream())

    async def generate():
        try:
            initial_epoch, initial_handoff, initial_digest = (
                await _stable_stream_projection_async()
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
            # The owner transition is an immediate invalidation channel for
            # non-authoritative WebUI text.  It is deliberately emitted even
            # before any status replay so a reconnect cannot retain a previous
            # owner's phrase until the 5-second control-health poll arrives.
            initial_task_projection = _task_owner_projection(_live_task_snapshot())
            if initial_task_projection is None:
                yield {
                    "event": "task_authority_lost",
                    "data": json.dumps({
                        "reason": "task_snapshot_projection_invalid",
                    }),
                }
            else:
                yield {
                    "event": "task_owner",
                    "data": json.dumps(initial_task_projection),
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
                    await _stable_stream_projection_async()
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
                        await _stable_stream_projection_async()
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
                    # Ring-buffer replay and a delayed queue delivery are both
                    # capable of carrying an otherwise well-formed status from
                    # an old checkpoint revision.  Do not forward it merely
                    # because the broader epoch digest still matches.
                    if not _status_event_is_current(event, delivery_epoch):
                        continue
                    if not _task_owner_event_is_current(event):
                        continue
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


def _evolution_state_snapshot():
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
            "pipeline_outcome": None,
            "grand_cost_total": 0.0,
            "gen_cost_total": 0.0,
            "generation_cost_identity": None,
            "generation_cost_policy": None,
            "transient_status": None,
            "transient_status_task": None,
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
    state["pipeline_outcome"] = None
    if checkpoint and isinstance(checkpoint.get("terminal_gate_outcome"), dict):
        outcome = checkpoint["terminal_gate_outcome"]
        state["pipeline_outcome"] = {
            key: outcome.get(key)
            for key in (
                "schema_version",
                "kind",
                "gate_name",
                "terminal_stage",
                "reason_code",
                "failure_class",
                "disposition",
                "receipt_digest",
            )
        }
    state["post_publication_handoff"] = handoff
    status_task = _live_task_snapshot()
    state["transient_status_task"] = _task_owner_projection(status_task)
    transient_status = _current_transient_status(
        {
            "msg": state.get("status"),
            "is_working": state.get("is_working"),
            **(
                state.get("status_identity")
                if isinstance(state.get("status_identity"), dict)
                else {}
            ),
        },
        epoch,
        task=status_task,
    )
    state["transient_status"] = transient_status
    if transient_status is None:
        # Do not project a process-memory phrase as current work.  The UI has
        # separate canonical task/health indicators and will display this
        # neutral text only until a newly stamped current status arrives.
        state["status"] = (
            "等待当前活动任务状态"
            if _task_is_active(status_task)
            and _active_generation_identity(epoch) is not None
            else "无可验证的当前活动任务状态"
        )
        state["is_working"] = False
    if checkpoint is None and handoff.get("status") != "none":
        state["pipeline_stage"] = "post_publication_handoff"
    return state


@router.get("/evolution/state")
async def evolution_state():
    """Current state snapshot without blocking unrelated ASGI requests."""

    return await run_blocking_isolated(
        _evolution_state_snapshot,
        thread_name_prefix="evolution-state-observer",
    )
