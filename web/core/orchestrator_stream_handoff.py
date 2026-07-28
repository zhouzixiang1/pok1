"""Actionable-stage handoff detection + checkpoint-aware stream polling.

Extracted from orchestrator.py as a single business responsibility: detect
when an MCP gate has just produced a deterministic next-tool step (or a
terminal generation boundary), and poll the orchestrator main-agent stream
for the next message with a checkpoint-aware stall ceiling.

Members moved here (all re-exported by orchestrator.py):

* ``_detect_actionable_stage_handoff``  -- route data when an MCP gate has
  reached a deterministic step / terminal boundary / blocked recovery.
* ``_await_next_stream_message``  -- checkpoint-aware polling of the next
  orchestrator stream message, enforcing ``ORCH_STREAM_STALL_TIMEOUT`` and
  the actionable-stage ceiling.

IMPORTANT -- shared-symbol access model
---------------------------------------
Symbols referenced by these bodies that live in ``orchestrator`` are written
as ``_o.<name>`` so they resolve against the live ``orchestrator`` module
attribute, matching the pattern proven by ``orchestrator_branch_guard`` /
``orchestrator_post_generation``.  This covers:

* constants: ``ORCH_STREAM_POLL_INTERVAL``, ``ORCH_STREAM_STALL_TIMEOUT``.
* exceptions: ``_OrchStreamStallTimeout``, ``_OrchActionableStageTimeout``.
* helpers re-exported from ``orchestrator_stage_routing``:
  ``_detect_actionable_stage_stall``, ``_latest_orchestrator_external_progress``,
  ``_pipeline_checkpoint_observation``, ``_coerce_event_ts``.

``cancel_provider_stream_task_bounded`` and ``log_system_event`` are imported
directly (they are stable third-party/module imports, not monkeypatched on
``orchestrator``).
"""

from __future__ import annotations

import asyncio
import time

import orchestrator as _o
from llm_query import cancel_provider_stream_task_bounded
def _detect_actionable_stage_handoff(
    *,
    baseline_checkpoint_identity=None,
    baseline_checkpoint=None,
    terminal_tool_result=None,
):
    """Return route data when an MCP gate has just produced a deterministic step."""
    stall = _o._detect_actionable_stage_stall(timeout_sec=0)
    if stall and (
        baseline_checkpoint_identity is None
        or stall.get("checkpoint_actionable_identity")
        != baseline_checkpoint_identity
    ):
        return stall
    observation = _o._pipeline_checkpoint_observation()
    checkpoint = observation.get("checkpoint")
    checkpoint_error = observation.get("error")
    if checkpoint_error:
        return {
            "next_v": (
                baseline_checkpoint.get("next_v")
                if isinstance(baseline_checkpoint, dict)
                else None
            ),
            "source_v": (
                baseline_checkpoint.get("source_v")
                if isinstance(baseline_checkpoint, dict)
                else None
            ),
            "stage": "checkpoint_recovery_blocked",
            "next_tool": None,
            "recovery_blocked": True,
            "issues": [str(checkpoint_error)],
            "directive": (
                "End the current provider stream. The checkpoint path is "
                "present but unreadable/invalid, or checkpoint authority could "
                "not be read. Outer recovery must fail closed and must not "
                "prepare another generation."
            ),
        }
    if (
        isinstance(checkpoint, dict)
        and checkpoint.get("stage") == "official_bootstrap_required"
    ):
        return {
            "next_v": checkpoint.get("next_v"),
            "source_v": checkpoint.get("source_v"),
            "stage": "official_bootstrap_required",
            "next_tool": None,
            "operator_action_required": True,
            "directive": (
                "Stop automatic evolution and wait for the explicit operator "
                "bootstrap-first-strict suite. Automation must not authorize or consume it."
            ),
        }
    if not checkpoint:
        try:
            from post_publication_handoff import pending_handoff_route

            handoff = pending_handoff_route()
        except Exception as exc:
            handoff = {
                "status": "blocked",
                "issues": [f"handoff_discovery_failed:{type(exc).__name__}"],
            }
        if handoff.get("status") == "pending":
            return {
                "next_v": handoff.get("version"),
                "source_v": handoff.get("source_v"),
                "stage": "post_publication_handoff",
                "next_tool": "run_archivist",
                "directive": (
                    "End the current provider stream and resume the exact "
                    "durable Archivist handoff."
                ),
            }
        if handoff.get("status") == "blocked":
            return {
                "next_v": None,
                "source_v": None,
                "stage": "post_publication_handoff_blocked",
                "next_tool": None,
                "recovery_blocked": True,
                "issues": list(handoff.get("issues") or []),
                "directive": (
                    "End the current provider stream. Checkpoint-free recovery "
                    "is blocked by post-publication handoff diagnostics; the "
                    "outer recovery loop must surface them and must not prepare."
                ),
            }
        if handoff.get("status") == "none" and baseline_checkpoint_identity is not None:
            (
                workflow_run_id,
                checkpoint_revision,
                previous_stage,
                next_v,
                source_v,
            ) = baseline_checkpoint_identity
            if not isinstance(baseline_checkpoint, dict):
                return {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": "generation_terminal_proof_blocked",
                    "next_tool": None,
                    "recovery_blocked": True,
                    "issues": ["terminal_baseline_checkpoint_missing"],
                    "directive": (
                        "End the current provider stream. A checkpoint vanished "
                        "without the full stream-owned baseline needed to prove "
                        "canonical termination."
                    ),
                }
            try:
                from tool_bot_management import validate_completed_abandon_handoff

                terminal_proof = validate_completed_abandon_handoff(
                    baseline_checkpoint,
                    terminal_tool_result,
                )
            except Exception as exc:
                issue = str(exc).strip() or type(exc).__name__
                return {
                    "workflow_run_id": workflow_run_id,
                    "checkpoint_revision": checkpoint_revision,
                    "previous_stage": previous_stage,
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": "generation_terminal_proof_blocked",
                    "next_tool": None,
                    "recovery_blocked": True,
                    "issues": [f"canonical_abandon_proof_invalid:{issue[:240]}"],
                    "directive": (
                        "End the current provider stream. The checkpoint "
                        "disappeared without an exact current-head abandon "
                        "transaction, ledger, finalize receipt, and matching "
                        "tool-result proof. Outer recovery must not prepare."
                    ),
                }
            return {
                "workflow_run_id": workflow_run_id,
                "checkpoint_revision": terminal_proof[
                    "checkpoint_identity"
                ]["checkpoint_revision"],
                "baseline_checkpoint_revision": checkpoint_revision,
                "previous_stage": previous_stage,
                "terminal_checkpoint_stage": terminal_proof[
                    "checkpoint_identity"
                ]["stage"],
                "next_v": next_v,
                "source_v": source_v,
                "stage": "generation_terminal",
                "next_tool": "prepare_generation",
                "scheduler_handoff_required": True,
                "terminal_proof": terminal_proof,
                "directive": (
                    "End the current provider stream after canonical generation "
                    "termination. The outer scheduler, not an MCP tool, owns "
                    "the next prepare_generation call."
                ),
            }
    return None


async def _await_next_stream_message(
    stream_iter,
    last_message_at=None,
    *,
    stream_started_at=None,
    baseline_owned_route_identity=None,
):
    """Wait for the next orchestrator stream message with checkpoint-aware polling.

    D (2026-07-09): also enforce a generic mid-stream stall ceiling
    (ORCH_STREAM_STALL_TIMEOUT) on main-stream silence. The ceiling is extended
    only by current-generation MCP tool/sub-role progress, which prevents a
    healthy long tool call from being mistaken for a dead SDK stream while still
    catching truly silent cycles before CYCLE_TIMEOUT (5400s).
    """
    pending = asyncio.create_task(stream_iter.__anext__())
    pending_cleanup_owned = False
    _silence_origin = last_message_at if last_message_at is not None else (stream_started_at or time.time())
    _last_progress_marker = None
    try:
        while True:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(pending),
                    timeout=max(0.1, _o.ORCH_STREAM_POLL_INTERVAL),
                )
            except asyncio.TimeoutError:
                # D: generic stall ceiling — fires with or without a checkpoint.
                if _o.ORCH_STREAM_STALL_TIMEOUT > 0:
                    progress = _o._latest_orchestrator_external_progress(_silence_origin)
                    if progress:
                        progress_ts = min(time.time(), _o._coerce_event_ts(progress.get("ts")))
                        if progress_ts > _silence_origin:
                            _silence_origin = progress_ts
                            marker = (
                                progress.get("source"),
                                progress.get("event_type"),
                                progress.get("ts"),
                            )
                            if marker != _last_progress_marker:
                                _last_progress_marker = marker
                                try:
                                    _o.log_system_event(
                                        "pipeline.orchestrator_stream_external_progress",
                                        "info",
                                        "Orchestrator main stream is silent, but current-generation tool progress is visible",
                                        {
                                            "progress_source": progress.get("source"),
                                            "progress_event_type": progress.get("event_type"),
                                            "progress_ts": round(progress_ts, 3),
                                            "next_v": progress.get("next_v"),
                                            "stage": progress.get("stage"),
                                            "role": progress.get("role"),
                                            "log_file": progress.get("log_file"),
                                            "stall_timeout": _o.ORCH_STREAM_STALL_TIMEOUT,
                                        },
                                    )
                                except Exception:
                                    pass
                            continue
                    silent_for = time.time() - _silence_origin
                    if silent_for >= _o.ORCH_STREAM_STALL_TIMEOUT:
                        msg = (
                            f"Orchestrator main-agent stream stalled: no stream "
                            f"message for {silent_for:.0f}s (ceiling "
                            f"{_o.ORCH_STREAM_STALL_TIMEOUT:.0f}s). Treating as "
                            f"infrastructure stall; cycle will retry."
                        )
                        try:
                            _o.log_system_event(
                                "pipeline.orchestrator_stream_stall_timeout",
                                "warn",
                                msg,
                                {
                                    "silent_for_sec": round(silent_for, 1),
                                    "stall_timeout": _o.ORCH_STREAM_STALL_TIMEOUT,
                                },
                            )
                        except Exception:
                            pass
                        pending_cleanup_owned = not await cancel_provider_stream_task_bounded(
                            pending,
                            "orchestrator_stream_stall_cancellation_unconfirmed",
                        )
                        raise _o._OrchStreamStallTimeout(msg)
                stall = _o._detect_actionable_stage_stall()
                if (
                    stall
                    and baseline_owned_route_identity is not None
                    and _o.ORCH_STREAM_STALL_TIMEOUT > 0
                ):
                    if (
                        stall.get("stream_owned_route_identity")
                        == baseline_owned_route_identity
                    ):
                        # The fresh stream still owns the exact semantic route
                        # it was started to execute. Its nested role may
                        # legitimately run longer than the stale-actionable
                        # ceiling; generic stream/tool progress supervision
                        # remains in force.
                        stall = None
                if not stall:
                    continue
                next_v = stall.get("next_v")
                stage = stall.get("stage")
                next_tool = stall.get("next_tool") or "unknown"
                msg = (
                    f"Orchestrator stream idle while v{next_v} is at actionable "
                    f"stage '{stage}' for {stall.get('elapsed_sec')}s; "
                    f"fresh cycle should call {next_tool}."
                )
                try:
                    _o.log_system_event(
                        "pipeline.actionable_stage_timeout",
                        "warn",
                        msg,
                        stall,
                    )
                except Exception:
                    pass
                pending_cleanup_owned = not await cancel_provider_stream_task_bounded(
                    pending,
                    "orchestrator_actionable_stall_cancellation_unconfirmed",
                )
                raise _o._OrchActionableStageTimeout(msg)
    except BaseException:
        if not pending.done() and not pending_cleanup_owned:
            await cancel_provider_stream_task_bounded(
                pending,
                "orchestrator_stream_parent_cancellation_unconfirmed",
            )
        raise
