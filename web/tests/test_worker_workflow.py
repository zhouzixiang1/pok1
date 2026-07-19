import hashlib
import json
from pathlib import Path
import shutil

import pytest

from bot_artifact import hash_path
from worker_workflow import (
    WORKER_WORKFLOW_DEFINITION_VERSION,
    WorkerArtifactStore,
    WorkerWorkflow,
    build_worker_envelope,
    initial_worker_state,
    next_worker_command,
    reduce_worker_event,
    replay_worker,
    replay_worker_events,
    validate_worker_envelope,
)
from workflow_kernel import WorkflowEvent, WorkflowStore, content_digest, reduce_events


def _checkpoint():
    return {
        "run_id": "149#0",
        "next_v": 149,
        "source_v": 122,
        "generation_attempt": 0,
        "stage": "master_planned",
    }


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _envelope(snapshot_hash):
    return build_worker_envelope(
        checkpoint=_checkpoint(),
        kind="quality_repair",
        source_stage="quality_failed",
        prepared_artifact_hash=snapshot_hash,
        prepared_snapshot_hash=snapshot_hash,
        source_artifact_hash=_sha("source"),
        tasks=[{
            "worker_id": "repair",
            "role": "Algorithmic Logic Architect",
            "target_files": ["policy.py"],
            "worker_prompt": "repair the frozen blocker",
        }],
        reviewer_feedback="Quality failed: compile(policy.py)",
        worker_template_hash=_sha("template"),
        work_item={"kind": "quality_repair"},
        backend_contract={"model": "weak-model"},
        precommit_rework_count=0,
        official_rework_count=0,
    )


def _workflow(tmp_path):
    store = WorkflowStore(tmp_path / "events.sqlite3")
    store.ensure_instance(
        "149#0", definition_version=WORKER_WORKFLOW_DEFINITION_VERSION
    )
    artifacts = WorkerArtifactStore(tmp_path / "artifacts")
    return WorkerWorkflow(store=store, artifacts=artifacts, run_id="149#0")


def test_envelope_is_canonical_and_tamper_evident():
    envelope = _envelope(_sha("candidate"))
    assert validate_worker_envelope(envelope) == []
    assert envelope["envelope_digest"] == content_digest({
        key: value for key, value in envelope.items() if key != "envelope_digest"
    })
    assert envelope["projection_preimage_artifact_hash"] == _sha("candidate")
    assert envelope["projection_preimage_snapshot_hash"] == _sha("candidate")

    tampered = json.loads(json.dumps(envelope))
    tampered["tasks"][0]["worker_prompt"] = "different task"
    assert "worker_envelope_digest_mismatch" in validate_worker_envelope(tampered)

    poisoned_prompt_context = json.loads(json.dumps(envelope))
    poisoned_prompt_context["worker_execution_context"] = {
        "recent_failures": [{"error": "legacy mutable failure"}],
    }
    poisoned_prompt_context["envelope_digest"] = content_digest({
        key: value
        for key, value in poisoned_prompt_context.items()
        if key != "envelope_digest"
    })
    assert (
        "worker_envelope_legacy_prompt_context_forbidden"
        in validate_worker_envelope(poisoned_prompt_context)
    )

    legacy = json.loads(json.dumps(envelope))
    legacy.pop("projection_preimage_snapshot_hash")
    assert (
        "worker_envelope_projection_preimage_snapshot_hash_missing"
        in validate_worker_envelope(legacy)
    )

    mismatched = json.loads(json.dumps(envelope))
    mismatched["projection_preimage_snapshot_hash"] = _sha("wrong snapshot")
    mismatched["envelope_digest"] = content_digest({
        key: value
        for key, value in mismatched.items()
        if key != "envelope_digest"
    })
    assert (
        "worker_envelope_projection_preimage_snapshot_mismatch"
        in validate_worker_envelope(mismatched)
    )


def test_artifact_store_captures_identity_and_materializes_exact_tree(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "policy.py").write_text("value = 1\n")
    (candidate / "tables").mkdir()
    (candidate / "tables" / "equity.bin").write_bytes(b"\x00\x01")
    (candidate / "__pycache__").mkdir()
    (candidate / "__pycache__" / "ignored.pyc").write_bytes(b"cache")
    artifacts = WorkerArtifactStore(tmp_path / "artifacts")

    digest = artifacts.capture(candidate)
    assert artifacts.capture(candidate) == digest

    (candidate / "policy.py").write_text("poisoned = True\n")
    (candidate / "partial.bin").write_bytes(b"partial")
    poisoned = hash_path(candidate)
    artifacts.materialize(
        digest,
        candidate,
        expected_destination_digest=poisoned,
    )

    assert (candidate / "policy.py").read_text() == "value = 1\n"
    assert (candidate / "tables" / "equity.bin").read_bytes() == b"\x00\x01"
    assert not (candidate / "partial.bin").exists()
    assert not (candidate / "__pycache__").exists()


def test_artifact_store_rejects_symlink(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "policy.py").write_text("value = 1\n")
    (candidate / "escape").symlink_to(tmp_path / "outside")
    artifacts = WorkerArtifactStore(tmp_path / "artifacts")

    with pytest.raises(Exception, match="symbolic links are forbidden"):
        artifacts.capture(candidate)


def test_v149_history_can_only_retry_worker_not_prepare_again():
    envelope = _envelope(_sha("candidate"))
    effect_id = "worker:149#0:deadbeef"
    events = [
        WorkflowEvent(
            run_id="149#0",
            seq=1,
            event_type="WorkerPrepared",
            schema_version=1,
            payload={"envelope": envelope, "effect_id": effect_id, "max_attempts": 3},
            payload_digest=_sha("one"),
            causation_id="prepared",
        ),
        WorkflowEvent(
            run_id="149#0",
            seq=2,
            event_type="EffectRequested",
            schema_version=1,
            payload={"effect_id": effect_id, "kind": "worker_llm", "max_attempts": 3},
            payload_digest=_sha("two"),
            causation_id="requested",
        ),
        WorkflowEvent(
            run_id="149#0",
            seq=3,
            event_type="EffectFailed",
            schema_version=1,
            payload={
                "effect_id": effect_id,
                "attempt": 1,
                "retryable": True,
                "error": "timeout",
            },
            payload_digest=_sha("three"),
            causation_id="failed",
        ),
    ]

    state = reduce_events(initial_worker_state("149#0"), events, reduce_worker_event)

    assert state["repair_prepared_count"] == 1
    assert state["status"] == "retry_wait"
    assert next_worker_command(state) == {
        "command": "claim_worker",
        "effect_id": effect_id,
        "attempt": 2,
    }


def test_infrastructure_retry_preserves_one_envelope_and_attempt_budget(tmp_path):
    workflow = _workflow(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "policy.py").write_text("value = 1\n")
    snapshot = workflow.artifacts.capture(candidate)
    envelope = _envelope(snapshot)

    prepared = workflow.prepare(envelope)
    first = workflow.request_or_claim(owner="worker-a", lease_seconds=1)
    failed = workflow.infrastructure_failed(first, ["timeout"])
    second = workflow.request_or_claim(owner="worker-b", lease_seconds=1)

    assert prepared["repair_prepared_count"] == 1
    assert failed["status"] == "retry_wait"
    assert second.attempt == 2
    assert workflow.state()["envelope"] == envelope
    assert workflow.state()["repair_prepared_count"] == 1


def test_availability_deferral_is_replayable_and_attempt_neutral(tmp_path):
    workflow = _workflow(tmp_path)
    candidate = tmp_path / "candidate-availability"
    candidate.mkdir()
    (candidate / "policy.py").write_text("value = 1\n")
    snapshot = workflow.artifacts.capture(candidate)
    workflow.prepare(_envelope(snapshot), max_attempts=3)
    first = workflow.request_or_claim(owner="worker-a", lease_seconds=60)
    availability = {
        "schema_version": 1,
        "active": True,
        "category": "billing_cycle_usage_limit",
        "summary": "provider billing-cycle usage limit reached",
        "evidence_digest": "b" * 64,
    }

    deferred = workflow.availability_deferred(first, availability)

    assert deferred["status"] == "availability_deferred"
    assert deferred["attempt"] == 0
    assert deferred["failure_class"] == "availability"
    assert deferred["availability"] == availability
    assert next_worker_command(deferred) == {
        "command": "wait_for_llm_availability",
        "effect_id": deferred["effect_id"],
        "attempt": 0,
        "availability": availability,
    }
    assert workflow.state() == deferred
    assert "EffectFailed" not in [
        event.event_type for event in workflow.store.events(workflow.run_id)
    ]

    resumed = workflow.resume_availability_deferred()
    assert resumed["status"] == "requested"
    second = workflow.request_or_claim(owner="worker-b", lease_seconds=60)
    assert second.attempt == 1
    assert second.lease_epoch == first.lease_epoch + 1


def test_operator_shutdown_interruption_replays_and_reclaims_after_restart(
    tmp_path,
):
    workflow = _workflow(tmp_path)
    candidate = tmp_path / "candidate-shutdown"
    candidate.mkdir()
    (candidate / "policy.py").write_text("value = 1\n")
    snapshot = workflow.artifacts.capture(candidate)
    workflow.prepare(_envelope(snapshot), max_attempts=3)
    first = workflow.request_or_claim(owner="pid:101", lease_seconds=3600)

    interrupted = workflow.operator_shutdown_interrupted(
        first,
        owner="pid:101",
    )

    assert interrupted["status"] == "shutdown_interrupted"
    assert interrupted["attempt"] == 0
    assert interrupted["failure_class"] == ""
    assert interrupted["interruption"] == {
        "kind": "operator_shutdown",
        "reason": "operator_shutdown",
        "lease_epoch": first.lease_epoch,
        "lease_owner": "pid:101",
        "metadata": {
            "workflow_run_id": workflow.run_id,
            "shutdown_requested": True,
        },
    }
    assert next_worker_command(interrupted) == {
        "command": "claim_worker",
        "effect_id": interrupted["effect_id"],
        "attempt": 1,
    }
    assert "EffectFailed" not in [
        event.event_type for event in workflow.store.events(workflow.run_id)
    ]

    restarted = WorkerWorkflow(
        store=WorkflowStore(workflow.store.path),
        artifacts=WorkerArtifactStore(workflow.artifacts.root),
        run_id=workflow.run_id,
    )
    replayed = restarted.state()
    assert replayed["status"] == interrupted["status"]
    assert replayed["attempt"] == interrupted["attempt"]
    assert replayed["effect_id"] == interrupted["effect_id"]
    assert replayed["interruption"] == interrupted["interruption"]
    second = restarted.request_or_claim(
        owner="pid:202",
        lease_seconds=3600,
    )
    assert second.attempt == first.attempt
    assert second.lease_epoch == first.lease_epoch + 1


def test_expired_crash_claim_then_shutdown_replays_without_attempt_rewind(
    tmp_path,
):
    workflow = _workflow(tmp_path)
    candidate = tmp_path / "candidate-crash-shutdown"
    candidate.mkdir()
    (candidate / "policy.py").write_text("value = 1\n")
    snapshot = workflow.artifacts.capture(candidate)
    workflow.prepare(_envelope(snapshot), max_attempts=3)
    crashed = workflow.request_or_claim(owner="pid:crashed", lease_seconds=1)
    replacement = workflow.store.claim_effect(
        crashed.effect_id,
        owner="pid:replacement",
        lease_seconds=3600,
        now=crashed.lease_until + 1,
    )
    assert replacement.attempt == 2
    assert replacement.lease_epoch == 2

    interrupted = workflow.operator_shutdown_interrupted(
        replacement,
        owner="pid:replacement",
    )
    replayed = workflow.state()

    assert interrupted["attempt"] == 1
    assert replayed["status"] == "shutdown_interrupted"
    assert replayed["attempt"] == 1
    assert replayed["interruption"]["lease_epoch"] == 2
    assert not any(
        event.event_type == "EffectFailed"
        for event in workflow.store.events(workflow.run_id)
    )

    reopened = WorkerWorkflow(
        store=WorkflowStore(workflow.store.path),
        artifacts=WorkerArtifactStore(workflow.artifacts.root),
        run_id=workflow.run_id,
    )
    third = reopened.request_or_claim(
        owner="pid:restart",
        lease_seconds=3600,
    )
    assert third.attempt == replacement.attempt
    assert third.lease_epoch == replacement.lease_epoch + 1


def test_worker_replay_rejects_forged_operator_shutdown_receipt():
    effect_id = "worker:149#0:deadbeef"
    event = WorkflowEvent(
        run_id="149#0",
        seq=1,
        event_type="EffectInterrupted",
        schema_version=1,
        payload={
            "effect_id": effect_id,
            "claimed_attempt": 1,
            "restored_attempt": 0,
            "lease_epoch": 1,
            "lease_owner": "pid:101",
            "interruption_kind": "operator_shutdown",
            "reason": "operator_shutdown",
            "metadata": {
                "workflow_run_id": "foreign-run",
                "shutdown_requested": True,
            },
        },
        payload_digest=_sha("forged"),
        causation_id="forged-shutdown",
    )

    with pytest.raises(
        RuntimeError,
        match="invalid EffectInterrupted operator shutdown receipt",
    ):
        replay_worker_events("149#0", [event])


def test_worker_replay_rejects_boolean_restored_attempt():
    effect_id = "worker:149#0:deadbeef"
    event = WorkflowEvent(
        run_id="149#0",
        seq=1,
        event_type="EffectInterrupted",
        schema_version=1,
        payload={
            "effect_id": effect_id,
            "claimed_attempt": 2,
            "restored_attempt": True,
            "lease_epoch": 1,
            "lease_owner": "pid:101",
            "interruption_kind": "operator_shutdown",
            "reason": "operator_shutdown",
            "metadata": {
                "workflow_run_id": "149#0",
                "shutdown_requested": True,
            },
        },
        payload_digest=_sha("forged-bool"),
        causation_id="forged-shutdown-bool",
    )

    with pytest.raises(
        RuntimeError,
        match="invalid EffectInterrupted operator shutdown receipt",
    ):
        replay_worker_events("149#0", [event])


def test_semantic_failure_waits_for_projection_before_new_cycle(tmp_path):
    workflow = _workflow(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "policy.py").write_text("value = 1\n")
    snapshot = workflow.artifacts.capture(candidate)
    workflow.prepare(_envelope(snapshot))
    first = workflow.request_or_claim(owner="worker-a", lease_seconds=1)

    state = workflow.semantic_failed(
        first,
        {"compile": "failed"},
        projection={"schema_version": 1, "stage": "repair_planned"},
    )

    assert state["semantic_attempt"] == 1
    assert state["status"] == "semantic_ready"
    assert state["effect_id"] != first.effect_id
    assert next_worker_command(state)["command"] == "project_failure"

    projected = workflow.failure_projected()
    assert projected["status"] == "completed"
    opened = workflow.open_cycle("new-repair-receipt")
    assert opened["status"] == "idle"


def test_output_receipt_precedes_projection_and_is_replayable(tmp_path):
    workflow = _workflow(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "policy.py").write_text("value = 1\n")
    baseline = workflow.artifacts.capture(candidate)
    workflow.prepare(_envelope(baseline))
    lease = workflow.request_or_claim(owner="worker-a", lease_seconds=1)
    (candidate / "policy.py").write_text("value = 2\n")
    output = workflow.artifacts.capture(candidate)

    ready = workflow.output_ready(
        lease,
        artifact_hash=output,
        snapshot_hash=output,
        projection={"schema_version": 1},
    )
    replayed = replay_worker(workflow.store, workflow.run_id)

    assert ready["status"] == "output_ready"
    assert replayed == ready
    assert next_worker_command(ready) == {
        "command": "project_output",
        "artifact_hash": output,
    }

    projected = workflow.projected()
    assert projected["status"] == "completed"
    assert next_worker_command(projected) == {"command": "none"}


def test_abandoned_worker_requires_outer_checkpoint_reconciliation(tmp_path):
    workflow = _workflow(tmp_path)
    candidate = tmp_path / "candidate-abandoned"
    candidate.mkdir()
    (candidate / "policy.py").write_text("value = 1\n")
    snapshot = workflow.artifacts.capture(candidate)
    workflow.prepare(_envelope(snapshot))

    abandoned = workflow.abandon("worker_infrastructure_exhausted")
    assert workflow.abandon("worker_infrastructure_exhausted") == abandoned
    with pytest.raises(
        RuntimeError,
        match="worker_abandon_fence_identity_invalid",
    ):
        workflow.abandon("different_outer_reason")

    assert abandoned["status"] == "abandoned"
    assert abandoned["abandon_reason"] == "worker_infrastructure_exhausted"
    assert next_worker_command(abandoned) == {
        "command": "reconcile_abandon",
        "reason": "worker_infrastructure_exhausted",
    }


@pytest.mark.parametrize("outcome", ["output", "semantic"])
def test_invalid_projection_schema_is_rejected_before_effect_completion(
    tmp_path, outcome
):
    workflow = _workflow(tmp_path)
    candidate = tmp_path / "candidate-invalid-projection"
    candidate.mkdir()
    (candidate / "policy.py").write_text("value = 1\n")
    snapshot = workflow.artifacts.capture(candidate)
    workflow.prepare(_envelope(snapshot))
    lease = workflow.request_or_claim(owner="worker-a", lease_seconds=10)
    event_count = len(workflow.store.events(workflow.run_id))

    with pytest.raises(ValueError, match="schema_version=1"):
        if outcome == "output":
            workflow.output_ready(
                lease,
                artifact_hash=snapshot,
                snapshot_hash=snapshot,
                projection={},
            )
        else:
            workflow.semantic_failed(
                lease,
                {"compile": "failed"},
                projection={},
            )

    assert len(workflow.store.events(workflow.run_id)) == event_count
    effect = workflow.store.effect(lease.effect_id)
    assert effect["status"] == "running"
    assert effect["lease_epoch"] == lease.lease_epoch


def test_prepare_is_idempotent_but_rejects_different_envelope(tmp_path):
    workflow = _workflow(tmp_path)
    envelope = _envelope(_sha("candidate"))
    first = workflow.prepare(envelope)
    second = workflow.prepare(envelope)
    assert first == second

    changed = json.loads(json.dumps(envelope))
    changed["prepared_artifact_hash"] = _sha("different")
    changed["envelope_digest"] = content_digest({
        key: value for key, value in changed.items() if key != "envelope_digest"
    })
    with pytest.raises(RuntimeError, match="differs from prepared input"):
        workflow.prepare(changed)


def test_materialization_journal_recovers_before_atomic_swap(
    tmp_path, monkeypatch
):
    artifacts = WorkerArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "source"
    source.mkdir()
    (source / "policy.py").write_text("value = 2\n")
    digest = artifacts.capture(source)
    destination = tmp_path / "candidate"
    destination.mkdir()
    (destination / "policy.py").write_text("value = 1\n")
    expected = hash_path(destination)
    original = artifacts._complete_materialization
    failed = False

    def crash_at_boundary(**kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("simulated process death")
        return original(**kwargs)

    monkeypatch.setattr(artifacts, "_complete_materialization", crash_at_boundary)
    with pytest.raises(RuntimeError, match="simulated process death"):
        artifacts.materialize(
            digest,
            destination,
            expected_destination_digest=expected,
        )
    monkeypatch.setattr(artifacts, "_complete_materialization", original)

    artifacts.materialize(
        digest,
        destination,
        expected_destination_digest=expected,
    )
    assert (destination / "policy.py").read_text() == "value = 2\n"


def test_materialization_recovers_after_journal_delete_failure(
    tmp_path, monkeypatch
):
    artifacts = WorkerArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "source"
    source.mkdir()
    (source / "policy.py").write_text("value = 2\n")
    digest = artifacts.capture(source)
    destination = tmp_path / "candidate"
    destination.mkdir()
    (destination / "policy.py").write_text("value = 1\n")
    expected = hash_path(destination)
    original = artifacts._remove_journal
    failed = False

    def fail_once(journal):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("crash before journal unlink")
        return original(journal)

    monkeypatch.setattr(artifacts, "_remove_journal", fail_once)
    with pytest.raises(RuntimeError, match="crash before journal unlink"):
        artifacts.materialize(
            digest,
            destination,
            expected_destination_digest=expected,
        )
    monkeypatch.setattr(artifacts, "_remove_journal", original)
    artifacts.materialize(
        digest,
        destination,
        expected_destination_digest=expected,
    )
    assert (destination / "policy.py").read_text() == "value = 2\n"


def test_materialization_recovery_never_overwrites_rebuilt_destination(
    tmp_path, monkeypatch
):
    artifacts = WorkerArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "source"
    source.mkdir()
    (source / "policy.py").write_text("value = 'output'\n")
    digest = artifacts.capture(source)
    destination = tmp_path / "candidate"
    destination.mkdir()
    (destination / "policy.py").write_text("value = 'preimage'\n")
    preimage = hash_path(destination)
    original = artifacts._complete_materialization

    monkeypatch.setattr(
        artifacts,
        "_complete_materialization",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("process death")),
    )
    with pytest.raises(RuntimeError, match="process death"):
        artifacts.materialize(
            digest,
            destination,
            expected_destination_digest=preimage,
        )
    monkeypatch.setattr(artifacts, "_complete_materialization", original)

    shutil.rmtree(destination)
    destination.mkdir()
    (destination / "policy.py").write_text("value = 'third-party'\n")
    third_party = hash_path(destination)
    with pytest.raises(RuntimeError, match="destination CAS mismatch"):
        artifacts.materialize(
            digest,
            destination,
            expected_destination_digest=preimage,
        )
    assert hash_path(destination) == third_party
    assert (destination / "policy.py").read_text() == "value = 'third-party'\n"


def test_materialization_atomically_retires_preimage_and_reports_ownership(tmp_path):
    artifacts = WorkerArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "source"
    source.mkdir()
    (source / "policy.py").write_text("value = 'output'\n")
    digest = artifacts.capture(source)
    destination = tmp_path / "candidate"
    destination.mkdir()
    (destination / "policy.py").write_text("value = 'preimage'\n")
    preimage = hash_path(destination)

    receipt = artifacts.materialize(
        digest,
        destination,
        expected_destination_digest=preimage,
    )

    assert receipt.installed is True
    assert receipt.receipt_digest
    retained = Path(receipt.retained_path)
    assert retained.is_dir()
    assert hash_path(retained) == preimage
    assert (retained / "policy.py").read_text() == "value = 'preimage'\n"
    assert not list((artifacts.root / ".materializations").glob("*.json"))


def test_materialization_preexisting_output_is_durable_noop(tmp_path):
    artifacts = WorkerArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "source"
    source.mkdir()
    (source / "policy.py").write_text("value = 'output'\n")
    digest = artifacts.capture(source)
    destination = tmp_path / "candidate"
    shutil.copytree(source, destination)

    receipt = artifacts.materialize(
        digest,
        destination,
        expected_destination_digest=_sha("superseded-preimage"),
    )

    assert receipt.installed is False
    assert (destination / "policy.py").read_text() == "value = 'output'\n"
    assert Path(receipt.retained_path).is_dir()


def test_materialization_mismatch_preserves_concurrent_displaced_tree(
    tmp_path, monkeypatch
):
    artifacts = WorkerArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "source"
    source.mkdir()
    (source / "policy.py").write_text("value = 'output'\n")
    digest = artifacts.capture(source)
    destination = tmp_path / "candidate"
    destination.mkdir()
    (destination / "policy.py").write_text("value = 'preimage'\n")
    preimage = hash_path(destination)
    original_move = artifacts._move_to_retained

    def replace_displaced_before_retire(prepared, retained):
        shutil.rmtree(prepared)
        prepared.mkdir()
        (prepared / "operator.dat").write_text("must survive\n")
        return original_move(prepared, retained)

    monkeypatch.setattr(
        artifacts,
        "_move_to_retained",
        replace_displaced_before_retire,
    )
    with pytest.raises(RuntimeError, match="changed during atomic CAS"):
        artifacts.materialize(
            digest,
            destination,
            expected_destination_digest=preimage,
        )

    retained = list((artifacts.root / ".retained_projections").iterdir())
    assert len(retained) == 1
    assert (retained[0] / "operator.dat").read_text() == "must survive\n"
    assert (destination / "policy.py").read_text() == "value = 'output'\n"
    assert list((artifacts.root / ".materializations").glob("*.json"))


def test_remove_if_matches_retires_tree_without_recursive_delete(tmp_path, monkeypatch):
    artifacts = WorkerArtifactStore(tmp_path / "artifacts")
    destination = tmp_path / "candidate"
    destination.mkdir()
    (destination / "policy.py").write_text("value = 1\n")
    digest = hash_path(destination)

    monkeypatch.setattr(
        "worker_workflow.shutil.rmtree",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("projection retirement must not recursively delete")
        ),
    )
    receipt = artifacts.remove_if_matches(destination, digest)

    assert receipt.installed is True
    assert not destination.exists()
    retained = Path(receipt.retained_path)
    assert retained.is_dir()
    assert (retained / "policy.py").read_text() == "value = 1\n"


def test_abandon_is_absorbing_and_rejects_late_worker_output(tmp_path):
    workflow = _workflow(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "policy.py").write_text("value = 1\n")
    snapshot = workflow.artifacts.capture(candidate)
    workflow.prepare(_envelope(snapshot))
    lease = workflow.request_or_claim(owner="worker", lease_seconds=10)
    abandoned = workflow.abandon("operator")

    assert abandoned["status"] == "abandoned"
    with pytest.raises(RuntimeError, match="lost its fenced lease"):
        workflow.output_ready(
            lease,
            artifact_hash=snapshot,
            snapshot_hash=snapshot,
            projection={"schema_version": 1},
        )
    assert workflow.state()["status"] == "abandoned"


def test_same_envelope_and_artifact_can_run_in_distinct_cycles(tmp_path):
    workflow = _workflow(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "policy.py").write_text("value = 1\n")
    snapshot = workflow.artifacts.capture(candidate)
    envelope = _envelope(snapshot)

    workflow.prepare(envelope)
    first = workflow.request_or_claim(owner="one", lease_seconds=10)
    workflow.output_ready(
        first,
        artifact_hash=snapshot,
        snapshot_hash=snapshot,
        projection={"schema_version": 1},
    )
    workflow.projected()
    workflow.open_cycle("new-checkpoint-receipt")
    workflow.prepare(envelope)
    second = workflow.request_or_claim(owner="two", lease_seconds=10)
    workflow.output_ready(
        second,
        artifact_hash=snapshot,
        snapshot_hash=snapshot,
        projection={"schema_version": 1},
    )
    final = workflow.projected()

    assert final["cycle"] == 1
    assert final["status"] == "completed"
    assert [
        event.event_type for event in workflow.store.events(workflow.run_id)
    ].count("WorkerProjected") == 2


def test_unknown_worker_history_event_fails_closed(tmp_path):
    workflow = _workflow(tmp_path)
    workflow.store.append_event(
        workflow.run_id,
        "FutureWorkerEvent",
        {},
        causation_id="future",
    )
    with pytest.raises(RuntimeError, match="unsupported Worker history event"):
        workflow.state()
