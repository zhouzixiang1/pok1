"""Durable Slice-2 adapter regressions; the adapter remains production-inert."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pipeline_job_contract import (
    JobContractError,
    build_job_envelope,
    build_job_receipt,
)
from producer_consumer_workflow_store import (
    EFFECT_KIND_PREFIX,
    ProducerConsumerStoreError,
    ProducerConsumerWorkflowAdapter,
    effect_id_for_envelope,
)
from workflow_kernel import (
    InvalidCompletion,
    WorkflowConflict,
    WorkflowStore,
    content_digest,
)


DIGESTS = {letter: letter * 64 for letter in "abcdef0123456789"}


def _refs(*, candidate_id="candidate-1", artifact_digest=DIGESTS["b"]):
    return [
        {"kind": "candidate", "subject": candidate_id, "digest": artifact_digest},
        {"kind": "charter", "subject": "charter-1", "digest": DIGESTS["a"]},
        {
            "kind": "contract",
            "subject": "national-evaluation-contract",
            "digest": DIGESTS["1"],
        },
        {
            "kind": "executor",
            "subject": "quality-consumer",
            "digest": DIGESTS["2"],
        },
        {"kind": "repository", "subject": "origin-main", "digest": DIGESTS["5"]},
        {"kind": "runtime", "subject": "national-runtime", "digest": DIGESTS["4"]},
    ]


def _envelope(**overrides):
    values = {
        "job_id": "job:draft-1:quality-static",
        "run_id": "draft:draft-1",
        "draft_id": "draft-1",
        "candidate_id": "candidate-1",
        "job_kind": "quality-static",
        "charter_digest": DIGESTS["a"],
        "artifact_digest": DIGESTS["b"],
        "dependency_receipt_digests": [],
        "idempotency_key": "draft-1:quality-static:v1",
        "resource_claim": {
            "resource_class": "cpu",
            "cpu_slots": 1,
            "memory_mb": 512,
            "gpu_slots": 0,
            "match_slots": 0,
            "official_slots": 0,
        },
        "priority_class": "compliance",
        "retry_policy": {
            "max_attempts": 3,
            "initial_backoff_sec": 1.0,
            "backoff_multiplier": 2.0,
            "max_backoff_sec": 10.0,
            "retryable_outcomes": ["infrastructure_failure"],
        },
        "deadline": {
            "submitted_at_epoch": 100.0,
            "not_before_epoch": 100.0,
            "expires_at_epoch": 1000.0,
        },
    }
    values.update(overrides)
    if "input_refs" not in overrides:
        values["input_refs"] = _refs(
            candidate_id=values["candidate_id"],
            artifact_digest=values["artifact_digest"],
        )
    return build_job_envelope(**values)


def _receipt(
    envelope,
    lease,
    *,
    started=110.0,
    finished=120.0,
    outcome="success",
    error="",
    lease_owner="consumer-a",
):
    return build_job_receipt(
        envelope=envelope,
        attempt=lease["attempt"],
        lease_epoch=lease["lease_epoch"],
        lease_owner=lease_owner,
        executor={
            "executor_id": "quality-consumer",
            "implementation_digest": DIGESTS["2"],
            "version": "1.0.0",
        },
        outcome=outcome,
        started_at_epoch=started,
        finished_at_epoch=finished,
        result_digest=DIGESTS["3"],
        evidence=[],
        complete_70_hand_sample_ids=[],
        error=error,
    )


def _adapter(tmp_path):
    return ProducerConsumerWorkflowAdapter(
        WorkflowStore(tmp_path / "producer-consumer.sqlite3")
    )


def test_envelope_keeps_draft_and_candidate_identity_distinct():
    envelope = _envelope()
    assert envelope["draft_id"] == "draft-1"
    assert envelope["candidate_id"] == "candidate-1"
    candidate_ref = next(
        ref for ref in envelope["input_refs"] if ref["kind"] == "candidate"
    )
    assert candidate_ref["subject"] == envelope["candidate_id"]

    with pytest.raises(JobContractError) as caught:
        _envelope(candidate_id="draft-1")
    assert "job_envelope_draft_candidate_identity_collapsed" in caught.value.issues

    with pytest.raises(JobContractError) as caught:
        _envelope(input_refs=_refs(candidate_id="other-candidate"))
    assert "job_candidate_ref_subject_mismatch" in caught.value.issues


def test_submit_is_durable_and_same_key_changed_candidate_conflicts(tmp_path):
    adapter = _adapter(tmp_path)
    envelope = _envelope()
    first = adapter.submit(envelope)
    replay = adapter.submit(deepcopy(envelope))

    assert first == replay
    assert first["effect_id"] == effect_id_for_envelope(envelope)
    assert adapter.store.instance(envelope["run_id"])["stream_version"] == 1

    changed = _envelope(
        job_id="job:draft-1:quality-static-replacement",
        candidate_id="candidate-2",
        artifact_digest=DIGESTS["c"],
    )
    with pytest.raises(
        ProducerConsumerStoreError,
        match="producer_consumer_idempotency_conflict",
    ):
        adapter.submit(changed)

    same_job_new_key = _envelope(
        idempotency_key="draft-1:quality-static:v2",
    )
    with pytest.raises(
        ProducerConsumerStoreError,
        match="producer_consumer_idempotency_conflict",
    ):
        adapter.submit(same_job_new_key)


def test_concurrent_submit_has_one_job_and_idempotency_cas_winner(tmp_path):
    adapter = _adapter(tmp_path)
    first = _envelope()
    second = _envelope(
        job_id="job:draft-1:quality-static-alternate",
        candidate_id="candidate-2",
        artifact_digest=DIGESTS["c"],
    )
    barrier = __import__("threading").Barrier(2)

    def submit(envelope):
        barrier.wait()
        try:
            return ("submitted", adapter.submit(envelope)["envelope"]["candidate_id"])
        except ProducerConsumerStoreError:
            return ("conflict", None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, (first, second)))
    assert sorted(result[0] for result in results) == ["conflict", "submitted"]
    effects = adapter.store.effects_for_run(first["run_id"])
    assert len(effects) == 1
    assert effects[0]["input_payload"]["envelope"]["candidate_id"] in {
        "candidate-1",
        "candidate-2",
    }


def test_concurrent_same_job_different_key_has_one_durable_winner(tmp_path):
    adapter = _adapter(tmp_path)
    first = _envelope()
    second = _envelope(idempotency_key="draft-1:quality-static:v2")
    barrier = __import__("threading").Barrier(2)

    def submit(envelope):
        barrier.wait()
        try:
            adapter.submit(envelope)
            return "submitted"
        except ProducerConsumerStoreError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, (first, second)))
    assert sorted(results) == ["conflict", "submitted"]
    effects = adapter.store.effects_for_run(first["run_id"])
    assert len(effects) == 1
    assert effects[0]["input_payload"]["envelope"]["idempotency_key"] in {
        first["idempotency_key"],
        second["idempotency_key"],
    }


def test_claim_and_heartbeat_are_bounded_by_frozen_envelope_deadline(tmp_path):
    adapter = _adapter(tmp_path)
    envelope = _envelope(deadline={
        "submitted_at_epoch": 100.0,
        "not_before_epoch": 110.0,
        "expires_at_epoch": 130.0,
    })
    effect_id = adapter.submit(envelope)["effect_id"]
    with pytest.raises(
        ProducerConsumerStoreError,
        match="producer_consumer_lease_outside_envelope_deadline",
    ):
        adapter.claim(
            effect_id, owner="consumer-a", lease_seconds=20, now=109
        )
    lease = adapter.claim(
        effect_id, owner="consumer-a", lease_seconds=100, now=110
    )
    assert lease["lease_until"] == 130
    with pytest.raises(
        ProducerConsumerStoreError,
        match="producer_consumer_lease_outside_envelope_deadline",
    ):
        adapter.heartbeat(
            effect_id,
            owner="consumer-a",
            lease_epoch=lease["lease_epoch"],
            lease_seconds=10,
            heartbeat_id="too-late",
            now=130,
        )


def test_claim_heartbeat_restart_and_receipt_completion_are_one_kernel_history(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    envelope = _envelope()
    effect_id = adapter.submit(envelope)["effect_id"]
    lease = adapter.claim(
        effect_id,
        owner="consumer-a",
        lease_seconds=20,
        now=100,
    )
    renewed = adapter.heartbeat(
        effect_id,
        owner="consumer-a",
        lease_epoch=lease["lease_epoch"],
        lease_seconds=60,
        heartbeat_id="quality-1",
        now=105,
    )
    assert renewed["attempt"] == lease["attempt"] == 1
    assert renewed["lease_epoch"] == lease["lease_epoch"] == 1
    assert renewed["lease_until"] == 165

    restarted = ProducerConsumerWorkflowAdapter(WorkflowStore(adapter.store.path))
    loaded = restarted.load(effect_id)
    assert loaded["envelope"] == envelope
    assert loaded["effect"]["lease_until"] == 165
    receipt = _receipt(envelope, renewed, finished=150)
    accepted = restarted.complete(
        effect_id,
        receipt=receipt,
        completion_id="quality-1",
        now=151,
    )
    assert accepted["accepted"] is True
    events = restarted.store.events(envelope["run_id"])
    assert [event.event_type for event in events] == [
        "EffectRequested",
        "EffectLeaseHeartbeat",
        "EffectCompleted",
        "ProducerConsumerJobReceiptAccepted",
    ]
    result = restarted.store.effect(effect_id)["result_payload"]
    assert result["receipt_digest"] == receipt["receipt_digest"]


def test_restart_recovery_requires_death_proof_and_reclaims_exact_epoch(tmp_path):
    adapter = _adapter(tmp_path)
    envelope = _envelope()
    effect_id = adapter.submit(envelope)["effect_id"]
    first = adapter.claim(
        effect_id,
        owner="dead-consumer",
        lease_seconds=10,
        now=100,
    )
    restarted = ProducerConsumerWorkflowAdapter(WorkflowStore(adapter.store.path))

    with pytest.raises(
        ProducerConsumerStoreError,
        match="producer_consumer_recovery_death_proof_required",
    ):
        restarted.recover(
            owner="consumer-a",
            lease_seconds=30,
            now=110,
            recovery_id="restart-1",
        )
    assert restarted.store.effect(effect_id)["lease_epoch"] == first["lease_epoch"]

    def death_proof(effect):
        proof = {
            "owner": effect["lease_owner"],
            "reason": "owner_process_missing_after_restart",
        }
        proof["proof_digest"] = content_digest(proof)
        return proof

    recovered = restarted.recover(
        owner="consumer-a",
        lease_seconds=30,
        now=110,
        recovery_id="restart-1",
        death_proof_resolver=death_proof,
    )
    assert recovered["conflicts"] == []
    assert len(recovered["leases"]) == 1
    second = recovered["leases"][0]
    assert second["attempt"] == first["attempt"] + 1
    assert second["lease_epoch"] == first["lease_epoch"] + 1
    assert second["lease_until"] == 140
    assert restarted.store.events(envelope["run_id"])[-1].event_type == (
        "EffectLeaseReclaimed"
    )

    stale_receipt = _receipt(
        envelope,
        {**first, "lease_epoch": first["lease_epoch"], "attempt": first["attempt"]},
        finished=105,
        started=101,
        lease_owner="dead-consumer",
    )
    with pytest.raises(ProducerConsumerStoreError) as caught:
        restarted.complete(
            effect_id,
            receipt=stale_receipt,
            completion_id="stale-owner",
            now=115,
        )
    assert "producer_consumer_receipt_attempt_stale" in caught.value.issues
    assert "producer_consumer_receipt_lease_epoch_stale" in caught.value.issues
    assert "producer_consumer_receipt_lease_owner_stale" in caught.value.issues


def test_infrastructure_retry_and_fresh_restart_claim_preserve_envelope(tmp_path):
    adapter = _adapter(tmp_path)
    envelope = _envelope()
    effect_id = adapter.submit(envelope)["effect_id"]
    first = adapter.claim(
        effect_id, owner="consumer-a", lease_seconds=30, now=100
    )
    failed = adapter.record_infrastructure_failure(
        effect_id,
        lease_owner="consumer-a",
        lease_epoch=first["lease_epoch"],
        error="sandbox_spawn_failed",
        failure_id="spawn-1",
        now=120,
    )
    assert failed["status"] == "retry"
    assert failed["input_payload"]["envelope"] == envelope

    restarted = ProducerConsumerWorkflowAdapter(WorkflowStore(adapter.store.path))
    recovered = restarted.recover(
        owner="consumer-a",
        lease_seconds=30,
        now=131,
        recovery_id="restart-2",
    )
    assert recovered["conflicts"] == []
    assert len(recovered["leases"]) == 1
    assert recovered["leases"][0]["attempt"] == 2
    assert recovered["leases"][0]["envelope_digest"] == envelope["envelope_digest"]


def test_recovery_reports_partial_concurrent_conflict_without_hiding_claim(
    tmp_path,
    monkeypatch,
):
    adapter = _adapter(tmp_path)
    first = _envelope()
    second = _envelope(
        job_id="job:draft-1:quality-dynamic",
        job_kind="quality-dynamic",
        idempotency_key="draft-1:quality-dynamic:v1",
        candidate_id="candidate-2",
        artifact_digest=DIGESTS["c"],
    )
    first_effect_id = adapter.submit(first)["effect_id"]
    second_effect_id = adapter.submit(second)["effect_id"]
    original_claim = adapter.store.claim_effect
    calls = 0

    def one_winner(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise WorkflowConflict("concurrent claimant won")
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(adapter.store, "claim_effect", one_winner)
    recovered = adapter.recover(
        owner="consumer-a",
        lease_seconds=30,
        now=101,
        recovery_id="concurrent-recovery",
    )
    assert len(recovered["leases"]) == 1
    assert len(recovered["conflicts"]) == 1
    assert recovered["conflicts"][0]["effect_id"] in {
        first_effect_id,
        second_effect_id,
    }
    assert recovered["conflicts"][0]["issue"] == (
        "producer_consumer_recovery_concurrent_conflict"
    )
    statuses = sorted(
        effect["status"]
        for effect in adapter.store.effects_for_run(first["run_id"])
    )
    assert statuses == ["requested", "running"]


def test_recovery_ignores_unrelated_effects_in_the_shared_kernel_outbox(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    envelope = _envelope()
    producer_effect_id = adapter.submit(envelope)["effect_id"]
    adapter.store.ensure_instance("unrelated-run", definition_version=1)
    adapter.store.request_effect(
        run_id="unrelated-run",
        effect_id="unrelated-effect",
        kind="worker-call",
        input_payload={"prompt": "unrelated"},
        causation_id="unrelated-submit",
        max_attempts=1,
        available_at=100,
    )

    recovered = adapter.recover(
        owner="consumer-a",
        lease_seconds=30,
        now=101,
        recovery_id="shared-outbox",
    )

    assert [lease["effect_id"] for lease in recovered["leases"]] == [
        producer_effect_id
    ]
    assert adapter.store.effect("unrelated-effect")["status"] == "requested"


def test_recovery_causation_is_stable_when_a_partial_crash_reorders_pending(
    tmp_path,
    monkeypatch,
):
    adapter = _adapter(tmp_path)
    first = _envelope()
    second = _envelope(
        job_id="job:draft-1:quality-dynamic",
        job_kind="quality-dynamic",
        idempotency_key="draft-1:quality-dynamic:v1",
        candidate_id="candidate-2",
        artifact_digest=DIGESTS["c"],
    )
    effect_ids = [
        adapter.submit(first)["effect_id"],
        adapter.submit(second)["effect_id"],
    ]
    for effect_id in effect_ids:
        adapter.claim(
            effect_id,
            owner="dead-consumer",
            lease_seconds=10,
            now=100,
        )

    def death_proof(effect):
        proof = {
            "owner": effect["lease_owner"],
            "reason": "owner_process_missing_after_restart",
        }
        proof["proof_digest"] = content_digest(proof)
        return proof

    original_reclaim = adapter.store.reclaim_effect_lease
    reclaim_calls = 0

    def crash_after_first_reclaim(*args, **kwargs):
        nonlocal reclaim_calls
        reclaim_calls += 1
        if reclaim_calls == 2:
            raise RuntimeError("simulated recovery process crash")
        return original_reclaim(*args, **kwargs)

    monkeypatch.setattr(
        adapter.store,
        "reclaim_effect_lease",
        crash_after_first_reclaim,
    )
    with pytest.raises(RuntimeError, match="simulated recovery process crash"):
        adapter.recover(
            owner="consumer-a",
            lease_seconds=30,
            now=110,
            recovery_id="same-restart-command",
            death_proof_resolver=death_proof,
        )

    restarted = ProducerConsumerWorkflowAdapter(WorkflowStore(adapter.store.path))
    recovered = restarted.recover(
        owner="consumer-a",
        lease_seconds=30,
        now=111,
        recovery_id="same-restart-command",
        death_proof_resolver=death_proof,
    )
    assert recovered["conflicts"] == []
    assert len(recovered["leases"]) == 1
    assert {effect["status"] for effect in restarted.store.effects_for_run(
        first["run_id"]
    )} == {"running"}


@pytest.mark.parametrize("owner", ["", "bad owner", "../owner"])
def test_claim_rejects_invalid_lease_owner_before_kernel_mutation(
    tmp_path,
    owner,
):
    adapter = _adapter(tmp_path)
    effect_id = adapter.submit(_envelope())["effect_id"]
    with pytest.raises(
        ProducerConsumerStoreError,
        match="producer_consumer_lease_owner_invalid",
    ):
        adapter.claim(
            effect_id,
            owner=owner,
            lease_seconds=30,
            now=101,
        )
    assert adapter.store.effect(effect_id)["status"] == "requested"


@pytest.mark.parametrize("now", [float("nan"), float("inf"), True])
def test_completion_rejects_nonfinite_time_before_writing_inbox(tmp_path, now):
    adapter = _adapter(tmp_path)
    envelope = _envelope()
    effect_id = adapter.submit(envelope)["effect_id"]
    lease = adapter.claim(
        effect_id,
        owner="consumer-a",
        lease_seconds=30,
        now=100,
    )
    with pytest.raises(
        ProducerConsumerStoreError,
        match="producer_consumer_completion_time_invalid",
    ):
        adapter.complete(
            effect_id,
            receipt=_receipt(envelope, lease, started=101, finished=104),
            completion_id="invalid-time",
            now=now,
        )
    assert adapter.store.effect(effect_id)["status"] == "running"


def test_recovery_preflights_all_deadlines_before_any_durable_claim(
    tmp_path,
    monkeypatch,
):
    adapter = _adapter(tmp_path)
    valid = _envelope()
    expired = _envelope(
        job_id="job:draft-1:quality-dynamic",
        job_kind="quality-dynamic",
        idempotency_key="draft-1:quality-dynamic:v1",
        candidate_id="candidate-2",
        artifact_digest=DIGESTS["c"],
        deadline={
            "submitted_at_epoch": 100.0,
            "not_before_epoch": 100.0,
            "expires_at_epoch": 110.0,
        },
    )
    valid_effect_id = adapter.submit(valid)["effect_id"]
    expired_effect_id = adapter.submit(expired)["effect_id"]
    monkeypatch.setattr(
        adapter.store,
        "pending_outbox",
        lambda **_kwargs: [
            {
                "effect_id": valid_effect_id,
                "kind": EFFECT_KIND_PREFIX + valid["job_kind"],
            },
            {
                "effect_id": expired_effect_id,
                "kind": EFFECT_KIND_PREFIX + expired["job_kind"],
            },
        ],
    )

    with pytest.raises(
        ProducerConsumerStoreError,
        match="producer_consumer_lease_outside_envelope_deadline",
    ):
        adapter.recover(
            owner="consumer-a",
            lease_seconds=30,
            now=120,
            recovery_id="deadline-preflight",
        )
    assert {
        effect["status"]
        for effect in adapter.store.effects_for_run(valid["run_id"])
    } == {"requested"}


def test_infrastructure_failure_cannot_be_recorded_after_lease_expiry(tmp_path):
    adapter = _adapter(tmp_path)
    effect_id = adapter.submit(_envelope())["effect_id"]
    lease = adapter.claim(
        effect_id, owner="consumer-a", lease_seconds=10, now=100
    )
    with pytest.raises(
        InvalidCompletion,
        match="stale producer/consumer infrastructure failure",
    ):
        adapter.record_infrastructure_failure(
            effect_id,
            lease_owner="consumer-a",
            lease_epoch=lease["lease_epoch"],
            error="late_timeout",
            failure_id="late-timeout",
            now=110,
        )
    assert adapter.store.effect(effect_id)["status"] == "running"


def test_fenced_cancel_survives_restart_and_late_receipt_cannot_complete(tmp_path):
    adapter = _adapter(tmp_path)
    envelope = _envelope()
    effect_id = adapter.submit(envelope)["effect_id"]
    lease = adapter.claim(
        effect_id, owner="consumer-a", lease_seconds=30, now=100
    )
    cancelled = adapter.cancel(
        effect_id,
        expected_status="running",
        expected_attempt=lease["attempt"],
        expected_lease_epoch=lease["lease_epoch"],
        expected_owner="consumer-a",
        reason="evaluation_contract_changed",
        cancel_id="contract-v2",
        now=105,
    )
    assert cancelled["status"] == "abandoned"

    restarted = ProducerConsumerWorkflowAdapter(WorkflowStore(adapter.store.path))
    assert restarted.load(effect_id)["effect"]["status"] == "abandoned"
    assert restarted.recover(
        owner="consumer-b",
        lease_seconds=30,
        now=106,
        recovery_id="after-cancel",
    ) == {"leases": [], "conflicts": []}
    with pytest.raises(ProducerConsumerStoreError) as caught:
        restarted.complete(
            effect_id,
            receipt=_receipt(envelope, lease, started=101, finished=104),
            completion_id="late-after-cancel",
            now=106,
        )
    assert "producer_consumer_receipt_live_lease_missing" in caught.value.issues


def test_queued_effect_can_be_cancelled_only_against_exact_zero_lease(tmp_path):
    adapter = _adapter(tmp_path)
    effect_id = adapter.submit(_envelope())["effect_id"]
    with pytest.raises(WorkflowConflict, match="stale effect cancellation"):
        adapter.cancel(
            effect_id,
            expected_status="requested",
            expected_attempt=1,
            expected_lease_epoch=0,
            expected_owner=None,
            reason="wrong_snapshot",
            cancel_id="wrong",
            now=101,
        )
    cancelled = adapter.cancel(
        effect_id,
        expected_status="requested",
        expected_attempt=0,
        expected_lease_epoch=0,
        expected_owner=None,
        reason="operator_drain",
        cancel_id="drain",
        now=101,
    )
    assert cancelled["status"] == "abandoned"


def test_production_entrypoints_do_not_import_inert_slice_modules():
    root = Path(__file__).resolve().parents[2]
    forbidden = (
        "mechanical_repair",
        "pipeline_job_contract",
        "producer_consumer_pipeline",
        "producer_consumer_workflow_store",
        # Slice 2b one-ahead wiring is an extension of the inert shadow (it
        # imports the two modules above).  It stays dormant until the explicit
        # ``POK_SLICE2B_ENABLED=1`` env var or ``pipeline_slice2b_enabled``
        # context flag is set truthy; see
        # ``producer_consumer_slice2b_activation.slice2b_active``.  Until
        # activation it must not appear in any production source either --
        # EXCEPT the sanctioned activation bridge module
        # ``producer_consumer_slice2b_activation`` and the orchestrator seam
        # that calls it (both default-off and reviewed as the Section 13
        # activation commit).
        "producer_consumer_slice2b",
    )
    inert_sources = {
        root / "web" / "core" / "mechanical_repair.py",
        root / "web" / "core" / "pipeline_job_contract.py",
        root / "web" / "core" / "producer_consumer_pipeline.py",
        root / "web" / "core" / "producer_consumer_workflow_store.py",
        root / "web" / "core" / "producer_consumer_slice2b.py",
    }
    # The sanctioned activation bridge is the only production source allowed to
    # reference the dormant slice2b module.  It is default-off (env/context
    # gated) and is the reviewed Section 13 cutover seam.  Two sanctioned call
    # sites: the orchestrator deterministic-route module (the workers_done seal
    # seam + promotion barrier) and the orchestrator loop-phases module (the
    # one-ahead draft-prepare launch hook).  Every reference in both is lazy
    # and gated behind ``slice2b_active``.
    sanctioned_activation_sources = {
        root / "web" / "core" / "producer_consumer_slice2b_activation.py",
        root / "web" / "core" / "orchestrator_deterministic_route.py",
        root / "web" / "core" / "orchestrator_loop_phases.py",
    }
    # ``orchestrator.py`` itself must remain free of any slice2b reference --
    # it reaches the activation bridge only through the deterministic-route
    # seam and a lazy importlib accessor whose module name is assembled at
    # runtime.  This keeps the dormant module inert at import time.
    orchestrator_path = root / "web" / "core" / "orchestrator.py"
    assert "producer_consumer_slice2b" not in orchestrator_path.read_text(
        encoding="utf-8"
    ), (orchestrator_path, "producer_consumer_slice2b leaked into orchestrator.py")
    production_entrypoints = [
        root / "web" / "main.py",
        *sorted(
            path
            for path in (root / "web" / "core").rglob("*.py")
            if path not in inert_sources
            and path not in sanctioned_activation_sources
            and "results" not in path.parts
        ),
        *sorted((root / "web" / "server").rglob("*.py")),
        *sorted((root / "scripts").rglob("*.py")),
        root / "sever" / "main.py",
        *sorted((root / "sever" / "engine").rglob("*.py")),
        *sorted((root / "sever" / "server").rglob("*.py")),
        *sorted((root / "sever" / "web").rglob("*.py")),
    ]
    assert production_entrypoints
    for path in production_entrypoints:
        source = path.read_text(encoding="utf-8")
        for module_name in forbidden:
            assert module_name not in source, (path, module_name)

    # The sanctioned activation source must itself be default-off: it must not
    # enable slice2b unconditionally and must reference the dormant module only
    # through the env/context-gated ``slice2b_active`` helper.
    activation_source = (
        root / "web" / "core" / "producer_consumer_slice2b_activation.py"
    ).read_text(encoding="utf-8")
    assert "POK_SLICE2B_ENABLED" in activation_source
    assert "def slice2b_active" in activation_source
