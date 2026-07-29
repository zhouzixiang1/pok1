from copy import deepcopy

import pytest

from bot_artifact import canonical_digest
from conftest import strict_bot_tag
from pipeline_job_contract import (
    JOB_ENVELOPE_KIND,
    JOB_ENVELOPE_SCHEMA_VERSION,
    JOB_KIND_POLICIES,
    JOB_RECEIPT_KIND,
    JOB_RECEIPT_SCHEMA_VERSION,
    JobContractError,
    JobIdempotencyConflict,
    PRIORITY_RANKS,
    accept_strength_sample,
    assert_idempotent_job_replay,
    build_evidence_ref,
    build_job_envelope,
    build_job_receipt,
    job_envelope_issues,
    job_receipt_issues,
)


DIGESTS = {letter: letter * 64 for letter in "abcdef0123456789"}


def _input_refs(
    *, candidate_id="candidate-1", native=False, rating=False, executor_id=None, **digests
):
    executor_id = executor_id or (
        "native-admission-consumer" if native else "quality-consumer"
    )
    values = {
        "charter": DIGESTS["a"],
        "candidate": DIGESTS["b"],
        "contract": DIGESTS["1"],
        "runtime": DIGESTS["4"],
        "repository": DIGESTS["5"],
        "executor": DIGESTS["2"],
        "opponent": DIGESTS["c"],
        "evaluator": DIGESTS["d"],
        "parser": DIGESTS["e"],
        "timing-plan": DIGESTS["0"],
        "seed-schedule": DIGESTS["6"],
        "replay-verifier": DIGESTS["7"],
        "published-identity": DIGESTS["8"],
        "official-certificate": DIGESTS["9"],
        "rating-cycle-authority": DIGESTS["3"],
    }
    values.update(digests)
    subjects = {
        "charter": "generation-charter",
        "candidate": candidate_id,
        "contract": "national-evaluation-contract",
        "runtime": "national-runtime",
        "repository": "origin-main",
        "executor": executor_id,
        "opponent": "opponent-artifact",
        "evaluator": "native-evaluator",
        "parser": "native-parser",
        "timing-plan": "native-timing-plan",
        "seed-schedule": "native-seed-schedule",
        "replay-verifier": "native-replay-verifier",
        "published-identity": f"{strict_bot_tag()}-published-identity",
        "official-certificate": f"{strict_bot_tag()}-certificate",
        "rating-cycle-authority": "immutable-rating-cycle-1",
    }
    kinds = [
        "charter",
        "candidate",
        "contract",
        "runtime",
        "repository",
        "executor",
    ]
    if native:
        kinds.extend([
            "opponent",
            "evaluator",
            "parser",
            "timing-plan",
            "seed-schedule",
            "replay-verifier",
        ])
    if rating:
        kinds.extend([
            "published-identity",
            "official-certificate",
            "rating-cycle-authority",
        ])
    return [
        {"kind": kind, "subject": subjects[kind], "digest": values[kind]}
        for kind in kinds
    ]


def _envelope(**overrides):
    values = {
        "job_id": "job:draft-1:quality",
        "run_id": "draft:draft-1",
        "draft_id": "draft-1",
        "candidate_id": "candidate-1",
        "job_kind": "quality-static",
        "charter_digest": DIGESTS["a"],
        "artifact_digest": DIGESTS["b"],
        "dependency_receipt_digests": [DIGESTS["e"]],
        "idempotency_key": "draft-1:quality-static:v1",
        "resource_claim": {
            "resource_class": "cpu",
            "cpu_slots": 2,
            "memory_mb": 1024,
            "gpu_slots": 0,
            "match_slots": 0,
            "official_slots": 0,
        },
        "priority_class": "compliance",
        "retry_policy": {
            "max_attempts": 3,
            "initial_backoff_sec": 2.0,
            "backoff_multiplier": 2.0,
            "max_backoff_sec": 30.0,
            "retryable_outcomes": ["infrastructure_failure"],
        },
        "deadline": {
            "submitted_at_epoch": 100.0,
            "not_before_epoch": 100.0,
            "expires_at_epoch": 200.0,
        },
    }
    values.update(overrides)
    if "input_refs" not in overrides:
        native = values["job_kind"] in {"native-admission", "native-rating"}
        policy = JOB_KIND_POLICIES.get(values["job_kind"])
        executor_id = (
            policy["executor_id"] if policy is not None else "quality-consumer"
        )
        values["input_refs"] = _input_refs(
            candidate_id=values["candidate_id"],
            native=native,
            rating=values["job_kind"] == "native-rating",
            executor_id=executor_id,
            charter=values["charter_digest"],
            candidate=values["artifact_digest"],
        )
    return build_job_envelope(**values)


def _native_envelope(**overrides):
    values = {
        "job_id": "job:draft-1:native-admission",
        "job_kind": "native-admission",
        "idempotency_key": "draft-1:native-admission:v1",
        "resource_claim": {
            "resource_class": "native_match",
            "cpu_slots": 2,
            "memory_mb": 1024,
            "gpu_slots": 0,
            "match_slots": 1,
            "official_slots": 0,
        },
        "priority_class": "compliance",
    }
    values.update(overrides)
    return _envelope(**values)


def _rating_envelope(**overrides):
    values = {
        "job_id": "job:draft-1:native-rating",
        "job_kind": "native-rating",
        "idempotency_key": "draft-1:native-rating:v1",
        "priority_class": "rating",
    }
    values.update(overrides)
    return _native_envelope(**values)


def _native_sample(
    *,
    sample_id="sample-1",
    authority="native_tcp",
    hands=70,
    complete=True,
    strength_admitted=True,
    candidate=DIGESTS["b"],
    opponent=DIGESTS["c"],
    evaluator=DIGESTS["d"],
    parser=DIGESTS["e"],
    timing_plan=DIGESTS["0"],
    seed_schedule=DIGESTS["6"],
    settlements=69,
    verifier=DIGESTS["7"],
    replay=DIGESTS["1"],
    runtime=DIGESTS["4"],
    repository=DIGESTS["5"],
    executor=DIGESTS["2"],
    executor_subject="native-admission-consumer",
    job_kind="native-admission",
    purpose="prepublication-native-admission",
    published_identity=None,
    official_certificate=None,
    rating_cycle_authority=None,
):
    return build_evidence_ref(
        evidence_id=sample_id,
        kind="strength_sample",
        authority=authority,
        digest=DIGESTS["f"],
        strength_sample_unit="70_hand_match",
        hands=hands,
        complete=complete,
        strength_admitted=strength_admitted,
        candidate_artifact_digest=candidate,
        opponent_artifact_digest=opponent,
        evaluator_digest=evaluator,
        parser_digest=parser,
        timing_plan_digest=timing_plan,
        seed_schedule_digest=seed_schedule,
        settlements=settlements,
        verifier_digest=verifier,
        replay_digest=replay,
        runtime_digest=runtime,
        repository_digest=repository,
        executor_digest=executor,
        executor_subject=executor_subject,
        job_kind=job_kind,
        purpose=purpose,
        published_identity_digest=published_identity,
        official_certificate_digest=official_certificate,
        rating_cycle_authority_digest=rating_cycle_authority,
    )


def _receipt(envelope=None, evidence=None, sample_ids=None, **overrides):
    envelope = envelope or _envelope()
    strength_allowed = JOB_KIND_POLICIES[envelope["job_kind"]]["strength_allowed"]
    if evidence is None and strength_allowed:
        refs = {item["kind"]: item for item in envelope["input_refs"]}
        policy = JOB_KIND_POLICIES[envelope["job_kind"]]
        evidence = [_native_sample(
            job_kind=envelope["job_kind"],
            purpose=policy["purpose"],
            executor=refs["executor"]["digest"],
            executor_subject=refs["executor"]["subject"],
            published_identity=(refs.get("published-identity") or {}).get("digest"),
            official_certificate=(refs.get("official-certificate") or {}).get("digest"),
            rating_cycle_authority=(
                refs.get("rating-cycle-authority") or {}
            ).get("digest"),
        )]
    elif evidence is None:
        evidence = []
    sample_ids = (
        ["sample-1"] if sample_ids is None and strength_allowed
        else ([] if sample_ids is None else sample_ids)
    )
    executor_ref = next(
        item for item in envelope["input_refs"] if item["kind"] == "executor"
    )
    values = {
        "envelope": envelope,
        "attempt": 1,
        "lease_epoch": 4,
        "lease_owner": "consumer-1",
        "executor": {
            "executor_id": executor_ref["subject"],
            "implementation_digest": executor_ref["digest"],
            "version": "1.0.0",
        },
        "outcome": "success",
        "started_at_epoch": 110.0,
        "finished_at_epoch": 120.0,
        "result_digest": DIGESTS["3"],
        "evidence": evidence,
        "complete_70_hand_sample_ids": sample_ids,
        "error": "",
    }
    values.update(overrides)
    return build_job_receipt(**values)


def _resign_receipt(value):
    value["receipt_digest"] = canonical_digest({
        key: item for key, item in value.items() if key != "receipt_digest"
    })


def _resign_envelope(value):
    value["idempotency_input_digest"] = canonical_digest({
        key: item
        for key, item in value.items()
        if key not in {"idempotency_input_digest", "envelope_digest"}
    })
    value["envelope_digest"] = canonical_digest({
        key: item for key, item in value.items() if key != "envelope_digest"
    })


def _accept(receipt, envelope=None, **overrides):
    expected = {
        "envelope": envelope or _native_envelope(),
        "sample_id": "sample-1",
        "expected_candidate_id": "candidate-1",
        "expected_candidate_artifact_digest": DIGESTS["b"],
        "expected_charter_digest": DIGESTS["a"],
        "expected_evaluation_contract_digest": DIGESTS["1"],
        "expected_attempt": 1,
        "expected_lease_epoch": 4,
        "expected_lease_owner": "consumer-1",
        "lease_until_epoch": 130.0,
        "accepted_at_epoch": 121.0,
        "expected_opponent_artifact_digest": DIGESTS["c"],
        "expected_evaluator_digest": DIGESTS["d"],
        "expected_parser_digest": DIGESTS["e"],
        "expected_timing_plan_digest": DIGESTS["0"],
        "expected_seed_schedule_digest": DIGESTS["6"],
        "expected_replay_verifier_digest": DIGESTS["7"],
        "expected_runtime_digest": DIGESTS["4"],
        "expected_repository_digest": DIGESTS["5"],
        "expected_executor_digest": DIGESTS["2"],
        "expected_published_identity_digest": None,
        "expected_official_certificate_digest": None,
        "expected_rating_cycle_authority_digest": None,
    }
    expected.update(overrides)
    return accept_strength_sample(receipt, **expected)


def test_envelope_is_exact_canonical_and_orders_inputs():
    envelope = _envelope()

    assert envelope["schema_version"] == JOB_ENVELOPE_SCHEMA_VERSION
    assert envelope["kind"] == JOB_ENVELOPE_KIND
    assert [item["kind"] for item in envelope["input_refs"]] == [
        "candidate",
        "charter",
        "contract",
        "executor",
        "repository",
        "runtime",
    ]
    assert envelope["priority"] == {
        "class": "compliance",
        "rank": PRIORITY_RANKS["compliance"],
    }
    assert job_envelope_issues(envelope) == []
    assert envelope["envelope_digest"] == canonical_digest({
        key: value for key, value in envelope.items() if key != "envelope_digest"
    })


def test_envelope_rejects_extra_fields_and_tampering():
    envelope = _envelope()
    envelope["prompt_can_raise_priority"] = True
    assert job_envelope_issues(envelope) == ["job_envelope_fields_mismatch"]

    envelope = _envelope()
    envelope["resource_claim"]["cpu_slots"] = 3
    assert "job_envelope_idempotency_input_digest_mismatch" in job_envelope_issues(
        envelope
    )
    assert "job_envelope_digest_mismatch" in job_envelope_issues(envelope)


@pytest.mark.parametrize(
    "overrides, issue",
    [
        (
            {"priority_class": "candidate-chosen"},
            "job_priority_class_invalid",
        ),
        (
            {
                "resource_claim": {
                    "resource_class": "native_match",
                    "cpu_slots": 1,
                    "memory_mb": 512,
                    "gpu_slots": 0,
                    "match_slots": 0,
                    "official_slots": 0,
                }
            },
            "job_native_match_slot_missing",
        ),
        (
            {
                "retry_policy": {
                    "max_attempts": 3,
                    "initial_backoff_sec": 1.0,
                    "backoff_multiplier": 2.0,
                    "max_backoff_sec": 5.0,
                    "retryable_outcomes": ["candidate_failure"],
                }
            },
            "job_retryable_outcomes_invalid",
        ),
        (
            {
                "deadline": {
                    "submitted_at_epoch": 100.0,
                    "not_before_epoch": 200.0,
                    "expires_at_epoch": 150.0,
                }
            },
            "job_deadline_expiry_order_invalid",
        ),
    ],
)
def test_envelope_builder_rejects_invalid_control_fields(overrides, issue):
    with pytest.raises(JobContractError) as caught:
        _envelope(**overrides)
    assert issue in caught.value.issues


def test_envelope_validator_is_total_for_non_json_nested_values():
    envelope = _envelope()
    envelope["input_refs"] = [object()]
    issues = job_envelope_issues(envelope)
    assert "job_envelope_input_refs_0_not_json_value" in issues


def test_idempotency_accepts_exact_replay_and_distinguishes_new_key():
    first = _envelope()
    exact = deepcopy(first)
    other_key = _envelope(idempotency_key="draft-1:quality-static:v2")

    assert assert_idempotent_job_replay(first, exact) is True
    assert assert_idempotent_job_replay(first, other_key) is False


@pytest.mark.parametrize(
    "changed",
    [
        {"artifact_digest": DIGESTS["c"]},
        {"candidate_id": "candidate-2"},
        {"job_id": "job:draft-1:quality-replacement"},
        {
            "deadline": {
                "submitted_at_epoch": 100.0,
                "not_before_epoch": 100.0,
                "expires_at_epoch": 201.0,
            }
        },
        {
            "resource_claim": {
                "resource_class": "cpu",
                "cpu_slots": 3,
                "memory_mb": 1024,
                "gpu_slots": 0,
                "match_slots": 0,
                "official_slots": 0,
            }
        },
    ],
)
def test_same_idempotency_key_with_any_changed_input_conflicts(changed):
    first = _envelope()
    proposed = _envelope(**changed)

    with pytest.raises(JobIdempotencyConflict) as caught:
        assert_idempotent_job_replay(first, proposed)
    assert caught.value.issues == ("job_idempotency_key_input_conflict",)


def test_receipt_is_exact_bound_and_canonical():
    envelope = _envelope()
    receipt = _receipt(envelope)

    assert receipt["schema_version"] == JOB_RECEIPT_SCHEMA_VERSION
    assert receipt["kind"] == JOB_RECEIPT_KIND
    assert receipt["job_id"] == envelope["job_id"]
    assert receipt["envelope_digest"] == envelope["envelope_digest"]
    assert receipt["complete_70_hand_sample_ids"] == []
    assert job_receipt_issues(receipt, envelope=envelope) == []


def test_receipt_rejects_attempt_lease_executor_and_deadline_drift():
    envelope = _envelope()
    with pytest.raises(JobContractError) as caught:
        _receipt(envelope, attempt=4)
    assert "job_receipt_attempt_exceeds_policy" in caught.value.issues

    with pytest.raises(JobContractError) as caught:
        _receipt(envelope, lease_epoch=0)
    assert "job_receipt_lease_epoch_invalid" in caught.value.issues

    with pytest.raises(JobContractError) as caught:
        _receipt(
            envelope,
            executor={
                "executor_id": "quality-consumer",
                "implementation_digest": "bad",
                "version": "1",
            },
        )
    assert "job_executor_implementation_digest_invalid" in caught.value.issues

    with pytest.raises(JobContractError) as caught:
        _receipt(envelope, finished_at_epoch=201.0)
    assert "job_receipt_finished_after_deadline" in caught.value.issues


def test_failure_receipt_requires_error_and_never_declares_strength():
    envelope = _envelope()
    receipt = _receipt(
        envelope,
        outcome="infrastructure_failure",
        error="runner unavailable",
        evidence=[],
        sample_ids=[],
    )
    assert job_receipt_issues(receipt, envelope=envelope) == []
    assert _accept(receipt, envelope)["accepted"] is False

    with pytest.raises(JobContractError) as caught:
        _receipt(
            envelope,
            outcome="candidate_failure",
            error="",
            evidence=[],
            sample_ids=[],
        )
    assert "job_receipt_failure_error_missing" in caught.value.issues


def test_complete_native_70_hand_sample_is_accepted():
    envelope = _native_envelope()
    receipt = _receipt(envelope)

    accepted = _accept(receipt, envelope)

    assert accepted["accepted"] is True
    assert accepted["issues"] == []
    assert accepted["sample"]["authority"] == "native_tcp"
    assert accepted["sample"]["hands"] == 70
    assert accepted["sample"]["settlements"] == 69
    assert accepted["receipt_digest"] == receipt["receipt_digest"]
    assert accepted["admission_identity_digest"] == accepted["sample"][
        "admission_identity_digest"
    ]
    assert accepted["rating_eligible"] is False
    assert accepted["pending_external_gates"] == [
        "raw_replay_resolver",
        "durable_admission_identity_cas",
    ]


def test_strength_acceptance_rejects_stale_envelope_and_lease():
    envelope = _native_envelope()
    receipt = _receipt(envelope)
    other = _native_envelope(
        job_id="job:draft-2:native-admission",
        run_id="draft:draft-2",
        draft_id="draft-2",
        idempotency_key="draft-2:native-admission:v1",
    )

    stale_envelope = _accept(receipt, other)
    assert stale_envelope["accepted"] is False
    assert "job_receipt_job_id_mismatch" in stale_envelope["issues"]
    assert "job_receipt_envelope_digest_mismatch" in stale_envelope["issues"]

    stale_lease = _accept(receipt, envelope, expected_lease_epoch=5)
    assert stale_lease["accepted"] is False
    assert "strength_receipt_lease_epoch_stale" in stale_lease["issues"]


@pytest.mark.parametrize(
    ("overrides", "issue"),
    [
        ({"expected_attempt": 2}, "strength_receipt_attempt_stale"),
        (
            {"expected_lease_owner": "consumer-2"},
            "strength_receipt_lease_owner_stale",
        ),
        # A current epoch cannot launder an old owner/attempt.
        (
            {
                "expected_attempt": 2,
                "expected_lease_epoch": 4,
                "expected_lease_owner": "consumer-2",
            },
            "strength_receipt_attempt_stale",
        ),
    ],
)
def test_strength_acceptance_binds_durable_active_attempt_owner_and_epoch(
    overrides,
    issue,
):
    envelope = _native_envelope()
    result = _accept(_receipt(envelope), envelope, **overrides)
    assert result["accepted"] is False
    assert issue in result["issues"]


@pytest.mark.parametrize(
    "lease_until, accepted_at, issue",
    [
        (119.0, 121.0, "strength_receipt_finished_after_lease"),
        (120.5, 121.0, "strength_receipt_arrived_after_lease"),
        (130.0, 119.0, "strength_acceptance_precedes_receipt"),
    ],
)
def test_strength_acceptance_rejects_late_or_impossible_timing(
    lease_until, accepted_at, issue
):
    envelope = _native_envelope()
    receipt = _receipt(envelope)

    result = _accept(
        receipt,
        envelope,
        lease_until_epoch=lease_until,
        accepted_at_epoch=accepted_at,
    )
    assert result["accepted"] is False
    assert issue in result["issues"]


def test_strength_acceptance_uses_workflow_kernel_strict_live_lease_boundary():
    envelope = _native_envelope()
    receipt = _receipt(envelope, finished_at_epoch=130.0)

    result = _accept(
        receipt,
        envelope,
        lease_until_epoch=130.0,
        accepted_at_epoch=130.0,
    )

    assert result["accepted"] is False
    assert "strength_receipt_finished_after_lease" in result["issues"]
    assert "strength_receipt_arrived_after_lease" in result["issues"]


@pytest.mark.parametrize("authority", ["official_exe", "arena"])
def test_official_and_arena_evidence_are_zero_strength(authority):
    envelope = _native_envelope()
    evidence = _native_sample(
        authority=authority,
        strength_admitted=False,
    )
    receipt = _receipt(envelope, evidence=[evidence], sample_ids=[])

    result = _accept(receipt, envelope)

    assert result["accepted"] is False
    assert "strength_sample_not_declared_complete_70" in result["issues"]
    assert "strength_sample_authority_mismatch" in result["issues"]

    forged = deepcopy(receipt)
    forged["evidence"][0]["strength_admitted"] = True
    forged["complete_70_hand_sample_ids"] = ["sample-1"]
    _resign_receipt(forged)
    issues = job_receipt_issues(forged, envelope=envelope)
    assert "job_evidence_0_strength_authority_forbidden" in issues
    assert "job_evidence_0_zero_strength_authority_admitted" in issues


def test_69_hand_result_cannot_enter_complete_sample_ids_or_barrier():
    envelope = _native_envelope()
    evidence = _native_sample(
        hands=69,
        complete=True,
        strength_admitted=False,
    )
    receipt = _receipt(envelope, evidence=[evidence], sample_ids=[])

    result = _accept(receipt, envelope)
    assert result["accepted"] is False
    assert "strength_sample_not_declared_complete_70" in result["issues"]
    assert "strength_sample_hands_mismatch" in result["issues"]

    forged = deepcopy(receipt)
    forged["evidence"][0]["strength_admitted"] = True
    forged["complete_70_hand_sample_ids"] = ["sample-1"]
    _resign_receipt(forged)
    assert "job_evidence_0_strength_hands_not_70" in job_receipt_issues(
        forged,
        envelope=envelope,
    )


@pytest.mark.parametrize(
    "field, expected, issue",
    [
        (
            "candidate_artifact_digest",
            DIGESTS["c"],
            "strength_sample_candidate_artifact_digest_mismatch",
        ),
        ("opponent_artifact_digest", DIGESTS["f"], "strength_active_opponent_digest_mismatch"),
        ("evaluator_digest", DIGESTS["f"], "strength_active_evaluator_digest_mismatch"),
        ("parser_digest", DIGESTS["f"], "strength_active_parser_digest_mismatch"),
    ],
)
def test_strength_acceptance_rejects_every_stale_identity(field, expected, issue):
    envelope = _native_envelope()
    receipt = _receipt(envelope)
    kwargs = {}
    if field == "candidate_artifact_digest":
        receipt = _receipt(
            envelope,
            evidence=[_native_sample(candidate=expected)],
        )
    elif field == "opponent_artifact_digest":
        kwargs["expected_opponent_artifact_digest"] = expected
    elif field == "evaluator_digest":
        kwargs["expected_evaluator_digest"] = expected
    else:
        kwargs["expected_parser_digest"] = expected

    result = _accept(receipt, envelope, **kwargs)
    assert result["accepted"] is False
    assert issue in result["issues"]


def test_receipt_validator_rejects_extra_field_and_non_json_evidence():
    envelope = _envelope()
    receipt = _receipt(envelope)
    receipt["reviewer_says_strong"] = True
    assert job_receipt_issues(receipt, envelope=envelope) == [
        "job_receipt_fields_mismatch"
    ]

    receipt = _receipt(envelope)
    receipt["evidence"] = [object()]
    issues = job_receipt_issues(receipt, envelope=envelope)
    assert "job_receipt_evidence_0_not_json_value" in issues


def test_closed_job_policy_owns_priority_resource_and_native_admission_rank():
    with pytest.raises(JobContractError) as caught:
        _envelope(job_kind="candidate-invented-job")
    assert "job_envelope_job_kind_unknown" in caught.value.issues

    with pytest.raises(JobContractError) as caught:
        _envelope(priority_class="recovery")
    assert "job_priority_class_policy_mismatch" in caught.value.issues

    with pytest.raises(JobContractError) as caught:
        _envelope(resource_claim={
            "resource_class": "mixed",
            "cpu_slots": 0,
            "memory_mb": 1024,
            "gpu_slots": 0,
            "match_slots": 0,
            "official_slots": 0,
        })
    assert "job_mixed_resource_empty" in caught.value.issues
    assert "job_resource_class_policy_mismatch" in caught.value.issues

    native = _native_envelope()
    assert native["priority"] == {"class": "compliance", "rank": 80}
    assert native["resource_claim"]["resource_class"] == "native_match"
    assert native["purpose"] == "prepublication-native-admission"
    assert _rating_envelope()["purpose"] == "published-pool-immutable-rating"

    purpose_drift = deepcopy(native)
    purpose_drift["purpose"] = "published-pool-immutable-rating"
    _resign_envelope(purpose_drift)
    assert "job_purpose_policy_mismatch" in job_envelope_issues(purpose_drift)


@pytest.mark.parametrize("missing_kind", [
    "candidate",
    "contract",
    "opponent",
    "evaluator",
    "parser",
    "timing-plan",
    "seed-schedule",
    "replay-verifier",
    "executor",
    "runtime",
    "repository",
])
def test_native_job_requires_every_frozen_identity_ref(missing_kind):
    refs = [
        item for item in _input_refs(native=True)
        if item["kind"] != missing_kind
    ]
    with pytest.raises(JobContractError) as caught:
        _native_envelope(input_refs=refs)
    assert (
        f"job_input_ref_{missing_kind.replace('-', '_')}_missing"
        in caught.value.issues
    )


def test_charter_candidate_and_executor_refs_are_cross_bound():
    with pytest.raises(JobContractError) as caught:
        _envelope(input_refs=_input_refs(charter=DIGESTS["f"]))
    assert "job_charter_ref_digest_mismatch" in caught.value.issues

    with pytest.raises(JobContractError) as caught:
        _envelope(input_refs=_input_refs(candidate=DIGESTS["f"]))
    assert "job_candidate_ref_digest_mismatch" in caught.value.issues

    with pytest.raises(JobContractError) as caught:
        _envelope(input_refs=[
            item for item in _input_refs() if item["kind"] != "contract"
        ])
    assert "job_input_ref_contract_missing" in caught.value.issues

    native = _native_envelope()
    with pytest.raises(JobContractError) as caught:
        _receipt(native, executor={
            "executor_id": "native-admission-consumer",
            "implementation_digest": DIGESTS["f"],
            "version": "1",
        })
    assert "job_receipt_executor_digest_ref_mismatch" in caught.value.issues


def test_non_native_and_official_jobs_cannot_emit_strength():
    sample = _native_sample()
    with pytest.raises(JobContractError) as caught:
        _receipt(_envelope(), evidence=[sample], sample_ids=["sample-1"])
    assert "job_receipt_strength_job_kind_forbidden" in caught.value.issues

    official = _envelope(
        job_id="job:draft-1:official",
        job_kind="official-certification",
        idempotency_key="draft-1:official:v1",
        priority_class="promotion",
        resource_claim={
            "resource_class": "official_exe",
            "cpu_slots": 1,
            "memory_mb": 1024,
            "gpu_slots": 0,
            "match_slots": 0,
            "official_slots": 1,
        },
    )
    with pytest.raises(JobContractError) as caught:
        _receipt(official, evidence=[sample], sample_ids=["sample-1"])
    assert "job_receipt_strength_job_kind_forbidden" in caught.value.issues


@pytest.mark.parametrize("field, override, issue", [
    (
        "expected_timing_plan_digest",
        DIGESTS["f"],
        "strength_active_timing_plan_digest_mismatch",
    ),
    (
        "expected_seed_schedule_digest",
        DIGESTS["f"],
        "strength_active_seed_schedule_digest_mismatch",
    ),
    (
        "expected_replay_verifier_digest",
        DIGESTS["f"],
        "strength_active_replay_verifier_digest_mismatch",
    ),
    (
        "expected_runtime_digest",
        DIGESTS["f"],
        "strength_active_runtime_digest_mismatch",
    ),
    (
        "expected_repository_digest",
        DIGESTS["f"],
        "strength_active_repository_digest_mismatch",
    ),
    (
        "expected_executor_digest",
        DIGESTS["f"],
        "strength_active_executor_digest_mismatch",
    ),
])
def test_strength_acceptance_cross_checks_all_active_refs(field, override, issue):
    envelope = _native_envelope()
    receipt = _receipt(envelope)
    result = _accept(receipt, envelope, **{field: override})
    assert result["accepted"] is False
    assert issue in result["issues"]


@pytest.mark.parametrize(
    "field, override, issue",
    [
        (
            "expected_candidate_id",
            "candidate-2",
            "strength_active_candidate_id_mismatch",
        ),
        (
            "expected_candidate_artifact_digest",
            DIGESTS["f"],
            "strength_active_candidate_artifact_digest_mismatch",
        ),
        (
            "expected_charter_digest",
            DIGESTS["f"],
            "strength_active_charter_digest_mismatch",
        ),
        (
            "expected_evaluation_contract_digest",
            DIGESTS["f"],
            "strength_active_evaluation_contract_digest_mismatch",
        ),
    ],
)
def test_strength_acceptance_binds_active_candidate_charter_and_contract(
    field,
    override,
    issue,
):
    envelope = _native_envelope()
    result = _accept(
        _receipt(envelope),
        envelope,
        **{field: override},
    )
    assert result["accepted"] is False
    assert issue in result["issues"]


@pytest.mark.parametrize("sample_override, issue", [
    (
        {"timing_plan": DIGESTS["f"]},
        "strength_sample_timing_plan_digest_mismatch",
    ),
    (
        {"seed_schedule": DIGESTS["f"]},
        "strength_sample_seed_schedule_digest_mismatch",
    ),
    (
        {"verifier": DIGESTS["f"]},
        "strength_sample_verifier_digest_mismatch",
    ),
    (
        {"runtime": DIGESTS["f"]},
        "strength_sample_runtime_digest_mismatch",
    ),
    (
        {"repository": DIGESTS["f"]},
        "strength_sample_repository_digest_mismatch",
    ),
    (
        {"executor": DIGESTS["f"]},
        "strength_sample_executor_digest_mismatch",
    ),
])
def test_strength_evidence_must_match_frozen_native_refs(sample_override, issue):
    envelope = _native_envelope()
    receipt = _receipt(envelope, evidence=[_native_sample(**sample_override)])
    result = _accept(receipt, envelope)
    assert result["accepted"] is False
    assert issue in result["issues"]


def test_strength_requires_69_settlements_and_stable_admission_identity():
    with pytest.raises(JobContractError) as caught:
        _native_sample(settlements=68)
    assert "job_evidence_0_strength_settlements_not_69" in caught.value.issues

    first = _native_sample(sample_id="sample-1")
    relabelled = _native_sample(sample_id="sample-2")
    assert first["admission_identity_digest"] == relabelled[
        "admission_identity_digest"
    ]

    with pytest.raises(JobContractError) as caught:
        _receipt(
            _native_envelope(),
            evidence=[first, relabelled],
            sample_ids=["sample-1", "sample-2"],
        )
    assert (
        "job_receipt_strength_admission_identity_duplicate"
        in caught.value.issues
    )

    forged = deepcopy(first)
    forged["admission_identity_digest"] = DIGESTS["f"]
    with pytest.raises(JobContractError) as caught:
        _receipt(_native_envelope(), evidence=[forged])
    assert "job_evidence_0_admission_identity_digest_mismatch" in caught.value.issues


def test_native_rating_has_distinct_purpose_and_published_authority_identity():
    admission_envelope = _native_envelope()
    admission = _accept(_receipt(admission_envelope), admission_envelope)

    rating_envelope = _rating_envelope()
    rating_receipt = _receipt(rating_envelope)
    rating = _accept(
        rating_receipt,
        rating_envelope,
        expected_published_identity_digest=DIGESTS["8"],
        expected_official_certificate_digest=DIGESTS["9"],
        expected_rating_cycle_authority_digest=DIGESTS["3"],
    )

    assert admission["accepted"] is rating["accepted"] is True
    assert admission["sample"]["purpose"] == "prepublication-native-admission"
    assert rating["sample"]["purpose"] == "published-pool-immutable-rating"
    assert rating["sample"]["executor_subject"] == "native-rating-consumer"
    assert rating["sample"]["published_identity_digest"] == DIGESTS["8"]
    assert rating["sample"]["official_certificate_digest"] == DIGESTS["9"]
    assert rating["sample"]["rating_cycle_authority_digest"] == DIGESTS["3"]
    assert admission["admission_identity_digest"] != rating[
        "admission_identity_digest"
    ]

    purpose_swapped = deepcopy(rating_receipt)
    purpose_swapped["evidence"][0]["purpose"] = (
        "prepublication-native-admission"
    )
    _resign_receipt(purpose_swapped)
    issues = job_receipt_issues(purpose_swapped, envelope=rating_envelope)
    assert "job_evidence_0_purpose_job_kind_mismatch" in issues
    assert "job_receipt_strength_purpose_mismatch" in issues


def test_native_rating_fails_closed_without_current_certificate_and_cycle_refs():
    missing_certificate_refs = [
        ref
        for ref in _input_refs(
            native=True,
            rating=True,
            executor_id="native-rating-consumer",
        )
        if ref["kind"] != "official-certificate"
    ]
    with pytest.raises(JobContractError) as caught:
        _rating_envelope(input_refs=missing_certificate_refs)
    assert "job_input_ref_official_certificate_missing" in caught.value.issues

    rating_envelope = _rating_envelope()
    stale_authority = _accept(
        _receipt(rating_envelope),
        rating_envelope,
        expected_published_identity_digest=DIGESTS["8"],
        expected_official_certificate_digest=DIGESTS["f"],
        expected_rating_cycle_authority_digest=DIGESTS["3"],
    )
    assert stale_authority["accepted"] is False
    assert (
        "strength_active_official_certificate_digest_mismatch"
        in stale_authority["issues"]
    )


@pytest.mark.parametrize(
    ("ref_kind", "sample_field", "accept_field"),
    [
        ("runtime", "runtime", "expected_runtime_digest"),
        ("repository", "repository", "expected_repository_digest"),
        ("executor", "executor", "expected_executor_digest"),
        (
            "replay-verifier",
            "verifier",
            "expected_replay_verifier_digest",
        ),
    ],
)
def test_strength_admission_identity_binds_every_execution_and_verifier_ref(
    ref_kind,
    sample_field,
    accept_field,
):
    baseline_envelope = _native_envelope()
    baseline = _accept(_receipt(baseline_envelope), baseline_envelope)

    changed = DIGESTS["f"]
    changed_refs = _input_refs(native=True, **{ref_kind: changed})
    changed_envelope = _native_envelope(
        job_id=f"job:draft-1:native-admission-{ref_kind}",
        idempotency_key=f"draft-1:native-admission-{ref_kind}:v1",
        input_refs=changed_refs,
    )
    changed_receipt = _receipt(
        changed_envelope,
        evidence=[_native_sample(**{sample_field: changed})],
    )
    accepted = _accept(
        changed_receipt,
        changed_envelope,
        **{accept_field: changed},
    )

    assert baseline["accepted"] is True
    assert accepted["accepted"] is True
    assert (
        accepted["admission_identity_digest"]
        != baseline["admission_identity_digest"]
    )


def test_exact_native_retry_keeps_sample_identity_but_not_receipt_identity():
    envelope = _native_envelope()
    evidence = [_native_sample()]
    first_receipt = _receipt(envelope, evidence=evidence)
    retry_receipt = _receipt(
        envelope,
        evidence=evidence,
        attempt=2,
        lease_epoch=5,
        lease_owner="consumer-2",
        started_at_epoch=122.0,
        finished_at_epoch=125.0,
    )

    first = _accept(first_receipt, envelope)
    retry = _accept(
        retry_receipt,
        envelope,
        expected_attempt=2,
        expected_lease_epoch=5,
        expected_lease_owner="consumer-2",
        accepted_at_epoch=126.0,
    )

    assert first["accepted"] is retry["accepted"] is True
    assert first["admission_identity_digest"] == retry[
        "admission_identity_digest"
    ]
    assert first["receipt_digest"] != retry["receipt_digest"]


def test_native_retry_cannot_turn_one_frozen_match_into_a_new_sample_identity():
    envelope = _native_envelope()
    first_receipt = _receipt(
        envelope,
        evidence=[_native_sample(replay=DIGESTS["1"])],
    )
    retry_receipt = _receipt(
        envelope,
        evidence=[_native_sample(replay=DIGESTS["3"])],
        attempt=2,
        lease_epoch=5,
        lease_owner="consumer-2",
        started_at_epoch=122.0,
        finished_at_epoch=125.0,
        result_digest=DIGESTS["8"],
    )

    first = _accept(first_receipt, envelope)
    retry = _accept(
        retry_receipt,
        envelope,
        expected_attempt=2,
        expected_lease_epoch=5,
        expected_lease_owner="consumer-2",
        accepted_at_epoch=126.0,
    )

    assert first["accepted"] is retry["accepted"] is True
    assert first["sample"]["replay_digest"] != retry["sample"]["replay_digest"]
    assert first["receipt_digest"] != retry["receipt_digest"]
    # The raw replay resolver may later prove one payload invalid, while the
    # stable CAS identity prevents a retry from counting both as strength.
    assert first["admission_identity_digest"] == retry[
        "admission_identity_digest"
    ]
    assert retry["rating_eligible"] is False
    assert "durable_admission_identity_cas" in retry["pending_external_gates"]


def test_huge_json_integers_fail_closed_without_raw_numeric_exceptions():
    envelope = _envelope()
    envelope["deadline"]["expires_at_epoch"] = 10**1000
    assert "job_envelope_deadline_expires_at_epoch_integer_out_of_range" in (
        job_envelope_issues(envelope)
    )

    with pytest.raises(JobContractError) as caught:
        _envelope(deadline={
            "submitted_at_epoch": 1,
            "not_before_epoch": 1,
            "expires_at_epoch": 10**1000,
        })
    assert any(
        issue.endswith("expires_at_epoch_integer_out_of_range")
        for issue in caught.value.issues
    )

    cyclic = []
    cyclic.append(cyclic)
    envelope = _envelope()
    envelope["input_refs"] = cyclic
    assert "job_envelope_input_refs_0_cyclic" in job_envelope_issues(envelope)
