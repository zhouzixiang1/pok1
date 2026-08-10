"""Deterministic checkpoint-route execution + recovery classification.

Extracted from orchestrator.py as a single business responsibility: execute
safe checkpoint routes without asking the Orchestrator LLM again, classify
every checkpoint-free result (circuit-breaker abandon, crossover
incompatible/exhausted, operator shutdown, terminal proof, publication
handoff), and advance one deterministic recovery step.

Members moved here (all re-exported by orchestrator.py):

* ``_try_deterministic_checkpoint_route``  -- execute one safe checkpoint
  route deterministically.
* ``_classify_recovery_after_deterministic_route``  -- prove why a
  deterministic route no longer has a live checkpoint.
* ``_advance_deterministic_recovery``  -- run one deterministic route and
  classify every checkpoint-free result.

IMPORTANT -- shared-symbol access model
---------------------------------------
Symbols referenced by these bodies that live in ``orchestrator`` are written
as ``_o.<name>`` so they resolve against the live ``orchestrator`` module
attribute, matching the pattern proven by ``orchestrator_branch_guard`` /
``orchestrator_post_generation``.  This covers every helper re-exported from
the other companions (stage routing, checkpoint recovery, tool-result
classification, post-generation cleanup, generation-context, llm-pause) plus
``log_system_event`` and the owned-session helpers.

``LLMAvailabilityBlocked`` / ``LLMAvailabilityPauseError`` and
``GenerationCostPolicy`` are imported directly (stable imports, not
monkeypatched on ``orchestrator``); ``asyncio`` is used for the timed sleeps.
"""

from __future__ import annotations

import asyncio

import orchestrator as _o
from blocking_runtime import run_async_off_event_loop
from llm_availability import LLMAvailabilityBlocked
from llm_availability_store import LLMAvailabilityPauseError
from orchestrator_cost_policy import GenerationCostPolicy


# Slice 2b one-ahead-buffer activation (default-off; see
# producer_consumer_slice2b_activation).  The activation lives in a per-process
# registry on the orchestrator module so the loop, the consumer task and the
# promotion barrier share one :class:`Slice2bActivation` instance.  These
# accessors stay no-ops when slice2b is inactive.

_SLICE2B_CONSUMER_OWNED_GATES = frozenset({
    "run_quality_gates",
    "run_review",
    "run_critic",
    "run_precommit_eval",
})


def _slice2b_consumer_in_flight(checkpoint, next_v) -> bool:
    """True when a sealed candidate's consumer gate chain is still running."""

    try:
        from producer_consumer_slice2b_activation import slice2b_active
    except Exception:
        return False
    if not slice2b_active():
        return False
    activation = _slice2b_ensure_activation()
    if activation is None:
        return False
    candidate_id = str(
        checkpoint.get("candidate_id") or f"candidate-v{next_v}"
    )
    # Query the PERSISTED lifecycle (not the in-memory _sealed_snapshots), so
    # this stays accurate after a process restart: a sealed-but-unresolved
    # candidate is still "in flight" even before recover_at_boot rehydrates
    # the in-memory registries.  snapshot() returns None for an unknown
    # candidate (never sealed here) -> not in flight.
    snapshot = activation.ledger.snapshot(candidate_id)
    if snapshot is None:
        return False
    return not activation.ledger.is_terminal(candidate_id)


def _slice2b_reap_dead_consumer(checkpoint, next_v) -> bool:
    """Detect and reap a zombie consumer task, returning True if reaped.

    A candidate can be left non-terminal (``consuming``) when its consumer
    asyncio task exited without reaching a terminal ledger state -- this
    happens when a gate records ``infrastructure_failure`` (the dispatcher
    returns without rejecting, intending the next ``run_once`` to retry) but
    nothing re-drives ``run_once``.  The result is a zombie: the persisted
    candidate stays ``consuming`` forever (so ``_slice2b_consumer_in_flight``
    keeps returning True) while the actual asyncio task is ``done()`` and the
    effect lease is expired.  The primary lane then busy-parks every second
    waiting for a consumer that will never finish.

    This fail-closed reaper detects that exact state -- candidate non-terminal
    AND its consumer task done/absent AND (defensively) the effect lease
    expired -- and marks the candidate ``rejected`` so the primary lane's
    ``_slice2b_consumer_rejected`` check canonically abandons the generation
    and the epoch allocates a fresh successor.  We prefer this over silently
    restarting the consumer because the lease-reclaim path for an expired
    ``running`` effect is fragile (the death-proof resolver cannot distinguish
    a freshly-restarted live task from the original dead owner), and a
    generation lost to an infrastructure failure is recoverable on the next
    attempt, whereas an infinite busy-park is not.
    """

    try:
        from producer_consumer_slice2b_activation import slice2b_active
    except Exception:
        return False
    if not slice2b_active():
        return False
    activation = _slice2b_ensure_activation()
    if activation is None:
        return False
    candidate_id = str(
        checkpoint.get("candidate_id") or f"candidate-v{next_v}"
    )
    try:
        if activation.ledger.is_terminal(candidate_id):
            return False
    except Exception:
        return False
    # The consumer task is created at launch and stored in _consumer_tasks.
    # A live (not-done) task means the gate chain is genuinely still running
    # (even if slow); do NOT reap in that case.  Only reap when the task is
    # done or absent for a non-terminal candidate (the zombie state).
    task = activation._consumer_tasks.get(candidate_id)
    if task is None:
        # RESTART-RECOVERY WINDOW: after a process restart, _consumer_tasks is
        # empty (it is an in-memory dict).  recover_at_boot re-stashes the
        # consumer factory into _scheduled_factories, but does NOT launch the
        # asyncio.Task -- only ensure_consumer_running (called later in the
        # loop, AFTER this reaper) materializes it.  Treating an empty task
        # registry as a death signal therefore false-positives EVERY restart
        # and reaps a legitimately-running (or about-to-be-relaunched) consumer
        # whose 4h lease is still valid.  If there is a scheduled factory for
        # this candidate, the task simply has not been relaunched yet -- do NOT
        # reap.  Only when there is no factory AND no task do we consider the
        # candidate truly orphaned (and still defer to the lease check below).
        if candidate_id in getattr(activation, "_scheduled_factories", {}):
            return False
    if task is not None and not task.done():
        # The task is still "live", but it may be wedged inside a gate handler
        # that never returns (e.g. ``run_precommit_eval`` blocked on a native
        # match that hung).  Detect that by checking the consumer checkpoint's
        # last-update time: if no gate progress for STALE_THRESHOLD seconds, the
        # consumer is effectively dead even though the asyncio task has not
        # raised.  Reap it so the generation is abandoned + retried instead of
        # wedging the primary lane forever.  This is a heuristic fence; a
        # genuinely-slow gate (e.g. a long precommit native regression) will
        # write intermediate checkpoint updates as it progresses, so a long
        # silent gap is a real stall, not normal operation.
        try:
            from evolution_infra import pipeline_state_path
            import time as _stale_time
            consumer_slot = str(checkpoint.get("consumer_checkpoint_slot") or "")
            if not consumer_slot:
                try:
                    consumer_slot = activation.ledger.consumer_checkpoint_slot(
                        candidate_id
                    )
                except Exception:
                    consumer_slot = ""
            if consumer_slot:
                cp_path = pipeline_state_path(consumer_slot)
                if cp_path.exists():
                    stale_age = _stale_time.time() - cp_path.stat().st_mtime
                    # When the consumer is in the precommit native-match phase,
                    # the checkpoint does NOT update between rounds (precommit
                    # writes its result only after ALL rounds complete, which can
                    # take 30-50 min).  A stale checkpoint alone would falsely
                    # reap a healthy consumer.  Detect active native matches
                    # (bwrap processes spawned by the consumer's precommit) and
                    # extend the stale threshold when they are running: a live
                    # bwrap process consuming CPU is genuine progress, not a
                    # wedge.  Only reap when there is NO native activity AND the
                    # checkpoint is stale (the real wedge signature: task live,
                    # checkpoint stale, zero native processes = loop blocked).
                    #
                    # The native-activity check uses TWO signals to avoid a
                    # round-gap false negative: a single ``pgrep bwrap`` snapshot
                    # can return empty during the brief window between one native
                    # match round ending and the next starting, which would drop
                    # the threshold to 1200s and falsely reap a healthy consumer
                    # mid-precommit.  We also check for python3.12 match worker
                    # processes (the actual bot processes inside bwrap) and the
                    # orchestrator's own CPU (high CPU = active consumer work).
                    native_active = False
                    try:
                        import subprocess as _sp
                        # Check for bwrap national matches OR python3.12
                        # processes spawned by the native match runtime.
                        for pattern in (
                            "bwrap.*national",
                            "python3.12.*national_bot",
                        ):
                            _ps = _sp.run(
                                ["pgrep", "-f", pattern],
                                capture_output=True,
                                text=True,
                                timeout=5.0,
                            )
                            if _ps.stdout.strip():
                                native_active = True
                                break
                    except Exception:
                        native_active = False
                    # 20 min stale + no native activity = wedged (loop blocked
                    # on synchronous spawn/lock).  120 min stale even WITH native
                    # activity = the matches themselves hung (defensive ceiling).
                    # The 120-min native ceiling accommodates the precommit native
                    # regression under GLM effort=max: 5 rounds x 70 hands x ~10
                    # min/round (slow GLM bot decisions) can take 50-90 min, and
                    # the consumer runs it on the shared event loop (no parallelism
                    # across rounds).  Without this headroom the reaper kills every
                    # healthy precommit.
                    threshold = 7200.0 if native_active else 1200.0
                    if stale_age > threshold:
                        _o.log_system_event(
                            "pipeline.slice2b_consumer_stale_reaped",
                            "warn",
                            (
                                f"Slice 2b reaping stale consumer for v{next_v}: "
                                f"task live but consumer checkpoint unchanged "
                                f"for {int(stale_age)}s (gate chain wedged); "
                                f"generation will be abandoned and retried."
                            ),
                            {
                                "next_v": next_v,
                                "candidate_id": candidate_id,
                                "stale_age_sec": int(stale_age),
                            },
                        )
                        # Fall through to the reject below.
                    else:
                        return False
                else:
                    return False
            else:
                return False
        except Exception:
            return False
    # Defensive second check: confirm the consumer effect lease is actually
    # expired before reaping, so a transient lease-renewal gap never reaps a
    # live consumer.  Read the workflow store directly, SCOPED to this
    # candidate's own effect (not a global LIMIT 1, which can return an
    # unrelated candidate's lease).  Fail OPEN on read errors OR a missing
    # database: a consumer whose lease cannot be positively proven expired
    # must not be reaped (reaping abandons a generation -- an irreversible
    # action that must require positive proof of death, not the absence of a
    # readable lease).
    import sqlite3
    import time as _time
    from evolution_infra import RESULTS_DIR
    db = RESULTS_DIR / "workflow" / "events.sqlite3"
    if not db.exists():
        # No workflow store to prove the lease expired -> cannot reap.
        return False
    # Resolve this candidate's envelope effect id from the ledger so the lease
    # query is scoped, not a global LIMIT 1.
    _envelope_effect_id = None
    try:
        _snap = activation.ledger.snapshot(candidate_id)
        if isinstance(_snap, dict):
            _envelope_effect_id = _snap.get("envelope_effect_id")
    except Exception:
        _envelope_effect_id = None
    try:
        conn = sqlite3.connect(str(db))
        try:
            if _envelope_effect_id:
                row = conn.execute(
                    "SELECT lease_until FROM effects WHERE effect_id = ?",
                    (_envelope_effect_id,),
                ).fetchone()
            else:
                # Fallback: producer-consumer effects for this generation.
                row = conn.execute(
                    "SELECT lease_until FROM effects "
                    "WHERE kind LIKE 'producer-consumer%' "
                    "AND status = 'running' LIMIT 1"
                ).fetchone()
        finally:
            conn.close()
    except Exception:
        # Fail OPEN: cannot prove the lease is expired, so do not reap.
        return False
    # Reap ONLY when positive proof exists that the lease is expired: a row
    # was read AND its lease_until is in the past.  No row / null lease means
    # we cannot prove expiry -> do not reap.
    if row is None or row[0] is None:
        return False
    if float(row[0]) > _time.time():
        # Lease still valid; consumer may still be working.  Do not reap.
        return False
    # Zombie confirmed: mark the candidate rejected so the primary lane
    # abandons this generation and the epoch retries.
    try:
        activation.ledger.reject(
            candidate_id=candidate_id,
            reason="consumer_task_zombie_reaped",
            completed_at=__import__("time").time(),
        )
        _o.log_system_event(
            "pipeline.slice2b_consumer_zombie_reaped",
            "warn",
            (
                f"Slice 2b reaped zombie consumer for v{next_v}: candidate "
                f"{candidate_id} non-terminal but its consumer task is "
                f"done/absent with an expired lease; generation will be "
                f"abandoned and retried."
            ),
            {"next_v": next_v, "candidate_id": candidate_id},
        )
        return True
    except Exception:
        return False


def _slice2b_consumer_rejected(checkpoint, next_v) -> str | None:
    """The reject reason when a sealed candidate's consumer gate chain failed.

    Returns the persisted ``terminal_reason`` (e.g.
    ``consumer_task_infrastructure_failure`` / ``gate_failed:<gate>``) when the
    sealed candidate for this generation reached the terminal ``rejected``
    state, or ``None`` when there is no sealed candidate or it has not been
    rejected.  Mirrors :func:`_slice2b_consumer_in_flight`'s activation /
    candidate-id resolution so it stays accurate after a process restart.
    """

    try:
        from producer_consumer_slice2b_activation import slice2b_active
    except Exception:
        return None
    if not slice2b_active():
        return None
    activation = _slice2b_ensure_activation()
    if activation is None:
        return None
    candidate_id = str(
        checkpoint.get("candidate_id") or f"candidate-v{next_v}"
    )
    snapshot = activation.ledger.snapshot(candidate_id)
    if snapshot is None:
        return None
    if snapshot.get("validation_outcome") == "rejected":
        return str(
            snapshot.get("terminal_reason") or "slice2b_consumer_rejected"
        )
    # EFFECT-EXHAUSTION BRIDGE (Defect B, 2026-08-05): the lifecycle row may
    # still be non-terminal (``consuming``) even though the workflow effect is
    # already ``exhausted`` (attempt >= max_attempts from cross-restart lease
    # reclaims).  The only former mapper was the consumer task's run_once→reject
    # branch, which requires a LIVE consumer task to execute — across restarts
    # that task may not run promptly, leaving the candidate objectively dead but
    # lifecycle-non-terminal for tens of minutes (the v52 wedge's 24-min
    # visibility gap).  Consult the effect status directly so the primary lane
    # can abandon a candidate whose effect is exhausted without waiting for a
    # consumer task to notice.  The consumer task's own reject (the canonical
    # mapper) still runs as a belt-and-suspenders when it next dispatches.
    if snapshot.get("validation_outcome") == "running":
        try:
            if activation._consumer_effect_exhausted(candidate_id):
                return (
                    "consumer_effect_attempts_exhausted:"
                    "detected_by_primary_lane"
                )
        except Exception:
            pass
    return None


def _slice2b_consumer_promoted(checkpoint, next_v) -> bool:
    """True when the sealed candidate's consumer gate chain already promoted it.

    Once the consumer reaches the terminal ``promoted`` state (commit_bot ran
    successfully), ``_slice2b_consumer_in_flight`` returns False (promoted is
    terminal).  Without this guard the primary lane would fall through its
    park checks and re-run the consumer-owned gates (quality/review/critic/
    precommit) inline -- a double-execution that re-submits the consumer jobs
    and trips ``producer_consumer_idempotency_conflict``.  When this returns
    True the primary lane must fast-forward to ``commit_bot`` (where the
    promotion barrier is a no-op because the candidate is already promoted),
    not re-enter the consumer-owned gates.
    """

    try:
        from producer_consumer_slice2b_activation import slice2b_active
    except Exception:
        return False
    if not slice2b_active():
        return False
    activation = _slice2b_ensure_activation()
    if activation is None:
        return False
    candidate_id = str(
        checkpoint.get("candidate_id") or f"candidate-v{next_v}"
    )
    snapshot = activation.ledger.snapshot(candidate_id)
    if snapshot is None:
        return False
    return snapshot.get("validation_outcome") == "promoted"


async def _slice2b_abandon_rejected_candidate(
    checkpoint, next_v, source_v, *, ui, outcome, reject_reason: str
) -> bool:
    """Canonically abandon a generation whose slice2b consumer rejected it.

    A rejected consumer candidate (infrastructure failure or gate failure)
    can never be promoted, so the primary lane must not spin at
    ``workers_done`` forever.  This mirrors the worker-terminal-abandon path
    (the ``else`` branch around line 840): it calls the canonical
    ``_do_abandon_generation`` with ``expected_abandon_identity``, applies the
    same bounded ``forced_abandon_reason_stage_not_allowed`` fallback, and
    crucially sets ``outcome["terminal_abandon_result"]`` so
    ``_classify_recovery_after_deterministic_route`` recognizes the
    ``generation_abandoned`` terminal action instead of looping on
    ``deterministic_checkpoint_disappeared_without_proof``.

    Returns True iff the generation was abandoned (checkpoint cleared).
    """

    from evolution_core import read_pipeline_checkpoint
    from tool_bot_management import (
        _do_abandon_generation,
        expected_abandon_identity,
    )

    abandon_reason = "worker_terminal_abandon"
    abandon_result = await _do_abandon_generation(
        reason=abandon_reason,
        **expected_abandon_identity(read_pipeline_checkpoint()),
    )
    abandoned = bool(abandon_result.get("abandoned"))
    # Bounded fallback (mirrors the worker-terminal-abandon path): if the
    # classified reason was refused solely because it is not authorized at
    # this stage, retry once with the always-allowed generic reason.  A
    # non-stage-guard refusal (e.g. a publication stage) is NOT retried.
    if (
        not abandoned
        and abandon_result.get("blocked") is True
        and abandon_result.get("reason")
        == "forced_abandon_reason_stage_not_allowed"
    ):
        abandon_result = await _do_abandon_generation(
            reason="abandon_generation",
            **expected_abandon_identity(read_pipeline_checkpoint()),
        )
        abandoned = bool(abandon_result.get("abandoned"))
    if outcome is not None:
        outcome["router_abandon_result"] = abandon_result
        outcome["terminal_abandon_result"] = (
            _o._completed_abandon_tool_result(abandon_result)
        )
    msg_abandon = (
        f"slice2b consumer rejected v{next_v} "
        f"(reason={reject_reason}); "
        f"{'abandoned generation' if abandoned else 'abandon did not complete'}."
    )
    if ui:
        ui.log_history(
            f"[Recovery] {msg_abandon}",
            "error" if not abandoned else "warn",
        )
    else:
        _o.log.warning(msg_abandon)
    try:
        _o.log_system_event(
            "pipeline.deterministic_route_abandoned",
            "warn" if abandoned else "error",
            msg_abandon,
            {
                "next_v": next_v,
                "source_v": source_v,
                "stage": "workers_done",
                "slice2b_reject_reason": reject_reason,
                "abandon_result": abandon_result,
            },
        )
    except Exception:
        pass
    return abandoned


def _slice2b_park_primary_consumer_gates(
    checkpoint,
    next_v,
    source_v,
    next_tool,
    *,
    ui,
    outcome,
    reason: str,
):
    """Record that the primary lane is parked while the consumer owns the gates."""

    if outcome is not None:
        outcome.clear()
        outcome.update({
            "checkpoint": checkpoint,
            "route": {
                "next_tool": next_tool,
                "stage": checkpoint.get("stage"),
                "next_v": next_v,
                "source_v": source_v,
            },
            "result": {
                "success": True,
                "slice2b_consumer_parked": True,
                "reason": reason,
                "candidate_id": str(
                    checkpoint.get("candidate_id") or f"candidate-v{next_v}"
                ),
            },
            "terminal_abandon_result": None,
        })
    try:
        _o.log_system_event(
            "pipeline.slice2b_primary_parked_for_consumer",
            "info",
            (
                f"Slice 2b parked primary v{next_v} at {next_tool}; "
                "consumer gate chain owns this generation."
            ),
            {
                "next_v": next_v,
                "source_v": source_v,
                "next_tool": next_tool,
                "stage": checkpoint.get("stage"),
                "reason": reason,
            },
        )
    except Exception:
        pass
    if ui:
        ui.log_history(
            f"[Recovery] Slice 2b parked v{next_v} at {next_tool} "
            "(consumer gate chain in flight).",
            "info",
        )
    return True
def _slice2b_ensure_activation():
    """Lazy-instantiate the process-wide activation registry when slice2b is on.

    Production code never called ``activation_registry("set", ...)`` before, so
    the seam's ``"get"`` always returned ``None`` and slice2b never fired even
    with ``POK_SLICE2B_ENABLED=1``.  This helper closes that gap: when slice2b
    is active and no activation is registered yet, it constructs the production
    adapter (backed by the same sqlite kernel path the strict-authority workflow
    uses) and registers it.  The adapter is idempotent -- a second call returns
    the already-registered instance.
    """
    try:
        from producer_consumer_slice2b_activation import slice2b_active
    except Exception:
        return None
    if not slice2b_active():
        return None
    activation = _o._slice2b_activation_registry("get")
    if activation is not None:
        # Re-run boot recovery on every call, not just the first. recover_at_boot
        # is idempotent (it only stashes factories for sealed-but-unresolved
        # candidates; already-registered candidates are skipped). Running it on
        # every ensure_activation catches candidates that the first-pass recovery
        # missed (e.g. a DB lock or timing issue at startup left a consuming
        # candidate without a scheduled factory -> the consumer never launched and
        # the primary parked forever at workers_done, burning zero LLM for 30+ min).
        try:
            activation.recover_at_boot()
        except Exception:
            pass
        return activation
    try:
        from pathlib import Path
        from workflow_kernel import WorkflowStore
        from producer_consumer_workflow_store import (
            ProducerConsumerWorkflowAdapter,
        )
        from evolution_infra import RESULTS_DIR
        store = WorkflowStore(Path(RESULTS_DIR) / "workflow" / "events.sqlite3")
        adapter = ProducerConsumerWorkflowAdapter(store)
        activation = _o._slice2b_activation_registry("set", adapter=adapter)
        # Boot recovery: re-schedule consumers for every sealed-but-unresolved
        # candidate from a prior process.  Idempotent + loop-safe (only stashes
        # factories; the task is materialized later from the event loop).
        try:
            activation.recover_at_boot()
        except Exception:
            pass
        return activation
    except Exception:
        return None


def _slice2b_consumer_slot_id(candidate_id):
    """Return the consumer checkpoint slot id for a sealed candidate.

    Convention: ``consumer-<candidate_id>``.  This is intentionally NOT a
    ``draft``-prefixed slot (``is_draft_slot`` only matches the ``draft``
    prefix -- ``evolution_infra.py:63-70``), so the consumer slot is treated as
    a *live* allocation holding the real gen-N ``next_v``.  That is correct:
    the consumer gate chain validates the same generation the primary lane
    sealed, so its checkpoint carries the same ``next_v``/``source_v``/
    ``epoch_binding`` and the live floor+1 CAS at
    ``evolution_infra_checkpoint_cas.py:752`` is satisfied naturally (the slot
    was seeded from the primary's already-valid epoch binding).  The consumer
    slot is fully isolated from the primary by a distinct file
    (``pipeline_state_consumer-<candidate_id>.json``), so the gate handlers'
    no-``slot_id`` reads/writes hit it instead of racing the primary.
    """

    return "consumer-" + str(candidate_id)


def _slice2b_seed_consumer_checkpoint(checkpoint, consumer_slot_id):
    """Copy the FULL primary checkpoint projection into the consumer slot.

    The consumer gate chain (``run_quality_gates`` / ``run_review`` /
    ``run_critic`` / ``run_precommit_eval``) reads and writes the checkpoint
    exclusively through the override-aware funnel (``_matching_checkpoint`` /
    ``read_pipeline_checkpoint`` / ``write_pipeline_checkpoint`` /
    ``_record_gate`` -- all take NO ``slot_id``, verified exhaustively in
    tool_gates.py / tool_gates_critic_review.py / tool_eval.py /
    tool_helpers.py).  Under ``active_slot_override(consumer_slot_id)`` those
    calls hit ``pipeline_state_<consumer_slot>.json`` instead of the primary
    file.  This seed materialises a complete starting checkpoint in that slot
    so the first gate handler's ``read_pipeline_checkpoint()`` returns the same
    gen-N state the primary sealed at ``workers_done``.

    Enumerates every persisted field (mirroring ``_promote_draft_to_primary``'s
    ``promote_fields`` completeness) so the consumer's repo_baseline /
    epoch_binding / gate_results / charter digests / master_plan all survive
    the slot copy -- the gate handlers depend on them.  Writes with an explicit
    ``slot_id`` so it lands on the consumer file regardless of the ambient
    override (an explicit non-None ``slot_id`` wins over the ContextVar,
    ``evolution_infra.py:194``).
    """

    try:
        from evolution_infra import write_pipeline_checkpoint, read_pipeline_checkpoint, pipeline_state_path
    except Exception:
        return False
    next_v = int(checkpoint.get("next_v") or 0)
    source_v = int(checkpoint.get("source_v") or 0)
    if next_v < 1:
        return False
    # IDEMPOTENCY GUARD: the seal seam (_slice2b_seal_at_workers_done) is
    # re-entered on every orchestrator route hit while the primary stays parked
    # at workers_done. Without this guard, the re-seed overwrites the consumer
    # slot with the primary's (gate_results=[] / workers_done) state on every
    # tick, destroying the consumer's accumulated gate progress (quality/review/
    # critic results) and resetting checkpoint_revision — a tight high-frequency
    # loop (rev 600+ in minutes) that never lets the gate chain advance.
    # Only seed on the FIRST seal; once the consumer slot file exists with any
    # revision, the consumer owns it.
    try:
        consumer_slot_path = pipeline_state_path(consumer_slot_id)
        if consumer_slot_path is not None and consumer_slot_path.exists():
            existing = read_pipeline_checkpoint(slot_id=consumer_slot_id)
            if isinstance(existing, dict) and existing.get("next_v") == next_v:
                # Consumer slot already seeded for this generation; do NOT
                # overwrite. Return True so the seam reports handled.
                return True
    except Exception:
        pass
    promote_fields = {
        "next_v": next_v,
        "source_v": source_v,
        "stage": str(checkpoint.get("stage") or "workers_done"),
        "master_plan": checkpoint.get("master_plan"),
        "parent2_v": checkpoint.get("parent2_v"),
        "direction_audit": checkpoint.get("direction_audit"),
        "audit_context": checkpoint.get("audit_context"),
        "gate_results": checkpoint.get("gate_results"),
        "worker_failure_count": checkpoint.get("worker_failure_count"),
        "worker_invocation_count": checkpoint.get("worker_invocation_count"),
        "reviewer_feedback": checkpoint.get("reviewer_feedback") or "",
        "charter_digest": checkpoint.get("charter_digest"),
        "candidate_artifact_hash": checkpoint.get("candidate_artifact_hash"),
        "candidate_manifest_digest": checkpoint.get("candidate_manifest_digest"),
        "workflow_run_id": checkpoint.get("workflow_run_id"),
        "audit_attempt": checkpoint.get("audit_attempt"),
        "precommit_attempt": checkpoint.get("precommit_attempt"),
        "precommit_rework_count": checkpoint.get("precommit_rework_count"),
        "official_rework_count": checkpoint.get("official_rework_count"),
        "timeout_extensions": checkpoint.get("timeout_extensions"),
        "literature_probe": checkpoint.get("literature_probe"),
        "prepare_scope_files": checkpoint.get("prepare_scope_files"),
        "official_job": checkpoint.get("official_job"),
        "repair_baseline_artifact_hash": checkpoint.get(
            "repair_baseline_artifact_hash"
        ),
        "review_attempt_journal": checkpoint.get("review_attempt_journal"),
        "identity_replan_history": checkpoint.get("identity_replan_history"),
        "publication_tier": checkpoint.get("publication_tier"),
        "generation_attempt": checkpoint.get("generation_attempt"),
    }
    try:
        ok = bool(
            write_pipeline_checkpoint(slot_id=consumer_slot_id, **promote_fields)
        )
    except Exception:
        return False
    if ok:
        try:
            _o.log_system_event(
                "pipeline.slice2b_consumer_slot_seeded",
                "info",
                (
                    f"Sealed candidate consumer slot seeded for v{next_v} "
                    f"at {consumer_slot_id}; gate chain isolated from primary."
                ),
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "consumer_slot_id": consumer_slot_id,
                },
            )
        except Exception:
            pass
    return ok


def _promote_consumer_slot_to_primary(
    consumer_slot_id, next_v, source_v, *, published_primary
):
    """CAS-collapse the consumer slot's final checkpoint onto the primary.

    After the consumer gate chain reaches PROMOTED (precommit passed), the
    consumer slot file holds gen-N's complete evidence: quality/review/critic/
    precommit ``gate_results``, ``master_plan``, ``audit_context``,
    ``reviewer_feedback``, ``review_attempt_journal``, ``stage`` (the consumer
    advanced through ``critic_checked`` / ``verified``).  The primary lane's
    ``commit_bot`` must see that evidence without re-running the gates.

    This reads the consumer slot under ``no_slot_override()`` (so an explicit
    ``slot_id`` read is not shadowed by any ambient override), builds the
    promote payload, and writes it to PRIMARY as a single CAS against the
    primary's ``expected_checkpoint_revision`` / ``expected_checkpoint_stage``
    / ``expected_workflow_run_id`` (captured from the parked primary).  The CAS
    refuses harmlessly if the primary moved -- the canonical fast-forward then
    re-runs the gates inline (the worst case is double work, never corruption).

    Modeled on ``_promote_draft_to_primary`` (the inverse draft->primary
    collapse).  Non-fatal: any exception is swallowed by the caller so the
    publication path never raises from the collapse.
    """

    try:
        from evolution_infra import (
            read_pipeline_checkpoint,
            write_pipeline_checkpoint,
            no_slot_override,
        )
    except Exception:
        return False
    with no_slot_override():
        consumer = read_pipeline_checkpoint(slot_id=consumer_slot_id)
    if not isinstance(consumer, dict) or not consumer:
        return False
    if int(consumer.get("next_v") or 0) != int(next_v):
        return False
    # Primary CAS expectations come from the snapshot captured when the primary
    # was parked (the quiescent reference).  ``published_primary`` is that
    # snapshot; absent it, fall back to the live primary read.
    primary_ref = published_primary if isinstance(published_primary, dict) else None
    if primary_ref is None:
        with no_slot_override():
            primary_ref = read_pipeline_checkpoint() or {}
    expected_revision = primary_ref.get("checkpoint_revision")
    expected_stage = primary_ref.get("stage")
    expected_run_id = primary_ref.get("workflow_run_id")
    # The PRIMARY's workflow_run_id is the publication identity the collapse
    # targets.  The consumer slot was seeded from the primary so they share it
    # in production, but pin the primary's value explicitly so a divergent
    # consumer run_id (defensive: legacy / cross-process drift) cannot trip the
    # identity-replacement guard (``Refusing checkpoint workflow identity
    # replacement``).  The CAS ``expected_workflow_run_id`` already pins the
    # existing primary id; this ``workflow_run_id`` arg pins the request id.
    promote_fields = {
        "next_v": int(next_v),
        "source_v": int(source_v),
        "stage": str(consumer.get("stage") or "verified"),
        "master_plan": consumer.get("master_plan"),
        "parent2_v": consumer.get("parent2_v"),
        "direction_audit": consumer.get("direction_audit"),
        "audit_context": consumer.get("audit_context"),
        "gate_results": consumer.get("gate_results"),
        "worker_failure_count": consumer.get("worker_failure_count"),
        "worker_invocation_count": consumer.get("worker_invocation_count"),
        "reviewer_feedback": consumer.get("reviewer_feedback") or "",
        "charter_digest": consumer.get("charter_digest"),
        "candidate_artifact_hash": consumer.get("candidate_artifact_hash"),
        "candidate_manifest_digest": consumer.get("candidate_manifest_digest"),
        "workflow_run_id": expected_run_id or consumer.get("workflow_run_id"),
        "audit_attempt": consumer.get("audit_attempt"),
        "precommit_attempt": consumer.get("precommit_attempt"),
        "precommit_rework_count": consumer.get("precommit_rework_count"),
        "official_rework_count": consumer.get("official_rework_count"),
        "timeout_extensions": consumer.get("timeout_extensions"),
        "literature_probe": consumer.get("literature_probe"),
        "prepare_scope_files": consumer.get("prepare_scope_files"),
        "official_job": consumer.get("official_job"),
        "repair_baseline_artifact_hash": consumer.get(
            "repair_baseline_artifact_hash"
        ),
        "review_attempt_journal": consumer.get("review_attempt_journal"),
        "identity_replan_history": consumer.get("identity_replan_history"),
        "publication_tier": consumer.get("publication_tier"),
        "generation_attempt": consumer.get("generation_attempt"),
        "expected_checkpoint_revision": expected_revision,
        "expected_checkpoint_stage": expected_stage,
        "expected_workflow_run_id": expected_run_id,
    }
    try:
        with no_slot_override():
            ok = bool(write_pipeline_checkpoint(**promote_fields))
    except Exception:
        return False
    if ok:
        try:
            _o.log_system_event(
                "pipeline.slice2b_consumer_slot_promoted",
                "info",
                (
                    f"Consumer slot collapsed to primary for v{next_v} "
                    f"({consumer_slot_id}); commit_bot may publish from primary."
                ),
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "consumer_slot_id": consumer_slot_id,
                    "expected_revision": expected_revision,
                    "expected_stage": expected_stage,
                },
            )
        except Exception:
            pass
        # TERMINAL CLEANUP: now that the consumer slot's evidence has been
        # collapsed onto the primary, clear the consumer slot checkpoint file so
        # it does not orphan (an orphan consumer slot file replays stale gate
        # evidence if the version is ever reused).  Best-effort: the collapse
        # already succeeded; a clear failure leaves a harmless history file.
        if ok:
            try:
                from evolution_infra import clear_pipeline_checkpoint

                clear_pipeline_checkpoint(slot_id=consumer_slot_id)
            except Exception:
                pass
    return ok


async def _slice2b_seal_at_workers_done(checkpoint, next_v, source_v, *, ui, outcome):
    """Seal the candidate and launch the background consumer gate chain.

    Returns True iff the Slice 2b one-ahead path handled this ``workers_done``
    seam.  When True, the canonical ``run_quality_gates`` handler is NOT
    invoked inline; the consumer task runs the *unchanged* canonical gate chain
    (``run_quality_gates`` -> ``run_review`` -> ``run_critic`` ->
    ``run_precommit_eval`` -> ``commit_bot``) in the background, and the
    producer is cleared to begin the next ``prepare_generation``.  The
    promotion barrier at ``commit_bot`` (see :func:`_slice2b_promotion_barrier`)
    synchronizes publication.

    FROZEN-SNAPSHOT ISOLATION: the consumer slot is seeded with a FULL copy of
    the primary checkpoint (``_slice2b_seed_consumer_checkpoint``) at seal
    time, and the gate chain runs under ``active_slot_override(consumer_slot)``
    (see :func:`producer_consumer_slice2b_activation._run_gate_chain`).  This
    isolates every consumer gate read/write to
    ``pipeline_state_consumer-<candidate_id>.json`` so the parked primary
    ``pipeline_state.json`` is never raced.  At promotion, the consumer slot is
    CAS-collapsed back onto primary (:func:`_promote_consumer_slot_to_primary`)
    so ``commit_bot`` sees the consumer's evidence.
    """

    try:
        from producer_consumer_slice2b_activation import (
            Slice2bActivation,
            build_snapshot_from_checkpoint,
            slice2b_active,
            stage_is_workers_done_seam,
        )
    except Exception:
        return False
    if not stage_is_workers_done_seam(checkpoint):
        return False
    if not slice2b_active():
        return False

    activation = _slice2b_ensure_activation()
    if activation is None:
        return False

    # ALREADY-SEALED GUARD: this seam is re-entered on every orchestrator route
    # hit while the primary stays parked at workers_done. If the candidate is
    # already sealed (lifecycle SEALED/CONSUMING, not terminal), do NOT re-seal
    # / re-seed / re-schedule — return False so the route falls through to the
    # _slice2b_park_primary_consumer_gates branch (which sleeps the primary
    # while the consumer owns the gate chain). Without this guard the seam
    # returns True on every tick, the primary re-routes, and the loop spins
    # forever (observed: "Resuming v30 at workers_done" every ~4s, consumer
    # slot checkpoint_revision climbing to 900+).
    _already_candidate_id = str(
        checkpoint.get("candidate_id") or f"candidate-v{next_v}"
    )
    try:
        _existing = activation.ledger.snapshot(_already_candidate_id)
    except Exception:
        _existing = None
    if _existing is not None:
        # Candidate is already sealed (SEALED/CONSUMING) OR already terminal
        # (PROMOTED/REJECTED). Either way, do NOT re-seal/re-seed/re-schedule.
        # Return False so the route falls through:
        #   - SEALED/CONSUMING (not terminal) -> the park branch sleeps the
        #     primary while the consumer owns the gate chain.
        #   - PROMOTED -> the promoted fast-forward (next_tool="commit_bot")
        #     publishes without re-running the consumer-owned gates.
        #   - REJECTED -> the rejected-candidate abandon path.
        # Returning True here (as the original seam did on every re-entry)
        # makes the primary re-route and spin forever.
        return False


        return False

    # The orchestrator already computed the content-bound artifact/manifest/
    # charter digests for the canonical gate chain; Slice 2b reuses them.  When
    # they are not present on the checkpoint projection (e.g. an older
    # checkpoint), Slice 2b refuses to seal rather than guessing -- the
    # canonical inline path then runs unchanged.
    artifact_hash = checkpoint.get("candidate_artifact_hash") or checkpoint.get(
        "artifact_hash"
    )
    manifest_digest = checkpoint.get("candidate_manifest_digest") or checkpoint.get(
        "manifest_digest"
    )
    charter_digest = checkpoint.get("charter_digest")
    if not (artifact_hash and manifest_digest and charter_digest):
        return False

    snapshot = build_snapshot_from_checkpoint(
        checkpoint,
        artifact_hash=artifact_hash,
        manifest_digest=manifest_digest,
        charter_digest=charter_digest,
        quality_native_match_timing_plan=checkpoint.get(
            "quality_native_match_timing_plan"
        ),
    )
    candidate_id = snapshot["candidate_id"]

    sealed = activation.seal_at_workers_done(
        snapshot=snapshot,
        run_id=snapshot["workflow_run_id"],
        job_id=f"job:{snapshot['draft_id']}:quality-static",
        idempotency_key=f"{snapshot['draft_id']}:quality-static:v1",
        artifact_digest=artifact_hash,
        resource_claim={
            "resource_class": "cpu",
            "cpu_slots": 1,
            "memory_mb": 512,
            "gpu_slots": 0,
            "match_slots": 0,
            "official_slots": 0,
        },
        retry_policy={
            # ``max_attempts`` counts envelope LEASE/RECLAIM cycles, not gate
            # executions.  Each process restart's ``_force_recover_consumer_
            # effect`` reclaims the lease and bumps ``attempt`` by 1 (workflow_
            # kernel_effects.py lease_effect).  At the former max_attempts=3 a
            # candidate that survived only 3 restarts (e.g. deploy + 2 crashes,
            # or repeated infra-retry restarts) exhausted the effect WITHOUT
            # EVER running a gate — the documented v52 wedge (2026-08-05).
            # 20 tolerates a restart every ~12 min across the 4h envelope
            # deadline while staying bounded.
            "max_attempts": 20,
            "initial_backoff_sec": 1.0,
            "backoff_multiplier": 2.0,
            "max_backoff_sec": 10.0,
            "retryable_outcomes": ["infrastructure_failure"],
        },
        deadline={
            "submitted_at_epoch": float(checkpoint.get("last_update_ts") or 0.0),
            "not_before_epoch": float(checkpoint.get("last_update_ts") or 0.0),
            # 4-hour envelope deadline: the consumer gate chain under GLM
            # effort=max (quality+review+critic+precommit, each 5-10 min) plus
            # precommit native matches (~30-50 min) plus fault-retry restarts
            # can easily exceed 1 hour.  An expired envelope makes
            # _bounded_lease_seconds reject every reclaim, wedging the consumer
            # forever.  4 hours gives generous headroom for a full generation
            # including restarts.
            "expires_at_epoch": float(checkpoint.get("last_update_ts") or 0.0) + 14400.0,
        },
        evaluation_contract_digest=str(
            checkpoint.get("evaluation_contract_digest") or charter_digest
        ),
        executor_digest=str(checkpoint.get("executor_digest") or charter_digest),
        repository_digest=str(checkpoint.get("repository_digest") or charter_digest),
        runtime_digest=str(checkpoint.get("runtime_digest") or charter_digest),
    )

    # FROZEN-SNAPSHOT ISOLATION: seed a consumer checkpoint slot with a FULL
    # copy of the primary checkpoint, so the background gate chain (which runs
    # under active_slot_override(consumer_slot)) reads/writes an isolated file
    # instead of racing the parked primary.  Persist the consumer slot id on
    # the candidate lifecycle so it survives a restart (boot recovery re-enters
    # the override with the same id).
    consumer_slot_id = _slice2b_consumer_slot_id(candidate_id)
    _slice2b_seed_consumer_checkpoint(checkpoint, consumer_slot_id)
    try:
        activation.ledger.set_consumer_checkpoint_slot(
            candidate_id=candidate_id,
            consumer_checkpoint_slot=consumer_slot_id,
        )
    except Exception:
        pass

    # Schedule the background consumer task running the canonical gate chain.
    # The seam runs synchronously (outside the orchestrator event loop), so we
    # register the factory here; the promotion barrier or the orchestrator loop
    # drives it via ``ensure_consumer_running`` from inside the loop.
    activation.schedule_consumer(
        candidate_id=candidate_id,
        gate_runner_factory=_o._slice2b_gate_runner_factory(next_v, source_v),
        consumer_slot_id=consumer_slot_id,
    )
    # Launch the consumer task immediately so the gate chain starts running in
    # the background while the producer advances to the next prepare.  This is
    # the one-ahead parallelism the dual-line model exists for: without it the
    # consumer only starts at the commit_bot promotion barrier (serial, not
    # parallel).  The seam is now async (called from the async
    # _try_deterministic_checkpoint_route), so we can await the launch here.
    await activation.ensure_consumer_running(candidate_id)

    if outcome is not None:
        outcome.clear()
        outcome.update({
            "checkpoint": checkpoint,
            "route": {"next_tool": "run_quality_gates", "stage": "workers_done"},
            "result": {
                "success": True,
                "slice2b_sealed": True,
                "candidate_id": candidate_id,
                "effect_id": sealed["effect_id"],
            },
            "terminal_abandon_result": None,
        })
    try:
        _o.log_system_event(
            "pipeline.slice2b_sealed_at_workers_done",
            "info",
            f"Slice 2b sealed v{next_v}; consumer gate chain launched in background, producer may advance.",
            {
                "next_v": next_v,
                "source_v": source_v,
                "candidate_id": candidate_id,
                "effect_id": sealed["effect_id"],
            },
        )
    except Exception:
        pass
    return True


async def _slice2b_promotion_barrier(checkpoint, next_v, source_v):
    """Synchronous fail-closed barrier before ``commit_bot`` publishes.

    Returns True iff Slice 2b is active AND there is a sealed candidate for
    this generation awaiting promotion.  When True, the caller MUST first
    :func:`await activation.await_promotion` and only proceed with the
    canonical ``commit_bot`` if it returns a promoted entry.  Returns False in
    all other cases (slice2b inactive, no sealed candidate, already promoted)
    so the canonical inline ``commit_bot`` runs unchanged.

    Phase 5b: after the consumer promotes gen N, this also attempts a
    best-effort promotion of the one-ahead draft (gen N+1) from the draft
    slot to the primary slot.  The promotion is idempotent and non-fatal: if
    the primary checkpoint is still active (gen N not yet fully archived),
    the draft is left in place for a later attempt or for the canonical
    prepare path to rediscover.
    """

    try:
        from producer_consumer_slice2b_activation import slice2b_active
    except Exception:
        return False
    if not slice2b_active():
        return False
    activation = _slice2b_ensure_activation()
    if activation is None:
        return False
    candidate_id = str(
        checkpoint.get("candidate_id") or f"candidate-v{next_v}"
    )
    # Check BOTH the in-memory registry AND the persisted lifecycle: after a
    # process restart the in-memory _sealed_snapshots may not yet contain a
    # candidate that was sealed in a prior process (recover_at_boot rehydrates
    # it, but this barrier can run before that completes). The persisted ledger
    # is the source of truth for "was this candidate sealed?".
    _persisted_entry = None
    try:
        _persisted_entry = activation.ledger.snapshot(candidate_id)
    except Exception:
        _persisted_entry = None
    if (
        candidate_id not in activation._sealed_snapshots
        and _persisted_entry is None
    ):
        # No one-ahead seal for this generation: canonical inline path.
        return False
    # Capture the QUIESCENT primary snapshot BEFORE awaiting promotion: this is
    # the CAS target the consumer-slot collapse will compare against.  The
    # primary is parked (stage stays ``workers_done``) for the whole consumer
    # window, so this snapshot is the reference the collapse CAS expects.
    # Read under no_slot_override() so an ambient draft override never shadows
    # the primary read.
    try:
        from evolution_infra import read_pipeline_checkpoint, no_slot_override

        with no_slot_override():
            parked_primary = read_pipeline_checkpoint() or {}
    except Exception:
        parked_primary = {}
    if activation.ledger.is_promoted(candidate_id):
        # Already promoted by the consumer; canonical commit_bot may publish.
        # Still collapse the consumer slot if it has evidence the primary lacks
        # (idempotent: a second collapse CAS-fails harmlessly once primary
        # matches).  ``is_promoted`` on a hot restart means the consumer already
        # finished in a prior process; the slot file may already be collapsed.
        consumer_slot = _slice2b_consumer_slot_id(candidate_id)
        _promote_consumer_slot_to_primary(
            consumer_slot, next_v, source_v, published_primary=parked_primary
        )
        return False
    if activation.ledger.is_terminal(candidate_id) and not activation.ledger.is_promoted(candidate_id):
        # Defense-in-depth: a REJECTED candidate reaching the commit_bot barrier
        # (e.g. via a future checkpoint-stage race) must NOT proceed to
        # await_promotion, which raises Slice2bError(one_ahead_barrier_rejected)
        # and would propagate as an unhandled infra crash.  Route to the
        # canonical rejected-candidate abandon path instead (return False so the
        # router re-derives the next route, where _slice2b_consumer_rejected
        # drives the abandon).
        return False
    # Slice 2b owns this publication: wait for the consumer to finish.
    await activation.await_promotion(candidate_id=candidate_id)
    # FROZEN-SNAPSHOT ISOLATION: collapse the consumer slot's final checkpoint
    # (carrying the consumer's quality/review/critic/precommit gate_results +
    # stage) onto the PRIMARY as a single CAS write, so the primary
    # ``commit_bot`` sees the consumer's evidence without re-running the gates.
    # Non-fatal: a CAS refusal (primary moved) leaves the canonical fast-
    # forward to re-run the gates inline -- the worst case is double work,
    # never corruption.
    consumer_slot = _slice2b_consumer_slot_id(candidate_id)
    _promote_consumer_slot_to_primary(
        consumer_slot, next_v, source_v, published_primary=parked_primary
    )
    # Best-effort draft promotion now that gen N's consumer has promoted.
    try:
        _promote_draft_to_primary(next_v)
    except Exception as exc:
        try:
            _o.log_system_event(
                "orchestrator.slice2b_draft_promotion_failed",
                "warn",
                f"One-ahead draft promotion failed at barrier: "
                f"{type(exc).__name__}: {exc}",
                {"next_v": next_v, "source_v": source_v},
            )
        except Exception:
            pass
    return True


def _promote_draft_to_primary(published_next_v):
    """Best-effort move of the one-ahead draft checkpoint to the primary slot.

    Deep-parallelism: the draft drives its FULL gate chain, so by the time the
    primary publishes it may already be ``verified`` in its own consumer slot
    (``consumer-candidate-v<reserved_next_v>``).  This function prefers that
    verified-evidence fast path: if the draft's consumer slot exists and is at
    ``verified``, it collapses the FULL consumer evidence (quality/review/
    critic/precommit gate_results + stage) onto the next primary so the primary
    loop resumes directly at ``commit_bot`` without re-running any gate.

    Fallback: if the draft has not yet sealed (still pre-computing), or its
    consumer is still mid-chain (not ``verified``), the legacy path writes the
    draft's ``workers_done`` checkpoint to primary so the canonical inline gate
    chain owns validation.  In every case the primary write goes through the
    canonical CAS, so it is refused harmlessly when the primary checkpoint is
    still active (gen N mid-publication) -- the draft is left in place and the
    promotion is retried at the next barrier firing or rediscovered by
    ``prepare_generation``.

    Non-fatal: any exception is swallowed by the caller.  Never raises into
    the publication path.
    """

    try:
        from evolution_infra import (
            read_pipeline_checkpoint,
            write_pipeline_checkpoint,
            clear_pipeline_checkpoint,
            no_slot_override,
        )
    except Exception:
        return False
    # Read the draft slot explicitly (bypass any ambient override).
    draft = read_pipeline_checkpoint(slot_id="draft")
    if not isinstance(draft, dict) or not draft:
        return False
    if draft.get("stage") != "workers_done":
        # Draft not yet at a promotable stage; leave it pre-computing.
        return False
    try:
        draft_next_v = int(draft.get("next_v") or 0)
    except (TypeError, ValueError):
        return False
    try:
        published_v = int(published_next_v)
    except (TypeError, ValueError):
        return False
    formal_next_v = published_v + 1
    # Shadow drafts may hold a provisional next_v; remap onto formal successor.
    if draft.get("is_draft") is not True and draft_next_v != formal_next_v:
        # Non-shadow mismatch: do not promote a stale draft.
        return False
    try:
        from generation_scheduler import _relocate_draft_candidate_to_live

        _relocate_draft_candidate_to_live(draft_next_v, formal_next_v)
    except Exception:
        pass

    # --- Deep-parallelism: the draft may have already driven its full gate
    # chain to ``verified`` in its own consumer slot
    # (``consumer-candidate-v<reserved_next_v>``).  We deliberately do NOT
    # collapse that verified evidence directly onto the primary here: the
    # checkpoint stage-transition guard refuses an empty-primary -> verified
    # write, and more importantly the existing promotion-barrier machinery
    # already handles this case correctly.  The draft's consumer slot
    # ``candidate_id`` (``candidate-v<formal_next_v>``) MATCHES the next
    # primary's candidate_id, so when the legacy promote below writes the
    # primary to ``workers_done`` and the primary's seal seam fires, the
    # ALREADY-SEALED GUARD (``_slice2b_seal_at_workers_done``) recognizes the
    # candidate as already-sealed+promoted, parks the primary, and the
    # promotion barrier (``_slice2b_promotion_barrier``) finds the promoted
    # candidate, collapses the consumer slot's verified evidence, and clears
    # commit_bot to publish -- WITHOUT re-running any gate.  This maximally
    # reuses the tested primary-consumer path.  We therefore preserve the
    # draft's consumer slot (do NOT clear it) so the barrier can collapse it.

    # Build a minimal promote payload from the draft fields.  Remap onto the
    # formal next_v so the primary loop's deterministic recovery picks up at
    # run_quality_gates.  The CAS refuses if the primary is still active.
    promote_fields = {
        "next_v": formal_next_v,
        "source_v": int(draft.get("source_v") or 0),
        "stage": "workers_done",
        "master_plan": draft.get("master_plan"),
        "parent2_v": draft.get("parent2_v"),
        "direction_audit": draft.get("direction_audit"),
        "audit_context": draft.get("audit_context"),
        "gate_results": draft.get("gate_results"),
        "worker_failure_count": draft.get("worker_failure_count"),
        "worker_invocation_count": draft.get("worker_invocation_count"),
        "reviewer_feedback": draft.get("reviewer_feedback") or "",
        "charter_digest": draft.get("charter_digest"),
        "candidate_artifact_hash": draft.get("candidate_artifact_hash"),
        "candidate_manifest_digest": draft.get("candidate_manifest_digest"),
        "workflow_run_id": draft.get("workflow_run_id"),
        "audit_attempt": draft.get("audit_attempt"),
        "precommit_attempt": draft.get("precommit_attempt"),
        "precommit_rework_count": draft.get("precommit_rework_count"),
        "official_rework_count": draft.get("official_rework_count"),
        "timeout_extensions": draft.get("timeout_extensions"),
        "literature_probe": draft.get("literature_probe"),
        "prepare_scope_files": draft.get("prepare_scope_files"),
        "official_job": draft.get("official_job"),
        "repair_baseline_artifact_hash": draft.get(
            "repair_baseline_artifact_hash"
        ),
        "review_attempt_journal": draft.get("review_attempt_journal"),
        "identity_replan_history": draft.get("identity_replan_history"),
        "publication_tier": draft.get("publication_tier"),
    }
    # Write to primary with the override bypassed (force primary slot).
    with no_slot_override():
        ok = bool(write_pipeline_checkpoint(**promote_fields))
    if not ok:
        # Primary still active or CAS refused; leave the draft for retry.
        return False
    # Success: clear the draft slot.
    clear_pipeline_checkpoint(slot_id="draft")
    # Release the multi-ahead version reservation for this draft slot so the
    # next draft can reserve the subsequent version (ordered promotion).
    try:
        activation = _slice2b_ensure_activation()
        if activation is not None:
            activation.ledger.release_draft_version(slot_id="draft")
    except Exception:
        pass
    try:
        _o.log_system_event(
            "orchestrator.slice2b_draft_promoted",
            "info",
            f"One-ahead draft promoted to primary at v{formal_next_v}",
            {
                "next_v": formal_next_v,
                "published_v": published_v,
                "provisional_next_v": draft_next_v,
            },
        )
    except Exception:
        pass
    return True


async def _try_deterministic_checkpoint_route(
    recovery,
    ui=None,
    *,
    log_level: str = "warn",
    label: str = "[Recovery]",
    cost_policy: GenerationCostPolicy | None = None,
    shutdown_mgr=None,
    outcome: dict | None = None,
):
    """Execute safe checkpoint routes without asking the Orchestrator LLM again."""
    if not recovery or recovery.get("action") != "resume":
        return False
    checkpoint = recovery.get("checkpoint") or {}
    _o._bind_generation_cost_runtime(
        checkpoint,
        ui=ui,
        policy=cost_policy,
    )
    _o._check_generation_cost_policy(ui)
    route = _o._resolve_recovery_route(checkpoint)
    if not route:
        return False

    next_tool = route.get("next_tool")
    next_v = route.get("next_v")
    source_v = route.get("source_v")
    parent2_v = route.get("parent2_v")
    stage = route.get("stage")

    if _o._deterministic_route_requires_llm(checkpoint, str(next_tool)):
        if not await _o._honor_active_llm_pause(ui, shutdown_mgr):
            return False

    saved_session_id = _o._load_orchestrator_session()
    if saved_session_id:
        session_clear_reason = (
            "deterministic_master_planned_route"
            if stage == "master_planned"
            else f"deterministic_{next_tool}_route"
        )
        _o._clear_orchestrator_session(reason=session_clear_reason)

    if next_v is None or source_v is None:
        return False

    if next_tool == "run_crossover":
        try:
            parent2_v = int(parent2_v) if parent2_v is not None else None
        except (TypeError, ValueError):
            parent2_v = None
        if parent2_v is None:
            return False

    # Slice 2b rejected-candidate guard (defense-in-depth): a sealed candidate
    # whose consumer gate chain reached the terminal ``rejected`` state can
    # never be promoted, so the primary lane MUST abandon this generation and
    # let the epoch allocate a fresh successor.  The dedicated workers_done
    # branch below already runs this check, but only when ``next_tool`` is
    # exactly ``run_quality_gates``.  If a checkpoint at ``workers_done`` ever
    # resolves to a different ``next_tool`` (a future route-policy change, or a
    # recovery edge), or if the primary re-enters at a later consumer-owned
    # gate (run_review/run_critic/run_precommit_eval) after the consumer
    # already rejected, the workers_done-only check is bypassed and the primary
    # parks/spins at that gate forever with zero LLM work (the documented
    # ``workers_done`` wedge).  Running this check here, for ANY next_tool while
    # the primary still owns a sealed candidate for this generation, makes the
    # abandon path robust to route-policy drift and gate-stage races alike.
    _rejected_reason = _slice2b_consumer_rejected(checkpoint, next_v)
    if _rejected_reason is not None:
        return await _slice2b_abandon_rejected_candidate(
            checkpoint,
            next_v,
            source_v,
            ui=ui,
            outcome=outcome,
            reject_reason=_rejected_reason,
        )

    # Slice 2b one-ahead seam: at workers_done, seal the candidate and launch
    # the background consumer gate chain instead of blocking on the inline
    # run_quality_gates.  The canonical gate chain runs unchanged inside the
    # consumer task; the producer is cleared to begin the next prepare.  When
    # slice2b is inactive (the default) or the seam refuses (missing digests,
    # high-water full), this returns False and the inline path runs unchanged.
    if next_tool == "run_quality_gates" and stage == "workers_done":
        if _slice2b_consumer_in_flight(checkpoint, next_v):
            # Before parking, reap any zombie consumer: a candidate left
            # non-terminal (``consuming``) whose asyncio task is done/absent
            # with an expired effect lease.  Without this the primary lane
            # busy-parks every second forever waiting for a consumer that
            # will never finish.  ``_slice2b_reap_dead_consumer`` marks the
            # candidate rejected; the ``_slice2b_consumer_rejected`` check
            # below then canonically abandons the generation.
            if _slice2b_reap_dead_consumer(checkpoint, next_v):
                # Candidate is now terminal-rejected; fall through to the
                # rejected-candidate abandon path below (do NOT park).
                pass
            else:
                return _slice2b_park_primary_consumer_gates(
                    checkpoint,
                    next_v,
                    source_v,
                    next_tool,
                    ui=ui,
                    outcome=outcome,
                    reason="consumer_gate_chain_in_flight",
                )
        # A rejected slice2b candidate (infrastructure failure or gate
        # failure) can never be promoted, so the primary lane must not spin
        # at workers_done forever.  Canonically abandon the generation so the
        # epoch allocates a fresh successor instead of looping on the same
        # dead checkpoint.
        rejected_reason = _slice2b_consumer_rejected(checkpoint, next_v)
        if rejected_reason is not None:
            return await _slice2b_abandon_rejected_candidate(
                checkpoint,
                next_v,
                source_v,
                ui=ui,
                outcome=outcome,
                reject_reason=rejected_reason,
            )
        if await _slice2b_seal_at_workers_done(
            checkpoint, next_v, source_v, ui=ui, outcome=outcome
        ):
            return True

    # Slice 2b park: while the background consumer owns the canonical gate chain
    # for a sealed candidate, the primary lane must not re-run those LLM gates.
    if (
        next_tool in _SLICE2B_CONSUMER_OWNED_GATES
        and _slice2b_consumer_in_flight(checkpoint, next_v)
    ):
        # Reap any zombie consumer before parking (see the workers_done branch
        # above for the full rationale).  If reaped, the candidate is now
        # terminal-rejected; fall through to the rejected-candidate abandon.
        if _slice2b_reap_dead_consumer(checkpoint, next_v):
            pass
        else:
            return _slice2b_park_primary_consumer_gates(
                checkpoint,
                next_v,
                source_v,
                next_tool,
                ui=ui,
                outcome=outcome,
                reason="consumer_gate_chain_in_flight",
            )

    # Slice 2b promoted fast-forward: once the consumer has PROMOTED the
    # candidate (its commit_bot ran successfully), the consumer-owned gates
    # (quality/review/critic/precommit) are already done.  ``_slice2b_consumer_in_flight``
    # returns False for a terminal promoted candidate, so without this guard
    # the primary lane falls through and re-runs run_quality_gates inline --
    # a double-execution that re-submits the consumer jobs and trips
    # ``producer_consumer_idempotency_conflict``.  Fast-forward directly to
    # commit_bot, where the promotion barrier is a no-op (candidate already
    # promoted) and the canonical commit_bot publishes idempotently.
    if (
        next_tool in _SLICE2B_CONSUMER_OWNED_GATES
        and _slice2b_consumer_promoted(checkpoint, next_v)
    ):
        # The consumer promoted the candidate.  BEFORE rewriting next_tool to
        # commit_bot, collapse the consumer slot's verified gate evidence onto
        # the primary so the primary advances past workers_done to verified/
        # critic_checked.  Without this collapse the primary stays at
        # workers_done, the route guard blocks commit_bot
        # (pipeline_route_guard_blocked), and the generation deadlocks even
        # though the consumer already finished.  The collapse is idempotent
        # (CAS-fails harmlessly if the primary already matches).
        try:
            _candidate_id = str(
                checkpoint.get("candidate_id") or f"candidate-v{next_v}"
            )
            _consumer_slot = _slice2b_consumer_slot_id(_candidate_id)
            from evolution_infra import read_pipeline_checkpoint, no_slot_override

            with no_slot_override():
                _parked_primary = read_pipeline_checkpoint() or {}
            _promote_consumer_slot_to_primary(
                _consumer_slot,
                next_v,
                source_v,
                published_primary=_parked_primary,
            )
        except Exception:
            pass
        next_tool = "commit_bot"

    # Slice 2b promotion barrier: at commit_bot, the canonical publication may
    # only proceed once the background consumer has promoted the sealed
    # candidate.  When slice2b is inactive or there is no sealed candidate for
    # this generation, this is a no-op and the inline commit_bot runs unchanged.
    if next_tool == "commit_bot":
        if await _slice2b_promotion_barrier(checkpoint, next_v, source_v):
            # Consumer promoted; fall through to the canonical commit_bot, which
            # owns the actual publication authority (unchanged).
            pass

    try:
        handler, args = _o._deterministic_route_handler_and_args(
            next_tool,
            checkpoint,
            next_v,
            source_v,
            parent2_v,
        )
    except Exception:
        handler, args = None, None

    if not callable(handler):
        return False

    msg = (
        f"{label} Deterministically routing v{next_v} at {stage} "
        f"to {next_tool}."
    )
    if ui:
        ui.log_history(msg, log_level)
    else:
        if log_level == "info":
            _o.log.info(msg)
        else:
            _o.log.warning(msg)
    try:
        _o.log_system_event(
            f"pipeline.deterministic_route_{next_tool}",
            log_level,
            msg,
            {
                "next_v": next_v,
                "source_v": source_v,
                "stage": stage,
                "parent2_v": checkpoint.get("parent2_v"),
                "route": route,
            },
        )
    except Exception:
        pass

    try:
        # OFF-LOAD THE CANONICAL HANDLER OFF THE ASGI EVENT LOOP. This is the
        # ROOT dispatch site for every deterministic checkpoint route (recovery
        # crash-restart AND normal streaming, on BOTH the primary lane and the
        # draft lane via _draft_prepare_task -> _advance_deterministic_recovery).
        # The handlers (prepare_next_gen / run_direction_audit / run_master /
        # run_quality_gates / run_review / run_critic / run_precommit_eval /
        # commit_bot / run_crossover / run_archivist / abandon_generation) are
        # async but perform heavy SYNCHRONOUS blocking I/O on the event loop:
        #   - the @tool wrapper's ensure_runtime_git_guard runs a ``git``
        #     subprocess (worktree status) at the top of every call;
        #   - prepare_next_gen calls get_active_bots/resolve_national_bot_spec
        #     (ssh-keygen signature verification subprocess);
        #   - run_precommit_eval drives the native TCP precommit match sequence
        #     (12 x 70-hand matches) whose _prepare_native_spec does inline file
        #     enumeration + hashing and whose subprocess lifecycle waits inline;
        #   - commit_bot does certificate validation.
        # Awaiting ``handler(args)`` directly on the orchestrator's ASGI loop
        # blocks every HTTP request for the full handler duration (10+ minutes
        # per precommit). This is the SAME defect class as the three prior
        # fixes (86b7aa77 + 30626e87 + 72653707) but the ROOT dispatch site;
        # offloading HERE covers all stage handlers uniformly on both lanes.
        #
        # run_async_off_event_loop drives the coroutine on a fresh PRIVATE event
        # loop inside an owned worker thread (same single-wakeup / context-
        # propagating boundary as run_blocking_isolated), so the orchestrator's
        # ASGI loop stays free to serve HTTP while the handler's own async
        # primitives (run_precommit_eval's native TCP match engine uses
        # asyncio.start_server/create_task/loop.time) still function on the
        # worker's private loop. ContextVars propagate via copy_context: the
        # post_publication_handoff authority token (set by
        # system_deterministic_route_authority below) and the native-match
        # dispatch nonce are visible inside the worker, so the @tool runtime
        # guard and slot-scoped checkpoint I/O behave identically. CAS/
        # checkpoint identities and recovery logic are unchanged -- only the
        # loop the handler coroutine runs on changes.
        if recovery.get("post_publication_handoff") is True:
            from tool_runtime_guard import system_deterministic_route_authority

            # Enter the authority context BEFORE offloading so the
            # _SYSTEM_DETERMINISTIC_ROUTE ContextVar is bound in the calling
            # context and propagates into the worker thread via copy_context.
            with system_deterministic_route_authority(next_tool, checkpoint):
                result = await run_async_off_event_loop(
                    handler,
                    args,
                    thread_name_prefix=f"det-route-{next_tool}",
                )
        else:
            result = await run_async_off_event_loop(
                handler,
                args,
                thread_name_prefix=f"det-route-{next_tool}",
            )
        data = _o._extract_tool_result_json(result)
        if outcome is not None:
            outcome.clear()
            outcome.update({
                "checkpoint": checkpoint,
                "route": route,
                "result": data,
                "terminal_abandon_result": _o._completed_abandon_tool_result(data),
            })
        # Direct checkpoint recovery bypasses the SDK ToolResult stream where
        # this conversion normally happens.  Re-establish the same typed
        # availability boundary before generic tool-error routing can retry an
        # Orchestrator/Worker or consume an infrastructure attempt.
        _o._raise_for_llm_availability_tool_result(data)
    except LLMAvailabilityBlocked as exc:
        # Direct deterministic routes do not pass through _run_one_cycle's SDK
        # stream catch. Persist the same durable control state here and leave the
        # checkpoint/attempt untouched.
        try:
            _o.persist_llm_pause(exc)
        except Exception as pause_exc:
            if ui:
                ui.log_history(
                    f"[Recovery] LLM pause persistence failed closed: {pause_exc}",
                    "error",
                )
                ui.set_status(
                    "Stopped: LLM pause persistence failed",
                    is_working=False,
                )
            _o.log.error(
                "Deterministic route could not persist LLM pause: %s",
                pause_exc,
            )
            raise LLMAvailabilityPauseError(
                "deterministic route could not persist the classified LLM pause"
            ) from pause_exc
        _o._clear_orchestrator_session(reason="deterministic_llm_availability_blocked")
        if await _o._honor_active_llm_pause(ui, shutdown_mgr):
            return True
        return False
    except LLMAvailabilityPauseError as exc:
        if ui:
            ui.log_history(f"[Recovery] LLM pause control failed closed: {exc}", "error")
            ui.set_status("Stopped: LLM pause control invalid", is_working=False)
        _o.log.error("Deterministic route LLM pause control failed closed: %s", exc)
        raise
    _o._check_generation_cost_policy(ui)
    error = data.get("error")
    success = data.get("success")
    worker_terminal_abandon = (
        next_tool == "execute_workers"
        and _o._is_worker_terminal_abandon_result(data)
    )
    if data.get("pending") and data.get("action") == "poll_commit_bot":
        wait_sec = max(5.0, min(60.0, float(data.get("retry_after_sec", 30) or 30)))
        try:
            _o.log_system_event(
                "pipeline.deterministic_route_pending",
                "info",
                f"Durable official certification remains pending for v{next_v}; polling in {wait_sec:g}s",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": stage,
                    "next_tool": next_tool,
                    "retry_after_sec": wait_sec,
                },
            )
        except Exception:
            pass
        await asyncio.sleep(wait_sec)
        return True
    if next_tool == "run_master" and _o._is_master_ensemble_pending_retry(
        data,
        checkpoint,
    ):
        wait_sec = max(
            5.0,
            min(60.0, float(data.get("retry_after_sec", 5) or 5)),
        )
        try:
            _o.log_system_event(
                "pipeline.deterministic_master_role_retry_pending",
                "warn" if not data.get("needs_attention") else "error",
                (
                    f"Journaled Master role {data.get('slot')} remains pending "
                    f"for v{next_v}; retrying in {wait_sec:g}s"
                ),
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": stage,
                    "slot": data.get("slot"),
                    "role_attempt": data.get("role_attempt"),
                    "accepted_slots": data.get("accepted_slots"),
                    "pending_slots": data.get("pending_slots"),
                    "retry_after_sec": wait_sec,
                    "needs_attention": bool(data.get("needs_attention")),
                },
            )
        except Exception:
            pass
        await asyncio.sleep(wait_sec)
        return True
    if (
        next_tool == "execute_workers"
        and _o._is_worker_operator_shutdown_interrupted(data, checkpoint)
        and shutdown_mgr is not None
        and shutdown_mgr.is_shutting_down
    ):
        try:
            _o.log_system_event(
                "pipeline.worker_operator_shutdown_interrupted",
                "info",
                (
                    f"Worker lease for v{next_v} was fenced attempt-neutrally "
                    "during operator shutdown"
                ),
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": stage,
                    "workflow_run_id": data.get("workflow_run_id"),
                    "effect_id": data.get("effect_id"),
                    "lease_epoch": data.get("lease_epoch"),
                    "claimed_attempt": data.get("claimed_attempt"),
                    "restored_attempt": data.get("restored_attempt"),
                    "checkpoint_preserved": True,
                },
            )
        except Exception:
            pass
        if ui:
            ui.log_history(
                "[Recovery] Worker stopped at the operator shutdown edge; "
                "the same frozen activity will be reclaimed after restart.",
                "info",
            )
        return False
    if error or worker_terminal_abandon:
        if next_tool == "execute_workers" and (
            _o._is_worker_circuit_breaker_result(data)
            or _o._is_precommit_rework_circuit_breaker_result(data)
            or _o._is_official_rework_circuit_breaker_result(data)
            or worker_terminal_abandon
        ):
            if _o._is_precommit_rework_circuit_breaker_result(data):
                abandon_reason = "precommit_rework_circuit_breaker"
            elif _o._is_official_rework_circuit_breaker_result(data):
                abandon_reason = "official_rework_circuit_breaker"
            elif _o._is_worker_circuit_breaker_result(data):
                abandon_reason = "worker_circuit_breaker"
            else:
                abandon_reason = _o._worker_terminal_abandon_reason(data)
            # Defect D fix: a terminal Worker journal may store a reason that is
            # only authorized at REWORK stages (e.g. ``frozen_rework_*``) while
            # the outer checkpoint sits at an INITIAL worker stage
            # (``master_planned``/``workers_done``/``quality_failed``) -- the
            # classic trigger is re-creating the checkpoint for an abandoned
            # version that reuses the dead workflow_run_id.  The generic_abandon
            # state guard would refuse that reason forever and the router would
            # re-dispatch ``execute_workers`` by stage on every cycle, producing
            # an unbounded loop with zero LLM/probe progress.  At an initial
            # worker stage the durable journal is genuinely terminal while the
            # outer checkpoint is still active, which is exactly the
            # ``worker_terminal_abandon`` classification the guard authorizes
            # (pipeline_state.py forced_rules).  Translate to that abstract
            # classification here; the concrete journal reason is already
            # persisted in the durable Worker tombstone and recorded in the
            # ``result`` payload below, so no audit detail is lost.  Rework
            # stages keep the concrete journal reason, which is authorized
            # there and carries useful repair provenance.
            _REWORK_STAGES = frozenset(
                {
                    "precommit_failed",
                    "repair_planned",
                    "rework_running",
                    "official_failed",
                }
            )
            if worker_terminal_abandon and stage not in _REWORK_STAGES:
                _journal_reason = abandon_reason
                abandon_reason = "worker_terminal_abandon"
                data = {
                    **data,
                    "worker_abandon_reason_classified": abandon_reason,
                    "worker_abandon_reason_journal": _journal_reason,
                }
            if (
                _o._is_official_rework_circuit_breaker_result(data)
                and data.get("abandoned") is True
            ):
                abandon_result = data.get("abandon_result") or {
                    "abandoned": True,
                    "reason": abandon_reason,
                }
                abandoned = True
            else:
                from evolution_core import read_pipeline_checkpoint
                from tool_bot_management import (
                    _do_abandon_generation,
                    expected_abandon_identity,
                )
                abandon_result = await _do_abandon_generation(
                    reason=abandon_reason,
                    **expected_abandon_identity(read_pipeline_checkpoint()),
                )
                abandoned = bool(abandon_result.get("abandoned"))
                # Bounded fallback (defect D): if the classified reason was
                # refused solely because it is not authorized at this stage
                # (``forced_abandon_reason_stage_not_allowed``), retry exactly
                # once with the always-allowed generic ``abandon_generation``
                # reason.  This guarantees the router can never loop forever on
                # a refused forced-abandon: the durable Worker journal is
                # terminal while the outer checkpoint is active, so the
                # generation genuinely cannot advance and must be abandoned.
                # The concrete reason stays in the Worker tombstone + the
                # ``result`` payload for audit; the generic reason only governs
                # the control-plane state-guard transition.  A non-stage-guard
                # refusal (e.g. publication_or_certification_stage_not_disposable)
                # is NOT retried -- those stages require genuine reconciliation.
                if (
                    not abandoned
                    and abandon_result.get("blocked") is True
                    and abandon_result.get("reason")
                    == "forced_abandon_reason_stage_not_allowed"
                ):
                    abandon_result = await _do_abandon_generation(
                        reason="abandon_generation",
                        **expected_abandon_identity(
                            read_pipeline_checkpoint()
                        ),
                    )
                    abandoned = bool(abandon_result.get("abandoned"))
            if outcome is not None:
                outcome["router_abandon_result"] = abandon_result
                outcome["terminal_abandon_result"] = (
                    _o._completed_abandon_tool_result(abandon_result)
                )
            msg_abandon = (
                f"{abandon_reason} reached for v{next_v}; "
                f"{'abandoned generation' if abandoned else 'abandon did not complete'}."
            )
            if ui:
                ui.log_history(f"[Recovery] {msg_abandon}", "error" if not abandoned else "warn")
            else:
                _o.log.warning(msg_abandon)
            try:
                _o.log_system_event(
                    "pipeline.deterministic_route_abandoned",
                    "warn" if abandoned else "error",
                    msg_abandon,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": stage,
                        "result": data,
                        "abandon_result": abandon_result,
                    },
                )
            except Exception:
                pass
            return abandoned

        if next_tool == "run_crossover" and _o._is_crossover_incompatible_result(data):
            abandoned = bool(data.get("abandoned"))
            msg_abandon = (
                f"crossover_incompatible reached for v{next_v}; "
                f"{'abandoned generation' if abandoned else 'abandon did not complete'}."
            )
            if ui:
                ui.log_history(f"[Recovery] {msg_abandon}", "warn" if abandoned else "error")
            else:
                _o.log.warning(msg_abandon) if abandoned else _o.log.error(msg_abandon)
            try:
                _o.log_system_event(
                    "pipeline.deterministic_route_abandoned",
                    "warn" if abandoned else "error",
                    msg_abandon,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": stage,
                        "result": data,
                    },
                )
            except Exception:
                pass
            return abandoned

        if next_tool == "run_crossover" and _o._is_crossover_llm_exhausted_result(data):
            abandoned = bool(data.get("abandoned"))
            msg_abandon = (
                f"crossover_llm_exhausted reached for v{next_v}; "
                f"{'abandoned generation' if abandoned else 'abandon did not complete'}."
            )
            if ui:
                ui.log_history(f"[Recovery] {msg_abandon}", "warn" if abandoned else "error")
            else:
                _o.log.warning(msg_abandon) if abandoned else _o.log.error(msg_abandon)
            try:
                _o.log_system_event(
                    "pipeline.deterministic_route_abandoned",
                    "warn" if abandoned else "error",
                    msg_abandon,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": stage,
                        "result": data,
                    },
                )
            except Exception:
                pass
            return abandoned

        detail = f"Deterministic {next_tool} route failed for v{next_v}: {str(error)[:180]}"
        if ui:
            ui.log_history(f"[Recovery] {detail}", "error")
        else:
            _o.log.error(detail)
        try:
            _o.log_system_event(
                "pipeline.deterministic_route_failed",
                "error",
                detail,
                {"next_v": next_v, "source_v": source_v, "stage": stage, "result": data},
            )
        except Exception:
            pass
        return False

    try:
        route_succeeded = success is not False
        _o.log_system_event(
            "pipeline.deterministic_route_done",
            "success" if route_succeeded else "warn",
            f"Deterministic {next_tool} route completed for v{next_v}",
            {
                "next_v": next_v,
                "source_v": source_v,
                "stage": stage,
                "success": route_succeeded,
                "reported_success": success,
            },
        )
    except Exception:
        pass
    return True


def _classify_recovery_after_deterministic_route(
    recovery,
    outcome,
    next_recovery,
):
    """Prove why a deterministic route no longer has a live checkpoint."""

    result = (outcome or {}).get("result")
    if isinstance(result, dict) and result.get("recovery_blocked") is True:
        issues = list(result.get("validation_errors") or [])
        return {
            "action": "blocked",
            "reason": str(result.get("error") or "deterministic_authority_blocked"),
            "checkpoint": (recovery or {}).get("checkpoint"),
            "diagnostics": {
                "active": True,
                "recoverable": False,
                "issues": issues or ["deterministic_authority_blocked"],
                "failure_class": result.get("failure_class"),
                "operator_action": result.get("action"),
            },
        }
    if next_recovery is not None:
        return next_recovery
    checkpoint = (recovery or {}).get("checkpoint") or {}
    if (recovery or {}).get("post_publication_handoff") is True:
        return {
            "action": "publication_handoff_completed",
            "checkpoint": checkpoint,
        }
    terminal_result = (outcome or {}).get("terminal_abandon_result")
    if terminal_result is not None:
        try:
            from tool_bot_management import validate_completed_abandon_handoff

            proof = validate_completed_abandon_handoff(
                checkpoint,
                terminal_result,
            )
        except Exception as exc:
            return {
                "action": "blocked",
                "reason": "deterministic_terminal_proof_invalid",
                "checkpoint": None,
                "diagnostics": {
                    "active": True,
                    "recoverable": False,
                    "issues": [
                        "deterministic_terminal_proof_invalid:"
                        f"{str(exc)[:240]}"
                    ],
                },
            }
        return {
            "action": "generation_abandoned",
            "checkpoint": None,
            "terminal_proof": proof,
        }
    return {
        "action": "blocked",
        "reason": "deterministic_checkpoint_disappeared_without_proof",
        "checkpoint": None,
        "diagnostics": {
            "active": True,
            "recoverable": False,
            "issues": [
                "deterministic_checkpoint_disappeared_without_proof"
            ],
        },
    }


async def _advance_deterministic_recovery(
    recovery,
    ui,
    *,
    cost_policy,
    shutdown_mgr,
    log_level="info",
    label="[Pipeline]",
    gen_ctx=None,
    gen_count=None,
):
    """Run one deterministic route and classify every checkpoint-free result."""

    outcome = {}
    routed = await _o._try_deterministic_checkpoint_route(
        recovery,
        ui,
        log_level=log_level,
        label=label,
        cost_policy=cost_policy,
        shutdown_mgr=shutdown_mgr,
        outcome=outcome,
    )
    if not routed and not outcome:
        return {
            "routed": False,
            "recovery": recovery,
            "outcome": outcome,
            "terminal_action": None,
        }
    next_recovery = _o._checkpoint_recovery_context(
        "deterministic_route",
        ui,
        log_level=log_level,
        label=label,
    )
    result = outcome.get("result") if isinstance(outcome, dict) else None
    shutdown_edge = bool(
        shutdown_mgr is not None and shutdown_mgr.is_shutting_down
    )
    if (
        shutdown_edge
        and isinstance(result, dict)
        and result.get("error") == "WORKER_OPERATOR_SHUTDOWN_INTERRUPTED"
    ):
        outcome_route = outcome.get("route") if isinstance(outcome, dict) else None
        route_is_worker = bool(
            isinstance(outcome_route, dict)
            and outcome_route.get("next_tool") == "execute_workers"
        )
        if (
            route_is_worker
            and _o._is_worker_operator_shutdown_interrupted(
                result,
                (recovery or {}).get("checkpoint") or {},
            )
        ):
            # This is a routed process-lifecycle boundary, not an invitation
            # to fall through to a new Orchestrator provider stream. Keep the
            # exact checkpoint recovery for the next process; both continuous
            # and one-gen drivers observe routed=True, loop once, and stop on
            # their shutdown edge before any provider dispatch.
            return {
                "routed": True,
                "recovery": next_recovery or recovery,
                "outcome": outcome,
                "terminal_action": "operator_shutdown_interrupted",
                "terminal_proof": None,
            }
        return {
            "routed": True,
            "recovery": {
                "action": "blocked",
                "reason": "worker_operator_shutdown_projection_invalid",
                "checkpoint": (recovery or {}).get("checkpoint"),
                "diagnostics": {
                    "active": True,
                    "recoverable": False,
                    "issues": [
                        "worker_operator_shutdown_projection_invalid"
                    ],
                },
            },
            "outcome": outcome,
            "terminal_action": "operator_shutdown_projection_invalid",
            "terminal_proof": None,
        }
    if (
        not routed
        and next_recovery is not None
        and next_recovery.get("action") == "resume"
        and not (
            isinstance(outcome.get("result"), dict)
            and outcome["result"].get("recovery_blocked") is True
        )
    ):
        return {
            "routed": False,
            "recovery": next_recovery,
            "outcome": outcome,
            "terminal_action": None,
        }
    classified = _o._classify_recovery_after_deterministic_route(
        recovery,
        outcome,
        next_recovery,
    )
    action = classified.get("action")
    terminal_action = action
    result = outcome.get("result") if isinstance(outcome, dict) else None
    if isinstance(result, dict) and (
        result.get("slice2b_sealed")
        or result.get("slice2b_consumer_parked")
    ):
        terminal_action = "slice2b_consumer_parked"
    terminal_proof = None
    if action == "publication_handoff_completed":
        cleanup_ctx = gen_ctx or _o._generation_context_from_checkpoint(
            (recovery or {}).get("checkpoint") or {},
            gen_count=gen_count or 1,
        )
        cleanup_ok = await _o._run_post_generation_cleanup_with_timeout(
            shutdown_mgr,
            ui,
            cleanup_ctx,
            gen_count=gen_count,
        )
        if cleanup_ok is True:
            classified = None
        else:
            classified = {
                "action": "blocked",
                "reason": "post_generation_cleanup_verification_failed",
                "checkpoint": None,
                "diagnostics": {
                    "active": True,
                    "recoverable": False,
                    "issues": [
                        "post_generation_cleanup_verification_failed"
                    ],
                },
            }
            terminal_action = "post_generation_cleanup_failed"
    elif action == "generation_abandoned":
        terminal_proof = classified.get("terminal_proof")
        try:
            _o.log_system_event(
                "orchestrator.deterministic_generation_abandoned",
                "warn",
                "Deterministic route reached a verified abandon boundary",
                terminal_proof or {},
            )
        except Exception:
            pass
        classified = None
    return {
        "routed": True,
        "recovery": classified,
        "outcome": outcome,
        "terminal_action": terminal_action,
        "terminal_proof": terminal_proof,
    }
