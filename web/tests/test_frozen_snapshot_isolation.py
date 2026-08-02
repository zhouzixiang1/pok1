"""Phase 2 (defect E (b)): FROZEN-SNAPSHOT ISOLATION for the slice2b consumer.

Verifies the fix that eliminates the consumer-primary checkpoint race: the
consumer gate chain (quality/review/critic/precommit) must read/write an
ISOLATED ``pipeline_state_consumer-<candidate_id>.json`` file seeded at seal
time, never the live primary ``pipeline_state.json``.  At promotion the
consumer slot is CAS-collapsed back onto the primary so ``commit_bot`` sees
the consumer's evidence without re-running gates.

Covers:

1. Consumer gate writes never touch ``pipeline_state.json`` (primary byte-
   unchanged while ``pipeline_state_consumer-<id>.json`` advances).
2. Promotion CAS-collapses the consumer slot onto the primary under the
   primary's CAS (gate_results + stage land on primary).
3. Boot recovery re-enters the override with the persisted consumer slot id
   (the slot file persists across a simulated restart).
4. The consumer slot's ``checkpoint_revision`` chain is independent of the
   primary's (they advance in lockstep only via the explicit collapse).

The autouse ``isolate_state`` fixture (conftest) redirects RESULTS_DIR /
BOTS_DIR to a tmp tree and materializes a parent bot, so these tests write
checkpoints against the synthetic authority without their own RESULTS_DIR
monkeypatch (which would shadow the fixture's parent-bot setup).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")

# Import orchestrator first so companion modules (which import
# ``orchestrator as _o``) initialize fully.
import orchestrator  # noqa: F401
import evolution_infra
from evolution_infra import (
    active_slot_override,
    no_slot_override,
    pipeline_state_path,
    read_pipeline_checkpoint,
    write_pipeline_checkpoint,
    clear_pipeline_checkpoint,
)

from producer_consumer_slice2b import (
    GATE_CHAIN_ORDER,
    Slice2bError,
)
from producer_consumer_slice2b_activation import (
    Slice2bActivation,
    build_snapshot_from_checkpoint,
)
from producer_consumer_workflow_store import ProducerConsumerWorkflowAdapter
from workflow_kernel import WorkflowStore


DIGESTS = {letter: letter * 64 for letter in "abcdef0123456789"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _results_dir() -> Path:
    """The autouse-isolated RESULTS_DIR (resolved at call time, not import)."""
    return Path(evolution_infra.RESULTS_DIR)


def _adapter(tmp_path: Path) -> ProducerConsumerWorkflowAdapter:
    return ProducerConsumerWorkflowAdapter(
        WorkflowStore(tmp_path / "slice2b.sqlite3")
    )


def _activation(tmp_path: Path) -> Slice2bActivation:
    """A fresh activation backed by a temp lifecycle sqlite file."""
    return Slice2bActivation(
        adapter=_adapter(tmp_path),
        lifecycle_db_path=tmp_path / "slice2b_lifecycle.sqlite3",
    )


def _seed_primary_checkpoint(
    *,
    next_v: int = 11,
    source_v: int = 10,
    stage: str = "workers_done",
) -> dict:
    """Write a primary checkpoint at workers_done and return its dict.

    ``next_v=11`` (parent published at v10) aligns with the synthetic
    authority fixture's published_high_water derivation.
    """
    artifact = DIGESTS["a"]
    manifest = DIGESTS["b"]
    charter = DIGESTS["c"]
    assert write_pipeline_checkpoint(
        next_v=next_v,
        source_v=source_v,
        stage=stage,
        candidate_artifact_hash=artifact,
        candidate_manifest_digest=manifest,
        charter_digest=charter,
        reviewer_feedback="",
    )
    return read_pipeline_checkpoint()


def _snapshot_for(checkpoint: dict) -> dict:
    return build_snapshot_from_checkpoint(
        checkpoint,
        artifact_hash=checkpoint.get("candidate_artifact_hash") or DIGESTS["a"],
        manifest_digest=checkpoint.get("candidate_manifest_digest") or DIGESTS["b"],
        charter_digest=checkpoint.get("charter_digest") or DIGESTS["c"],
    )


def _gate_factory_writes_gate_results(*, receipt_digest: str = DIGESTS["9"]):
    """A gate factory whose runners write a distinct gate_results entry each.

    Each runner calls ``write_pipeline_checkpoint`` with NO slot_id (exactly as
    the canonical handlers do via ``_record_gate``), advancing the stage to the
    gate's canonical post-stage so the checkpoint's stage-gate allowlist
    (``STAGE_GATE_ALLOWLIST``) retains each gate's evidence.  Under the consumer
    override these writes must land on the consumer slot file, proving the
    isolation funnel works end-to-end.
    """

    # Canonical post-stage per consumer gate (mirrors pipeline_state stages),
    # and the gate_results KEY each handler stores under (the canonical
    # _record_gate mapping: run_quality_gates -> "quality", etc.).
    gate_post_stage = {
        "run_quality_gates": "quality_passed",
        "run_review": "reviewed",
        "run_critic": "critic_checked",
        "run_precommit_eval": "verified",
    }
    gate_results_key = {
        "run_quality_gates": "quality",
        "run_review": "review",
        "run_critic": "critic",
        "run_precommit_eval": "precommit_eval",
    }

    def factory():
        runners = {}

        def make(gate_name):
            async def run(snapshot):
                next_v = int(snapshot.get("next_v") or 0)
                source_v = int(snapshot.get("source_v") or 0)
                # Mirror what _record_gate does: a no-slot write that carries a
                # gate_results entry.  Under active_slot_override this targets
                # the consumer slot; without it, it would race the primary.
                recorded = write_pipeline_checkpoint(
                    next_v,
                    source_v,
                    gate_post_stage.get(gate_name, "verified"),
                    gate_results={
                        gate_results_key.get(gate_name, gate_name): {
                            "digest": receipt_digest
                        }
                    },
                )
                assert recorded, f"consumer gate write refused at {gate_name}"
                return {
                    "outcome": "success",
                    "result_digest": receipt_digest,
                    "detail": {"gate": gate_name},
                }

            return run

        for gate_name in GATE_CHAIN_ORDER:
            if gate_name == "commit_bot":
                async def commit(snapshot):
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
# 1. Consumer gate writes never touch pipeline_state.json
# ---------------------------------------------------------------------------


def test_consumer_gate_writes_never_touch_primary(tmp_path):
    """Under the consumer override, every gate write hits the consumer file;
    the primary file is byte-identical before vs after the chain runs."""

    import orchestrator_deterministic_route as odr

    primary = _seed_primary_checkpoint()
    primary_path = pipeline_state_path()
    primary_bytes = primary_path.read_bytes()
    next_v = primary["next_v"]

    activation = _activation(tmp_path)
    # Build the snapshot from the written primary so digests line up, then
    # seal it (this submits the envelope so the dispatcher can lease it).
    snapshot = _snapshot_for(primary)
    candidate_id = snapshot["candidate_id"]
    consumer_slot = "consumer-" + candidate_id
    consumer_path = pipeline_state_path(consumer_slot)
    activation.seal_at_workers_done(
        snapshot=snapshot,
        run_id=snapshot["workflow_run_id"],
        job_id=f"job:{snapshot['draft_id']}:quality-static",
        idempotency_key=f"{snapshot['draft_id']}:quality-static:v1",
        artifact_digest=snapshot["artifact_hash"],
        resource_claim={
            "resource_class": "cpu",
            "cpu_slots": 1,
            "memory_mb": 512,
            "gpu_slots": 0,
            "match_slots": 0,
            "official_slots": 0,
        },
        retry_policy={
            "max_attempts": 3,
            "initial_backoff_sec": 1.0,
            "backoff_multiplier": 2.0,
            "max_backoff_sec": 10.0,
            "retryable_outcomes": ["infrastructure_failure"],
        },
        deadline={
            "submitted_at_epoch": 100.0,
            "not_before_epoch": 100.0,
            "expires_at_epoch": 1000.0,
        },
        evaluation_contract_digest=DIGESTS["1"],
        executor_digest=DIGESTS["2"],
        repository_digest=DIGESTS["5"],
        runtime_digest=DIGESTS["4"],
    )

    # Seed the consumer slot from the primary (mirror the seal-time helper).
    assert odr._slice2b_seed_consumer_checkpoint(primary, consumer_slot)
    assert consumer_path.exists()

    async def driver():
        task = activation.launch_consumer_task(
            candidate_id=candidate_id,
            gate_runner_factory=_gate_factory_writes_gate_results(),
            now=100.0,
            lease_seconds=300.0,
            consumer_slot_id=consumer_slot,
        )
        await task

    asyncio.run(driver())

    # Primary file is byte-unchanged: the consumer never touched it.
    assert primary_path.read_bytes() == primary_bytes

    # Consumer slot advanced: the gate chain progressed through every consumer
    # gate to the final ``verified`` stage, and its revision incremented (each
    # gate write bumped checkpoint_revision).  The primary's revision is
    # unchanged at the seed value.
    consumer = read_pipeline_checkpoint(slot_id=consumer_slot)
    assert consumer is not None
    assert consumer["stage"] == "verified"
    assert consumer["checkpoint_revision"] > primary["checkpoint_revision"]
    assert read_pipeline_checkpoint()["checkpoint_revision"] == primary[
        "checkpoint_revision"
    ]
    # The final gate's evidence survives the stage-gate allowlist at verified
    # (which retains precommit_eval).
    assert "precommit_eval" in consumer.get("gate_results", {})


# ---------------------------------------------------------------------------
# 2. Promotion CAS-collapses consumer slot to primary
# ---------------------------------------------------------------------------


def test_promotion_collapses_consumer_slot_to_primary(tmp_path, caplog):
    """After the consumer promotes, the collapse writes the consumer's
    gate_results + stage onto the PRIMARY under the primary's CAS."""

    import logging
    caplog.set_level(logging.WARNING, logger="pok.infra")
    import orchestrator_deterministic_route as odr

    primary = _seed_primary_checkpoint()
    next_v = primary["next_v"]
    source_v = primary["source_v"]
    expected_revision = primary["checkpoint_revision"]
    expected_stage = primary["stage"]
    expected_run_id = primary["workflow_run_id"]

    consumer_slot = "consumer-collapse-test"
    # Seed the consumer slot FROM the primary (production path: shares the
    # workflow_run_id), then advance it under the override to simulate the
    # gate chain progressing to verified with full evidence.
    assert odr._slice2b_seed_consumer_checkpoint(primary, consumer_slot)
    with active_slot_override(consumer_slot):
        assert write_pipeline_checkpoint(
            next_v,
            source_v,
            "verified",
            gate_results={
                "quality": {"digest": DIGESTS["1"]},
                "review": {"digest": DIGESTS["2"]},
                "critic": {"digest": DIGESTS["3"]},
                "precommit_eval": {"digest": DIGESTS["9"]},
            },
            reviewer_feedback="consumer reviewed",
        )
    consumer_before = read_pipeline_checkpoint(slot_id=consumer_slot)
    assert consumer_before is not None

    ok = odr._promote_consumer_slot_to_primary(
        consumer_slot,
        next_v,
        source_v,
        published_primary=primary,
    )
    if not ok:
        import sys as _sys
        for rec in caplog.records:
            print(f"CAPLOG {rec.levelname} {rec.getMessage()[:240]}", file=_sys.stderr)
    assert ok, "collapse CAS should succeed against the quiescent primary"

    # Primary now carries the consumer's evidence.
    collapsed = read_pipeline_checkpoint()
    assert collapsed is not None
    assert collapsed["stage"] == "verified"
    assert collapsed["reviewer_feedback"] == "consumer reviewed"
    gr = collapsed["gate_results"]
    assert gr.get("precommit_eval", {}).get("digest") == DIGESTS["9"]
    assert gr.get("quality", {}).get("digest") == DIGESTS["1"]
    # Revision advanced exactly once (primary was expected_revision).
    assert collapsed["checkpoint_revision"] == expected_revision + 1
    # The publication identity is the PRIMARY's (not the consumer's).
    assert collapsed["workflow_run_id"] == expected_run_id


def test_promotion_collapse_refuses_when_primary_moved(tmp_path):
    """If the primary advanced between park and collapse, the CAS refuses and
    the collapse returns False (non-fatal; canonical path re-runs gates)."""

    import orchestrator_deterministic_route as odr

    primary = _seed_primary_checkpoint()
    next_v = primary["next_v"]
    source_v = primary["source_v"]
    # Simulate the primary having moved: bump its revision by writing again.
    assert write_pipeline_checkpoint(
        next_v=next_v,
        source_v=source_v,
        stage="workers_done",
        touch_stage_timestamp=True,
    )
    moved = read_pipeline_checkpoint()
    assert moved["checkpoint_revision"] != primary["checkpoint_revision"]

    consumer_slot = "consumer-stale"
    assert odr._slice2b_seed_consumer_checkpoint(primary, consumer_slot)
    with active_slot_override(consumer_slot):
        assert write_pipeline_checkpoint(next_v, source_v, "verified")
    # Collapse targets the STALE primary snapshot -> CAS must refuse.
    ok = odr._promote_consumer_slot_to_primary(
        consumer_slot,
        next_v,
        source_v,
        published_primary=primary,  # stale expectations
    )
    assert ok is False
    # Primary untouched by the refused collapse.
    after = read_pipeline_checkpoint()
    assert after["stage"] == "workers_done"


# ---------------------------------------------------------------------------
# 3. Boot recovery re-enters override with persisted consumer slot
# ---------------------------------------------------------------------------


def test_boot_recovery_reuses_persisted_consumer_slot(tmp_path):
    """The consumer slot file persists across a simulated restart, and
    recover_at_boot threads the persisted consumer_checkpoint_slot id into
    the scheduled factory so the resumed gate chain re-binds the override."""

    import orchestrator_deterministic_route as odr

    primary = _seed_primary_checkpoint()
    snapshot = _snapshot_for(primary)
    candidate_id = snapshot["candidate_id"]
    consumer_slot = "consumer-" + candidate_id

    # Simulate seal-time state: seed the slot + persist the slot id.
    assert odr._slice2b_seed_consumer_checkpoint(primary, consumer_slot)
    activation = _activation(tmp_path)
    activation._sealed_snapshots[candidate_id] = dict(snapshot)
    activation._dispatch_clocks[candidate_id] = 100.0
    activation.ledger.start(
        candidate_id=candidate_id,
        sealed_artifact_hash=snapshot["artifact_hash"],
        envelope_effect_id="effect-1",
        envelope_digest=snapshot["snapshot_digest"],
        sealed_snapshot=dict(snapshot),
    )
    activation.ledger.set_consumer_checkpoint_slot(
        candidate_id=candidate_id,
        consumer_checkpoint_slot=consumer_slot,
    )
    # The slot file exists on disk (persisted).
    assert pipeline_state_path(consumer_slot).exists()

    # Simulate a restart: drop in-memory registries, then recover.
    fresh = _activation(tmp_path)
    recovered = fresh.recover_at_boot()
    assert any(
        r["candidate_id"] == candidate_id for r in recovered["rescheduled"]
    )
    # The scheduled factory carries the persisted consumer slot id.
    scheduled = fresh._scheduled_factories[candidate_id]
    assert len(scheduled) == 4
    _, _, _, threaded_slot = scheduled
    assert threaded_slot == consumer_slot
    # The persisted slot id is recoverable from the lifecycle.
    assert (
        fresh.ledger.consumer_checkpoint_slot(candidate_id) == consumer_slot
    )


def test_boot_recovery_reseeds_missing_consumer_slot(tmp_path):
    """If the consumer slot file was wiped (operator reset), recover_at_boot
    re-seeds it from the primary checkpoint so the resumed chain has a start."""

    import orchestrator_deterministic_route as odr

    primary = _seed_primary_checkpoint()
    snapshot = _snapshot_for(primary)
    candidate_id = snapshot["candidate_id"]
    consumer_slot = "consumer-" + candidate_id

    activation = _activation(tmp_path)
    activation._sealed_snapshots[candidate_id] = dict(snapshot)
    activation._dispatch_clocks[candidate_id] = 100.0
    activation.ledger.start(
        candidate_id=candidate_id,
        sealed_artifact_hash=snapshot["artifact_hash"],
        envelope_effect_id="effect-1",
        envelope_digest=snapshot["snapshot_digest"],
        sealed_snapshot=dict(snapshot),
    )
    activation.ledger.set_consumer_checkpoint_slot(
        candidate_id=candidate_id,
        consumer_checkpoint_slot=consumer_slot,
    )
    # Slot file is GONE (simulated wipe).  Primary still at workers_done.
    slot_path = pipeline_state_path(consumer_slot)
    assert not slot_path.exists()

    fresh = _activation(tmp_path)
    fresh.recover_at_boot()
    # Recovery re-seeded the slot from the primary checkpoint.
    assert slot_path.exists()
    reseeded = read_pipeline_checkpoint(slot_id=consumer_slot)
    assert reseeded is not None
    assert reseeded["next_v"] == primary["next_v"]


# ---------------------------------------------------------------------------
# 4. Consumer slot revision chain is independent of primary
# ---------------------------------------------------------------------------


def test_consumer_slot_revision_independent_of_primary(tmp_path):
    """The consumer slot's checkpoint_revision advances independently; a
    primary write does not move the consumer's revision and vice versa."""

    primary = _seed_primary_checkpoint()
    next_v = primary["next_v"]
    source_v = primary["source_v"]
    primary_rev_0 = primary["checkpoint_revision"]

    consumer_slot = "consumer-rev-test"
    assert write_pipeline_checkpoint(
        next_v=next_v,
        source_v=source_v,
        stage="workers_done",
        slot_id=consumer_slot,
    )
    consumer_0 = read_pipeline_checkpoint(slot_id=consumer_slot)
    assert consumer_0 is not None
    consumer_rev_0 = consumer_0["checkpoint_revision"]

    # Advance the consumer slot under the override (simulates a gate write).
    with active_slot_override(consumer_slot):
        assert write_pipeline_checkpoint(
            next_v,
            source_v,
            "quality_passed",
            gate_results={"quality": {"digest": DIGESTS["1"]}},
        )
    # Primary revision unchanged.
    primary_after = read_pipeline_checkpoint()
    assert primary_after["checkpoint_revision"] == primary_rev_0
    assert primary_after["stage"] == "workers_done"
    # Consumer revision advanced.
    consumer_after = read_pipeline_checkpoint(slot_id=consumer_slot)
    assert consumer_after["checkpoint_revision"] == consumer_rev_0 + 1
    assert consumer_after["stage"] == "quality_passed"

    # Now advance the primary (e.g. an operator touch); consumer is unaffected.
    assert write_pipeline_checkpoint(
        next_v,
        source_v,
        "workers_done",
        touch_stage_timestamp=True,
    )
    primary_touch = read_pipeline_checkpoint()
    consumer_touch = read_pipeline_checkpoint(slot_id=consumer_slot)
    assert primary_touch["checkpoint_revision"] == primary_rev_0 + 1
    assert consumer_touch["checkpoint_revision"] == consumer_rev_0 + 1


# ---------------------------------------------------------------------------
# Consumer slot id convention + is_draft_slot non-collision
# ---------------------------------------------------------------------------


def test_consumer_slot_id_is_not_a_draft_slot():
    """The consumer-<id> convention must NOT match is_draft_slot, so the slot
    is treated as a live allocation (floor+1 CAS satisfied naturally)."""
    import evolution_infra as ei

    slot = "consumer-candidate-v143"
    assert ei.is_draft_slot(slot) is False
    assert ei.is_draft_slot(None) is False


def test_consumer_slot_id_convention():
    import orchestrator_deterministic_route as odr

    assert (
        odr._slice2b_consumer_slot_id("candidate-v143")
        == "consumer-candidate-v143"
    )


# ---------------------------------------------------------------------------
# Re-seed idempotency guard (defect: seal seam re-fired on every route hit
# while primary parked at workers_done, overwriting consumer gate progress)
# ---------------------------------------------------------------------------


def test_seed_consumer_checkpoint_is_idempotent_and_preserves_gate_progress():
    """Re-entering _slice2b_seed_consumer_checkpoint must NOT overwrite an
    existing consumer slot that already has gate progress.

    Root cause fixed: the seal seam (_slice2b_seal_at_workers_done) fires on
    every orchestrator route hit while the primary stays parked at
    workers_done. Without the guard, the re-seed overwrites the consumer slot
    with the primary's gate_results=[] / workers_done state, destroying the
    consumer's quality/review/critic results and spinning checkpoint_revision
    in a tight loop. The guard makes seeding first-time-only.
    """
    import orchestrator_deterministic_route as odr
    from evolution_infra import (
        write_pipeline_checkpoint,
        read_pipeline_checkpoint,
    )

    next_v, source_v = 144, 143
    consumer_slot = odr._slice2b_consumer_slot_id(f"candidate-v{next_v}")

    # Primary checkpoint the seal sees (workers_done, no gate_results).
    primary_ckpt = {
        "next_v": next_v,
        "source_v": source_v,
        "stage": "workers_done",
        "master_plan": {"analysis": "seeded"},
        "gate_results": {},
        "checkpoint_revision": 1,
    }

    # First seed: materialises the consumer slot.
    assert odr._slice2b_seed_consumer_checkpoint(primary_ckpt, consumer_slot) is not False
    seeded = read_pipeline_checkpoint(slot_id=consumer_slot)
    assert seeded is not None
    assert seeded["next_v"] == next_v

    # Simulate the consumer gate chain advancing the slot (quality passed).
    write_pipeline_checkpoint(
        next_v,
        source_v,
        "quality_passed",
        slot_id=consumer_slot,
        master_plan=primary_ckpt["master_plan"],
        gate_results={"quality": {"passed": True}},
        expected_checkpoint_stage="workers_done",
        expected_workflow_run_id=seeded.get("workflow_run_id"),
    )
    advanced = read_pipeline_checkpoint(slot_id=consumer_slot)
    assert advanced["stage"] == "quality_passed"
    assert advanced["gate_results"] == {"quality": {"passed": True}}
    consumer_rev_after_gate = advanced["checkpoint_revision"]

    # Re-enter the seal seam (primary still workers_done, route hit again).
    # The guard must detect the existing consumer slot and NOT overwrite it.
    odr._slice2b_seed_consumer_checkpoint(primary_ckpt, consumer_slot)

    preserved = read_pipeline_checkpoint(slot_id=consumer_slot)
    # Gate progress preserved (NOT reset to workers_done / empty gate_results).
    assert preserved["stage"] == "quality_passed", (
        f"re-seed destroyed consumer progress: stage={preserved['stage']!r}"
    )
    assert preserved["gate_results"] == {"quality": {"passed": True}}
    # Revision did not reset (the re-seed did not overwrite).
    assert preserved["checkpoint_revision"] == consumer_rev_after_gate
