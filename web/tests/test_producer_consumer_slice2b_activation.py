"""Slice 2b one-ahead-buffer activation wiring regressions.

These tests prove the *activation* layer described in section 13 step 2 of
``docs/evolution-producer-consumer-pipeline-v1.md``: the orchestrator seam at
``workers_done`` that seals the candidate, launches the background consumer
gate chain, lets the producer advance, and synchronizes publication through
the fail-closed promotion barrier.

Coverage:

* ``slice2b_active`` is default-off and honors ``POK_SLICE2B_ENABLED=1`` and the
  ``pipeline_slice2b_enabled`` context flag.
* :class:`Slice2bActivation` seals at ``workers_done``, registers the
  one-ahead slot, and clears the producer to advance.
* The background consumer task runs the *injected canonical gate chain* to
  terminal state (promoted or rejected) and notifies the coordinator.
* The promotion barrier blocks publication until the consumer promotes, and
  fails closed (raises) on rejection.
* The migration receipt is content-bound and written to ``results/``.
* The orchestrator seam (``_slice2b_seal_at_workers_done`` /
  ``_slice2b_promotion_barrier``) degrades to the canonical inline path when
  slice2b is inactive, the stage is wrong, or the activation is not
  configured.

The canonical gate chain (``run_quality_gates``/``run_review``/``run_critic``/
``run_precommit_eval``/``commit_bot``) is intentionally stubbed here; the
production activation injects the real handlers unchanged.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from producer_consumer_slice2b import (
    GATE_CHAIN_ORDER,
    Slice2bError,
)
from producer_consumer_slice2b_activation import (
    MIGRATION_RECEIPT_SCHEMA,
    SLICE2B_ACTIVATION_VERSION,
    SLICE2B_ENV_VAR,
    Slice2bActivation,
    build_snapshot_from_checkpoint,
    slice2b_active,
    stage_is_workers_done_seam,
    write_activation_migration_receipt,
)
from producer_consumer_workflow_store import ProducerConsumerWorkflowAdapter
from workflow_kernel import WorkflowStore


DIGESTS = {letter: letter * 64 for letter in "abcdef0123456789"}

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


def _adapter(tmp_path: Path) -> ProducerConsumerWorkflowAdapter:
    return ProducerConsumerWorkflowAdapter(
        WorkflowStore(tmp_path / "slice2b.sqlite3")
    )


def _checkpoint(
    *,
    next_v: int = 143,
    source_v: int = 142,
    artifact_hash: str = DIGESTS["a"],
    manifest_digest: str = DIGESTS["b"],
    charter_digest: str = DIGESTS["c"],
    stage: str = "workers_done",
) -> dict:
    return {
        "stage": stage,
        "next_v": next_v,
        "source_v": source_v,
        "workflow_run_id": "run-1",
        "candidate_id": f"candidate-v{next_v}",
        "draft_id": f"draft-v{next_v}",
        "checkpoint_revision": 1,
        "last_update_ts": 100.0,
        "candidate_artifact_hash": artifact_hash,
        "candidate_manifest_digest": manifest_digest,
        "charter_digest": charter_digest,
        "epoch_binding": {
            "evaluation_epoch": "epoch-2026-07-28",
            "workflow_run_id": "run-1",
            "target_lease_digest": DIGESTS["d"],
            "generation_ordinal": next_v,
            "canonical_version": next_v,
        },
    }


def _snapshot(checkpoint=None, **overrides):
    """Build a snapshot from a checkpoint with optional digest overrides."""

    ckpt = checkpoint or _checkpoint()
    return build_snapshot_from_checkpoint(
        ckpt,
        artifact_hash=overrides.get("artifact_hash", DIGESTS["a"]),
        manifest_digest=overrides.get("manifest_digest", DIGESTS["b"]),
        charter_digest=overrides.get("charter_digest", DIGESTS["c"]),
    )


def _seal_kwargs(snapshot):
    """Full seal_candidate kwargs matching the job-envelope contract schema."""

    return dict(
        snapshot=snapshot,
        run_id=snapshot["workflow_run_id"],
        job_id=f"job:{snapshot['draft_id']}:quality-static",
        idempotency_key=f"{snapshot['draft_id']}:quality-static:v1",
        artifact_digest=snapshot["artifact_hash"],
        resource_claim=_RESOURCE_CLAIM,
        retry_policy=_RETRY_POLICY,
        deadline=_DEADLINE,
        evaluation_contract_digest=DIGESTS["1"],
        executor_digest=DIGESTS["2"],
        repository_digest=DIGESTS["5"],
        runtime_digest=DIGESTS["4"],
    )


def _gate_runner_factory(
    *,
    fail_at: str | None = None,
    infra_at: str | None = None,
    receipt_digest: str = DIGESTS["9"],
):
    """Build a factory returning a fresh GATE_CHAIN_ORDER runner map."""

    def factory():
        runners = {}

        def make(gate_name):
            async def run(snapshot):
                if fail_at == gate_name:
                    return {
                        "outcome": "candidate_failure",
                        "result_digest": DIGESTS["0"],
                        "detail": {"gate": gate_name},
                    }
                if infra_at == gate_name:
                    return {
                        "outcome": "infrastructure_failure",
                        "result_digest": DIGESTS["0"],
                        "detail": {"gate": gate_name},
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
                    }

                runners[gate_name] = commit
            else:
                runners[gate_name] = make(gate_name)
        return runners

    return factory


# ---------------------------------------------------------------------------
# slice2b_active flag control
# ---------------------------------------------------------------------------


def test_slice2b_inactive_by_default(monkeypatch):
    monkeypatch.delenv(SLICE2B_ENV_VAR, raising=False)
    assert slice2b_active() is False
    assert slice2b_active({}) is False
    assert slice2b_active({"pipeline_slice2b_enabled": False}) is False
    assert slice2b_active(None) is False


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "on"])
def test_slice2b_active_via_env_var(monkeypatch, value):
    monkeypatch.setenv(SLICE2B_ENV_VAR, value)
    assert slice2b_active() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_slice2b_inactive_for_falsy_env(monkeypatch, value):
    monkeypatch.setenv(SLICE2B_ENV_VAR, value)
    assert slice2b_active() is False


def test_slice2b_active_via_context_flag(monkeypatch):
    monkeypatch.delenv(SLICE2B_ENV_VAR, raising=False)
    assert slice2b_active({"pipeline_slice2b_enabled": True}) is True


def test_slice2b_env_var_wins_over_context(monkeypatch):
    """A truthy env var enables even when the context flag is false."""

    monkeypatch.setenv(SLICE2B_ENV_VAR, "1")
    assert slice2b_active({"pipeline_slice2b_enabled": False}) is True


# ---------------------------------------------------------------------------
# stage_is_workers_done_seam
# ---------------------------------------------------------------------------


def test_stage_is_workers_done_seam_only_at_workers_done():
    assert stage_is_workers_done_seam({"stage": "workers_done"}) is True
    assert stage_is_workers_done_seam({"stage": "quality_passed"}) is False
    assert stage_is_workers_done_seam({}) is False
    assert stage_is_workers_done_seam(None) is False


# ---------------------------------------------------------------------------
# build_snapshot_from_checkpoint
# ---------------------------------------------------------------------------


def test_build_snapshot_from_checkpoint_binds_identity():
    snapshot = _snapshot()
    assert snapshot["candidate_id"] == "candidate-v143"
    assert snapshot["draft_id"] == "draft-v143"
    assert snapshot["next_v"] == 143
    assert snapshot["source_v"] == 142
    assert snapshot["workflow_run_id"] == "run-1"
    assert snapshot["artifact_hash"] == DIGESTS["a"]
    assert "snapshot_digest" in snapshot


def test_build_snapshot_from_checkpoint_backfills_epoch_binding():
    checkpoint = _checkpoint()
    del checkpoint["epoch_binding"]
    snapshot = build_snapshot_from_checkpoint(
        checkpoint,
        artifact_hash=DIGESTS["a"],
        manifest_digest=DIGESTS["b"],
        charter_digest=DIGESTS["c"],
    )
    # Missing epoch_binding is backfilled from the identity fields rather than
    # crashing; the canonical gate chain re-validates the full target identity.
    assert snapshot["epoch_binding"]["workflow_run_id"] == "run-1"
    assert snapshot["epoch_binding"]["canonical_version"] == 143


# ---------------------------------------------------------------------------
# Slice2bActivation: seal at workers_done
# ---------------------------------------------------------------------------


def test_seal_at_workers_done_registers_one_ahead_slot(tmp_path):
    activation = Slice2bActivation(adapter=_adapter(tmp_path))
    snapshot = _snapshot()
    sealed = activation.seal_at_workers_done(**_seal_kwargs(snapshot))
    assert sealed["candidate_id"] == "candidate-v143"
    # The one-ahead slot is occupied: producer may NOT advance until the
    # consumer finishes.
    assert activation.producer_may_advance() is False
    # Sealing the same candidate again is idempotent and does not raise.
    sealed_again = activation.seal_at_workers_done(**_seal_kwargs(snapshot))
    assert sealed_again["effect_id"] == sealed["effect_id"]


def test_seal_at_workers_done_high_water_refuses_second_candidate(tmp_path):
    activation = Slice2bActivation(adapter=_adapter(tmp_path))
    snap_a = _snapshot(_checkpoint(next_v=143, source_v=142))
    activation.seal_at_workers_done(**_seal_kwargs(snap_a))
    snap_b = _snapshot(
        _checkpoint(
            next_v=144,
            source_v=143,
            artifact_hash=DIGESTS["f"],
            manifest_digest=DIGESTS["e"],
        ),
        artifact_hash=DIGESTS["f"],
        manifest_digest=DIGESTS["e"],
    )
    with pytest.raises(Slice2bError, match="high_water_exceeded"):
        activation.seal_at_workers_done(**_seal_kwargs(snap_b))


# ---------------------------------------------------------------------------
# Slice2bActivation: background consumer runs the canonical gate chain
# ---------------------------------------------------------------------------


def _run_consumer(activation, candidate_id, factory):
    async def driver():
        task = activation.launch_consumer_task(
            candidate_id=candidate_id,
            gate_runner_factory=factory,
        )
        await task

    asyncio.run(driver())


def test_consumer_task_promotes_and_drains_one_ahead_slot(tmp_path):
    activation = Slice2bActivation(adapter=_adapter(tmp_path))
    snapshot = _snapshot()
    activation.seal_at_workers_done(**_seal_kwargs(snapshot))
    candidate_id = snapshot["candidate_id"]

    _run_consumer(activation, candidate_id, _gate_runner_factory())

    entry = activation.ledger.snapshot(candidate_id)
    assert entry["validation_outcome"] == "promoted"
    assert set(entry["gate_results"]) == set(GATE_CHAIN_ORDER)
    # The one-ahead slot drained after promotion: producer may advance again.
    assert activation.producer_may_advance() is True


def test_consumer_task_rejects_on_candidate_failure(tmp_path):
    activation = Slice2bActivation(adapter=_adapter(tmp_path))
    snapshot = _snapshot()
    activation.seal_at_workers_done(**_seal_kwargs(snapshot))
    candidate_id = snapshot["candidate_id"]

    _run_consumer(activation, candidate_id, _gate_runner_factory(fail_at="run_review"))

    entry = activation.ledger.snapshot(candidate_id)
    assert entry["validation_outcome"] == "rejected"
    assert entry["terminal_reason"] == "gate_failed:run_review"
    # The slot drains even on rejection so the producer is not permanently stuck.
    assert activation.producer_may_advance() is True


def test_consumer_task_records_infrastructure_failure_as_running(tmp_path):
    """A gate runner that raises is logged as an infrastructure failure.

    The dispatcher records the infrastructure failure and leaves the ledger
    ``running`` so a future retry can resume on the same envelope (the
    canonical contract).  The promotion barrier therefore never observes a
    promoted entry for this candidate and fails closed -- publication does NOT
    proceed.  This is the fail-closed guarantee for infra errors.
    """

    activation = Slice2bActivation(adapter=_adapter(tmp_path))
    snapshot = _snapshot()
    activation.seal_at_workers_done(**_seal_kwargs(snapshot))
    candidate_id = snapshot["candidate_id"]

    def factory():
        def make(name):
            async def run(snapshot):
                raise RuntimeError("canonical handler blew up")
            return run
        return {name: make(name) for name in GATE_CHAIN_ORDER}

    _run_consumer(activation, candidate_id, factory)

    entry = activation.ledger.snapshot(candidate_id)
    assert entry is not None
    # The dispatcher paused on infrastructure failure; the ledger is NOT
    # promoted, so the promotion barrier cannot release publication.
    assert entry["validation_outcome"] != "promoted"
    # The infrastructure failure was recorded against the first gate.
    gates = entry["gate_results"]
    assert gates
    first_gate = next(iter(gates))
    assert gates[first_gate]["outcome"] == "infrastructure_failure"
    # The producer is NOT cleared to advance (the slot is still in flight).
    assert activation.producer_may_advance() is False


# ---------------------------------------------------------------------------
# Slice2bActivation: promotion barrier (synchronous fail-closed)
# ---------------------------------------------------------------------------


def test_promotion_barrier_blocks_until_consumer_promotes(tmp_path):
    activation = Slice2bActivation(adapter=_adapter(tmp_path))
    snapshot = _snapshot()
    activation.seal_at_workers_done(**_seal_kwargs(snapshot))
    candidate_id = snapshot["candidate_id"]

    async def driver():
        activation.launch_consumer_task(
            candidate_id=candidate_id,
            gate_runner_factory=_gate_runner_factory(),
        )
        return await activation.await_promotion(candidate_id=candidate_id)

    entry = asyncio.run(driver())
    assert entry["validation_outcome"] == "promoted"


def test_promotion_barrier_fails_closed_on_rejection(tmp_path):
    activation = Slice2bActivation(adapter=_adapter(tmp_path))
    snapshot = _snapshot()
    activation.seal_at_workers_done(**_seal_kwargs(snapshot))
    candidate_id = snapshot["candidate_id"]

    async def driver():
        activation.launch_consumer_task(
            candidate_id=candidate_id,
            gate_runner_factory=_gate_runner_factory(fail_at="run_review"),
        )
        with pytest.raises(Slice2bError, match="rejected"):
            await activation.await_promotion(candidate_id=candidate_id)

    asyncio.run(driver())


# ---------------------------------------------------------------------------
# Migration receipt (design doc Section 13)
# ---------------------------------------------------------------------------


def test_migration_receipt_is_content_bound_and_written(tmp_path):
    head = "abc123def456"
    receipt = write_activation_migration_receipt(
        results_dir=tmp_path,
        head_commit=head,
        activated_at=1700000000.0,
    )
    assert receipt["schema"] == MIGRATION_RECEIPT_SCHEMA
    assert receipt["head_commit"] == head
    assert receipt["slice2b_version"] == SLICE2B_ACTIVATION_VERSION
    assert receipt["canonical_gate_chain_unchanged"] is True
    assert receipt["canonical_gate_chain"] == [
        "run_quality_gates",
        "run_review",
        "run_critic",
        "run_precommit_eval",
        "commit_bot",
    ]
    assert receipt["safety_invariants"]["default_off"] is True
    assert receipt["safety_invariants"]["promotion_barrier_fail_closed"] is True
    assert "receipt_digest" in receipt
    written = json.loads(
        (tmp_path / "slice2b_activation_migration_receipt.json").read_text()
    )
    assert written["receipt_digest"] == receipt["receipt_digest"]


def test_migration_receipt_changes_with_head(tmp_path):
    r1 = write_activation_migration_receipt(
        results_dir=tmp_path, head_commit="aaa", activated_at=1.0,
    )
    r2 = write_activation_migration_receipt(
        results_dir=tmp_path, head_commit="bbb", activated_at=1.0,
    )
    assert r1["receipt_digest"] != r2["receipt_digest"]


# ---------------------------------------------------------------------------
# Orchestrator seam integration (degrades to canonical when inactive)
# ---------------------------------------------------------------------------


def test_seal_seam_returns_false_when_slice2b_inactive(monkeypatch):
    """The orchestrator seam is a no-op when slice2b is not opted in."""

    monkeypatch.delenv(SLICE2B_ENV_VAR, raising=False)
    import orchestrator_deterministic_route as odr

    checkpoint = _checkpoint()
    # No activation registered; slice2b inactive -> seam refuses.
    assert odr._slice2b_seal_at_workers_done(
        checkpoint, 143, 142, ui=None, outcome=None,
    ) is False


def test_seal_seam_returns_false_when_stage_not_workers_done(monkeypatch):
    monkeypatch.setenv(SLICE2B_ENV_VAR, "1")
    import orchestrator_deterministic_route as odr

    checkpoint = _checkpoint(stage="quality_passed")
    assert odr._slice2b_seal_at_workers_done(
        checkpoint, 143, 142, ui=None, outcome=None,
    ) is False


def test_seal_seam_returns_false_when_activation_not_registered(monkeypatch, tmp_path):
    """slice2b active but no activation instance configured -> refuses."""

    monkeypatch.setenv(SLICE2B_ENV_VAR, "1")
    import orchestrator as _o
    import orchestrator_deterministic_route as odr

    _o._slice2b_activation_registry("clear")
    checkpoint = _checkpoint()
    try:
        assert odr._slice2b_seal_at_workers_done(
            checkpoint, 143, 142, ui=None, outcome=None,
        ) is False
    finally:
        _o._slice2b_activation_registry("clear")


def test_seal_seam_returns_false_when_digests_missing(monkeypatch, tmp_path):
    """Missing artifact/manifest/charter digests -> refuses (no guessing)."""

    monkeypatch.setenv(SLICE2B_ENV_VAR, "1")
    import orchestrator as _o
    import orchestrator_deterministic_route as odr

    _o._slice2b_activation_registry("set", adapter=_adapter(tmp_path))
    try:
        checkpoint = _checkpoint()
        del checkpoint["candidate_artifact_hash"]
        del checkpoint["candidate_manifest_digest"]
        assert odr._slice2b_seal_at_workers_done(
            checkpoint, 143, 142, ui=None, outcome=None,
        ) is False
    finally:
        _o._slice2b_activation_registry("clear")


def test_seal_seam_seals_and_schedules_consumer_when_active(monkeypatch, tmp_path):
    """End-to-end: at workers_done + slice2b active, the seam seals + schedules.

    The synchronous seam cannot create an asyncio.Task (no running loop), so it
    schedules the consumer factory.  The orchestrator loop (here, a test driver)
    then drains it via ``ensure_consumer_running`` and the gate chain runs to
    promotion.
    """

    monkeypatch.setenv(SLICE2B_ENV_VAR, "1")
    import orchestrator as _o
    import orchestrator_deterministic_route as odr

    adapter = _adapter(tmp_path)
    activation = _o._slice2b_activation_registry("set", adapter=adapter)
    # Inject a stub gate-runner factory so the consumer task does not try to
    # import the real canonical handlers.
    _o._slice2b_gate_runner_factory = lambda nv, sv: _gate_runner_factory()
    try:
        checkpoint = _checkpoint()
        outcome = {}
        result = odr._slice2b_seal_at_workers_done(
            checkpoint, 143, 142, ui=None, outcome=outcome,
        )
        assert result is True
        assert outcome["result"]["slice2b_sealed"] is True
        assert outcome["result"]["candidate_id"] == "candidate-v143"
        # The producer may NOT advance until the consumer finishes (high-water).
        assert activation.producer_may_advance() is False
        # The consumer is scheduled, not yet running.
        assert "candidate-v143" in activation._scheduled_factories

        async def driver():
            task = await activation.ensure_consumer_running("candidate-v143")
            assert task is not None
            await task

        asyncio.run(driver())
        assert activation.ledger.is_promoted("candidate-v143")
        # After promotion the slot drains: producer may advance.
        assert activation.producer_may_advance() is True
    finally:
        _o._slice2b_activation_registry("clear")


def test_promotion_barrier_noop_when_slice2b_inactive(monkeypatch):
    monkeypatch.delenv(SLICE2B_ENV_VAR, raising=False)
    import orchestrator_deterministic_route as odr

    checkpoint = _checkpoint()
    assert asyncio.run(
        odr._slice2b_promotion_barrier(checkpoint, 143, 142)
    ) is False


def test_promotion_barrier_noop_when_no_sealed_candidate(monkeypatch, tmp_path):
    """No sealed candidate for this generation -> canonical inline path."""

    monkeypatch.setenv(SLICE2B_ENV_VAR, "1")
    import orchestrator as _o
    import orchestrator_deterministic_route as odr

    _o._slice2b_activation_registry("set", adapter=_adapter(tmp_path))
    try:
        checkpoint = _checkpoint()
        assert asyncio.run(
            odr._slice2b_promotion_barrier(checkpoint, 143, 142)
        ) is False
    finally:
        _o._slice2b_activation_registry("clear")


def test_promotion_barrier_waits_when_sealed_candidate_in_flight(monkeypatch, tmp_path):
    """Sealed candidate awaiting promotion -> barrier drives + waits for consumer."""

    monkeypatch.setenv(SLICE2B_ENV_VAR, "1")
    import orchestrator as _o
    import orchestrator_deterministic_route as odr

    adapter = _adapter(tmp_path)
    activation = _o._slice2b_activation_registry("set", adapter=adapter)
    _o._slice2b_gate_runner_factory = lambda nv, sv: _gate_runner_factory()
    try:
        checkpoint = _checkpoint()
        # Seam seals + schedules the consumer (no task created yet; no loop).
        odr._slice2b_seal_at_workers_done(
            checkpoint, 143, 142, ui=None, outcome=None,
        )
        candidate_id = "candidate-v143"

        async def driver():
            # Barrier runs inside the loop; it drives the scheduled consumer
            # via ``await_promotion`` -> ``ensure_consumer_running``.
            return await odr._slice2b_promotion_barrier(checkpoint, 143, 142)

        owned = asyncio.run(driver())
        assert owned is True
        assert activation.ledger.is_promoted(candidate_id)
    finally:
        _o._slice2b_activation_registry("clear")


def test_promotion_barrier_fails_closed_on_rejection(monkeypatch, tmp_path):
    monkeypatch.setenv(SLICE2B_ENV_VAR, "1")
    import orchestrator as _o
    import orchestrator_deterministic_route as odr

    adapter = _adapter(tmp_path)
    activation = _o._slice2b_activation_registry("set", adapter=adapter)
    _o._slice2b_gate_runner_factory = lambda nv, sv: _gate_runner_factory(fail_at="commit_bot")
    try:
        checkpoint = _checkpoint()
        odr._slice2b_seal_at_workers_done(
            checkpoint, 143, 142, ui=None, outcome=None,
        )
        candidate_id = "candidate-v143"

        async def driver():
            with pytest.raises(Slice2bError, match="rejected"):
                await odr._slice2b_promotion_barrier(checkpoint, 143, 142)

        asyncio.run(driver())
        assert activation.ledger.snapshot(candidate_id)["validation_outcome"] == "rejected"
    finally:
        _o._slice2b_activation_registry("clear")


# ---------------------------------------------------------------------------
# Registry lifecycle
# ---------------------------------------------------------------------------


def test_activation_registry_set_get_clear(tmp_path):
    import orchestrator as _o

    _o._slice2b_activation_registry("clear")
    assert _o._slice2b_activation_registry("get") is None
    activation = _o._slice2b_activation_registry("set", adapter=_adapter(tmp_path))
    assert activation is not None
    assert _o._slice2b_activation_registry("get") is activation
    _o._slice2b_activation_registry("clear")
    assert _o._slice2b_activation_registry("get") is None


def test_activation_registry_set_requires_adapter():
    import orchestrator as _o

    with pytest.raises(ValueError):
        _o._slice2b_activation_registry("set", adapter=None)
