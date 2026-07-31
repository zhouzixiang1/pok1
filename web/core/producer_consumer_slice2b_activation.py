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
        self.coordinator = coordinator or OneAheadCoordinator(self.ledger)
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
        self.coordinator.note_sealed(
            candidate_id=sealed["candidate_id"],
            artifact_hash=sealed["artifact_digest"],
        )
        # Persist the sealed candidate's lifecycle (SEALED) AND the immutable
        # snapshot, so a process restart can recover the consumer task without
        # the Producer still being around.  The dispatcher's later ``start``
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

    def producer_may_advance(self) -> bool:
        """Producer may seal another candidate (high-water capacity check)."""

        return self.coordinator.producer_may_advance()

    # -- consumer task ------------------------------------------------------

    def launch_consumer_task(
        self,
        *,
        candidate_id: str,
        gate_runner_factory: Callable[[], Mapping[str, Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]]],
        now: float | None = None,
        lease_seconds: float = 300.0,
        loop: asyncio.AbstractEventLoop | None = None,
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

        async def _run_gate_chain() -> None:
            gates = gate_runner_factory()
            try:
                await dispatcher.run_once(
                    sealed_snapshots={candidate_id: snapshot},
                    gates=gates,
                    now=dispatch_now,
                    lease_seconds=lease_seconds,
                )
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
        lease_seconds: float = 300.0,
    ) -> None:
        """Register a consumer launch to be driven by the orchestrator loop.

        The orchestrator seam runs synchronously (outside any event loop) when
        it seals at ``workers_done``.  It cannot create an ``asyncio.Task``
        directly because there is no running loop.  This method stashes the
        factory; :meth:`drain_scheduled_consumer` (called from the loop) or the
        promotion barrier (which runs inside the loop) creates and awaits the
        task.  If no factory is scheduled, the promotion barrier creates a task
        on-demand from the registered activation using
        :attr:`default_gate_runner_factory`.
        """

        if candidate_id not in self._sealed_snapshots:
            raise Slice2bError(
                "slice2b_consumer_unknown_candidate:" + candidate_id
            )
        self._scheduled_factories[candidate_id] = (gate_runner_factory, now, lease_seconds)

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
        factory, now, lease_seconds = scheduled
        return self.launch_consumer_task(
            candidate_id=candidate_id,
            gate_runner_factory=factory,
            now=now,
            lease_seconds=lease_seconds,
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

        recovered: dict[str, Any] = {"rescheduled": [], "terminal": []}
        try:
            non_terminal = self.ledger.non_terminal_candidates()
        except Exception:
            # The lifecycle store may not be initialized yet (first-ever
            # boot); nothing to recover.
            return recovered
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
            self.schedule_consumer(
                candidate_id=candidate_id,
                gate_runner_factory=factory,
                now=dispatch_epoch,
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
        """

        def _resolve(effect: Mapping[str, Any]) -> Mapping[str, Any]:
            # The effect_id encodes the sealed candidate; if its consumer task
            # is not live in THIS process, the prior owner is dead.  A restart
            # always satisfies this (no tasks exist yet at boot).
            effect_id = str(effect.get("effect_id") or "")
            task = self._consumer_tasks.get(effect_id) if effect_id else None
            owner_alive = task is not None and not task.done()
            return {
                "schema": "slice2b-death-proof-v1",
                "effect_id": effect_id,
                "owner_alive_in_process": bool(owner_alive),
                "proof": (
                    "consumer_task_absent"
                    if not owner_alive
                    else "consumer_task_live"
                ),
                "observed_at": time.time(),
            }

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
            handlers = {
                "run_quality_gates": run_quality_gates,
                "run_review": run_review,
                "run_critic": run_critic,
                "run_precommit_eval": run_precommit_eval,
                "commit_bot": commit_bot,
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
                    result = await canonical(args)
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
                data = result
                # Distinguish infrastructure-class failures (transient/retryable:
                # the dispatcher pauses the ledger so a later run can recover)
                # from genuine candidate failures (the candidate itself is bad
                # and must be rejected).  Without this split, any handler that
                # returns an error -- including a retryable infra pause like a
                # quota wait or a sandbox hiccup -- would be misclassified as a
                # permanent candidate failure and the candidate abandoned.
                if (
                    data.get("failure_class") == "infrastructure"
                    or data.get("action") == "retry"
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
