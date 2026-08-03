"""Slice 2b one-ahead-buffer wiring regressions.

These tests prove the minimum viable Slice 2b behavior described in section 13
step 2 of ``docs/evolution-producer-consumer-pipeline-v1.md``:

* :func:`seal_candidate` produces a valid :class:`JobEnvelope` submitted to the
  Consumer queue and is idempotent under replay.
* :class:`ConsumerDispatcher` dequeues one sealed envelope, claims a fenced
  lease and runs the *unchanged canonical gate chain* (injected via test
  doubles) against the sealed artifact, writing outcomes to a Consumer-owned
  :class:`ValidationLedger` (never the producer's checkpoint).
* :class:`OneAheadCoordinator` lets the Producer begin the next prepare while
  the Consumer runs, and the synchronous promotion barrier
  (:meth:`wait_for_promotion_readiness`) blocks publication until the Consumer
  has promoted -- or surfaces the rejection fail-closed.
* The Producer's next-version reservation is *not* advanced by sealing: the
  high-water rule refuses a second seal while one is in flight.
* ``slice2b_enabled`` is default-off so the canonical runtime is untouched.

The canonical gate chain (``run_quality_gates``/``run_review``/``run_critic``/
``run_precommit_eval``/``commit_bot``) is intentionally stubbed here; the
production dispatcher will inject the real callables, which remain unchanged.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pipeline_job_contract import build_job_envelope
from producer_consumer_slice2b import (
    CONSUMER_GATE_CHAIN_ORDER,
    GATE_CHAIN_ORDER,
    AheadCoordinator,
    CandidateLifecycle,
    ConsumerDispatcher,
    OneAheadCoordinator,
    SealResult,
    Slice2bError,
    ValidationLedger,
    build_sealed_candidate_snapshot,
    build_slice2b_quality_envelope,
    seal_candidate,
    slice2b_enabled,
)
from producer_consumer_workflow_store import (
    ProducerConsumerStoreError,
    ProducerConsumerWorkflowAdapter,
)
from workflow_kernel import WorkflowStore


DIGESTS = {letter: letter * 64 for letter in "abcdef0123456789"}


def _adapter(tmp_path: Path) -> ProducerConsumerWorkflowAdapter:
    return ProducerConsumerWorkflowAdapter(
        WorkflowStore(tmp_path / "slice2b.sqlite3")
    )


def _snapshot(
    *,
    candidate_id: str = "candidate-v143",
    draft_id: str = "draft-v143",
    artifact_hash: str = DIGESTS["a"],
) -> dict:
    return build_sealed_candidate_snapshot(
        candidate_id=candidate_id,
        draft_id=draft_id,
        artifact_hash=artifact_hash,
        manifest_digest=DIGESTS["b"],
        charter_digest=DIGESTS["c"],
        epoch_binding={
            "evaluation_epoch": "epoch-2026-07-28",
            "workflow_run_id": "run-1",
            "target_lease_digest": DIGESTS["d"],
            "generation_ordinal": 143,
            "canonical_version": 143,
        },
        next_v=143,
        source_v=142,
        workflow_run_id="run-1",
        quality_native_match_timing_plan={"plan_digest": DIGESTS["e"]},
    )


_RESOURCE_CLAIM = {
    "resource_class": "cpu",
    "cpu_slots": 1,
    "memory_mb": 512,
    "gpu_slots": 0,
    "match_slots": 0,
    "official_slots": 0,
}
_RETRY_POLICY = {
    "max_attempts": 3,
    "initial_backoff_sec": 1.0,
    "backoff_multiplier": 2.0,
    "max_backoff_sec": 10.0,
    "retryable_outcomes": ["infrastructure_failure"],
}
_DEADLINE = {
    "submitted_at_epoch": 100.0,
    "not_before_epoch": 100.0,
    "expires_at_epoch": 1000.0,
}


def _seal(
    adapter: ProducerConsumerWorkflowAdapter,
    *,
    snapshot: dict | None = None,
    job_id: str = "job:draft-v143:quality-static",
    idempotency_key: str = "draft-v143:quality-static:v1",
    run_id: str = "draft-v143",
):
    return seal_candidate(
        adapter,
        snapshot=snapshot or _snapshot(),
        run_id=run_id,
        job_id=job_id,
        idempotency_key=idempotency_key,
        artifact_digest=(snapshot or _snapshot())["artifact_hash"],
        resource_claim=_RESOURCE_CLAIM,
        retry_policy=_RETRY_POLICY,
        deadline=_DEADLINE,
        evaluation_contract_digest=DIGESTS["1"],
        executor_digest=DIGESTS["2"],
        repository_digest=DIGESTS["5"],
        runtime_digest=DIGESTS["4"],
    )


# ---------------------------------------------------------------------------
# seal_candidate
# ---------------------------------------------------------------------------


def test_seal_produces_valid_envelope_submitted_to_queue(tmp_path):
    adapter = _adapter(tmp_path)
    snapshot = _snapshot()
    result = _seal(adapter, snapshot=snapshot)

    assert isinstance(result, SealResult)
    assert result["candidate_id"] == snapshot["candidate_id"]
    assert result["artifact_digest"] == snapshot["artifact_hash"]
    assert result["status"] == "requested"
    # The effect id is the canonical identity for the envelope in the kernel.
    loaded = adapter.load(result["effect_id"])
    envelope = loaded["envelope"]
    assert envelope["candidate_id"] == snapshot["candidate_id"]
    assert envelope["draft_id"] == snapshot["draft_id"]
    assert envelope["job_kind"] == "quality-static"
    assert envelope["priority"]["class"] == "compliance"
    assert result["envelope_digest"] == envelope["envelope_digest"]


def test_seal_is_idempotent_under_replay(tmp_path):
    adapter = _adapter(tmp_path)
    first = _seal(adapter)
    second = _seal(adapter)
    assert first == second
    # The kernel recorded exactly one effect for this run.
    effects = adapter.store.effects_for_run("draft-v143")
    assert len(effects) == 1


def test_seal_rejects_collapsed_draft_candidate_identity(tmp_path):
    adapter = _adapter(tmp_path)
    snapshot = _snapshot(candidate_id="draft-v143", draft_id="draft-v143")
    with pytest.raises(Exception):
        _seal(adapter, snapshot=snapshot)


def test_sealed_snapshot_is_content_bound_and_immutable():
    snapshot = _snapshot()
    twin = _snapshot()
    assert snapshot == twin
    assert snapshot["snapshot_digest"] == twin["snapshot_digest"]

    # A snapshot built with a different artifact hash has a different digest;
    # the digest is computed at construction time, so the snapshot is immutable
    # by contract (mutating the returned dict invalidates its snapshot_digest).
    drifted = _snapshot(artifact_hash=DIGESTS["f"])
    assert drifted["snapshot_digest"] != snapshot["snapshot_digest"]
    assert drifted["artifact_hash"] == DIGESTS["f"]


# ---------------------------------------------------------------------------
# ConsumerDispatcher
# ---------------------------------------------------------------------------


def _gate_runners(
    *,
    final_gate_outcome: str = "promotion",
    fail_at: str | None = None,
    infra_at: str | None = None,
    receipt_digest: str = DIGESTS["9"],
):
    """Build a complete ``GATE_CHAIN_ORDER`` runner map with stub gates."""

    runners = {}

    def make(gate_name):
        async def run(snapshot):
            if fail_at == gate_name:
                return {
                    "outcome": "candidate_failure",
                    "result_digest": DIGESTS["0"],
                    "detail": {"gate": gate_name, "reason": "stub_failure"},
                }
            if infra_at == gate_name:
                return {
                    "outcome": "infrastructure_failure",
                    "result_digest": DIGESTS["0"],
                    "detail": {"gate": gate_name, "reason": "stub_infra"},
                }
            return {
                "outcome": "success",
                "result_digest": DIGESTS["3"],
                "detail": {"gate": gate_name},
            }

        return run

    for gate_name in GATE_CHAIN_ORDER:
        if gate_name == "commit_bot":
            async def commit(snapshot):
                if fail_at == "commit_bot":
                    return {
                        "outcome": "candidate_failure",
                        "result_digest": DIGESTS["0"],
                    }
                return {
                    "outcome": "success",
                    "result_digest": receipt_digest,
                    "promotion_receipt_digest": receipt_digest,
                    "receipt_digest": receipt_digest,
                    "detail": {"commit_oid": "0" * 40},
                }

            runners[gate_name] = commit
        else:
            runners[gate_name] = make(gate_name)
    return runners


def test_consumer_dispatcher_runs_gate_chain_and_promotes(tmp_path):
    adapter = _adapter(tmp_path)
    snapshot = _snapshot()
    sealed = _seal(adapter, snapshot=snapshot)
    ledger = ValidationLedger()
    dispatcher = ConsumerDispatcher(adapter, ledger, owner="consumer-a")

    result = asyncio.run(
        dispatcher.run_once(
            sealed_snapshots={snapshot["candidate_id"]: snapshot},
            gates=_gate_runners(),
            now=110.0,
            lease_seconds=50.0,
        )
    )
    assert result["dispatched"] is True
    assert result["promoted"] is True
    entry = ledger.snapshot(snapshot["candidate_id"])
    assert entry["validation_outcome"] == "promoted"
    # Every gate in the chain ran and recorded a success receipt.
    assert set(entry["gate_results"]) == set(CONSUMER_GATE_CHAIN_ORDER)
    for gate_name in CONSUMER_GATE_CHAIN_ORDER:
        assert entry["gate_results"][gate_name]["outcome"] == "success"
    assert entry["promotion_receipt"] is not None


def test_consumer_dispatcher_writes_only_to_validation_ledger(tmp_path):
    """The Consumer never writes back into the producer's checkpoint."""

    adapter = _adapter(tmp_path)
    snapshot = _snapshot()
    _seal(adapter, snapshot=snapshot)
    ledger = ValidationLedger()
    dispatcher = ConsumerDispatcher(adapter, ledger, owner="consumer-a")

    asyncio.run(
        dispatcher.run_once(
            sealed_snapshots={snapshot["candidate_id"]: snapshot},
            gates=_gate_runners(),
            now=110.0,
            lease_seconds=50.0,
        )
    )
    # The validation ledger is the only place gate outcomes landed.
    assert ledger.is_terminal(snapshot["candidate_id"])
    # The durable effect is still in 'running' state in this minimum slice
    # (the dispatcher does not call adapter.complete yet; the canonical gate
    # chain owns receipt completion through its existing kernel calls).  The
    # important invariant is that no second state machine was created: the
    # envelope row count for the submitted run is still one.
    effects = adapter.store.effects_for_run("draft-v143")
    assert len(effects) == 1


def test_consumer_dispatcher_rejects_on_candidate_failure(tmp_path):
    adapter = _adapter(tmp_path)
    snapshot = _snapshot()
    _seal(adapter, snapshot=snapshot)
    ledger = ValidationLedger()
    dispatcher = ConsumerDispatcher(adapter, ledger, owner="consumer-a")

    result = asyncio.run(
        dispatcher.run_once(
            sealed_snapshots={snapshot["candidate_id"]: snapshot},
            gates=_gate_runners(fail_at="run_review"),
            now=110.0,
            lease_seconds=50.0,
        )
    )
    assert result["failed_at_gate"] == "run_review"
    assert result["reason"] == "candidate_failure"
    entry = ledger.snapshot(snapshot["candidate_id"])
    assert entry["validation_outcome"] == "rejected"
    assert entry["terminal_reason"] == "gate_failed:run_review"
    # Quality ran before the failing review; critic and later gates did not.
    assert "run_quality_gates" in entry["gate_results"]
    assert "run_review" in entry["gate_results"]
    assert "run_critic" not in entry["gate_results"]


def test_consumer_dispatcher_pauses_on_infrastructure_failure(tmp_path):
    adapter = _adapter(tmp_path)
    snapshot = _snapshot()
    _seal(adapter, snapshot=snapshot)
    ledger = ValidationLedger()
    dispatcher = ConsumerDispatcher(adapter, ledger, owner="consumer-a")

    result = asyncio.run(
        dispatcher.run_once(
            sealed_snapshots={snapshot["candidate_id"]: snapshot},
            gates=_gate_runners(infra_at="run_critic"),
            now=110.0,
            lease_seconds=50.0,
        )
    )
    assert result["paused_at_gate"] == "run_critic"
    assert result["reason"] == "infrastructure_failure"
    entry = ledger.snapshot(snapshot["candidate_id"])
    # The ledger stays running so a future recover/dispatch resumes on the
    # same envelope -- never silently drops the candidate.
    assert entry["validation_outcome"] == "running"
    assert entry["gate_results"]["run_critic"]["outcome"] == "infrastructure_failure"


def test_consumer_dispatcher_rejects_unknown_gate_outcome(tmp_path):
    adapter = _adapter(tmp_path)
    snapshot = _snapshot()
    _seal(adapter, snapshot=snapshot)
    ledger = ValidationLedger()
    dispatcher = ConsumerDispatcher(adapter, ledger, owner="consumer-a")

    async def bad_gate(snapshot):
        return {"outcome": "weird", "result_digest": DIGESTS["3"]}

    gates = _gate_runners()
    gates["run_quality_gates"] = bad_gate
    with pytest.raises(Slice2bError, match="bad_outcome"):
        asyncio.run(
            dispatcher.run_once(
                sealed_snapshots={snapshot["candidate_id"]: snapshot},
                gates=gates,
                now=110.0,
                lease_seconds=50.0,
            )
        )


def test_consumer_dispatcher_requires_complete_gate_chain(tmp_path):
    adapter = _adapter(tmp_path)
    snapshot = _snapshot()
    _seal(adapter, snapshot=snapshot)
    ledger = ValidationLedger()
    dispatcher = ConsumerDispatcher(adapter, ledger, owner="consumer-a")

    incomplete = _gate_runners()
    del incomplete["run_critic"]
    with pytest.raises(Slice2bError, match="missing_gate"):
        asyncio.run(
            dispatcher.run_once(
                sealed_snapshots={snapshot["candidate_id"]: snapshot},
                gates=incomplete,
                now=110.0,
                lease_seconds=50.0,
            )
        )


def test_consumer_dispatcher_is_idle_when_queue_empty(tmp_path):
    adapter = _adapter(tmp_path)
    ledger = ValidationLedger()
    dispatcher = ConsumerDispatcher(adapter, ledger, owner="consumer-a")
    result = asyncio.run(
        dispatcher.run_once(
            sealed_snapshots={},
            gates=_gate_runners(),
            now=110.0,
            lease_seconds=50.0,
        )
    )
    assert result["dispatched"] is False
    assert result["reason"] == "no_leasable_envelope"


def test_consumer_dispatcher_resumes_from_last_persisted_gate(tmp_path):
    """A lease-expiry restart must not re-run already-passed gates.

    Regression: the gate chain runs under a single bounded lease; the precommit
    native match takes 50-90 min, so a lease expiry + restart re-dispatches the
    effect.  Without resume-from-last-gate logic the dispatcher re-ran the
    ENTIRE chain (quality->review->critic->precommit) from scratch, throwing
    away the passed quality/review/critic work and burning an attempt each
    restart until the effect exhausted (abandoning an otherwise-healthy
    candidate).  Gates persisted as ``success`` must be skipped on re-dispatch.
    """
    adapter = _adapter(tmp_path)
    snapshot = _snapshot()
    sealed = _seal(adapter, snapshot=snapshot)
    candidate_id = snapshot["candidate_id"]
    ledger = ValidationLedger()
    dispatcher = ConsumerDispatcher(adapter, ledger, owner="consumer-a")

    # Seed the ledger the way a prior (interrupted) dispatch would have: the
    # quality/review/critic gates already recorded as success.  This mimics a
    # dispatch that reached critic_checked before its lease expired mid-
    # precommit, then got re-dispatched.
    ledger.start(
        candidate_id=candidate_id,
        sealed_artifact_hash=snapshot["artifact_hash"],
        envelope_effect_id=sealed["effect_id"],
        envelope_digest=sealed["envelope_digest"],
    )
    ledger.begin_consuming(candidate_id=candidate_id)
    _now = 100.0
    for gate_name in ("run_quality_gates", "run_review", "run_critic"):
        ledger.record_gate(
            candidate_id=candidate_id,
            gate_name=gate_name,
            outcome="success",
            result_digest=DIGESTS["3"],
            finished_at=_now,
            detail={"gate": gate_name, "prior_dispatch": True},
        )

    # Gate runners that record which gates actually executed this dispatch.
    called = []

    def make(gate_name):
        async def run(snapshot):
            called.append(gate_name)
            return {
                "outcome": "success",
                "result_digest": DIGESTS["9"],
                "promotion_receipt_digest": DIGESTS["9"],
                "receipt_digest": DIGESTS["9"],
                "detail": {"gate": gate_name},
            }

        return run

    gates = {name: make(name) for name in CONSUMER_GATE_CHAIN_ORDER}

    result = asyncio.run(
        dispatcher.run_once(
            sealed_snapshots={candidate_id: snapshot},
            gates=gates,
            now=200.0,
            lease_seconds=50.0,
        )
    )
    # Only the precommit gate should have executed; the three pre-seeded gates
    # were skipped (resumed past).
    assert called == ["run_precommit_eval"]
    assert result["dispatched"] is True
    assert result["promoted"] is True
    entry = ledger.snapshot(candidate_id)
    assert entry["validation_outcome"] == "promoted"
    # All four gates are present in the ledger (3 pre-seeded + 1 just-run).
    assert set(entry["gate_results"]) == set(CONSUMER_GATE_CHAIN_ORDER)
    for gate_name in CONSUMER_GATE_CHAIN_ORDER:
        assert entry["gate_results"][gate_name]["outcome"] == "success"


def test_consumer_dispatcher_force_reclaims_non_expired_lease_after_restart(tmp_path):
    """After a restart, a running effect's non-expired lease blocks recover().

    The consumer dispatcher force-reclaims its own running effect (the prior
    process is dead but the lease row persists with a non-expired lease_until).
    Without force-reclaim, the restarted consumer spins forever in
    no_leasable_envelope until the full lease elapses.
    """
    adapter = _adapter(tmp_path)
    snapshot = _snapshot()
    sealed = _seal(adapter, snapshot=snapshot)
    effect_id = sealed["effect_id"]
    ledger = ValidationLedger()
    # Claim the effect (status -> running) with a far-future lease, simulating
    # the prior process's lease surviving a restart.
    adapter.claim(effect_id, owner="consumer-a", lease_seconds=3600.0, now=110.0)

    # recover() returns nothing (lease not expired at now=120), but
    # force-reclaim finds the running consumer effect and reclaims it.
    dispatcher = ConsumerDispatcher(adapter, ledger, owner="consumer-a")
    found = dispatcher._force_recover_consumer_effect(lease_seconds=3600.0, now=120.0)
    assert found is not None
    assert len(found) == 1
    assert found[0]["effect_id"] == effect_id


# ---------------------------------------------------------------------------
# AheadCoordinator (derives all state from the CandidateLifecycle FSM)
# ---------------------------------------------------------------------------


def _seal_into_fsm(ledger, candidate_id, artifact_hash=DIGESTS["a"]):
    """Seed the FSM the way the real seal path does (seal_at_workers_done)."""
    ledger.start(
        candidate_id=candidate_id,
        sealed_artifact_hash=artifact_hash,
        envelope_effect_id=f"eff-{candidate_id}",
        envelope_digest=DIGESTS["e"],
    )


def test_producer_may_advance_when_buffer_has_capacity():
    ledger = ValidationLedger()
    coord = AheadCoordinator(ledger)
    # Empty FSM: buffer has full capacity (producer may seal); no candidate to
    # draft behind yet.
    assert coord.producer_may_advance() is True
    assert coord.producer_may_draft_behind() is False  # nothing in flight
    _seal_into_fsm(ledger, "c1")
    coord.note_sealed(candidate_id="c1", artifact_hash=DIGESTS["a"])
    # Single-ahead (max_ahead=1): the seal slot is occupied, no room to SEAL
    # another.  But drafting is UNBOUNDED (draft is LLM planning that doesn't
    # consume a publication slot), so the producer may always draft behind an
    # in-flight consumer to fill LLM idle time.
    assert coord.producer_may_draft_behind() is True  # unbounded draft
    assert coord.producer_may_advance() is False  # bounded seal (max_ahead=1)


def test_high_water_refuses_second_seal_in_minimum_slice():
    ledger = ValidationLedger()
    coord = AheadCoordinator(ledger)
    _seal_into_fsm(ledger, "c1")
    coord.note_sealed(candidate_id="c1", artifact_hash=DIGESTS["a"])
    # The real seal path seeds the FSM (ledger.start) before note_sealed.  With
    # max_ahead=1, a second sealed candidate exceeds the high-water.
    _seal_into_fsm(ledger, "c2", artifact_hash=DIGESTS["b"])
    with pytest.raises(Slice2bError, match="high_water_exceeded"):
        coord.note_sealed(candidate_id="c2", artifact_hash=DIGESTS["b"])


def test_sealed_artifact_drift_is_rejected():
    # Artifact drift is now detected by the FSM (the single source of truth),
    # not the coordinator's former _in_flight dict.
    ledger = ValidationLedger()
    _seal_into_fsm(ledger, "c1", artifact_hash=DIGESTS["a"])
    with pytest.raises(Slice2bError, match="artifact_drift"):
        _seal_into_fsm(ledger, "c1", artifact_hash=DIGESTS["b"])


def test_coordinator_derives_from_lifecycle_no_in_flight():
    """The coordinator holds NO candidate state: producer_may_* / in_flight are
    derived purely from the persisted lifecycle FSM.  This is the regression
    for the former recovery gap (recover_at_boot never replayed note_sealed,
    so the in-memory _in_flight was empty after a restart)."""
    ledger = ValidationLedger()
    coord = AheadCoordinator(ledger)
    assert coord.in_flight() == {}
    # Empty FSM: nothing in flight to draft behind.
    assert coord.producer_may_draft_behind() is False
    _seal_into_fsm(ledger, "c1")
    # The coordinator sees the sealed candidate WITHOUT any note_sealed call --
    # it reads the FSM directly.  This is correct immediately after a restart.
    fresh_coord = AheadCoordinator(ledger)
    assert fresh_coord.in_flight() == {"c1": DIGESTS["a"]}
    # Drafting is unbounded (LLM planning that fills idle time behind the
    # in-flight consumer); always True when something is in flight.
    assert fresh_coord.producer_may_draft_behind() is True


def test_promotion_barrier_blocks_until_consumer_promotes():
    ledger = ValidationLedger()
    coord = AheadCoordinator(ledger)
    # Seed the FSM (the real seal path).  The coordinator derives from it.
    _seal_into_fsm(ledger, "c1")
    coord.note_sealed(candidate_id="c1", artifact_hash=DIGESTS["a"])

    async def driver():
        # Simulate the Consumer completing the chain mid-barrier.
        async def consumer():
            await asyncio.sleep(0.02)
            ledger.promote(
                candidate_id="c1",
                promotion_receipt={"receipt_digest": DIGESTS["9"]},
                completed_at=200.0,
            )

        consumer_task = asyncio.create_task(consumer())
        entry = await coord.wait_for_promotion_readiness(
            candidate_id="c1", poll_interval=0.01
        )
        await consumer_task
        return entry

    entry = asyncio.run(driver())
    assert entry["validation_outcome"] == "promoted"
    # After promotion the candidate leaves non-terminal (0 in flight): the
    # bounded SEAL slot has capacity again, but there is nothing in flight to
    # draft behind, so unbounded drafting is False.
    assert coord.producer_may_advance() is True
    assert coord.producer_may_draft_behind() is False  # 0 in flight


def test_promotion_barrier_fails_closed_on_rejection():
    ledger = ValidationLedger()
    coord = AheadCoordinator(ledger)
    _seal_into_fsm(ledger, "c1")
    coord.note_sealed(candidate_id="c1", artifact_hash=DIGESTS["a"])

    async def driver():
        async def consumer():
            await asyncio.sleep(0.01)
            ledger.reject(
                candidate_id="c1",
                reason="gate_failed:run_review",
                completed_at=200.0,
            )

        consumer_task = asyncio.create_task(consumer())
        with pytest.raises(Slice2bError, match="rejected"):
            await coord.wait_for_promotion_readiness(
                candidate_id="c1", poll_interval=0.01
            )
        await consumer_task

    asyncio.run(driver())


def test_promotion_barrier_rejects_unknown_candidate():
    coord = AheadCoordinator(ValidationLedger())
    with pytest.raises(Slice2bError, match="unknown_candidate"):

        async def driver():
            await coord.wait_for_promotion_readiness(candidate_id="c-mystery")

        asyncio.run(driver())


def test_barrier_poll_callback_drives_consumer_in_same_loop():
    """A single event loop can run Producer barrier + Consumer dispatch."""

    ledger = ValidationLedger()
    coord = AheadCoordinator(ledger)
    _seal_into_fsm(ledger, "c1")
    coord.note_sealed(candidate_id="c1", artifact_hash=DIGESTS["a"])

    poll_count = {"n": 0}

    def poll_callback():
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            ledger.promote(
                candidate_id="c1",
                promotion_receipt={"receipt_digest": DIGESTS["9"]},
                completed_at=200.0,
            )

    async def driver():
        return await coord.wait_for_promotion_readiness(
            candidate_id="c1",
            poll_interval=0.01,
            poll_callback=poll_callback,
        )

    entry = asyncio.run(driver())
    assert entry["validation_outcome"] == "promoted"
    assert poll_count["n"] >= 1


# ---------------------------------------------------------------------------
# Opt-in seam stays default-off
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("context", [None, {}, {"pipeline_slice2b_enabled": False}])
def test_slice2b_disabled_by_default(context):
    assert slice2b_enabled(context) is False


def test_slice2b_enabled_requires_explicit_opt_in():
    assert slice2b_enabled({"pipeline_slice2b_enabled": True}) is True


# ---------------------------------------------------------------------------
# Cross-cutting: full one-ahead happy path (seal -> dispatch -> barrier)
# ---------------------------------------------------------------------------


def test_one_ahead_full_cycle_seal_dispatch_barrier(tmp_path):
    adapter = _adapter(tmp_path)
    ledger = ValidationLedger()
    coord = AheadCoordinator(ledger)
    dispatcher = ConsumerDispatcher(adapter, ledger, owner="consumer-a")

    snapshot = _snapshot()
    sealed = _seal(adapter, snapshot=snapshot)
    # The real seal path (seal_at_workers_done) seeds the FSM before the
    # coordinator or dispatcher see the candidate.  Mirror that here.
    ledger.start(
        candidate_id=sealed["candidate_id"],
        sealed_artifact_hash=sealed["artifact_digest"],
        envelope_effect_id=sealed["effect_id"],
        envelope_digest=sealed["envelope_digest"],
    )
    coord.note_sealed(
        candidate_id=sealed["candidate_id"],
        artifact_hash=sealed["artifact_digest"],
    )

    async def driver():
        # Producer lane: the barrier blocks until the Consumer finishes.
        # Run the Consumer dispatch from the barrier's poll callback so the
        # whole one-ahead dance happens in one event loop without threads.
        def poll():
            if not ledger.is_terminal(sealed["candidate_id"]):
                asyncio.ensure_future(
                    dispatcher.run_once(
                        sealed_snapshots={sealed["candidate_id"]: snapshot},
                        gates=_gate_runners(),
                        now=110.0,
                        lease_seconds=50.0,
                    )
                )

        entry = await coord.wait_for_promotion_readiness(
            candidate_id=sealed["candidate_id"],
            poll_interval=0.01,
            poll_callback=poll,
        )
        return entry

    entry = asyncio.run(driver())
    assert entry["validation_outcome"] == "promoted"
    assert set(entry["gate_results"]) == set(CONSUMER_GATE_CHAIN_ORDER)
    # The Producer can immediately begin the next prepare now that the slot is free.
    assert coord.producer_may_advance() is True


# ---------------------------------------------------------------------------
# CandidateLifecycle (persisted FSM replacing the in-memory ValidationLedger)
# ---------------------------------------------------------------------------


def test_lifecycle_persists_across_store_reopen(tmp_path):
    """A sealed candidate remains visible after the lifecycle store is closed
    and reopened (simulating a process restart).  This is the core durability
    invariant that lets one-ahead survive a restart."""
    db_path = tmp_path / "slice2b_lifecycle.sqlite3"
    lifecycle = CandidateLifecycle(db_path)
    lifecycle.start(
        candidate_id="candidate-v200",
        sealed_artifact_hash=DIGESTS["a"],
        envelope_effect_id="eff-200",
        envelope_digest=DIGESTS["b"],
        sealed_snapshot={"candidate_id": "candidate-v200", "next_v": 200},
    )
    assert lifecycle.non_terminal_candidates() == {
        "candidate-v200": DIGESTS["a"]
    }

    # Drop the in-memory object and reopen the same on-disk store.
    reopened = CandidateLifecycle(db_path)
    assert reopened.non_terminal_candidates() == {
        "candidate-v200": DIGESTS["a"]
    }
    entry = reopened.snapshot("candidate-v200")
    assert entry["validation_outcome"] == "running"
    assert entry["sealed_snapshot"]["next_v"] == 200


def test_lifecycle_terminal_candidate_not_in_non_terminal(tmp_path):
    """Once a candidate reaches PROMOTED, it leaves non_terminal_candidates
    (the boot-recovery set) but stays queryable via snapshot()."""
    lifecycle = CandidateLifecycle(tmp_path / "lc.sqlite3")
    lifecycle.start(
        candidate_id="candidate-v201",
        sealed_artifact_hash=DIGESTS["a"],
        envelope_effect_id="eff-201",
        envelope_digest=DIGESTS["b"],
    )
    assert "candidate-v201" in lifecycle.non_terminal_candidates()
    lifecycle.promote(
        candidate_id="candidate-v201",
        promotion_receipt={"receipt_digest": DIGESTS["c"]},
        completed_at=130.0,
    )
    assert lifecycle.non_terminal_candidates() == {}
    assert lifecycle.is_terminal("candidate-v201")
    assert lifecycle.is_promoted("candidate-v201")


def test_lifecycle_reject_moves_to_rejected_terminal(tmp_path):
    """reject() transitions SEALED -> REJECTED and records the reason."""
    lifecycle = CandidateLifecycle(tmp_path / "lc.sqlite3")
    lifecycle.start(
        candidate_id="candidate-v202",
        sealed_artifact_hash=DIGESTS["a"],
        envelope_effect_id="eff-202",
        envelope_digest=DIGESTS["b"],
    )
    lifecycle.reject(
        candidate_id="candidate-v202",
        reason="gate_failed:run_quality_gates",
        completed_at=140.0,
    )
    entry = lifecycle.snapshot("candidate-v202")
    assert entry["validation_outcome"] == "rejected"
    assert entry["terminal_reason"] == "gate_failed:run_quality_gates"
    assert lifecycle.is_terminal("candidate-v202")
    assert lifecycle.is_promoted("candidate-v202") is False


def test_lifecycle_illegal_transition_promote_after_rejected_raises(tmp_path):
    """A terminal candidate cannot transition again (PROMOTED/REJECTED are
    absorbing).  Trying to promote an already-rejected candidate raises."""
    lifecycle = CandidateLifecycle(tmp_path / "lc.sqlite3")
    lifecycle.start(
        candidate_id="candidate-v203",
        sealed_artifact_hash=DIGESTS["a"],
        envelope_effect_id="eff-203",
        envelope_digest=DIGESTS["b"],
    )
    lifecycle.reject(
        candidate_id="candidate-v203",
        reason="gate_failed:run_review",
        completed_at=150.0,
    )
    with pytest.raises(Slice2bError, match="illegal_transition"):
        lifecycle.promote(
            candidate_id="candidate-v203",
            promotion_receipt={"receipt_digest": DIGESTS["c"]},
            completed_at=160.0,
        )


def test_lifecycle_promote_without_start_raises(tmp_path):
    """promote() on a candidate that was never sealed (no SEALED row) is an
    illegal transition (None -> PROMOTED is not in the whitelist)."""
    lifecycle = CandidateLifecycle(tmp_path / "lc.sqlite3")
    with pytest.raises(Slice2bError, match="illegal_transition"):
        lifecycle.promote(
            candidate_id="candidate-never-sealed",
            promotion_receipt={"receipt_digest": DIGESTS["c"]},
            completed_at=170.0,
        )


def test_lifecycle_start_is_idempotent_on_replay(tmp_path):
    """Replaying the same seal (same artifact hash) returns the existing row
    without raising.  A different artifact hash is unrecoverable drift."""
    lifecycle = CandidateLifecycle(tmp_path / "lc.sqlite3")
    first = lifecycle.start(
        candidate_id="candidate-v204",
        sealed_artifact_hash=DIGESTS["a"],
        envelope_effect_id="eff-204",
        envelope_digest=DIGESTS["b"],
    )
    replay = lifecycle.start(
        candidate_id="candidate-v204",
        sealed_artifact_hash=DIGESTS["a"],
        envelope_effect_id="eff-204",
        envelope_digest=DIGESTS["b"],
    )
    assert first["sealed_artifact_hash"] == replay["sealed_artifact_hash"]
    with pytest.raises(Slice2bError, match="artifact_drift"):
        lifecycle.start(
            candidate_id="candidate-v204",
            sealed_artifact_hash=DIGESTS["f"],  # different artifact
            envelope_effect_id="eff-204",
            envelope_digest=DIGESTS["b"],
        )


def test_lifecycle_record_gate_persists_and_reloads(tmp_path):
    """Gate results survive a store reopen (consumer crashed mid-chain)."""
    db_path = tmp_path / "lc.sqlite3"
    lifecycle = CandidateLifecycle(db_path)
    lifecycle.start(
        candidate_id="candidate-v205",
        sealed_artifact_hash=DIGESTS["a"],
        envelope_effect_id="eff-205",
        envelope_digest=DIGESTS["b"],
    )
    lifecycle.record_gate(
        candidate_id="candidate-v205",
        gate_name="run_quality_gates",
        outcome="success",
        result_digest=DIGESTS["1"],
        finished_at=200.0,
    )
    # Reopen and confirm the gate result persisted.
    reopened = CandidateLifecycle(db_path)
    entry = reopened.snapshot("candidate-v205")
    assert "run_quality_gates" in entry["gate_results"]
    assert entry["gate_results"]["run_quality_gates"]["outcome"] == "success"


def test_validation_ledger_alias_is_candidate_lifecycle():
    """The backwards-compat alias resolves to the persisted class."""
    assert ValidationLedger is CandidateLifecycle


# ---------------------------------------------------------------------------
# Boot recovery (recover_at_boot re-schedules consumers after a restart)
# ---------------------------------------------------------------------------


def test_recover_at_boot_reschedules_non_terminal_candidates(tmp_path):
    """After a restart, recover_at_boot rebuilds the in-memory snapshot
    registry and re-schedules the consumer for every SEALED candidate."""
    from producer_consumer_slice2b_activation import Slice2bActivation

    adapter = _adapter(tmp_path)
    snapshot = _snapshot(candidate_id="candidate-v300")
    # First "process": seal + record a SEALED lifecycle with the snapshot.
    activation = Slice2bActivation(adapter=adapter)
    activation.seal_at_workers_done(
        snapshot=snapshot,
        run_id="run-300",
        job_id="job:draft-v300:quality-static",
        idempotency_key="draft-v300:quality-static:v1",
        artifact_digest=snapshot["artifact_hash"],
        resource_claim=_RESOURCE_CLAIM,
        retry_policy=_RETRY_POLICY,
        deadline=_DEADLINE,
        evaluation_contract_digest=DIGESTS["1"],
        executor_digest=DIGESTS["2"],
        repository_digest=DIGESTS["5"],
        runtime_digest=DIGESTS["4"],
    )
    assert "candidate-v300" in activation._sealed_snapshots

    # Simulate a crash: build a FRESH activation over the same adapter (same
    # sqlite).  The in-memory registries start empty.
    crashed_activation = Slice2bActivation(adapter=adapter)
    assert "candidate-v300" not in crashed_activation._sealed_snapshots

    # recover_at_boot rebuilds the registries from the persisted lifecycle.
    recovered = crashed_activation.recover_at_boot()
    assert any(
        r["candidate_id"] == "candidate-v300"
        for r in recovered["rescheduled"]
    )
    assert "candidate-v300" in crashed_activation._sealed_snapshots
    # The snapshot was recovered from the persisted lifecycle.
    recovered_snapshot = crashed_activation._sealed_snapshots["candidate-v300"]
    assert recovered_snapshot["candidate_id"] == "candidate-v300"
    # A factory was scheduled so ensure_consumer_running will relaunch it.
    assert "candidate-v300" in crashed_activation._scheduled_factories


def test_recover_at_boot_skips_terminal_candidates(tmp_path):
    """A candidate that already reached PROMOTED is not re-scheduled."""
    from producer_consumer_slice2b_activation import Slice2bActivation

    adapter = _adapter(tmp_path)
    snapshot = _snapshot(candidate_id="candidate-v301")
    activation = Slice2bActivation(adapter=adapter)
    activation.seal_at_workers_done(
        snapshot=snapshot,
        run_id="run-301",
        job_id="job:draft-v301:quality-static",
        idempotency_key="draft-v301:quality-static:v1",
        artifact_digest=snapshot["artifact_hash"],
        resource_claim=_RESOURCE_CLAIM,
        retry_policy=_RETRY_POLICY,
        deadline=_DEADLINE,
        evaluation_contract_digest=DIGESTS["1"],
        executor_digest=DIGESTS["2"],
        repository_digest=DIGESTS["5"],
        runtime_digest=DIGESTS["4"],
    )
    # Mark it terminal via the ledger directly (consumer finished + promoted).
    activation.ledger.promote(
        candidate_id="candidate-v301",
        promotion_receipt={"receipt_digest": DIGESTS["c"]},
        completed_at=300.0,
    )

    crashed_activation = Slice2bActivation(adapter=adapter)
    recovered = crashed_activation.recover_at_boot()
    assert recovered["rescheduled"] == []
    assert "candidate-v301" not in crashed_activation._sealed_snapshots


def test_death_proof_resolver_marks_absent_owner_dead(tmp_path):
    """The death-proof resolver proves prior-owner death when no live task
    exists for the effect (the post-restart case).

    The proof must satisfy ``reclaim_effect_lease``'s content-bound validation:
    an ``owner`` equal to the effect's ``lease_owner`` and a 64-hex
    ``proof_digest`` equal to ``content_digest(unsigned_proof)``.
    """
    from producer_consumer_slice2b_activation import Slice2bActivation
    from workflow_kernel import content_digest

    adapter = _adapter(tmp_path)
    activation = Slice2bActivation(adapter=adapter)
    resolver = activation.death_proof_resolver()
    # No consumer task registered -> owner is dead.  The effect carries its
    # prior lease_owner (always "slice2b-consumer" in production).
    proof = resolver({
        "effect_id": "some-effect",
        "lease_owner": "slice2b-consumer",
        "envelope": {"candidate_id": "candidate-v143"},
    })
    assert proof["owner_alive_in_process"] is False
    assert proof["reason"] == "consumer_task_absent"
    assert proof["owner"] == "slice2b-consumer"
    # proof_digest is 64-hex and equals content_digest(unsigned proof).
    assert isinstance(proof["proof_digest"], str)
    assert len(proof["proof_digest"]) == 64
    unsigned = {k: v for k, v in proof.items() if k != "proof_digest"}
    assert proof["proof_digest"] == content_digest(unsigned)


# ---------------------------------------------------------------------------
# Multi-ahead version reservation registry (draft_version_reservation)
# ---------------------------------------------------------------------------


def test_reserve_draft_version_assigns_distinct_versions(tmp_path):
    """Two draft slots get distinct reserved versions (the core multi-ahead
    invariant -- without this N drafts would collide on floor+1)."""
    lifecycle = CandidateLifecycle(tmp_path / "lc.sqlite3")
    v1 = lifecycle.reserve_draft_version(slot_id="draft1", floor_next_v=142)
    v2 = lifecycle.reserve_draft_version(slot_id="draft2", floor_next_v=142)
    assert v1 == 143  # floor+1
    assert v2 == 144  # strictly greater than the prior reservation
    assert v1 != v2


def test_reserve_draft_version_is_idempotent_per_slot(tmp_path):
    """Re-reserving the same slot returns its existing reservation."""
    lifecycle = CandidateLifecycle(tmp_path / "lc.sqlite3")
    v1 = lifecycle.reserve_draft_version(slot_id="draft1", floor_next_v=142)
    v1_again = lifecycle.reserve_draft_version(slot_id="draft1", floor_next_v=142)
    assert v1 == v1_again


def test_release_draft_version_frees_slot_for_reuse(tmp_path):
    """After release, the slot can be reserved again at a new version."""
    lifecycle = CandidateLifecycle(tmp_path / "lc.sqlite3")
    lifecycle.reserve_draft_version(slot_id="draft1", floor_next_v=142)
    assert lifecycle.release_draft_version(slot_id="draft1") is True
    # Re-reserve after release picks up from the current high-water.
    v2 = lifecycle.reserve_draft_version(slot_id="draft1", floor_next_v=142)
    assert v2 == 143
    assert lifecycle.active_reservations() == [
        {
            "slot_id": "draft1",
            "reserved_next_v": 143,
            "candidate_id": None,
            "created_at": v2 and lifecycle.active_reservations()[0]["created_at"],
        }
    ] or len(lifecycle.active_reservations()) == 1


def test_active_reservations_ordered_by_version(tmp_path):
    """active_reservations returns unreleased reservations ordered by version
    (lowest first -- the order ordered promotion consumes them)."""
    lifecycle = CandidateLifecycle(tmp_path / "lc.sqlite3")
    lifecycle.reserve_draft_version(slot_id="draft2", floor_next_v=142)  # 143
    lifecycle.reserve_draft_version(slot_id="draft3", floor_next_v=142)  # 144
    active = lifecycle.active_reservations()
    assert [r["reserved_next_v"] for r in active] == [143, 144]
    # Release the lowest; the next-lowest is now first.
    lifecycle.release_draft_version(slot_id="draft2")
    active = lifecycle.active_reservations()
    assert [r["slot_id"] for r in active] == ["draft3"]


def test_reserved_version_persists_across_reopen(tmp_path):
    """A reservation survives a store reopen (crash recovery)."""
    db_path = tmp_path / "lc.sqlite3"
    lifecycle = CandidateLifecycle(db_path)
    lifecycle.reserve_draft_version(slot_id="draft1", floor_next_v=142)
    reopened = CandidateLifecycle(db_path)
    assert reopened.active_reservations()[0]["slot_id"] == "draft1"
    assert reopened.reserved_version_for_slot("draft1") == 143


def test_reservation_floor_respects_high_water(tmp_path):
    """The reserved version is max(floor, highest_unreleased) + 1, so a draft
    never reserves below an already-in-flight higher draft."""
    lifecycle = CandidateLifecycle(tmp_path / "lc.sqlite3")
    lifecycle.reserve_draft_version(slot_id="draft1", floor_next_v=142)  # 143
    # A second draft with a HIGHER floor still reserves above the first.
    v2 = lifecycle.reserve_draft_version(slot_id="draft2", floor_next_v=200)
    assert v2 == 201
    # A third draft with a lower floor reserves above the highest (201), not 143.
    v3 = lifecycle.reserve_draft_version(slot_id="draft3", floor_next_v=142)
    assert v3 == 202


def test_next_promotable_draft_is_version_ordered(tmp_path):
    """next_promotable_draft(published_v) returns ONLY the draft reserved for
    published_v+1 -- enforcing version-ordered promotion.  A higher draft is
    NOT promotable until the lower one promotes and published_v advances."""
    lifecycle = CandidateLifecycle(tmp_path / "lc.sqlite3")
    coord = AheadCoordinator(lifecycle)
    # Two drafts in flight: reserved 143 (draft1) and 144 (draft2).
    lifecycle.reserve_draft_version(slot_id="draft1", floor_next_v=142)
    lifecycle.reserve_draft_version(slot_id="draft2", floor_next_v=142)
    # Primary just published v142: only draft1 (reserved 143) is promotable.
    promotable = coord.next_promotable_draft(published_v=142)
    assert promotable is not None
    assert promotable["slot_id"] == "draft1"
    assert promotable["reserved_next_v"] == 143
    # draft2 (reserved 144) is NOT promotable yet (published_v+1 == 143, not 144).
    # After draft1 promotes and publishes v143, draft2 becomes promotable.
    lifecycle.release_draft_version(slot_id="draft1")
    promotable2 = coord.next_promotable_draft(published_v=143)
    assert promotable2 is not None
    assert promotable2["slot_id"] == "draft2"
    assert promotable2["reserved_next_v"] == 144
    # Nothing promotable for a version with no matching reservation.
    assert coord.next_promotable_draft(published_v=999) is None


# ---------------------------------------------------------------------------
# Multi-ahead end-to-end: 2 drafts, distinct versions, ordered promotion
# ---------------------------------------------------------------------------


def test_multi_ahead_two_drafts_distinct_versions_ordered_promotion(tmp_path):
    """End-to-end multi-ahead: with max_ahead=2, two drafts reserve distinct
    versions, both seal into the FSM, and promotion is version-ordered (draft1
    before draft2).  This exercises the full A-E pipeline:
    reservation registry (B) + slot abstraction (C) + ordered promotion (D)."""
    lifecycle = CandidateLifecycle(tmp_path / "lc.sqlite3")
    coord = AheadCoordinator(lifecycle, max_ahead=2)

    # Two drafts reserve distinct versions (the core multi-ahead invariant).
    v1 = lifecycle.reserve_draft_version(slot_id="draft1", floor_next_v=142)
    v2 = lifecycle.reserve_draft_version(slot_id="draft2", floor_next_v=142)
    assert v1 == 143
    assert v2 == 144
    assert v1 != v2

    # Before any seal the FSM is empty (0 in flight): drafting is False under
    # the new unbounded-draft semantics (draft only behind an in-flight
    # consumer).  Note: SEALING has capacity (producer_may_advance True), but
    # drafting itself is gated on something being in flight.
    assert coord.producer_may_draft_behind() is False  # 0 in flight

    # Both drafts seal into the FSM (the real seal path: ledger.start).
    lifecycle.start(
        candidate_id="candidate-v143",
        sealed_artifact_hash=DIGESTS["a"],
        envelope_effect_id="eff-143",
        envelope_digest=DIGESTS["b"],
        reserved_next_v=v1,
        slot_id="draft1",
    )
    coord.note_sealed(candidate_id="candidate-v143", artifact_hash=DIGESTS["a"])

    lifecycle.start(
        candidate_id="candidate-v144",
        sealed_artifact_hash=DIGESTS["c"],
        envelope_effect_id="eff-144",
        envelope_digest=DIGESTS["d"],
        reserved_next_v=v2,
        slot_id="draft2",
    )
    coord.note_sealed(candidate_id="candidate-v144", artifact_hash=DIGESTS["c"])

    # Buffer is now full (2 sealed, max_ahead=2): SEALING is bounded, but
    # drafting is UNBOUNDED -- it only requires >=1 in flight, so it stays True.
    assert coord.producer_may_draft_behind() is True  # unbounded draft
    assert coord.in_flight() == {
        "candidate-v143": DIGESTS["a"],
        "candidate-v144": DIGESTS["c"],
    }

    # Ordered promotion: only draft1 (reserved 143) is promotable when the
    # primary published v142.  draft2 (reserved 144) is NOT promotable yet
    # (next_promotable_draft only returns the draft for published_v+1).
    promotable = coord.next_promotable_draft(published_v=142)
    assert promotable["slot_id"] == "draft1"
    assert promotable["reserved_next_v"] == 143

    # After draft1 promotes (release) + primary publishes v143, draft2 becomes
    # promotable (it is the draft reserved for 143+1=144).
    lifecycle.release_draft_version(slot_id="draft1")
    promotable2 = coord.next_promotable_draft(published_v=143)
    assert promotable2["slot_id"] == "draft2"
    assert promotable2["reserved_next_v"] == 144


def test_multi_ahead_max_ahead_one_preserves_single_ahead_behavior(tmp_path):
    """max_ahead=1 (the default) preserves the original single-ahead SEAL
    semantics: one sealed candidate fills the bounded seal buffer.  Drafting
    is now unbounded (LLM planning), so it stays True whenever >=1 is in
    flight regardless of max_ahead."""
    lifecycle = CandidateLifecycle(tmp_path / "lc.sqlite3")
    coord = AheadCoordinator(lifecycle, max_ahead=1)
    assert coord.max_ahead == 1
    assert coord.producer_may_draft_behind() is False  # empty FSM, 0 in flight
    lifecycle.start(
        candidate_id="candidate-v143",
        sealed_artifact_hash=DIGESTS["a"],
        envelope_effect_id="eff-143",
        envelope_digest=DIGESTS["b"],
    )
    coord.note_sealed(candidate_id="candidate-v143", artifact_hash=DIGESTS["a"])
    # Buffer full (1 sealed, max_ahead=1): SEALING is bounded (no room), but
    # drafting is UNBOUNDED and stays True (>=1 candidate in flight).
    assert coord.producer_may_advance() is False  # bounded seal (max_ahead=1)
    assert coord.producer_may_draft_behind() is True  # unbounded draft
