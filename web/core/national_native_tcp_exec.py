"""Direct-artifact native TCP execution cluster, extracted from ``national_native``.

Holds ``_run_direct_artifact_tcp_pair`` -- the bounded direct-content-bound
policy-artifact native TCP runner that drives one complete 70-hand match
between two resolved bot specs under the immutable
:class:`NativeMatchTimingPlan`.  This is the single execution site consumed by
both ``run_native_tcp_pair`` and ``run_native_strength_pair``.

The parent module (``national_native``) keeps a thin delegate shell so that
intra-module callers continue to resolve through the parent namespace.
``_run_direct_artifact_tcp_pair`` itself is neither monkeypatched nor invoked
directly by the test suite, so no delegate is strictly required for test
compatibility; the delegate exists purely to preserve the public surface of
``national_native``.

Implementation contract
-----------------------
The companion imports the parent module as ``_nn`` and routes every
parent-local helper call (``resolve_bot``, ``acquire_match_slots_async``,
``_prepare_native_spec_bounded``, ``_run_tcp_server_with_processes``,
``_trace_decisions_from_overrides``) through ``_nn.<name>``.  This is required
because several of those names (``resolve_bot``, ``acquire_match_slots_async``,
``_run_tcp_server_with_processes``) are monkeypatched by the test suite via
``monkeypatch.setattr(national_native, "<name>", fake)``; resolving them
through the parent namespace ensures those patches are observed at call time.

Timing-plan helpers and the ``NativeMatchTimingPlan`` type come from the
``national_native_timing`` companion (immutable, not monkeypatched) and are
imported directly.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any

import national_native as _nn
from national_native_timing import (
    NATIVE_LAUNCH_HEARTBEAT_INTERVAL_SEC,
    NativeMatchTimingPlan,
    _annotate_native_full_match_liveness,
    _canonical_timing_digest,
    _resolve_native_match_timing_plan,
    validate_native_match_timing_evidence,
)


async def _run_direct_artifact_tcp_pair(
    bot_a_token: str | Path,
    bot_b_token: str | Path,
    hands: int,
    *,
    deck_seed_base: int | None,
    bot_seed_base: int | None,
    timeout_sec: float | None,
    timing_plan: NativeMatchTimingPlan | dict[str, Any] | None = None,
    bot_a_env_overrides: dict[str, str | int | None] | None = None,
    bot_b_env_overrides: dict[str, str | int | None] | None = None,
    native_full_match_liveness_budget: dict[str, float | int] | None = None,
    capture_events: bool = False,
    sanitize_parent_environment: bool = True,
    control_execution_ticket: dict[str, Any] | None = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    if not sanitize_parent_environment:
        raise ValueError("native strength timing must not inherit parent environment")
    if native_full_match_liveness_budget is not None:
        raise ValueError(
            "raw native full-match liveness budgets are not execution authority; "
            "pass the immutable timing_plan instead"
        )
    trace_decisions = (
        _nn._trace_decisions_from_overrides("bot_a", bot_a_env_overrides)
        or _nn._trace_decisions_from_overrides("bot_b", bot_b_env_overrides)
    )
    if control_execution_ticket is not None and (
        capture_events is not True
        or int(hands) != 70
    ):
        raise ValueError(
            "first strict control ticket requires one captured 70-hand "
            "direct-artifact match"
        )
    label_a, dir_a = _nn.resolve_bot(bot_a_token)
    system_control_b = control_execution_ticket is not None
    if control_execution_ticket is not None:
        from first_strict_execution_journal import normalize_execution_scope

        ticket_input = control_execution_ticket.get("input_payload") or {}
        ticket_scope = normalize_execution_scope(ticket_input.get("scope"))
        if label_a != ticket_scope["candidate_label"]:
            raise ValueError("first strict candidate label mismatch")
        dir_b = Path(bot_b_token).absolute()
        label_a = ticket_scope["candidate_label"]
        label_b = ticket_scope["control_id"]
    else:
        ticket_scope = {}
        label_b, dir_b = _nn.resolve_bot(bot_b_token)
    hands = max(1, min(70, int(hands)))
    frozen_timing_plan = _resolve_native_match_timing_plan(
        timing_plan,
        hands=hands,
        requested_timeout_sec=timeout_sec,
    )
    capacity_owner = (
        f"native_tcp:{label_a}:{label_b}:{os.getpid()}:{time.monotonic_ns()}"
    )
    capacity_lease = None
    bound_progress_callback = None
    # This digest is a runtime-only prelaunch identity, not replay or strength
    # evidence.  It is fixed before capacity wait and artifact preparation so
    # the exact provider dispatch can prove liveness across the whole bounded
    # operation.  Artifact bytes are independently bound by NativeBotSpec
    # before either process or socket is launched.
    match_run_nonce = uuid.uuid4().hex
    match_identity_digest = _canonical_timing_digest({
        "schema_version": 3,
        "identity_kind": "runtime_only_native_prelaunch",
        "bot_a_label": label_a,
        "bot_a_path": str(dir_a.absolute()),
        "bot_b_label": label_b,
        "bot_b_path": str(dir_b.absolute()),
        "system_control_b": system_control_b,
        "hands": hands,
        "deck_seed_base": deck_seed_base,
        "bot_seed_base": bot_seed_base,
        "timing_plan_digest": frozen_timing_plan.digest(),
        "control_match_run_id": str(
            (control_execution_ticket or {}).get("match_run_id") or ""
        ),
        "match_run_nonce": match_run_nonce,
    })
    operation_started_at_epoch: float | None = None
    engine_phase_started_at: float | None = None
    finalizing_phase_started_at: float | None = None
    terminal_progress_reported = False
    terminal_outcome = "runner_raised"
    launch_heartbeat_stop = asyncio.Event()
    launch_heartbeat_task: asyncio.Task | None = None

    async def bound_progress_callback(projection: dict[str, Any]) -> bool:
        nonlocal operation_started_at_epoch
        nonlocal engine_phase_started_at, finalizing_phase_started_at
        nonlocal terminal_progress_reported
        if progress_callback is None:
            return False
        if not isinstance(projection, dict):
            return False
        event_type = str(projection.get("event_type") or "")
        terminal_event = (
            projection.get("terminal") is True or event_type == "terminal"
        )
        if terminal_event:
            if terminal_progress_reported:
                return True
            outcome = str(projection.get("terminal_outcome") or "")
            if outcome not in {
                "runner_returned",
                "runner_raised",
                "runner_cancelled",
            }:
                return False
            # This event is consumed by the identity-aware reporter; it never
            # becomes a persistent liveness projection.
            enriched = {
                "event_type": "terminal",
                "terminal": True,
                "terminal_outcome": outcome,
                "match_identity_digest": match_identity_digest,
                "timing_plan_digest": frozen_timing_plan.digest(),
            }
        elif event_type == "launching":
            try:
                phase_started_at = float(
                    projection.get("phase_started_at_epoch")
                )
            except (TypeError, ValueError):
                return False
            if not math.isfinite(phase_started_at) or phase_started_at <= 0.0:
                return False
            if operation_started_at_epoch is None:
                operation_started_at_epoch = phase_started_at
            elif phase_started_at != operation_started_at_epoch:
                return False
            if engine_phase_started_at is not None:
                return False
            phase_budget_us = frozen_timing_plan.launch_timeout_us
            enriched = {
                **dict(projection),
                "hand": None,
                "liveness_phase": "launching",
                "phase_started_at_epoch": phase_started_at,
                "phase_budget_us": phase_budget_us,
                "match_identity_digest": match_identity_digest,
                "timing_plan_digest": frozen_timing_plan.digest(),
                "hands": frozen_timing_plan.hands,
                "effective_timeout_us": frozen_timing_plan.effective_timeout_us,
                "operation_started_at_epoch": operation_started_at_epoch,
                "operation_deadline_epoch": (
                    operation_started_at_epoch
                    + frozen_timing_plan.first_strict_lease_timeout_us
                    / 1_000_000.0
                ),
                "operation_budget_us": (
                    frozen_timing_plan.first_strict_lease_timeout_us
                ),
                "phase_deadline_epoch": (
                    phase_started_at + phase_budget_us / 1_000_000.0
                ),
            }
        elif event_type == "finalizing":
            if engine_phase_started_at is None or operation_started_at_epoch is None:
                return False
            if projection.get("hand") != frozen_timing_plan.hands:
                return False
            try:
                phase_started_at = float(
                    projection.get("phase_started_at_epoch")
                )
            except (TypeError, ValueError):
                return False
            if not math.isfinite(phase_started_at) or phase_started_at <= 0.0:
                return False
            if finalizing_phase_started_at is None:
                finalizing_phase_started_at = phase_started_at
            elif phase_started_at != finalizing_phase_started_at:
                return False
            phase_budget_us = frozen_timing_plan.finalization_timeout_us
            enriched = {
                **dict(projection),
                "liveness_phase": "finalizing",
                "phase_started_at_epoch": finalizing_phase_started_at,
                "phase_budget_us": phase_budget_us,
                "match_identity_digest": match_identity_digest,
                "timing_plan_digest": frozen_timing_plan.digest(),
                "hands": frozen_timing_plan.hands,
                "effective_timeout_us": frozen_timing_plan.effective_timeout_us,
                "operation_started_at_epoch": operation_started_at_epoch,
                "operation_deadline_epoch": (
                    operation_started_at_epoch
                    + frozen_timing_plan.first_strict_lease_timeout_us
                    / 1_000_000.0
                ),
                "operation_budget_us": (
                    frozen_timing_plan.first_strict_lease_timeout_us
                ),
                "phase_deadline_epoch": (
                    finalizing_phase_started_at
                    + phase_budget_us / 1_000_000.0
                ),
            }
        else:
            if event_type == "engine_started":
                try:
                    engine_phase_started_at = float(
                        projection.get("phase_started_at_epoch")
                    )
                except (TypeError, ValueError):
                    return False
                if (
                    not math.isfinite(engine_phase_started_at)
                    or engine_phase_started_at <= 0.0
                ):
                    return False
                launch_heartbeat_stop.set()
            if engine_phase_started_at is None or operation_started_at_epoch is None:
                # Only the trusted runner can declare the actual engine
                # boundary.  Do not derive it from an arbitrary first
                # action/settlement callback.
                return False
            phase_budget_us = frozen_timing_plan.effective_timeout_us
            enriched = {
                **dict(projection),
                "liveness_phase": "engine_running",
                "phase_started_at_epoch": engine_phase_started_at,
                "phase_budget_us": phase_budget_us,
                "match_identity_digest": match_identity_digest,
                "timing_plan_digest": frozen_timing_plan.digest(),
                "hands": frozen_timing_plan.hands,
                "effective_timeout_us": frozen_timing_plan.effective_timeout_us,
                "operation_started_at_epoch": operation_started_at_epoch,
                "operation_deadline_epoch": (
                    operation_started_at_epoch
                    + frozen_timing_plan.first_strict_lease_timeout_us
                    / 1_000_000.0
                ),
                "operation_budget_us": (
                    frozen_timing_plan.first_strict_lease_timeout_us
                ),
                "phase_deadline_epoch": (
                    engine_phase_started_at + phase_budget_us / 1_000_000.0
                ),
            }
        try:
            callback_result = progress_callback(enriched)
            if asyncio.iscoroutine(callback_result):
                callback_result = await callback_result
            # A reporter may explicitly reject an identity-mismatched or
            # failed unlink.  Only an acknowledged terminal clear suppresses
            # the outer finally retry; generic callbacks returning None retain
            # backward-compatible success semantics.
            if terminal_event and callback_result is not False:
                terminal_progress_reported = True
            return callback_result is not False
        except Exception:
            # The native engine remains authoritative.  A failed
            # orchestrator sidecar write must not change the match result.
            return False

    async def refresh_launch_progress(phase_started_at_epoch: float) -> None:
        """Refresh freshness only; the launch phase deadline stays immutable."""

        while not launch_heartbeat_stop.is_set():
            try:
                await asyncio.wait_for(
                    launch_heartbeat_stop.wait(),
                    timeout=NATIVE_LAUNCH_HEARTBEAT_INTERVAL_SEC,
                )
                return
            except asyncio.TimeoutError:
                accepted = await bound_progress_callback({
                    "event_type": "launching",
                    "phase_started_at_epoch": phase_started_at_epoch,
                })
                if not accepted:
                    return

    try:
        # Launch liveness begins before the bounded capacity and preparation
        # phases.  A provider reaching this tool near its original deadline
        # can therefore receive exactly one plan-bound extension instead of
        # timing out while a valid first-strict lease still owns the effect.
        if progress_callback is not None:
            launch_started_at_epoch = time.time()
            launch_accepted = await bound_progress_callback({
                "event_type": "launching",
                "phase_started_at_epoch": launch_started_at_epoch,
            })
            if launch_accepted:
                launch_heartbeat_task = asyncio.create_task(
                    refresh_launch_progress(launch_started_at_epoch),
                    name="native-tcp-launch-heartbeat",
                )
        # Queue duration is part of the immutable timing plan.  In particular
        # the first-strict journal ticket is claimed before this wait, so the
        # ticket's system-owned lease covers this bounded interval.
        capacity_lease = await _nn.acquire_match_slots_async(
            capacity_owner,
            count=1,
            timeout=frozen_timing_plan.capacity_queue_timeout_us / 1_000_000.0,
        )
        if progress_callback is not None and operation_started_at_epoch is not None:
            await bound_progress_callback({
                "event_type": "launching",
                "phase_started_at_epoch": operation_started_at_epoch,
            })
        spec_a = await _nn._prepare_native_spec_bounded(
            label_a,
            dir_a,
            timing_plan=frozen_timing_plan,
            expected_artifact_hash=str(
                ticket_scope.get("candidate_artifact_hash") or ""
            ),
        )
        if progress_callback is not None and operation_started_at_epoch is not None:
            await bound_progress_callback({
                "event_type": "launching",
                "phase_started_at_epoch": operation_started_at_epoch,
            })
        spec_b = await _nn._prepare_native_spec_bounded(
            label_b,
            dir_b,
            timing_plan=frozen_timing_plan,
            system_control=system_control_b,
            expected_artifact_hash=str(
                ticket_scope.get("control_artifact_hash") or ""
            ),
        )
        if progress_callback is not None and operation_started_at_epoch is not None:
            await bound_progress_callback({
                "event_type": "launching",
                "phase_started_at_epoch": operation_started_at_epoch,
            })
        runner_kwargs = {
            "hands": hands,
            "timing_plan": frozen_timing_plan,
            "deck_seed_base": deck_seed_base,
            "bot_seed_base": bot_seed_base,
            "capture_events": capture_events,
            "trace_decisions": trace_decisions,
            "progress_callback": (
                bound_progress_callback if progress_callback is not None else None
            ),
        }
        if control_execution_ticket is not None:
            runner_kwargs["control_execution_ticket"] = control_execution_ticket
        result = await _nn._run_tcp_server_with_processes(
            spec_a,
            spec_b,
            **runner_kwargs,
        )
        if control_execution_ticket is not None:
            # The control runner seals and journals this exact object.  Do not
            # copy or mutate it after return, or the outer idempotent journal
            # completion would correctly reject the changed replay bytes.
            if not isinstance(result, dict) or validate_native_match_timing_evidence(
                result,
                timing_plan=frozen_timing_plan,
            ):
                raise RuntimeError(
                    "first strict control runner timing evidence missing or drifted"
                )
        else:
            # The production runner already annotates before returning.  Keep
            # this idempotent adapter for isolated direct-runner test doubles.
            result = _annotate_native_full_match_liveness(result, frozen_timing_plan)
        terminal_outcome = "runner_returned"
        return result
    finally:
        # A completed/failed match must not leave its last `settle` sidecar
        # eligible to extend a later non-engine provider stall.  The reporter
        # recognizes this terminal projection and clears only its own
        # checkpoint-bound heartbeat; ordinary callbacks may ignore it.
        launch_heartbeat_stop.set()
        if launch_heartbeat_task is not None:
            if not launch_heartbeat_task.done():
                launch_heartbeat_task.cancel()
            try:
                await launch_heartbeat_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if capacity_lease is not None:
            capacity_lease.release()
        if progress_callback is not None and not terminal_progress_reported:
            try:
                task = asyncio.current_task()
                final_outcome = (
                    "runner_cancelled"
                    if task is not None and task.cancelling()
                    else terminal_outcome
                )
                await bound_progress_callback({
                    "event_type": "terminal",
                    "terminal": True,
                    "terminal_outcome": final_outcome,
                })
            except Exception:
                pass
