"""Pipeline watchdog + stability-projection maintenance coroutines.

Extracted from orchestrator.py as a single business responsibility: a
background watchdog that monitors the pipeline checkpoint for stuck stages
and forces a stale-session restart, plus a proactive stability-cache
refresh coroutine that keeps the health cache verified without relying on
browser polling.

Members moved here (all re-exported by orchestrator.py):

* ``_watchdog_coroutine``  -- monitors pipeline_state.json for stuck stages.
* ``_stability_projection_maintenance_tick``  -- request a proactive,
  still-fail-closed stability-cache refresh.
* ``_stability_projection_maintenance_coroutine``  -- keep the health cache
  verified without relying on browser polling.

IMPORTANT -- shared-symbol access model
---------------------------------------
Symbols referenced by these bodies that live in ``orchestrator`` are written
as ``_o.<name>`` so they resolve against the live ``orchestrator`` module
attribute, matching the pattern proven by ``orchestrator_branch_guard`` /
``orchestrator_post_generation``.  This covers:

* the module logger ``log`` and ``log_system_event``.
* the owned-session helper ``_clear_orchestrator_session``.
* mutable module flags ``_watchdog_triggered`` and
  ``_orchestrator_provider_stream_active`` (read via ``_o.<name>``; the
  watchdog writes ``_o._watchdog_triggered = True`` so the flag on the live
  ``orchestrator`` module object is updated, exactly as
  ``orchestrator_loop``'s bare-global read observed it when the body lived
  here).
* constant ``STABILITY_OBSERVATION_MAINTENANCE_INTERVAL``.

``run_blocking_isolated`` is imported directly from ``blocking_runtime``
(stable import, not monkeypatched on ``orchestrator``); ``asyncio`` /
``time`` are stdlib.
"""

from __future__ import annotations

import asyncio
import time

import orchestrator as _o
async def _watchdog_coroutine(ui, shutdown_mgr, check_interval=60):
    """Background coroutine that monitors pipeline_state.json for stuck stages.

    Every `check_interval` seconds, reads the pipeline checkpoint and checks
    `last_stage_change_ts`. If more than WATCHDOG_TIMEOUT seconds have elapsed
    with no stage change, clears the orchestrator session and sets the
    _watchdog_triggered flag so the main loop will restart from the checkpoint.

    Only triggers when:
      - this process owns an active orchestrator provider stream
      - The checkpoint stage is in the recoverable set
      - No stage change for > WATCHDOG_TIMEOUT seconds
    """
    from evolution_infra import WATCHDOG_TIMEOUT
    from evolution_core import read_pipeline_checkpoint
    from pipeline_state import (
        pipeline_runtime_activity_ts,
        session_recoverable_stages,
    )

    recoverable_stages = session_recoverable_stages()

    while True:
        if shutdown_mgr and shutdown_mgr.is_shutting_down:
            return
        try:
            await asyncio.sleep(check_interval)
            if shutdown_mgr and shutdown_mgr.is_shutting_down:
                return

            # Provider session IDs are never persisted.  The in-process owned
            # stream flag supplies liveness without granting history authority.
            if not _o._orchestrator_provider_stream_active:
                continue

            # read_pipeline_checkpoint does file I/O + JSON parse — offload it
            # off the ASGI event loop so the 60s watchdog poll does not stall
            # HTTP (the file is small but JSON parse + fsync read can spike).
            from blocking_runtime import run_blocking_isolated

            checkpoint = await run_blocking_isolated(
                read_pipeline_checkpoint,
                thread_name_prefix="watchdog-checkpoint-read",
            )
            if not checkpoint:
                continue

            stage = checkpoint.get("stage", "unknown")
            if stage not in recoverable_stages:
                continue

            last_ts = max(
                float(checkpoint.get("last_stage_change_ts") or 0.0),
                pipeline_runtime_activity_ts(checkpoint),
            )
            if last_ts <= 0:
                continue

            elapsed = time.time() - last_ts
            if elapsed > WATCHDOG_TIMEOUT:
                next_v = checkpoint.get("next_v", "?")
                msg = (f"[Watchdog] Pipeline stuck at '{stage}' for v{next_v} "
                       f"({elapsed:.0f}s > {WATCHDOG_TIMEOUT}s). "
                       f"Clearing session to force restart.")
                if ui:
                    ui.log_history(msg, "warn")
                else:
                    _o.log.warning(msg)
                _o.log_system_event("pipeline.watchdog_recovery", "warn",
                                 "Watchdog triggered: clearing stale orchestrator session",
                                 {"next_v": next_v, "stage": stage,
                                  "elapsed_s": round(elapsed, 1),
                                  "watchdog_timeout": WATCHDOG_TIMEOUT})
                _o._clear_orchestrator_session()
                # Set the flag on the live orchestrator module object so the
                # main loop's bare-global ``_watchdog_triggered`` read observes
                # it (the body previously used ``global _watchdog_triggered``).
                _o._watchdog_triggered = True
                # Exit — the main loop will detect the flag and restart
                return
        except asyncio.CancelledError:
            return
        except Exception as e:
            _o.log.debug("Watchdog check error (non-fatal): %s", e)


def _stability_projection_maintenance_tick() -> None:
    """Request a proactive, still-fail-closed stability-cache refresh.

    The cache owns the remote verifier's single-flight lease.  This tick only
    supplies the current epoch authority and asks it to prefetch before its
    existing verified result expires; it never writes observation state or
    treats a pending/stale result as healthy.
    """

    from epoch_authority import epoch_stream_authority_digest, strict_epoch_projection
    from stability_observation import (
        STABILITY_VERIFICATION_PREFETCH_LEAD_SEC,
        stability_observation_cached_projection,
    )

    authority_digest = epoch_stream_authority_digest(strict_epoch_projection())
    if not isinstance(authority_digest, str) or len(authority_digest) != 64:
        raise RuntimeError("stability_maintenance_epoch_authority_unavailable")
    stability_observation_cached_projection(
        expected_epoch_authority_digest=authority_digest,
        prefetch_lead_sec=STABILITY_VERIFICATION_PREFETCH_LEAD_SEC,
    )


async def _stability_projection_maintenance_coroutine(
    shutdown_mgr,
    *,
    check_interval: float = _o.STABILITY_OBSERVATION_MAINTENANCE_INTERVAL,
) -> None:
    """Keep the health cache verified without relying on browser polling."""

    interval = max(0.1, float(check_interval))
    while True:
        if shutdown_mgr and shutdown_mgr.is_shutting_down:
            return
        try:
            await _o.run_blocking_isolated(
                _o._stability_projection_maintenance_tick,
                thread_name_prefix="stability-observation-maintenance",
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            # Health remains fail-closed at the existing TTL boundary.  Do not
            # turn a maintenance diagnostic into an unbounded UI/event flood.
            _o.log.debug("Stability maintenance refresh failed: %s", exc)
        if shutdown_mgr:
            try:
                await asyncio.wait_for(
                    shutdown_mgr.wait_for_shutdown(),
                    timeout=interval,
                )
            except asyncio.TimeoutError:
                continue
            return
        await asyncio.sleep(interval)
