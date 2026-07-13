import hashlib
import json

import pytest

from worker_workflow import (
    WorkerArtifactStore,
    WorkerWorkflow,
    build_worker_envelope,
    initial_worker_state,
    next_worker_command,
    reduce_worker_event,
    replay_worker,
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
            "target_files": ["strategy.py"],
            "worker_prompt": "repair the frozen blocker",
        }],
        reviewer_feedback="Quality failed: compile(strategy.py)",
        worker_template_hash=_sha("template"),
        worker_execution_context={
            "schema_version": 1,
            "exhausted_block": "",
            "recent_failures": [],
        },
        work_item={"kind": "quality_repair"},
        backend_contract={"model": "weak-model"},
        precommit_rework_count=0,
        official_rework_count=0,
    )


def _workflow(tmp_path):
    store = WorkflowStore(tmp_path / "events.sqlite3")
    store.ensure_instance("149#0", definition_version=1)
    artifacts = WorkerArtifactStore(tmp_path / "artifacts")
    return WorkerWorkflow(store=store, artifacts=artifacts, run_id="149#0")


def test_envelope_is_canonical_and_tamper_evident():
    envelope = _envelope(_sha("candidate"))
    assert validate_worker_envelope(envelope) == []
    assert envelope["envelope_digest"] == content_digest({
        key: value for key, value in envelope.items() if key != "envelope_digest"
    })

    tampered = json.loads(json.dumps(envelope))
    tampered["tasks"][0]["worker_prompt"] = "different task"
    assert "worker_envelope_digest_mismatch" in validate_worker_envelope(tampered)


def test_artifact_store_captures_identity_and_materializes_exact_tree(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "strategy.py").write_text("value = 1\n")
    (candidate / "tables").mkdir()
    (candidate / "tables" / "equity.bin").write_bytes(b"\x00\x01")
    (candidate / "__pycache__").mkdir()
    (candidate / "__pycache__" / "ignored.pyc").write_bytes(b"cache")
    artifacts = WorkerArtifactStore(tmp_path / "artifacts")

    digest = artifacts.capture(candidate)
    assert artifacts.capture(candidate) == digest

    (candidate / "strategy.py").write_text("poisoned = True\n")
    (candidate / "partial.bin").write_bytes(b"partial")
    artifacts.materialize(digest, candidate)

    assert (candidate / "strategy.py").read_text() == "value = 1\n"
    assert (candidate / "tables" / "equity.bin").read_bytes() == b"\x00\x01"
    assert not (candidate / "partial.bin").exists()
    assert not (candidate / "__pycache__").exists()


def test_artifact_store_rejects_symlink(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "strategy.py").write_text("value = 1\n")
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
    (candidate / "strategy.py").write_text("value = 1\n")
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


def test_semantic_failure_waits_for_projection_before_new_cycle(tmp_path):
    workflow = _workflow(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "strategy.py").write_text("value = 1\n")
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
    (candidate / "strategy.py").write_text("value = 1\n")
    baseline = workflow.artifacts.capture(candidate)
    workflow.prepare(_envelope(baseline))
    lease = workflow.request_or_claim(owner="worker-a", lease_seconds=1)
    (candidate / "strategy.py").write_text("value = 2\n")
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
    (candidate / "strategy.py").write_text("value = 1\n")
    snapshot = workflow.artifacts.capture(candidate)
    workflow.prepare(_envelope(snapshot))

    abandoned = workflow.abandon("worker_infrastructure_exhausted")

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
    (candidate / "strategy.py").write_text("value = 1\n")
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


@pytest.mark.parametrize("journal_write_to_fail", [1, 2, 3])
def test_materialization_journal_recovers_each_rename_boundary(
    tmp_path, monkeypatch, journal_write_to_fail
):
    artifacts = WorkerArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "source"
    source.mkdir()
    (source / "strategy.py").write_text("value = 2\n")
    digest = artifacts.capture(source)
    destination = tmp_path / "candidate"
    destination.mkdir()
    (destination / "strategy.py").write_text("value = 1\n")
    original = artifacts._write_journal
    calls = 0

    def crash_at_boundary(journal, payload):
        nonlocal calls
        calls += 1
        if calls == journal_write_to_fail:
            raise RuntimeError("simulated process death")
        return original(journal, payload)

    monkeypatch.setattr(artifacts, "_write_journal", crash_at_boundary)
    with pytest.raises(RuntimeError, match="simulated process death"):
        artifacts.materialize(digest, destination)
    monkeypatch.setattr(artifacts, "_write_journal", original)

    artifacts.materialize(digest, destination)
    assert (destination / "strategy.py").read_text() == "value = 2\n"
    assert not list(tmp_path.glob(".candidate.workflow-*-*"))


def test_materialization_recovers_after_journal_delete_failure(
    tmp_path, monkeypatch
):
    artifacts = WorkerArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "source"
    source.mkdir()
    (source / "strategy.py").write_text("value = 2\n")
    digest = artifacts.capture(source)
    destination = tmp_path / "candidate"
    destination.mkdir()
    (destination / "strategy.py").write_text("value = 1\n")
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
        artifacts.materialize(digest, destination)
    monkeypatch.setattr(artifacts, "_remove_journal", original)
    artifacts.materialize(digest, destination)
    assert (destination / "strategy.py").read_text() == "value = 2\n"


def test_abandon_is_absorbing_and_rejects_late_worker_output(tmp_path):
    workflow = _workflow(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "strategy.py").write_text("value = 1\n")
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
    (candidate / "strategy.py").write_text("value = 1\n")
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
