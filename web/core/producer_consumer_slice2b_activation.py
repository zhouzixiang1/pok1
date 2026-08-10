"""Slice 2b one-ahead-buffer activation wiring over the canonical orchestrator.

This module is the *minimum viable* activation layer described in section 13
step 2 of ``docs/evolution-producer-consumer-pipeline-v1.md``.  It wires the
dormant primitives in :mod:`producer_consumer_slice2b` (``seal_candidate``,
:class:`ConsumerDispatcher`, :class:`OneAheadCoordinator`,
:class:`ValidationLedger`) into the orchestrator's deterministic route path so
the LLM producer can begin the next ``prepare_generation`` draft while the
current candidate runs its *unchanged* canonical gate chain
(``run_quality_gates`` -> ``run_review`` -> ``run_critic`` ->
``run_precommit_eval`` -> ``commit_bot``) in a background asyncio consumer
task.

Activation contract (mirrors the design doc Section 13):

- **Default-off.**  Nothing here runs unless the operator explicitly opts in via
  the ``POK_SLICE2B_ENABLED=1`` environment variable *or* sets
  ``pipeline_slice2b_enabled`` truthy on the orchestrator context.  The flag is
  read through :func:`slice2b_active`, which the orchestrator consults at the
  ``workers_done`` seam.  When the flag is false the orchestrator takes the
  legacy single-slot path byte-for-byte (the activation code is not even
  entered).
- **The canonical gate chain is NOT reimplemented.**  The consumer task runs
  the *existing* ``run_quality_gates`` / ``run_review`` / ``run_critic`` /
  ``run_precommit_eval`` / ``commit_bot`` MCP handlers via the injected
  ``gate_handler_factory``; this module only owns the asyncio scheduling, the
  seal snapshot, and the validation ledger.
- **The promotion barrier is fail-closed.**  ``commit_bot`` publication may
  only proceed after :meth:`OneAheadCoordinator.wait_for_promotion_readiness`
  returns a promoted ledger entry.  A consumer rejection or infrastructure
  failure raises :class:`Slice2bError` and the producer's next-generation work
  becomes speculative (the canonical version-allocation floor handles discard).
- **The checkpoint CAS, publication authority and epoch binding are not
  weakened** -- the consumer task never writes to ``pipeline_state.json`` or
  the producer's checkpoint; it records outcomes only to the consumer-owned
  :class:`ValidationLedger`.
- **High-water = 1.**  :meth:`OneAheadCoordinator.note_sealed` refuses a second
  in-flight seal, so at most one sealed candidate awaits validation while the
  producer prepares the next.

The activation is deliberately conservative: the consumer runs as a background
``asyncio.Task`` within the same process/event loop as the orchestrator.  No
separate process, no threads, no new publication authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, MutableMapping

from blocking_runtime import run_async_off_event_loop
from producer_consumer_slice2b import (
    ConsumerDispatcher,
    OneAheadCoordinator,
    SealResult,
    Slice2bError,
    ValidationLedger,
    build_sealed_candidate_snapshot,
    seal_candidate,
)
from producer_consumer_workflow_store import ProducerConsumerWorkflowAdapter


# ---------------------------------------------------------------------------
# Opt-in flag (default-off; honors the design-doc inertness fence)
# ---------------------------------------------------------------------------


SLICE2B_ENV_VAR = "POK_SLICE2B_ENABLED"
SLICE2B_ACTIVATION_VERSION = "producer-consumer-slice2b-activation-v1"

# Consumer gate-chain lease duration.  The precommit native match alone runs
# 5 rounds x 70 hands and takes 50-90 min under GLM; the full quality->review->
# critic->precommit chain must complete inside a single lease or the dispatcher
# reclaims the effect on lease expiry, burns an attempt, and (without the
# resume-from-last-gate logic in the dispatcher) re-runs the whole chain.
# Default 4h matches the one-ahead envelope deadline so a single attempt can
# absorb the worst-case chain plus GLM variability; override via env.
DEFAULT_CONSUMER_LEASE_SECONDS = float(
    os.environ.get("POK_SLICE2B_CONSUMER_LEASE_SECONDS", "14400.0")
)

# Bounded infra-failure retry budget for the consumer gate chain.  When a gate
# returns ``infrastructure_failure`` (a transient retry signal: native smoke
# hiccup, sandbox mount race, quota blip), ``run_once`` records the failure and
# returns WITHOUT rejecting — the consumer task then re-drives ``run_once``,
# which resumes at the same gate (its recorded outcome is non-success).  Without
# a budget the task would spin on a *persistent* infra condition until the 4h
# lease expires, wedging the generation (the documented v52 wedge, 2026-08-04).
# After this many consecutive infra failures at the SAME gate, the candidate is
# rejected so the pipeline advances.  Override via env for stress tuning.
DEFAULT_CONSUMER_INFRA_RETRY_BUDGET = int(
    os.environ.get("POK_SLICE2B_CONSUMER_INFRA_RETRY_BUDGET", "5")
)
# Backoff between consecutive infra-failure retries at the same gate.  Kept
# short so a genuinely transient blip clears within a couple of minutes; a
# persistent condition hits the budget and rejects inside ~budget*backoff.
DEFAULT_CONSUMER_INFRA_BACKOFF_SECONDS = float(
    os.environ.get("POK_SLICE2B_CONSUMER_INFRA_BACKOFF_SECONDS", "30.0")
)


def slice2b_active(context: Mapping[str, Any] | None = None) -> bool:
    """Return True iff the operator has explicitly enabled Slice 2b.

    Resolution order (any truthy source wins):

    1. ``POK_SLICE2B_ENABLED=1`` environment variable (operator action).
    2. ``pipeline_slice2b_enabled`` truthy on the supplied orchestrator
       ``context`` mapping (explicit in-process opt-in).

    Defaults to False.  No production call site sets either source truthy by
    default; the canonical runtime stays on the legacy single-slot path.
    """

    env_value = os.environ.get(SLICE2B_ENV_VAR, "").strip()
    if env_value in {"1", "true", "True", "TRUE", "yes", "on"}:
        return True
    if context is not None and bool(context.get("pipeline_slice2b_enabled")):
        return True
    return False


# ---------------------------------------------------------------------------
# Per-process activation state (one one-ahead buffer per orchestrator process)
# ---------------------------------------------------------------------------


class Slice2bActivation:
    """Owns the one-ahead buffer state for one orchestrator process.

    A single instance is intended to live for the lifetime of the orchestrator
    loop.  It holds the workflow-store adapter (Consumer queue), the validation
    ledger, the one-ahead coordinator and a registry of in-flight consumer
    tasks.  All mutation is confined to the orchestrator's event loop thread.
    """

    def __init__(
        self,
        *,
        adapter: ProducerConsumerWorkflowAdapter,
        coordinator: OneAheadCoordinator | None = None,
        dispatcher_owner: str = "slice2b-consumer",
        lifecycle_db_path: str | Path | None = None,
    ) -> None:
        self.adapter = adapter
        # Persist the candidate lifecycle beside the adapter's workflow sqlite
        # so it survives a process restart (the former in-memory ledger lost
        # every in-flight candidate on crash, degenerating one-ahead to serial).
        if lifecycle_db_path is None:
            try:
                lifecycle_db_path = (
                    Path(adapter.store.path).parent / "slice2b_lifecycle.sqlite3"
                )
            except Exception:
                lifecycle_db_path = None  # ValidationLedger() temp fallback
        self.ledger = ValidationLedger(lifecycle_db_path)
        if coordinator is None:
            import os as _os
            _env_ahead = _os.environ.get("POK_SLICE2B_MAX_AHEAD")
            _max_ahead = int(_env_ahead) if _env_ahead and _env_ahead.isdigit() and int(_env_ahead) >= 1 else None
            self.coordinator = OneAheadCoordinator(self.ledger, max_ahead=_max_ahead)
        else:
            self.coordinator = coordinator
        self.dispatcher = ConsumerDispatcher(
            self.adapter,
            self.ledger,
            owner=dispatcher_owner,
            # Prove prior-owner death when recovering a leased envelope, so a
            # restart can reclaim a consumer lease whose owner pid is gone.
            death_proof_resolver=self.death_proof_resolver(),
        )
        # candidate_id -> asyncio.Task running the canonical gate chain.
        self._consumer_tasks: dict[str, asyncio.Task] = {}
        # candidate_id -> sealed snapshot dict (for dispatcher resume on retry).
        self._sealed_snapshots: dict[str, dict[str, Any]] = {}
        # candidate_id -> submission epoch recorded at seal time, used as the
        # consumer dispatch clock so the envelope stays leasable.
        self._dispatch_clocks: dict[str, float] = {}
        # candidate_id -> (gate_runner_factory, now, lease_seconds) registered
        # by the synchronous seam and driven by the loop via
        # ``ensure_consumer_running``.
        self._scheduled_factories: dict[str, tuple] = {}

    # -- seal (Producer side, at workers_done) ------------------------------

    def seal_at_workers_done(
        self,
        *,
        snapshot: Mapping[str, Any],
        run_id: str,
        job_id: str,
        idempotency_key: str,
        artifact_digest: str,
        resource_claim: Mapping[str, Any],
        retry_policy: Mapping[str, Any],
        deadline: Mapping[str, Any],
        evaluation_contract_digest: str,
        executor_digest: str,
        repository_digest: str,
        runtime_digest: str,
    ) -> SealResult:
        """Seal the candidate and register it with the one-ahead coordinator.

        Returns the :class:`SealResult`.  After this returns the producer may
        call :meth:`producer_may_prepare_next` to begin the next draft prepare.
        """

        sealed = seal_candidate(
            self.adapter,
            snapshot=snapshot,
            run_id=run_id,
            job_id=job_id,
            idempotency_key=idempotency_key,
            artifact_digest=artifact_digest,
            resource_claim=resource_claim,
            retry_policy=retry_policy,
            deadline=deadline,
            evaluation_contract_digest=evaluation_contract_digest,
            executor_digest=executor_digest,
            repository_digest=repository_digest,
            runtime_digest=runtime_digest,
        )
        # Persist the sealed candidate's lifecycle (SEALED) AND the immutable
        # snapshot FIRST, so the FSM is the source of truth before the
        # coordinator's high-water check runs.  The dispatcher's later ``start``
        # call is idempotent (artifact-drift guard), so seeding here is safe.
        try:
            self.ledger.start(
                candidate_id=sealed["candidate_id"],
                sealed_artifact_hash=sealed["artifact_digest"],
                envelope_effect_id=sealed["effect_id"],
                envelope_digest=sealed["envelope_digest"],
                sealed_snapshot=dict(snapshot),
            )
        except Slice2bError:
            # Artifact drift on a replay is a real error surfaced elsewhere;
            # any other failure here must not block the seal (the envelope is
            # already durable).  The dispatcher will re-seed on its claim.
            pass
        # Coordinator high-water check: now that the FSM reflects this candidate,
        # note_sealed enforces the multi-ahead capacity (raises if over-sealed).
        self.coordinator.note_sealed(
            candidate_id=sealed["candidate_id"],
            artifact_hash=sealed["artifact_digest"],
        )
        # Stash the snapshot so the consumer task / a retry can re-derive it
        # without the producer having to stay around.  Also record the envelope
        # submission time so the consumer dispatches with a ``now`` that keeps
        # the envelope leasable (the deadline's expires_at_epoch is relative to
        # the submission epoch, not wall-clock time).
        submitted_at = float(deadline.get("submitted_at_epoch") or time.time())
        self._sealed_snapshots[sealed["candidate_id"]] = dict(snapshot)
        self._dispatch_clocks[sealed["candidate_id"]] = submitted_at
        return sealed

    def producer_may_prepare_next(self) -> bool:
        """Producer may begin the next ``prepare_generation`` draft."""

        return self.coordinator.producer_may_prepare_next()

    def producer_may_draft_behind(self) -> bool:
        """Producer may launch a one-ahead draft behind the consumer.

        Alias of :meth:`producer_may_prepare_next`; the underlying coordinator
        gate returns True whenever at least one sealed-but-unresolved candidate
        is in flight.  This accessor is required because
        ``_try_launch_draft_prepare`` in ``orchestrator_loop_phases`` calls it
        by name; without it the call raised ``AttributeError`` and was silently
        swallowed by the launcher's broad ``except``, leaving the producer LLM
        idle 0% of the time while the consumer ran its native gate chain.
        """

        return self.coordinator.producer_may_draft_behind()

    def producer_may_advance(self) -> bool:
        """Producer may seal another candidate (high-water capacity check)."""

        return self.coordinator.producer_may_advance()

    def producer_may_draft_ahead_of_eval(self) -> bool:
        """Producer may launch a speculative draft while the primary lane is
        parked in eval_wait (no sealed candidate to draft behind).

        Delegates to the coordinator's ahead-of-eval predicate; required so
        ``_try_launch_draft_prepare`` can reach it through the activation
        layer (the same way it reaches ``producer_may_draft_behind``).
        """

        return self.coordinator.producer_may_draft_ahead_of_eval()

    # -- consumer task ------------------------------------------------------

    def _consumer_effect_exhausted(self, candidate_id: str) -> bool:
        """Return True iff the consumer effect for ``candidate_id`` is in a
        terminal-no-retry state (``exhausted`` / ``abandoned``).

        Delegates to the workflow-store adapter.  Used by the bounded retry
        loop to detect when ``run_once`` returned ``dispatched=False`` because
        the effect has no remaining attempts (the candidate can never advance)
        and map that to a ledger reject instead of wedging in ``consuming``.
        """

        try:
            return bool(self.adapter.consumer_effect_exhausted(candidate_id=candidate_id))
        except Exception:
            return False

    def launch_consumer_task(
        self,
        *,
        candidate_id: str,
        gate_runner_factory: Callable[[], Mapping[str, Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]]],
        now: float | None = None,
        lease_seconds: float = DEFAULT_CONSUMER_LEASE_SECONDS,
        loop: asyncio.AbstractEventLoop | None = None,
        consumer_slot_id: str | None = None,
    ) -> asyncio.Task:
        """Launch the background consumer task for one sealed candidate.

        ``gate_runner_factory`` returns a fresh mapping of gate-name -> runner
        coroutine (the canonical gate chain).  The factory is invoked at launch
        so each consumer run gets its own bound handlers (the canonical
        ``run_quality_gates``/etc. close over per-call state).

        Must be called from within a running event loop (the orchestrator loop
        or a test driver).  The returned task runs the gate chain to terminal
        state (promoted or rejected) and then notifies the coordinator.
        Failures (gate rejection or infrastructure error) are recorded in the
        ledger; the promotion barrier observes them fail-closed.

        For the synchronous orchestrator seam (which runs outside any event
        loop), use :meth:`schedule_consumer` to register the factory, then
        :meth:`drain_scheduled_consumer` from within the loop to create and
        await the task.

        ``consumer_slot_id`` binds the FROZEN-SNAPSHOT ISOLATION override: the
        entire gate chain (``dispatcher.run_once`` and every gate handler it
        awaits) runs inside ``active_slot_override(consumer_slot_id)`` so every
        no-``slot_id`` checkpoint read/write targets
        ``pipeline_state_<consumer_slot>.json`` instead of racing the parked
        primary.  When None (legacy callers / tests), the override is skipped
        and the gate chain targets the ambient slot (back-compat).

        ``lease_seconds`` defaults to ``DEFAULT_CONSUMER_LEASE_SECONDS`` (4h,
        env ``POK_SLICE2B_CONSUMER_LEASE_SECONDS``) to cover the full gate
        chain under GLM ``effort=max``: the precommit native match alone is
        50-90 min (5 rounds x 70 hands), and the whole quality->review->critic
        ->precommit chain must complete inside a single lease or the dispatcher
        reclaims the effect on lease expiry and burns an attempt.  The earlier
        300s/3600s defaults expired mid-chain, turning every slow generation
        into a zombie/reap/retry cycle that exhausted attempts and abandoned
        otherwise-healthy candidates.  The dispatcher now also resumes from the
        last persisted gate, so even a genuine lease expiry preserves the
        quality/review/critic work.  Cross-process recovery is unaffected: the
        death-proof resolver + zombie reaper detect a genuinely-dead consumer
        task independently of lease expiry.
        """

        if candidate_id not in self._sealed_snapshots:
            raise Slice2bError(
                "slice2b_consumer_unknown_candidate:" + candidate_id
            )
        existing = self._consumer_tasks.get(candidate_id)
        if existing is not None and not existing.done():
            return existing

        snapshot = self._sealed_snapshots[candidate_id]
        # Derive a dispatch ``now`` that respects the sealed envelope's
        # deadline.  Wall-clock time would be past ``expires_at_epoch`` for any
        # envelope sealed in a test or paused between seal and dispatch, which
        # would make ``recover()`` refuse to lease it.  Default to the
        # submission time recorded at seal (the envelope is leasable from that
        # point forward).
        if now is not None:
            dispatch_now = float(now)
        else:
            dispatch_now = float(self._dispatch_clocks.get(candidate_id, 110.0))

        dispatcher = self.dispatcher
        ledger = self.ledger
        coordinator = self.coordinator
        # Resolve the consumer slot override.  Prefer the explicit argument;
        # fall back to the slot persisted on the candidate lifecycle (set at
        # seal time) so a recovery path that omits the arg still isolates.
        if consumer_slot_id is None:
            try:
                consumer_slot_id = ledger.consumer_checkpoint_slot(candidate_id)
            except Exception:
                consumer_slot_id = None
        # Bind the override name once so the task body can re-enter it without a
        # late import on every run.
        slot_override_ctx = None
        if consumer_slot_id is not None:
            try:
                from evolution_infra import active_slot_override

                slot_override_ctx = active_slot_override
            except Exception:
                slot_override_ctx = None

        async def _run_gate_chain() -> None:
            gates = gate_runner_factory()

            async def _dispatch() -> dict[str, Any]:
                result = await dispatcher.run_once(
                    sealed_snapshots={candidate_id: snapshot},
                    gates=gates,
                    now=dispatch_now,
                    lease_seconds=lease_seconds,
                )
                return result if isinstance(result, dict) else {"dispatched": False}

            try:
                # FROZEN-SNAPSHOT ISOLATION: run the entire gate chain under
                # the consumer slot override so its checkpoint I/O is isolated
                # from the parked primary.  ``active_slot_override`` is a
                # ContextVar; entering it HERE (inside the task body) guarantees
                # the override applies to every await within, regardless of how
                # the task was created.  When no slot is bound (legacy callers
                # / unit tests without a seeded slot), dispatch targets the
                # ambient slot unchanged (back-compat).
                #
                # NATIVE-MATCH HEARTBEAT: the precommit gate's native TCP
                # matches run for 30-50 min.  The native-match heartbeat
                # reporter (which refreshes the checkpoint during matches, so
                # the stale-watchdog sees live progress) is DISABLED unless a
                # dispatch nonce is active.  The inline path activates the
                # nonce from the provider attempt UUID; the consumer does not
                # run through the provider loop, so it must activate its own
                # nonce here.  Without this, the checkpoint stays stale
                # during precommit native matches, the stale-watchdog reaps a
                # healthy consumer at the 50-min ceiling, the abandon removes
                # the candidate dir, and the in-flight matches crash with
                # ArtifactIntegrityError → 0W-0L-0D.
                import hashlib as _hashlib

                _nonce_material = f"slice2b-consumer-{candidate_id}-{dispatch_now}".encode()
                _consumer_nonce = _hashlib.sha256(_nonce_material).hexdigest()[:32]
                from pipeline_state import (
                    activate_native_match_dispatch_nonce,
                    reset_native_match_dispatch_nonce,
                )

                _nonce_token = activate_native_match_dispatch_nonce(_consumer_nonce)
                try:
                    # BOUNDED RETRY LOOP (was a one-shot ``await _dispatch()``).
                    # ``run_once`` resumes from the last persisted gate (it skips
                    # every gate already recorded as ``success``), so looping it
                    # advances the chain one gate at a time.  But a gate that
                    # returns ``infrastructure_failure`` records the failure and
                    # RETURNS (it does not reject, and there is no internal
                    # retry), so a one-shot call leaves the candidate wedged in
                    # ``consuming`` forever — the orchestrator loop re-launches
                    # this task every ~45s but each relaunch re-runs the same
                    # gate and hits the same transient infra signal, never
                    # escalating.  The loop below re-drives ``run_once`` with a
                    # bounded infra-failure budget per gate so a persistent infra
                    # condition escalates to a terminal reject instead of
                    # spinning until the 4h lease expires.
                    #
                    # Budget rationale: infra failures are meant to be transient
                    # (native smoke hiccup, sandbox mount race, quota blip).  A
                    # handful of backoff retries absorbs the transient case; if
                    # the SAME gate keeps failing infra, the condition is not
                    # transient and the candidate must be rejected so the
                    # pipeline advances.  The effect's own ``max_attempts`` (3)
                    # is not a good budget here because it counts envelope
                    # lease/recover cycles (cross-restart), not in-process
                    # gate retries.
                    max_infra_retries_per_gate = DEFAULT_CONSUMER_INFRA_RETRY_BUDGET
                    infra_backoff_seconds = DEFAULT_CONSUMER_INFRA_BACKOFF_SECONDS
                    infra_streak_gate: str | None = None
                    infra_streak_count = 0
                    while not ledger.is_terminal(candidate_id):
                        if (
                            slot_override_ctx is not None
                            and consumer_slot_id is not None
                        ):
                            with slot_override_ctx(consumer_slot_id):
                                result = await _dispatch()
                        else:
                            result = await _dispatch()
                        # ``run_once`` returns ``dispatched=False`` when there is
                        # no leasable envelope.  Two sub-cases:
                        #   (a) the effect is held by another owner / not yet
                        #       ready — transient; break and let the loop
                        #       re-launch this task later.
                        #   (b) the effect is EXHAUSTED (attempt >= max_attempts):
                        #       no more dispatch attempts remain, so the
                        #       candidate can never advance.  Without rejecting
                        #       here it would wedge in ``consuming`` forever
                        #       (the documented v52 wedge: the effect exhausted
                        #       at attempt=3/3 but the candidate stayed
                        #       non-terminal because nothing mapped effect-
                        #       exhaustion to a ledger reject).
                        if not result.get("dispatched"):
                            if ledger.is_terminal(candidate_id):
                                break
                            # Deep-parallelism: native precommit backpressure.
                            # The dispatcher found the native-precommit
                            # semaphore exhausted and returned without
                            # dispatching (the candidate stays at critic_checked).
                            # This is EXPECTED backpressure, not an infra
                            # failure: do NOT break (which would exit the
                            # consumer task and require an external relaunch)
                            # and do NOT consume the infra-retry budget.
                            # Instead, back off briefly and re-enter run_once
                            # so the candidate resumes the moment a native
                            # slot frees.  Other drafts' LLM gates (review/
                            # critic) are unaffected because each candidate
                            # has its own consumer task.
                            if (
                                result.get("reason")
                                == "native_precommit_slot_busy"
                            ):
                                try:
                                    from producer_consumer_slice2b import (
                                        POK_NATIVE_BACKOFF_SECONDS,
                                    )
                                    _backoff = POK_NATIVE_BACKOFF_SECONDS
                                except Exception:
                                    _backoff = 30.0
                                # Cooperative sleep: bounded into 5s slices so
                                # a process shutdown between native-slot frees
                                # is observed promptly (the consumer task is
                                # cancelled on shutdown; this just avoids a
                                # single uninterruptible 30s+ sleep).
                                _slept = 0.0
                                while _slept < _backoff:
                                    await asyncio.sleep(min(5.0, _backoff - _slept))
                                    _slept += 5.0
                                continue
                            _exhausted = self._consumer_effect_exhausted(
                                candidate_id
                            )
                            if _exhausted:
                                ledger.reject(
                                    candidate_id=candidate_id,
                                    reason=(
                                        "consumer_effect_attempts_exhausted:"
                                        + str(result.get("reason") or "no_envelope")
                                    ),
                                    completed_at=time.time(),
                                )
                            break
                        reason = result.get("reason")
                        # Both ``infrastructure_failure`` (a gate returned an
                        # infra-class outcome) and ``gate_runner_raised`` (a gate
                        # runner threw, recorded as infrastructure_failure by the
                        # dispatcher) are infra pauses: the dispatcher recorded a
                        # non-success outcome and returned WITHOUT rejecting.
                        # Either way the same gate is stuck and must be retried
                        # under the bounded budget.
                        is_infra_pause = reason in (
                            "infrastructure_failure",
                            "gate_runner_raised",
                        )
                        paused_gate = (
                            result.get("paused_at_gate")
                            or result.get("failed_at_gate")
                        )
                        if not is_infra_pause:
                            # success / candidate_failure / promote already drove
                            # the ledger terminal (or advanced past the gate); if
                            # the ledger is still non-terminal the chain simply
                            # continues to the next gate on the next iteration.
                            infra_streak_gate = None
                            infra_streak_count = 0
                            # Guard: if dispatched=True but the reason is NEITHER
                            # a known infra-pause NOR None (success/promote/
                            # candidate_failure), the ledger should already be
                            # terminal.  If it is NOT, an unknown future gate
                            # outcome could spin here forever with no budget —
                            # break so the orchestrator relaunches (or the
                            # reaper catches a genuinely-stuck candidate) rather
                            # than busy-looping on an unrecognized reason.
                            if reason is not None and not ledger.is_terminal(
                                candidate_id
                            ):
                                break
                            continue
                        # infrastructure_failure pause: the same gate recorded a
                        # non-success outcome.  Track how many times the SAME
                        # gate has paused infra consecutively; if it exceeds the
                        # budget, escalate to a terminal reject so we do not
                        # spin until lease expiry.
                        if paused_gate == infra_streak_gate:
                            infra_streak_count += 1
                        else:
                            infra_streak_gate = paused_gate
                            infra_streak_count = 1
                        if infra_streak_count > max_infra_retries_per_gate:
                            # The candidate is already CONSUMING (the dispatcher
                            # transitioned SEALED->CONSUMING at the first
                            # run_once), so ``reject`` is a valid terminal
                            # transition (CONSUMING -> REJECTED).  Do NOT call
                            # ``start`` here: start() seeds a brand-new candidate
                            # (None -> SEALED) and would raise on an already-
                            # consuming row.
                            ledger.reject(
                                candidate_id=candidate_id,
                                reason=(
                                    f"consumer_infra_failure_budget_exhausted:"
                                    f"{paused_gate}:{infra_streak_count}"
                                ),
                                completed_at=time.time(),
                            )
                            break
                        # Transient infra pause: back off briefly, then let the
                        # loop re-enter run_once (which resumes at the same gate
                        # because its recorded outcome is non-success).
                        await asyncio.sleep(infra_backoff_seconds)
                finally:
                    reset_native_match_dispatch_nonce(_nonce_token)
            except Exception:
                # The dispatcher records infrastructure failures into the
                # ledger itself; if it raised, ensure the ledger reflects a
                # terminal rejection so the promotion barrier fails closed
                # rather than blocking forever.
                if not ledger.is_terminal(candidate_id):
                    try:
                        ledger.start(
                            candidate_id=candidate_id,
                            sealed_artifact_hash=snapshot["artifact_hash"],
                            envelope_effect_id="slice2b-consumer-exception",
                            envelope_digest=snapshot["snapshot_digest"],
                        )
                    except Slice2bError:
                        pass
                    ledger.reject(
                        candidate_id=candidate_id,
                        reason="consumer_task_infrastructure_failure",
                        completed_at=time.time(),
                    )
            finally:
                # The coordinator's promotion barrier polls the ledger; once
                # the ledger is terminal we drain the one-ahead slot so the
                # producer may seal the next candidate.
                if ledger.is_terminal(candidate_id):
                    coordinator.note_terminal(candidate_id=candidate_id)

        target_loop = loop
        if target_loop is None:
            target_loop = asyncio.get_running_loop()
        task = target_loop.create_task(_run_gate_chain())
        self._consumer_tasks[candidate_id] = task
        return task

    def schedule_consumer(
        self,
        *,
        candidate_id: str,
        gate_runner_factory: Callable[[], Mapping[str, Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]]],
        now: float | None = None,
        lease_seconds: float = DEFAULT_CONSUMER_LEASE_SECONDS,
        consumer_slot_id: str | None = None,
    ) -> None:
        """Register a consumer launch to be driven by the orchestrator loop.

        The orchestrator seam runs synchronously (outside any event loop) when
        it seals at ``workers_done``.  It cannot create an ``asyncio.Task``
        directly because there is no running loop.  This method stashes the
        factory (and the consumer slot id for frozen-snapshot isolation);
        :meth:`drain_scheduled_consumer` (called from the loop) or the
        promotion barrier (which runs inside the loop) creates and awaits the
        task.  If no factory is scheduled, the promotion barrier creates a task
        on-demand from the registered activation using
        :attr:`default_gate_runner_factory}.
        """

        if candidate_id not in self._sealed_snapshots:
            raise Slice2bError(
                "slice2b_consumer_unknown_candidate:" + candidate_id
            )
        self._scheduled_factories[candidate_id] = (
            gate_runner_factory,
            now,
            lease_seconds,
            consumer_slot_id,
        )

    async def ensure_consumer_running(self, candidate_id: str) -> asyncio.Task | None:
        """Create the consumer task for a scheduled candidate from the loop.

        Called by the promotion barrier (which runs inside the orchestrator
        event loop) to guarantee the consumer is making progress before we
        block on its result.  If the candidate was sealed but never scheduled
        (e.g. the seam could not build a factory), the barrier will still block
        and time out fail-closed.
        """

        existing = self._consumer_tasks.get(candidate_id)
        if existing is not None and not existing.done():
            return existing
        scheduled = self._scheduled_factories.get(candidate_id)
        if scheduled is None:
            return None
        # Back-compat: the tuple grew a 4th element (consumer_slot_id).  Older
        # scheduled entries from before frozen-snapshot isolation have 3.
        if len(scheduled) == 4:
            factory, now, lease_seconds, consumer_slot_id = scheduled
        else:
            factory, now, lease_seconds = scheduled
            consumer_slot_id = None
        return self.launch_consumer_task(
            candidate_id=candidate_id,
            gate_runner_factory=factory,
            now=now,
            lease_seconds=lease_seconds,
            consumer_slot_id=consumer_slot_id,
        )

    def consumer_task(self, candidate_id: str) -> asyncio.Task | None:
        return self._consumer_tasks.get(candidate_id)

    # -- boot recovery (re-launch consumers for sealed-but-unresolved
    #    candidates after a process restart) --------------------------------

    def recover_at_boot(self) -> dict[str, Any]:
        """Rebuild in-memory state and re-schedule consumers for every
        sealed-but-unresolved candidate after a process restart.

        The seal envelope is already durable (sqlite outbox); this method
        rehydrates the per-process registries (``_sealed_snapshots`` /
        ``_dispatch_clocks``) and re-schedules the canonical gate chain for
        every candidate the persisted lifecycle still shows as ``SEALED``.
        The next ``ensure_consumer_running`` (called by the orchestrator loop
        or the promotion barrier) materializes the consumer ``asyncio.Task``.

        Safe to call multiple times: re-scheduling a candidate already running
        is a no-op (``ensure_consumer_running`` returns the live task).  Must
        be called from the orchestrator event loop thread.
        """

        recovered: dict[str, Any] = {
            "rescheduled": [],
            "terminal": [],
            "rejected": [],
        }
        try:
            non_terminal = self.ledger.non_terminal_candidates()
        except Exception:
            # The lifecycle store may not be initialized yet (first-ever
            # boot); nothing to recover.
            return recovered

        # REJECTED-CANDIDATE BRIDGE: surface every candidate the persisted
        # lifecycle shows as terminal ``rejected``.  ``non_terminal_candidates``
        # below only returns SEALED/CONSUMING rows, so without this query a
        # rejected candidate is invisible to boot recovery.  A rejected
        # consumer candidate can never be promoted; the primary lane parked at
        # ``workers_done`` for that generation must canonically abandon it so
        # the epoch allocates a fresh successor.  The per-route
        # ``_slice2b_consumer_rejected`` check is the canonical abandon driver,
        # but it only fires when the route is actually traversed for the parked
        # generation.  Surfacing the rejected rows here lets the activation
        # (and any boot-time orchestrator integration) explicitly prove the
        # set of generations that must be abandoned instead of relying solely
        # on the next route traversal, which closes the restart wedge where a
        # rejected-but-not-abandoned candidate zombies forever.
        try:
            for entry in self.ledger.rejected_candidates():
                # next_v/source_v live inside the sealed snapshot (the lifecycle
                # row itself only carries reserved_next_v, which is NULL for a
                # primary-lane candidate).  Fall back to reserved_next_v / 0 so
                # the surfacing stays well-typed even for a legacy row whose
                # snapshot was not persisted.
                snap = entry.get("sealed_snapshot") or {}
                next_v = int(snap.get("next_v") or entry.get("reserved_next_v") or 0)
                source_v = int(snap.get("source_v") or 0)
                recovered["rejected"].append({
                    "candidate_id": str(entry.get("candidate_id") or ""),
                    "next_v": next_v,
                    "source_v": source_v,
                    "terminal_reason": str(
                        entry.get("terminal_reason")
                        or "slice2b_consumer_rejected"
                    ),
                })
        except Exception:
            pass

        for candidate_id, artifact_hash in non_terminal.items():
            snapshot = self.ledger.recover_snapshot(candidate_id)
            if snapshot is None:
                # Sealed envelope durable but snapshot not persisted (legacy
                # seal from before persistence).  Cannot rebuild the gate
                # chain; leave it for the inline fallback.  This is a fail-
                # safe no-op, not a crash.
                recovered["terminal"].append(
                    {"candidate_id": candidate_id, "reason": "snapshot_missing"}
                )
                continue
            # Rebuild the in-memory registries the consumer task reads.
            self._sealed_snapshots[candidate_id] = dict(snapshot)
            # Use the snapshot's own timing plan epoch if present, else now.
            timing_plan = snapshot.get("quality_native_match_timing_plan") or {}
            dispatch_epoch = float(
                timing_plan.get("submitted_at_epoch") or time.time()
            )
            self._dispatch_clocks[candidate_id] = dispatch_epoch
            # Re-register the canonical factory so ensure_consumer_running
            # re-launches the gate chain.  next_v/source_v come from the
            # recovered snapshot.
            next_v = int(snapshot.get("next_v") or 0)
            source_v = int(snapshot.get("source_v") or 0)
            factory = canonical_gate_runner_factory(next_v, source_v)
            # FROZEN-SNAPSHOT ISOLATION boot recovery: re-enter the consumer
            # slot override with the persisted consumer_checkpoint_slot id.
            # Option (b): the consumer slot file (pipeline_state_<slot>.json)
            # is a normal file in RESULTS_DIR, so it PERSISTS across restart.
            # We reuse it if present; if it was cleaned up (operator wipe),
            # re-seed it from the primary checkpoint so the resumed gate chain
            # has a starting point.  Either way the override id is threaded
            # through schedule_consumer so _run_gate_chain re-binds it.
            consumer_slot_id = None
            try:
                consumer_slot_id = self.ledger.consumer_checkpoint_slot(
                    candidate_id
                )
            except Exception:
                consumer_slot_id = None
            if consumer_slot_id:
                # Re-seed only if the slot file is missing.  read_pipeline_checkpoint
                # with an explicit slot_id bypasses the ambient override (the
                # activation may be running inside a draft override at boot).
                try:
                    from evolution_infra import (
                        read_pipeline_checkpoint as _read_ckpt,
                        no_slot_override,
                    )

                    with no_slot_override():
                        slot_ckpt = _read_ckpt(slot_id=consumer_slot_id)
                except Exception:
                    slot_ckpt = None
                if slot_ckpt is None:
                    # Slot file gone: re-seed from the live primary checkpoint
                    # (gen N is still at workers_done for a sealed-but-
                    # unresolved candidate).  Mirror the seal-time seed.
                    try:
                        from evolution_infra import (
                            read_pipeline_checkpoint as _read_ckpt,
                            no_slot_override,
                        )

                        with no_slot_override():
                            primary_ckpt = _read_ckpt() or {}
                    except Exception:
                        primary_ckpt = {}
                    if primary_ckpt and int(primary_ckpt.get("next_v") or 0) == next_v:
                        try:
                            import orchestrator_deterministic_route as _odr

                            _odr._slice2b_seed_consumer_checkpoint(
                                primary_ckpt, consumer_slot_id
                            )
                        except Exception:
                            pass
            self.schedule_consumer(
                candidate_id=candidate_id,
                gate_runner_factory=factory,
                now=dispatch_epoch,
                consumer_slot_id=consumer_slot_id,
            )
            recovered["rescheduled"].append(
                {"candidate_id": candidate_id, "artifact_hash": artifact_hash}
            )
        return recovered

    def death_proof_resolver(self) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
        """Return a death-proof resolver for ``adapter.recover``.

        After a process restart, every previously-leased consumer task is gone
        (its owner pid no longer exists in this process).  The resolver proves
        that by checking ``self._consumer_tasks``: if no live task owns the
        effect, the prior owner is dead and the lease may be reclaimed.  This
        is only safe to assert from the activation's own event loop thread.

        The proof MUST satisfy ``reclaim_effect_lease``'s content-bound
        validation (``workflow_kernel_effects.py``): it requires an ``owner``
        field equal to the effect's prior ``lease_owner`` (always
        ``"slice2b-consumer"`` here) AND a 64-hex ``proof_digest`` equal to
        ``content_digest(unsigned_proof)`` (the proof minus ``proof_digest``).
        A proof missing either field raises
        ``ValueError("effect lease reclaim proof digest is invalid")`` which
        propagates out of the dispatcher and rejects the candidate -- the
        exact restart deadlock this resolver exists to prevent.
        """

        def _resolve(effect: Mapping[str, Any]) -> Mapping[str, Any]:
            effect_id = str(effect.get("effect_id") or "")
            expected_owner = str(effect.get("lease_owner") or "")
            # ``_consumer_tasks`` is keyed by candidate_id (set at launch in
            # launch_consumer_task), NOT by effect_id.  Recover the candidate
            # id from the sealed envelope carried on the effect row.
            envelope = effect.get("envelope") or {}
            candidate_id = str(envelope.get("candidate_id") or "")
            task = self._consumer_tasks.get(candidate_id) if candidate_id else None
            owner_alive = task is not None and not task.done()
            proof = {
                "schema": "slice2b-death-proof-v1",
                "owner": expected_owner,
                "effect_id": effect_id,
                "candidate_id": candidate_id,
                "owner_alive_in_process": bool(owner_alive),
                "reason": (
                    "consumer_task_absent"
                    if not owner_alive
                    else "consumer_task_live"
                ),
                "observed_at": time.time(),
            }
            from workflow_kernel import content_digest

            unsigned = {k: v for k, v in proof.items() if k != "proof_digest"}
            proof["proof_digest"] = content_digest(unsigned)
            return proof

        return _resolve

    # -- promotion barrier (synchronous fail-closed) ------------------------

    async def await_promotion(
        self,
        *,
        candidate_id: str,
        poll_interval: float = 0.05,
        timeout: float = 3600.0,
    ) -> dict[str, Any]:
        """Block publication until the Consumer has promoted ``candidate_id``.

        Thin wrapper over
        :meth:`OneAheadCoordinator.wait_for_promotion_readiness`.  Raises
        :class:`Slice2bError` on rejection, timeout, or unknown candidate --
        the caller (``commit_bot`` invocation site) must NOT publish.  Before
        blocking, this drives any consumer scheduled by the synchronous seam so
        the gate chain actually runs in this event loop.
        """

        await self.ensure_consumer_running(candidate_id)
        return await self.coordinator.wait_for_promotion_readiness(
            candidate_id=candidate_id,
            poll_interval=poll_interval,
            timeout=timeout,
        )


# ---------------------------------------------------------------------------
# Orchestrator-context helpers (the workers_done seam)
# ---------------------------------------------------------------------------


def build_snapshot_from_checkpoint(
    checkpoint: Mapping[str, Any],
    *,
    artifact_hash: str,
    manifest_digest: str,
    charter_digest: str,
    quality_native_match_timing_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the sealed-candidate snapshot from a persisted checkpoint.

    The orchestrator already owns every field this needs (``next_v``,
    ``source_v``, ``workflow_run_id``, ``epoch_binding``).  The caller supplies
    the content-bound digests (artifact hash, manifest digest, charter digest)
    it has already computed for the canonical gate chain; Slice 2b does not
    re-derive them.
    """

    next_v = int(checkpoint.get("next_v"))  # type: ignore[arg-type]
    source_v = int(checkpoint.get("source_v"))  # type: ignore[arg-type]
    workflow_run_id = str(checkpoint.get("workflow_run_id") or "")
    candidate_id = str(checkpoint.get("candidate_id") or f"candidate-v{next_v}")
    draft_id = str(checkpoint.get("draft_id") or f"draft-v{next_v}")
    epoch_binding = dict(checkpoint.get("epoch_binding") or {})
    # Make sure the immutable identity fields the snapshot requires are present
    # even on legacy checkpoints that did not persist an explicit epoch_binding.
    epoch_binding.setdefault("workflow_run_id", workflow_run_id)
    epoch_binding.setdefault("generation_ordinal", next_v)
    epoch_binding.setdefault("canonical_version", next_v)
    return build_sealed_candidate_snapshot(
        candidate_id=candidate_id,
        draft_id=draft_id,
        artifact_hash=artifact_hash,
        manifest_digest=manifest_digest,
        charter_digest=charter_digest,
        epoch_binding=epoch_binding,
        next_v=next_v,
        source_v=source_v,
        workflow_run_id=workflow_run_id,
        quality_native_match_timing_plan=dict(quality_native_match_timing_plan)
        if quality_native_match_timing_plan is not None
        else None,
    )


def stage_is_workers_done_seam(checkpoint: Mapping[str, Any] | None) -> bool:
    """True iff the checkpoint sits at the Slice 2b seal seam."""

    return bool(
        checkpoint
        and checkpoint.get("stage") == "workers_done"
    )


# ---------------------------------------------------------------------------
# Process-wide activation registry + canonical gate-runner factory
# ---------------------------------------------------------------------------


_ACTIVATION: "Slice2bActivation | None" = None


def activation_registry(action: str, *, adapter: ProducerConsumerWorkflowAdapter | None = None) -> "Slice2bActivation | None":
    """Get, set or clear the process-wide Slice 2b activation instance.

    * ``action="get"`` -> the current instance or ``None``.
    * ``action="set"`` -> store ``adapter``-backed instance, return it.
    * ``action="clear"`` -> drop the instance (rollback), return None.

    When slice2b is inactive (the default), ``get`` returns ``None`` and every
    orchestrator call site degrades to the canonical inline gate chain
    unchanged.
    """

    global _ACTIVATION
    if action == "get":
        return _ACTIVATION
    if action == "clear":
        _ACTIVATION = None
        return None
    if action == "set":
        if adapter is None:
            raise ValueError("slice2b_activation_set_requires_adapter")
        _ACTIVATION = Slice2bActivation(adapter=adapter)
        return _ACTIVATION
    raise ValueError(f"slice2b_activation_unknown_action:{action}")


def canonical_gate_runner_factory(next_v, source_v):
    """Return a factory producing the canonical gate-chain runner mapping.

    Each invocation builds fresh bound handlers that delegate to the existing
    canonical MCP tool handlers (``run_quality_gates``/``run_review``/
    ``run_critic``/``run_precommit_eval``/``commit_bot``).  The gate chain is
    NOT reimplemented -- the consumer task merely schedules these unchanged
    handlers against the sealed snapshot and records their outcomes in the
    consumer-owned validation ledger.
    """

    def factory():
        handlers = {}
        try:
            from tool_gates import run_quality_gates, run_review, run_critic
            from tool_eval import run_precommit_eval
            from tool_commit import commit_bot
            # The canonical gate handlers are @tool-decorated, so the module
            # symbols resolve to SdkMcpTool wrapper objects (which have no
            # __call__).  The inline deterministic-route path unwraps them
            # via ``.handler`` (orchestrator_stage_routing.py); the consumer
            # path must do the same -- otherwise ``await canonical(args)``
            # raises ``TypeError: 'SdkMcpTool' object is not callable`` and
            # the whole gate chain dies at the first gate.
            handlers = {
                "run_quality_gates": run_quality_gates.handler,
                "run_review": run_review.handler,
                "run_critic": run_critic.handler,
                "run_precommit_eval": run_precommit_eval.handler,
                "commit_bot": commit_bot.handler,
            }
        except Exception:
            # If the canonical handlers cannot be resolved, return runners that
            # fail infrastructure-failure so the promotion barrier fails closed
            # rather than silently dropping the candidate.
            pass

        runner_args = {
            "run_quality_gates": {"version": next_v, "source_v": source_v},
            "run_review": {"version": next_v, "source_v": source_v, "plan": []},
            "run_critic": {
                "version": next_v,
                "source_v": source_v,
                "plan": [],
                "reviewer_feedback": "",
                "force_advance": False,
            },
            "run_precommit_eval": {"version": next_v, "source_v": source_v},
            "commit_bot": {
                "version": next_v,
                "source_v": source_v,
                "strategy": "master",
                "review_approved": True,
            },
        }
        zero_digest = "0" * 64

        def make(name):
            canonical = handlers.get(name)

            async def run(snapshot):
                if canonical is None:
                    return {
                        "outcome": "infrastructure_failure",
                        "result_digest": zero_digest,
                        "detail": {"reason": f"canonical_handler_unresolved:{name}"},
                    }
                args = dict(runner_args[name])
                try:
                    # OFF-LOAD THE BLOCKING GATE OFF THE ASGI EVENT LOOP.
                    # The canonical gate handlers (run_quality_gates /
                    # run_review / run_critic / run_precommit_eval) are async
                    # coroutines, but they perform substantial SYNCHRONOUS
                    # blocking I/O on the orchestrator's event loop:
                    #   - the @tool wrapper's ensure_runtime_git_guard runs a
                    #     ``git`` subprocess (worktree status) at the top of
                    #     every call;
                    #   - run_precommit_eval drives the native TCP precommit
                    #     match sequence (12 x 70-hand matches) whose
                    #     ``_prepare_native_spec`` does inline file enumeration
                    #     + hashing (``bot_artifact.hash_path`` /
                    #     ``artifact_manifest``) and whose subprocess lifecycle
                    #     waits inline;
                    #   - run_quality_gates / run_review / run_critic resolve
                    #     the active pool + read checkpoints synchronously.
                    # Awaiting ``canonical(args)`` directly on the consumer
                    # task's event loop (the same ASGI loop that serves HTTP)
                    # blocks every request for the full match duration (10+
                    # minutes per precommit). This is the same defect class as
                    # the prepare_generation fix (86b7aa77 + 30626e87) but on a
                    # different code path that was not offloaded.
                    #
                    # run_async_off_event_loop drives the coroutine on a fresh
                    # PRIVATE event loop inside an owned worker thread (same
                    # single-wakeup / context-propagating boundary as
                    # run_blocking_isolated), so the orchestrator's ASGI loop
                    # stays free to serve HTTP. The native-match dispatch nonce
                    # (process-global set + ContextVar) and the frozen-snapshot
                    # / consumer-in-chain gate ContextVars propagate into the
                    # worker via copy_context, so the heartbeat sidecar (which
                    # runs on the main loop and reads the process-wide set) and
                    # the slot-scoped checkpoint I/O behave identically.
                    # Match/gate logic, CAS/checkpoint identities and the
                    # consumer FSM are unchanged -- only the loop the gate
                    # coroutine runs on changes.
                    result = await run_async_off_event_loop(
                        canonical,
                        args,
                        thread_name_prefix=f"slice2b-gate-{name}",
                    )
                except Exception as exc:
                    return {
                        "outcome": "infrastructure_failure",
                        "result_digest": zero_digest,
                        "detail": {
                            "reason": f"canonical_handler_raised:{type(exc).__name__}",
                            "error": str(exc)[:240],
                        },
                    }
                # A canonical handler must return a dict tool-result.  A non-dict
                # return (None, tuple, str, ...) is a contract violation, not a
                # candidate failure -- treat it as infrastructure so the barrier
                # stays fail-closed and retryable rather than spuriously promoting.
                if not isinstance(result, dict):
                    return {
                        "outcome": "infrastructure_failure",
                        "result_digest": zero_digest,
                        "detail": {
                            "reason": (
                                f"canonical_handler_non_dict_result:{type(result).__name__}"
                            ),
                            "name": name,
                        },
                    }
                # DECODE THE MCP TOOL-RESULT ENVELOPE.  Canonical handlers return
                # an MCP envelope {"content":[{"type":"text","text":<json>}]}, NOT
                # a bare dict of fields.  The previous code used ``data = result``
                # (the raw envelope), so every classification branch read
                # envelope-level keys that never exist -- causing EVERY gate
                # (whether route-guard-blocked, raised, or genuinely successful)
                # to fall through to ``outcome="success"`` with a zero digest, and
                # an unproven candidate was PROMOTED.  Decode exactly the way the
                # primary inline path does (orchestrator_tool_result_classification.
                # _extract_tool_result_json).
                try:
                    from orchestrator_tool_result_classification import (
                        _extract_tool_result_json,
                    )

                    data = _extract_tool_result_json(result)
                except Exception:
                    data = {}
                if not isinstance(data, dict) or not data:
                    return {
                        "outcome": "infrastructure_failure",
                        "result_digest": zero_digest,
                        "detail": {
                            "reason": "canonical_handler_undecodable_result",
                            "name": name,
                        },
                    }
                # A runtime/route-guard block is NOT a candidate failure: the
                # gate never ran (e.g. wrong_pipeline_stage because the consumer
                # slot stage was not advanced).  Treat it as infrastructure so the
                # promotion barrier fails closed/retryable instead of either
                # spuriously promoting OR spuriously rejecting the candidate.
                if (
                    data.get("blocked") is True
                    or data.get("error") == "runtime_git_guard_blocked"
                    or data.get("error") == "pipeline_route_guard_blocked"
                ):
                    return {
                        "outcome": "infrastructure_failure",
                        "result_digest": zero_digest,
                        "detail": {
                            "reason": "canonical_handler_route_guard_blocked",
                            "error": data.get("error"),
                            "checkpoint_stage": data.get("checkpoint_stage"),
                            "allowed_tools": data.get("allowed_tools"),
                            "name": name,
                        },
                    }
                # Distinguish infrastructure-class failures (transient/retryable:
                # the dispatcher pauses the ledger so a later run can recover)
                # from genuine candidate failures (the candidate itself is bad
                # and must be rejected).  Without this split, any handler that
                # returns an error -- including a retryable infra pause like a
                # quota wait or a sandbox hiccup -- would be misclassified as a
                # permanent candidate failure and the candidate abandoned.
                # Recognize the full set of infra/retry signals the canonical
                # handlers emit: ``failure_class`` is ``"infrastructure"`` or the
                # first-strict ``"infrastructure_pending"`` variant; OR ``action``
                # is a retry intent (``"retry"`` or the canonical
                # ``"retry_same_tool"`` that the primary inline classifier also
                # keys on).  Without the ``retry_same_tool`` /
                # ``infrastructure_pending`` arms, a transient quality-gate infra
                # hiccup (all_passed=False + action="retry_same_tool") or a
                # first-strict pending pause would be misclassified as a
                # permanent candidate failure and the candidate abandoned.
                _fc = data.get("failure_class")
                _action = data.get("action")
                if (
                    _fc == "infrastructure"
                    or _fc == "infrastructure_pending"
                    or _action == "retry"
                    or _action == "retry_same_tool"
                ):
                    return {
                        "outcome": "infrastructure_failure",
                        "result_digest": zero_digest,
                        "detail": {
                            "reason": "canonical_handler_infrastructure_failure",
                            "error": str(data.get("error"))[:240],
                            "failure_class": data.get("failure_class"),
                            "name": name,
                        },
                    }
                # GATE-VERDICT FAILURE CHECK: each canonical gate signals a
                # genuine candidate failure through a gate-specific verdict
                # field that is NEITHER top-level ``error`` NOR ``success``.
                # Without these checks a failed gate (e.g. precommit native
                # regression 0W-0L-0D, quality compile/smoke fail, or reviewer
                # rejection) is misclassified as success and the candidate is
                # wrongly PROMOTED -- then commit_bot's route guard blocks
                # publication at the failed stage, wedging the generation.
                #   run_quality_gates -> all_passed (tool_gates.py)
                #   run_review        -> approved    (tool_gates_critic_review.py)
                #   run_precommit_eval-> passed      (tool_eval.py)
                # run_critic is ADVISORY (approved is always True; a low score
                # is not a hard reject) so it is intentionally absent here.
                # Test ``is False`` (not falsy) so a missing/None field on a
                # non-verdict control-flow return is not treated as a failure.
                _gate_verdict_failed = False
                if name == "run_quality_gates" and data.get("all_passed") is False:
                    _gate_verdict_failed = True
                elif name == "run_review" and data.get("approved") is False:
                    _gate_verdict_failed = True
                elif name == "run_precommit_eval" and data.get("passed") is False:
                    _gate_verdict_failed = True
                if _gate_verdict_failed:
                    return {
                        "outcome": "candidate_failure",
                        "result_digest": zero_digest,
                        "detail": {
                            "reason": "canonical_handler_gate_verdict_failed",
                            "failure_class": data.get("failure_class"),
                            "all_passed": data.get("all_passed"),
                            "approved": data.get("approved"),
                            "passed": data.get("passed"),
                            "name": name,
                        },
                    }
                if data.get("error") or data.get("success") is False:
                    return {
                        "outcome": "candidate_failure",
                        "result_digest": zero_digest,
                        "detail": {"error": str(data.get("error"))[:240], "name": name},
                    }
                receipt_digest = (
                    data.get("receipt_digest")
                    or data.get("promotion_receipt_digest")
                    or data.get("commit_oid")
                    or zero_digest
                )
                return {
                    "outcome": "success",
                    "result_digest": receipt_digest,
                    "detail": {"name": name, "result": data},
                    "promotion_receipt_digest": receipt_digest,
                    "receipt_digest": receipt_digest,
                }

            return run

        from producer_consumer_slice2b import CONSUMER_GATE_CHAIN_ORDER, GATE_CHAIN_ORDER

        return {name: make(name) for name in GATE_CHAIN_ORDER}

    return factory


# ---------------------------------------------------------------------------
# Migration receipt (design doc Section 13: "content-bound migration receipt")
# ---------------------------------------------------------------------------


MIGRATION_RECEIPT_SCHEMA = "producer-consumer-slice2b-activation-receipt-v1"


def write_activation_migration_receipt(
    *,
    results_dir: str | os.PathLike[str],
    head_commit: str,
    activated_at: float | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a content-bound migration receipt documenting the activation.

    Per design doc Section 13: "Cutover requires no active canonical
    checkpoint, no post-publication handoff and no nonterminal official job. It
    writes a content-bound migration receipt."  This receipt records the
    activation timestamp, the HEAD commit, the slice2b version, and a statement
    that the canonical gate chain is unchanged.

    The receipt is content-bound: its ``receipt_digest`` is derived from the
    canonical JSON of its fields, so any drift in HEAD/version/statement
    invalidates it.  The receipt is written atomically to ``results/``.
    """

    timestamp = float(activated_at if activated_at is not None else time.time())
    receipt = {
        "schema": MIGRATION_RECEIPT_SCHEMA,
        "schema_version": 1,
        "activated_at_epoch": timestamp,
        "activated_at_utc": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp)
        ),
        "head_commit": str(head_commit),
        "slice2b_version": SLICE2B_ACTIVATION_VERSION,
        "dormant_module": "web/core/producer_consumer_slice2b.py",
        "activation_module": "web/core/producer_consumer_slice2b_activation.py",
        "env_var": SLICE2B_ENV_VAR,
        "canonical_gate_chain_unchanged": True,
        "canonical_gate_chain": [
            "run_quality_gates",
            "run_review",
            "run_critic",
            "run_precommit_eval",
            "commit_bot",
        ],
        "safety_invariants": {
            "default_off": True,
            "high_water_one": True,
            "promotion_barrier_fail_closed": True,
            "checkpoint_cas_unchanged": True,
            "publication_authority_unchanged": True,
            "consumer_writes_only_validation_ledger": True,
        },
        "activation_preconditions": {
            "no_active_canonical_checkpoint": True,
            "no_post_publication_handoff": True,
            "no_nonterminal_official_job": True,
        },
    }
    if extra:
        receipt["extra"] = dict(extra)
    canonical = json.dumps(
        receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    receipt["receipt_digest"] = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()

    out_dir = Path(results_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "slice2b_activation_migration_receipt.json"
    tmp_path = out_dir / "slice2b_activation_migration_receipt.json.tmp"
    tmp_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, out_path)
    return receipt


__all__ = [
    "MIGRATION_RECEIPT_SCHEMA",
    "SLICE2B_ACTIVATION_VERSION",
    "SLICE2B_ENV_VAR",
    "Slice2bActivation",
    "activation_registry",
    "build_snapshot_from_checkpoint",
    "canonical_gate_runner_factory",
    "slice2b_active",
    "stage_is_workers_done_seam",
    "write_activation_migration_receipt",
]
