"""Schema-v1 producer/consumer artifact-pipeline reducer regressions."""

from copy import deepcopy

import pytest

import producer_consumer_pipeline as pipeline
from bot_namespace import bot_name, bot_tag
from conftest import STRICT_TARGET_V
from mechanical_repair import build_mechanical_repair_output


TARGET = {
    "evaluation_epoch": "national_tcp_policy_v1:epoch-1",
    "workflow_run_id": f"generation:{STRICT_TARGET_V}:workflow-v64",
    "target_lease_digest": "7" * 64,
    "generation_ordinal": 1,
    "canonical_version": STRICT_TARGET_V,
    "canonical_bot_name": bot_name(STRICT_TARGET_V),
    "canonical_tag": bot_tag(STRICT_TARGET_V),
}
ARTIFACT_A = "a" * 64
ARTIFACT_B = "b" * 64
ARTIFACT_C = "c" * 64
MANIFEST_A = "d" * 64
MANIFEST_B = "e" * 64
MANIFEST_C = "f" * 64
COMMIT = {"object_format": "sha1", "oid": "3" * 40}
REMOTE_MAIN = {"object_format": "sha1", "oid": "4" * 40}
BOT_TAG_OBJECT = {"object_format": "sha1", "oid": "5" * 40}
HIGH_WATER_TAG_OBJECT = {"object_format": "sha1", "oid": "6" * 40}
OFFICIAL_CERTIFICATE_DIGEST = "2" * 64
TREE_DIGEST = "4" * 64
COMPLETED_DIGEST = "5" * 64
REMOTE_RESOLVER_DIGEST = "6" * 64
REPAIR_RESOLVER_DIGEST = "7" * 64
GIT_OBJECT_FORMAT = "sha1"
INPUT_POLICY_BYTES = b"def decide(context):\n    return {'intent': 'pass'}\n"
OUTPUT_POLICY_BYTES = (
    b"# lexical-only\ndef decide( context ):\n"
    b"    return { 'intent': 'pass' }\n"
)
NATIONAL_BOT_BYTES = b"system-national-runtime\n"
PRECOMPUTE_BYTES = b"system-precompute\n"
PROMOTION_AUTHORITY_PACKAGES = {}


def _remote_proof(**overrides):
    values = {
        "target_identity": TARGET,
        "object_format": GIT_OBJECT_FORMAT,
        "remote_name": "origin",
        "remote_main": REMOTE_MAIN,
        "commit": COMMIT,
        "bot_tag_object": BOT_TAG_OBJECT,
        "high_water_tag_object": HIGH_WATER_TAG_OBJECT,
        "resolver_digest": REMOTE_RESOLVER_DIGEST,
    }
    values.update(overrides)
    return pipeline.build_remote_publication_proof(**values)


def _promotion_receipt(*, remote_proof=None, **overrides):
    values = {
        "target_identity": TARGET,
        "candidate_id": "candidate-a",
        "artifact_hash": ARTIFACT_A,
        "official_certificate_digest": OFFICIAL_CERTIFICATE_DIGEST,
        "object_format": GIT_OBJECT_FORMAT,
        "commit": COMMIT,
        "tree_digest": TREE_DIGEST,
        "completed_digest": COMPLETED_DIGEST,
        "annotated_tag": BOT_TAG_OBJECT,
        "remote_proof": remote_proof or _remote_proof(),
    }
    values.update(overrides)
    return pipeline.build_promotion_receipt(**values)


def _promotion_payload_from_package(
    remote,
    receipt,
    *,
    reason="all_gates_and_publication_verified",
    resolver_digest=REMOTE_RESOLVER_DIGEST,
):
    PROMOTION_AUTHORITY_PACKAGES[
        (receipt["receipt_digest"], resolver_digest)
    ] = {
        "authority": "strict-publication-authority-resolver-v1",
        "official_policy_id": "official-full-v5",
        "resolver_digest": resolver_digest,
        "promotion_receipt": receipt,
        "remote_proof": remote,
    }
    return {
        "reason": reason,
        "promotion_receipt_digest": receipt["receipt_digest"],
        "resolver_digest": resolver_digest,
    }


def _promotion_payload(reason="all_gates_and_publication_verified"):
    remote = _remote_proof()
    receipt = _promotion_receipt(remote_proof=remote)
    return _promotion_payload_from_package(remote, receipt, reason=reason)


def _published_proof_fields(payload):
    resolved = PROMOTION_AUTHORITY_PACKAGES[
        (payload["promotion_receipt_digest"], payload["resolver_digest"])
    ]
    receipt = resolved["promotion_receipt"]
    remote = resolved["remote_proof"]
    return {
        "promotion_receipt_digest": receipt["receipt_digest"],
        "official_certificate_digest": receipt["official_certificate_digest"],
        "git_object_format": receipt["object_format"],
        "commit_oid": receipt["commit"]["oid"],
        "tree_digest": receipt["tree_digest"],
        "completed_digest": receipt["completed_digest"],
        "annotated_tag_oid": receipt["annotated_tag"]["oid"],
        "remote_proof_digest": remote["proof_digest"],
        "remote_main_oid": remote["remote_main"]["oid"],
        "high_water_tag_oid": remote["high_water_tag_object"]["oid"],
    }


PUBLICATION_DIGESTS = _published_proof_fields(_promotion_payload())


def _resign_remote_proof(proof):
    proof["proof_digest"] = pipeline.content_digest({
        key: value for key, value in proof.items() if key != "proof_digest"
    })


def _resign_promotion_receipt(receipt):
    receipt["receipt_digest"] = pipeline.content_digest({
        key: value for key, value in receipt.items() if key != "receipt_digest"
    })


def _resign_repair_binding(binding):
    binding["binding_digest"] = pipeline.content_digest({
        key: value for key, value in binding.items() if key != "binding_digest"
    })


def _repair_binding(
    *,
    parent_artifact_hash=ARTIFACT_A,
    parent_manifest_digest=MANIFEST_A,
    output_artifact_hash=ARTIFACT_B,
    output_manifest_digest=MANIFEST_B,
):
    repair = build_mechanical_repair_output(
        input_policy=INPUT_POLICY_BYTES,
        proposed_policy=OUTPUT_POLICY_BYTES,
        input_national_bot=NATIONAL_BOT_BYTES,
        proposed_national_bot=NATIONAL_BOT_BYTES,
        input_precompute=PRECOMPUTE_BYTES,
        proposed_precompute=PRECOMPUTE_BYTES,
    )
    return pipeline.build_mechanical_repair_binding(
        parent_artifact_hash=parent_artifact_hash,
        parent_manifest_digest=parent_manifest_digest,
        output_artifact_hash=output_artifact_hash,
        output_manifest_digest=output_manifest_digest,
        mechanical_repair_receipt=repair.receipt,
        resolver_digest=REPAIR_RESOLVER_DIGEST,
        input_policy_bytes=INPUT_POLICY_BYTES,
        output_policy_bytes=OUTPUT_POLICY_BYTES,
        input_national_bot_bytes=NATIONAL_BOT_BYTES,
        output_national_bot_bytes=NATIONAL_BOT_BYTES,
        input_precompute_bytes=PRECOMPUTE_BYTES,
        output_precompute_bytes=PRECOMPUTE_BYTES,
    )


def _repair_policy_resolver(artifact_hash, manifest_digest):
    return {
        "artifact_hash": artifact_hash,
        "manifest_digest": manifest_digest,
        "members": {
            "national_bot.py": NATIONAL_BOT_BYTES,
            "policy.py": (
                INPUT_POLICY_BYTES
                if artifact_hash == ARTIFACT_A
                else OUTPUT_POLICY_BYTES
            ),
            "precompute.py": PRECOMPUTE_BYTES,
        },
        "resolver_digest": REPAIR_RESOLVER_DIGEST,
    }


def _promotion_authority_resolver(receipt_digest, resolver_digest):
    return deepcopy(PROMOTION_AUTHORITY_PACKAGES[(receipt_digest, resolver_digest)])


def _apply(
    state,
    event_type,
    event_id,
    work_item_id,
    *,
    candidate_id=None,
    artifact_hash=None,
    payload=None,
):
    event = pipeline.build_event(
        state,
        event_type,
        event_id=event_id,
        work_item_id=work_item_id,
        candidate_id=candidate_id,
        artifact_hash=artifact_hash,
        payload=payload,
    )
    return pipeline.reduce_event(
        state,
        event,
        mechanical_repair_policy_resolver=(
            _repair_policy_resolver if event_type == "repair_child_created" else None
        ),
        promotion_authority_resolver=(
            _promotion_authority_resolver if event_type == "candidate_promoted" else None
        ),
    ), event


def _queue_and_start(state, work_item_id, prefix):
    state, first = _apply(state, "draft_queued", f"{prefix}-queued", work_item_id)
    state, second = _apply(state, "producer_started", f"{prefix}-started", work_item_id)
    return state, [first, second]


def _seal(state, work_item_id, prefix, candidate_id, artifact_hash, manifest_digest):
    return _apply(
        state,
        "artifact_sealed",
        f"{prefix}-sealed",
        work_item_id,
        candidate_id=candidate_id,
        artifact_hash=artifact_hash,
        payload={"manifest_digest": manifest_digest},
    )


def _sealed_candidate(
    state,
    *,
    work_item_id="draft-a",
    prefix="a",
    candidate_id="candidate-a",
    artifact_hash=ARTIFACT_A,
    manifest_digest=MANIFEST_A,
):
    state, events = _queue_and_start(state, work_item_id, prefix)
    state, sealed = _seal(
        state,
        work_item_id,
        prefix,
        candidate_id,
        artifact_hash,
        manifest_digest,
    )
    return state, [*events, sealed]


def _item(state, work_item_id):
    return next(row for row in state["items"] if row["work_item_id"] == work_item_id)


def test_initial_projection_is_unpublished_content_bound_and_deterministic():
    first = pipeline.initial_projection(TARGET)
    second = pipeline.initial_projection(dict(reversed(list(TARGET.items()))))

    assert first == second
    assert first["queue_revision"] == 0
    assert first["event_ids"] == []
    assert first["event_digests"] == []
    assert first["target_published"] is False
    assert first["promoted_candidate_id"] is None
    assert first["published_identity"] is None
    assert first["target_identity"]["canonical_tag"] == bot_tag(STRICT_TARGET_V)
    assert first["projection_digest"] == pipeline.content_digest({
        key: value for key, value in first.items() if key != "projection_digest"
    })

    tampered = deepcopy(first)
    tampered["target_identity"]["canonical_tag"] = "national-bot-v999"
    with pytest.raises(pipeline.PipelineContractError):
        pipeline.validate_projection(tampered)


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ({**TARGET, "extra": True}, "fields"),
        ({**TARGET, "generation_ordinal": 0}, "inconsistent"),
        ({**TARGET, "canonical_bot_name": "national_v144"}, "inconsistent"),
        ({**TARGET, "canonical_tag": "national_v143"}, "inconsistent"),
        ({**TARGET, "workflow_run_id": ""}, "workflow_run_id"),
        ({**TARGET, "target_lease_digest": "7" * 63}, "lease digest"),
    ],
)
def test_target_identity_is_exact_and_backend_owned(target, message):
    with pytest.raises(pipeline.PipelineContractError, match=message):
        pipeline.initial_projection(target)


def test_producer_lifecycle_seals_once_and_enters_consumer_queue():
    state = pipeline.initial_projection(TARGET)
    state, _ = _apply(state, "draft_queued", "draft-queued", "draft-a")
    queued = _item(state, "draft-a")
    assert (queued["macro_state"], queued["substate"]) == ("producing", "queued")
    assert queued["allowed_actions"] == ["claim_production"]
    assert queued["blocked_reasons"] == ["artifact_not_sealed"]

    state, _ = _apply(state, "producer_started", "producer-started", "draft-a")
    assert _item(state, "draft-a")["producer_attempt"] == 1
    state, _ = _seal(
        state,
        "draft-a",
        "candidate-a",
        "candidate-a",
        ARTIFACT_A,
        MANIFEST_A,
    )
    sealed = _item(state, "draft-a")
    assert (sealed["macro_state"], sealed["substate"]) == (
        "awaiting_validation",
        "queued",
    )
    assert sealed["candidate_id"] == "candidate-a"
    assert sealed["artifact_hash"] == ARTIFACT_A
    assert sealed["manifest_digest"] == MANIFEST_A
    assert sealed["allowed_actions"][0] == "claim_validation"

    # Sealing again is a state regression even with identical bytes.
    event = pipeline.build_event(
        state,
        "artifact_sealed",
        event_id="candidate-a-sealed-again",
        work_item_id="draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload={"manifest_digest": MANIFEST_A},
    )
    with pytest.raises(pipeline.PipelineContractError, match="only a running producer"):
        pipeline.reduce_event(state, event)


def test_producer_retry_and_backpressure_are_explicit_nonterminal_states():
    state = pipeline.initial_projection(TARGET)
    state, _ = _queue_and_start(state, "draft-a", "a")
    state, _ = _apply(
        state,
        "producer_retry_scheduled",
        "a-retry",
        "draft-a",
        payload={"reason": "provider_transport"},
    )
    assert _item(state, "draft-a")["substate"] == "retry"
    state, _ = _apply(
        state,
        "producer_backpressured",
        "a-backpressure",
        "draft-a",
        payload={"reason": "sealed_queue_capacity"},
    )
    blocked = _item(state, "draft-a")
    assert blocked["substate"] == "backpressured"
    assert blocked["allowed_actions"] == ["release_backpressure"]
    assert "producer_backpressured" in blocked["blocked_reasons"]
    state, _ = _apply(
        state,
        "producer_backpressure_released",
        "a-release",
        "draft-a",
    )
    assert _item(state, "draft-a")["substate"] == "queued"


def test_infrastructure_retry_preserves_exact_candidate_and_artifact():
    state, _ = _sealed_candidate(pipeline.initial_projection(TARGET))
    state, _ = _apply(
        state,
        "validation_started",
        "a-validation-1",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
    )
    state, _ = _apply(
        state,
        "validation_infra_blocked",
        "a-infra",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload={"reason": "executor_unavailable"},
    )
    assert _item(state, "draft-a")["substate"] == "infra_blocked"
    state, _ = _apply(
        state,
        "validation_retry_scheduled",
        "a-validation-retry",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload={"reason": "lease_reclaimed_after_death_proof"},
    )
    assert _item(state, "draft-a")["substate"] == "retry"

    mismatched = pipeline.build_event(
        state,
        "validation_started",
        event_id="a-validation-wrong-hash",
        work_item_id="draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_B,
    )
    old_digest = state["projection_digest"]
    with pytest.raises(pipeline.PipelineContractError, match="identity mismatch"):
        pipeline.reduce_event(state, mismatched)
    assert state["projection_digest"] == old_digest
    assert _item(state, "draft-a")["artifact_hash"] == ARTIFACT_A


def test_terminal_rejection_cannot_regress_and_repair_is_a_new_sealed_child():
    state, _ = _sealed_candidate(pipeline.initial_projection(TARGET))
    state, _ = _apply(
        state,
        "validation_started",
        "a-validation",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
    )
    state, _ = _apply(
        state,
        "candidate_rejected",
        "a-rejected",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload={"reason": "compile_contract_failed"},
    )
    parent = _item(state, "draft-a")
    assert (parent["macro_state"], parent["substate"]) == (
        "validation_completed",
        "rejected",
    )
    assert parent["allowed_actions"] == ["create_repair_child"]

    invalid_restart = pipeline.build_event(
        state,
        "validation_started",
        event_id="a-invalid-restart",
        work_item_id="draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
    )
    with pytest.raises(pipeline.PipelineContractError, match="terminal artifact"):
        pipeline.reduce_event(state, invalid_restart)

    state, _ = _apply(
        state,
        "repair_child_created",
        "repair-b-created",
        "repair-b",
        candidate_id="candidate-b",
        artifact_hash=ARTIFACT_B,
        payload={
            "parent_work_item_id": "draft-a",
            "parent_candidate_id": "candidate-a",
            "parent_artifact_hash": ARTIFACT_A,
            "manifest_digest": MANIFEST_B,
            "mechanical_repair_binding": _repair_binding(),
        },
    )
    child = _item(state, "repair-b")
    assert (child["macro_state"], child["substate"]) == (
        "awaiting_validation",
        "queued",
    )
    assert child["candidate_id"] != parent["candidate_id"]
    assert child["artifact_hash"] != parent["artifact_hash"]
    assert child["repair_parent"] == {
        "work_item_id": "draft-a",
        "candidate_id": "candidate-a",
        "artifact_hash": ARTIFACT_A,
    }
    binding = _repair_binding()
    receipt = binding["mechanical_repair_receipt"]
    assert child["mechanical_repair_binding_digest"] == binding["binding_digest"]
    assert child["mechanical_repair_receipt_digest"] == receipt["receipt_digest"]
    assert binding["mechanical_repair_receipt_digest"] == receipt["receipt_digest"]
    assert child["mechanical_repair_semantic_digest"] == receipt[
        "policy_semantic_identity"
    ]["semantic_digest"]
    assert child["mechanical_repair_detector_identity_digest"] == receipt[
        "policy_semantic_identity"
    ]["detector_identity_digest"]
    assert child["mechanical_repair_input_policy_sha256"] == receipt["input"][
        "policy_sha256"
    ]
    assert child["mechanical_repair_output_policy_sha256"] == receipt["output"][
        "policy_sha256"
    ]


@pytest.mark.parametrize(
    ("candidate_id", "artifact_hash", "match"),
    [
        ("candidate-a", ARTIFACT_B, "reused parent identity"),
        ("candidate-b", ARTIFACT_A, "reused parent identity"),
    ],
)
def test_repair_child_cannot_reuse_parent_id_or_hash(candidate_id, artifact_hash, match):
    state, _ = _sealed_candidate(pipeline.initial_projection(TARGET))
    state, _ = _apply(
        state,
        "validation_started",
        "a-validation",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
    )
    state, _ = _apply(
        state,
        "candidate_rejected",
        "a-rejected",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload={"reason": "mechanical_failure"},
    )
    event = pipeline.build_event(
        state,
        "repair_child_created",
        event_id="invalid-repair",
        work_item_id="repair-b",
        candidate_id=candidate_id,
        artifact_hash=artifact_hash,
        payload={
            "parent_work_item_id": "draft-a",
            "parent_candidate_id": "candidate-a",
            "parent_artifact_hash": ARTIFACT_A,
            "manifest_digest": MANIFEST_B,
            "mechanical_repair_binding": _repair_binding(
                output_artifact_hash=artifact_hash,
            ),
        },
    )
    with pytest.raises(pipeline.PipelineContractError, match=match):
        pipeline.reduce_event(
            state,
            event,
            mechanical_repair_policy_resolver=_repair_policy_resolver,
        )


def test_repair_child_requires_resolved_parent_and_child_policy_bytes():
    state, _ = _sealed_candidate(pipeline.initial_projection(TARGET))
    state, _ = _apply(
        state,
        "validation_started",
        "a-validation",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
    )
    state, _ = _apply(
        state,
        "candidate_rejected",
        "a-rejected",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload={"reason": "mechanical_failure"},
    )
    event = pipeline.build_event(
        state,
        "repair_child_created",
        event_id="repair-resolver-required",
        work_item_id="repair-b",
        candidate_id="candidate-b",
        artifact_hash=ARTIFACT_B,
        payload={
            "parent_work_item_id": "draft-a",
            "parent_candidate_id": "candidate-a",
            "parent_artifact_hash": ARTIFACT_A,
            "manifest_digest": MANIFEST_B,
            "mechanical_repair_binding": _repair_binding(),
        },
    )

    with pytest.raises(
        pipeline.PipelineContractError,
        match="artifact resolver is required",
    ):
        pipeline.reduce_event(state, event)

    def wrong_resolver_identity(artifact_hash, manifest_digest):
        resolution = _repair_policy_resolver(artifact_hash, manifest_digest)
        return {**resolution, "resolver_digest": ARTIFACT_C}

    with pytest.raises(
        pipeline.PipelineContractError,
        match="artifact resolution resolver mismatch",
    ):
        pipeline.reduce_event(
            state,
            event,
            mechanical_repair_policy_resolver=wrong_resolver_identity,
        )

    def wrong_member_path(artifact_hash, manifest_digest):
        resolution = _repair_policy_resolver(artifact_hash, manifest_digest)
        members = dict(resolution["members"])
        members["helper.py"] = members.pop("precompute.py")
        return {**resolution, "members": members}

    with pytest.raises(
        pipeline.PipelineContractError,
        match="artifact resolution members mismatch",
    ):
        pipeline.reduce_event(
            state,
            event,
            mechanical_repair_policy_resolver=wrong_member_path,
        )

    def changed_system_member(artifact_hash, manifest_digest):
        resolution = _repair_policy_resolver(artifact_hash, manifest_digest)
        if artifact_hash != ARTIFACT_A:
            resolution["members"]["national_bot.py"] = b"changed-system-runtime\n"
        return resolution

    with pytest.raises(
        pipeline.PipelineContractError,
        match="output_national_bot_bytes_digest_mismatch",
    ):
        pipeline.reduce_event(
            state,
            event,
            mechanical_repair_policy_resolver=changed_system_member,
        )

    def strategy_changed_child(artifact_hash, manifest_digest):
        return {
            "artifact_hash": artifact_hash,
            "manifest_digest": manifest_digest,
            "members": {
                "national_bot.py": NATIONAL_BOT_BYTES,
                "policy.py": (
                    INPUT_POLICY_BYTES
                    if artifact_hash == ARTIFACT_A
                    else b"def decide(context):\n    return {'intent': 'fold'}\n"
                ),
                "precompute.py": PRECOMPUTE_BYTES,
            },
            "resolver_digest": REPAIR_RESOLVER_DIGEST,
        }

    with pytest.raises(
        pipeline.PipelineContractError,
        match="output_policy_bytes_digest_mismatch",
    ):
        pipeline.reduce_event(
            state,
            event,
            mechanical_repair_policy_resolver=strategy_changed_child,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("input_artifact_hash", ARTIFACT_C, "input_artifact_hash mismatch"),
        ("output_artifact_hash", ARTIFACT_C, "output_artifact_hash mismatch"),
        ("input_policy_sha256", ARTIFACT_C, "input_policy_sha256 mismatch"),
        ("output_policy_sha256", ARTIFACT_C, "output_policy_sha256 mismatch"),
        (
            "input_national_bot_sha256",
            ARTIFACT_C,
            "input_national_bot_sha256 mismatch",
        ),
        (
            "output_national_bot_sha256",
            ARTIFACT_C,
            "output_national_bot_sha256 mismatch",
        ),
        (
            "input_precompute_sha256",
            ARTIFACT_C,
            "input_precompute_sha256 mismatch",
        ),
        (
            "output_precompute_sha256",
            ARTIFACT_C,
            "output_precompute_sha256 mismatch",
        ),
        (
            "detector_identity_digest",
            ARTIFACT_C,
            "detector_identity_digest mismatch",
        ),
        ("semantic_digest", ARTIFACT_C, "semantic_digest mismatch"),
        (
            "mechanical_repair_receipt_digest",
            ARTIFACT_C,
            "mechanical_repair_receipt_digest mismatch",
        ),
    ],
)
def test_repair_child_cross_binds_artifact_policy_detector_and_semantic_hashes(
    field,
    value,
    message,
):
    state, _ = _sealed_candidate(pipeline.initial_projection(TARGET))
    state, _ = _apply(
        state,
        "validation_started",
        "a-validation",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
    )
    state, _ = _apply(
        state,
        "candidate_rejected",
        "a-rejected",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload={"reason": "mechanical_failure"},
    )
    binding = deepcopy(_repair_binding())
    binding[field] = value
    _resign_repair_binding(binding)
    event = pipeline.build_event(
        state,
        "repair_child_created",
        event_id=f"repair-binding-{field}",
        work_item_id="repair-b",
        candidate_id="candidate-b",
        artifact_hash=ARTIFACT_B,
        payload={
            "parent_work_item_id": "draft-a",
            "parent_candidate_id": "candidate-a",
            "parent_artifact_hash": ARTIFACT_A,
            "manifest_digest": MANIFEST_B,
            "mechanical_repair_binding": binding,
        },
    )

    with pytest.raises(pipeline.PipelineContractError, match=message):
        pipeline.reduce_event(
            state,
            event,
            mechanical_repair_policy_resolver=_repair_policy_resolver,
        )


def test_promotion_publishes_one_identity_and_fences_all_same_target_work():
    state = pipeline.initial_projection(TARGET)
    state, _ = _sealed_candidate(state)
    state, _ = _apply(state, "draft_queued", "b-queued", "draft-b")
    state, _ = _sealed_candidate(
        state,
        work_item_id="draft-c",
        prefix="c",
        candidate_id="candidate-c",
        artifact_hash=ARTIFACT_C,
        manifest_digest=MANIFEST_C,
    )
    state, _ = _apply(
        state,
        "validation_started",
        "a-validation",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
    )
    state, _ = _apply(
        state,
        "candidate_promoted",
        "a-promoted",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload=_promotion_payload(),
    )

    assert state["target_published"] is True
    assert state["promoted_candidate_id"] == "candidate-a"
    assert state["published_identity"] == {
        **TARGET,
        "candidate_id": "candidate-a",
        "artifact_hash": ARTIFACT_A,
        **PUBLICATION_DIGESTS,
    }
    assert _item(state, "draft-a")["substate"] == "promoted"
    for work_item_id in ("draft-b", "draft-c"):
        fenced = _item(state, work_item_id)
        assert (fenced["macro_state"], fenced["substate"]) == (
            "validation_completed",
            "superseded",
        )
        assert fenced["superseded_by_candidate_id"] == "candidate-a"
        assert fenced["superseded_by_artifact_hash"] == ARTIFACT_A
        assert fenced["supersession_fence_digest"] == PUBLICATION_DIGESTS[
            "promotion_receipt_digest"
        ]
        assert fenced["allowed_actions"] == []
    # The unsealed draft is fenced without inventing an artifact identity.
    assert _item(state, "draft-b")["candidate_id"] is None
    assert _item(state, "draft-b")["artifact_hash"] is None

    future = pipeline.build_event(
        state,
        "draft_queued",
        event_id="post-promotion-draft",
        work_item_id="draft-d",
    )
    with pytest.raises(pipeline.PipelineContractError, match="published target"):
        pipeline.reduce_event(state, future)


def test_promotion_requires_complete_content_bound_publication_identity():
    state, _ = _sealed_candidate(pipeline.initial_projection(TARGET))
    state, _ = _apply(
        state,
        "validation_started",
        "a-validation",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
    )
    incomplete = pipeline.build_event(
        state,
        "candidate_promoted",
        event_id="a-incomplete-promotion",
        work_item_id="draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload={"reason": "text_is_not_publication_proof"},
    )
    with pytest.raises(pipeline.PipelineContractError, match="payload is not exact"):
        pipeline.reduce_event(state, incomplete)

    remote = _remote_proof()
    receipt = _promotion_receipt(remote_proof=remote)
    receipt["official_certificate_digest"] = "2" * 63
    _resign_promotion_receipt(receipt)
    invalid_digest_payload = _promotion_payload_from_package(remote, receipt)
    invalid_digest = pipeline.build_event(
        state,
        "candidate_promoted",
        event_id="a-invalid-promotion-digest",
        work_item_id="draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload=invalid_digest_payload,
    )
    with pytest.raises(pipeline.PipelineContractError, match="official certificate digest"):
        pipeline.reduce_event(
            state,
            invalid_digest,
            promotion_authority_resolver=_promotion_authority_resolver,
        )


def test_promotion_reference_requires_non_echoing_authority_resolution():
    state, _ = _sealed_candidate(pipeline.initial_projection(TARGET))
    state, _ = _apply(
        state,
        "validation_started",
        "a-validation",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
    )
    payload = _promotion_payload()
    event = pipeline.build_event(
        state,
        "candidate_promoted",
        event_id="a-promotion-no-authority",
        work_item_id="draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload=payload,
    )

    with pytest.raises(pipeline.PipelineContractError, match="resolver is required"):
        pipeline.reduce_event(state, event)

    def mismatched_resolver(receipt_digest, resolver_digest):
        resolved = _promotion_authority_resolver(receipt_digest, resolver_digest)
        resolved["resolver_digest"] = "8" * 64
        return resolved

    with pytest.raises(pipeline.PipelineContractError, match="identity mismatch"):
        pipeline.reduce_event(
            state,
            event,
            promotion_authority_resolver=mismatched_resolver,
        )

    def generic_official_resolver(receipt_digest, resolver_digest):
        resolved = _promotion_authority_resolver(receipt_digest, resolver_digest)
        resolved["official_policy_id"] = "generic-official-envelope"
        return resolved

    with pytest.raises(pipeline.PipelineContractError, match="official-full-v5"):
        pipeline.reduce_event(
            state,
            event,
            promotion_authority_resolver=generic_official_resolver,
        )


def test_promotion_accepts_explicit_sha1_and_sha256_object_formats():
    sha1 = _promotion_payload()
    resolved_sha1 = _promotion_authority_resolver(
        sha1["promotion_receipt_digest"], sha1["resolver_digest"]
    )
    assert resolved_sha1["remote_proof"]["object_format"] == "sha1"
    assert resolved_sha1["promotion_receipt"]["object_format"] == "sha1"
    assert len(resolved_sha1["promotion_receipt"]["commit"]["oid"]) == 40

    sha256_commit = {"object_format": "sha256", "oid": "a" * 64}
    sha256_remote_main = {"object_format": "sha256", "oid": "b" * 64}
    sha256_bot_tag = {"object_format": "sha256", "oid": "c" * 64}
    sha256_high_water = {"object_format": "sha256", "oid": "d" * 64}
    remote = _remote_proof(
        object_format="sha256",
        commit=sha256_commit,
        remote_main=sha256_remote_main,
        bot_tag_object=sha256_bot_tag,
        high_water_tag_object=sha256_high_water,
    )
    receipt = _promotion_receipt(
        remote_proof=remote,
        object_format="sha256",
        commit=sha256_commit,
        annotated_tag=sha256_bot_tag,
    )
    assert remote["object_format"] == receipt["object_format"] == "sha256"
    assert len(receipt["commit"]["oid"]) == 64


@pytest.mark.parametrize(
    "tamper",
    ["commit", "tag", "remote_peeled_commit", "receipt_object_format"],
)
def test_promotion_parser_cross_binds_receipt_commit_tag_and_remote_proof(tamper):
    state, _ = _sealed_candidate(pipeline.initial_projection(TARGET))
    state, _ = _apply(
        state,
        "validation_started",
        "a-validation",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
    )
    base_payload = _promotion_payload()
    resolved = _promotion_authority_resolver(
        base_payload["promotion_receipt_digest"], base_payload["resolver_digest"]
    )
    remote = resolved["remote_proof"]
    receipt = resolved["promotion_receipt"]
    if tamper == "commit":
        receipt["commit"]["oid"] = "8" * 40
    elif tamper == "tag":
        receipt["annotated_tag"]["oid"] = "8" * 40
    elif tamper == "remote_peeled_commit":
        remote["bot_tag_peeled_commit"]["oid"] = "8" * 40
        _resign_remote_proof(remote)
        receipt["remote_proof_digest"] = remote["proof_digest"]
    else:
        receipt["object_format"] = "sha256"
    _resign_promotion_receipt(receipt)
    payload = _promotion_payload_from_package(remote, receipt)
    event = pipeline.build_event(
        state,
        "candidate_promoted",
        event_id=f"promotion-cross-bind-{tamper}",
        work_item_id="draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload=payload,
    )

    with pytest.raises(pipeline.PipelineContractError, match="(cross-bound|formats differ)"):
        pipeline.reduce_event(
            state,
            event,
            promotion_authority_resolver=_promotion_authority_resolver,
        )


@pytest.mark.parametrize(
    ("object_format", "oid"),
    [
        ("sha1", "1" * 64),
        ("sha256", "1" * 40),
        ("sha512", "1" * 128),
    ],
)
def test_promotion_rejects_oid_length_or_unknown_explicit_object_format(
    object_format,
    oid,
):
    malformed = {"object_format": object_format, "oid": oid}
    with pytest.raises(pipeline.PipelineContractError):
        _remote_proof(object_format=object_format, commit=malformed)


def test_unpublished_target_never_exposes_published_identity():
    state, _ = _sealed_candidate(pipeline.initial_projection(TARGET))
    assert state["target_identity"] == TARGET
    assert state["target_published"] is False
    assert state["published_identity"] is None
    assert state["promoted_candidate_id"] is None

    forged = deepcopy(state)
    forged["published_identity"] = {
        **TARGET,
        "candidate_id": "candidate-a",
        "artifact_hash": ARTIFACT_A,
    }
    forged["projection_digest"] = pipeline.content_digest({
        key: value for key, value in forged.items() if key != "projection_digest"
    })
    with pytest.raises(pipeline.PipelineContractError, match="unpublished target"):
        pipeline.validate_projection(forged)


def test_quarantine_is_closed_without_publishing():
    state, _ = _sealed_candidate(pipeline.initial_projection(TARGET))
    state, _ = _apply(
        state,
        "candidate_quarantined",
        "a-candidate-quarantined",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload={"reason": "operator_or_target_fence"},
    )
    row = _item(state, "draft-a")
    assert row["macro_state"] == "validation_completed"
    assert row["substate"] == "quarantined"
    assert state["target_published"] is False
    assert state["published_identity"] is None


def test_explicit_supersession_binds_one_live_sealed_superseder_and_cas_fence():
    state, _ = _sealed_candidate(pipeline.initial_projection(TARGET))
    state, _ = _sealed_candidate(
        state,
        work_item_id="draft-b",
        prefix="b",
        candidate_id="candidate-b",
        artifact_hash=ARTIFACT_B,
        manifest_digest=MANIFEST_B,
    )
    fence = pipeline.supersession_fence_digest(
        state,
        work_item_id="draft-a",
        superseded_by_candidate_id="candidate-b",
        superseded_by_artifact_hash=ARTIFACT_B,
    )
    state, _ = _apply(
        state,
        "candidate_superseded",
        "a-superseded-by-b",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload={
            "reason": "repair_child_selected",
            "superseded_by_candidate_id": "candidate-b",
            "superseded_by_artifact_hash": ARTIFACT_B,
            "promotion_fence_digest": fence,
        },
    )
    row = _item(state, "draft-a")
    assert (row["macro_state"], row["substate"]) == (
        "validation_completed",
        "superseded",
    )
    assert row["superseded_by_candidate_id"] == "candidate-b"
    assert row["superseded_by_artifact_hash"] == ARTIFACT_B
    assert row["supersession_fence_digest"] == fence
    assert state["target_published"] is False


def test_unsealed_direct_supersession_and_tampered_fence_fail_closed():
    unsealed, _ = _apply(
        pipeline.initial_projection(TARGET),
        "draft_queued",
        "draft-queued",
        "draft-a",
    )
    assert _item(unsealed, "draft-a")["allowed_actions"] == ["claim_production"]
    invalid = pipeline.build_event(
        unsealed,
        "candidate_superseded",
        event_id="unsealed-supersede",
        work_item_id="draft-a",
        payload={
            "reason": "not_allowed",
            "superseded_by_candidate_id": "candidate-b",
            "superseded_by_artifact_hash": ARTIFACT_B,
            "promotion_fence_digest": "8" * 64,
        },
    )
    with pytest.raises(pipeline.PipelineContractError, match="sealed validation candidate"):
        pipeline.reduce_event(unsealed, invalid)

    state, _ = _sealed_candidate(pipeline.initial_projection(TARGET))
    state, _ = _sealed_candidate(
        state,
        work_item_id="draft-b",
        prefix="b",
        candidate_id="candidate-b",
        artifact_hash=ARTIFACT_B,
        manifest_digest=MANIFEST_B,
    )
    tampered = pipeline.build_event(
        state,
        "candidate_superseded",
        event_id="tampered-supersede",
        work_item_id="draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload={
            "reason": "repair_child_selected",
            "superseded_by_candidate_id": "candidate-b",
            "superseded_by_artifact_hash": ARTIFACT_B,
            "promotion_fence_digest": "8" * 64,
        },
    )
    with pytest.raises(pipeline.PipelineContractError, match="fence mismatch"):
        pipeline.reduce_event(state, tampered)


def test_unknown_missing_tampered_and_out_of_order_events_fail_closed():
    state = pipeline.initial_projection(TARGET)
    valid = pipeline.build_event(
        state,
        "draft_queued",
        event_id="draft-queued",
        work_item_id="draft-a",
    )
    cases = []

    unknown = deepcopy(valid)
    unknown["event_type"] = "invented_state"
    unknown["event_digest"] = pipeline.content_digest({
        key: value for key, value in unknown.items() if key != "event_digest"
    })
    cases.append(unknown)

    missing = deepcopy(valid)
    missing.pop("target_identity_digest")
    cases.append(missing)

    tampered = deepcopy(valid)
    tampered["work_item_id"] = "different-draft"
    cases.append(tampered)

    skipped = deepcopy(valid)
    skipped["queue_revision"] = 2
    skipped["event_digest"] = pipeline.content_digest({
        key: value for key, value in skipped.items() if key != "event_digest"
    })
    cases.append(skipped)

    wrong_previous = deepcopy(valid)
    wrong_previous["previous_projection_digest"] = ARTIFACT_A
    wrong_previous["event_digest"] = pipeline.content_digest({
        key: value for key, value in wrong_previous.items() if key != "event_digest"
    })
    cases.append(wrong_previous)

    wrong_epoch = deepcopy(valid)
    wrong_epoch["target_identity"]["evaluation_epoch"] = "national_tcp_policy_v1:epoch-2"
    wrong_epoch["target_identity_digest"] = pipeline.content_digest(
        wrong_epoch["target_identity"]
    )
    wrong_epoch["event_digest"] = pipeline.content_digest({
        key: value for key, value in wrong_epoch.items() if key != "event_digest"
    })
    cases.append(wrong_epoch)

    for event in cases:
        with pytest.raises(pipeline.PipelineContractError):
            pipeline.reduce_event(state, event)
        assert state == pipeline.initial_projection(TARGET)


def test_event_id_is_unique_across_complete_projection_history():
    state, _ = _apply(
        pipeline.initial_projection(TARGET),
        "draft_queued",
        "event-one",
        "draft-a",
    )
    replay_id = pipeline.build_event(
        state,
        "producer_started",
        event_id="event-one",
        work_item_id="draft-a",
    )
    with pytest.raises(pipeline.PipelineContractError, match="already consumed"):
        pipeline.reduce_event(state, replay_id)


def test_projection_actions_and_block_reasons_cannot_be_forged():
    state, _ = _apply(
        pipeline.initial_projection(TARGET),
        "draft_queued",
        "draft-queued",
        "draft-a",
    )
    forged = deepcopy(state)
    forged["items"][0]["allowed_actions"] = ["promote_candidate"]
    forged["projection_digest"] = pipeline.content_digest({
        key: value for key, value in forged.items() if key != "projection_digest"
    })
    with pytest.raises(pipeline.PipelineContractError, match="reducer-owned"):
        pipeline.validate_projection(forged)


def test_replay_of_bound_event_stream_is_byte_identical():
    state = pipeline.initial_projection(TARGET)
    events = []
    state, produced = _queue_and_start(state, "draft-a", "a")
    events.extend(produced)
    state, event = _seal(
        state,
        "draft-a",
        "a",
        "candidate-a",
        ARTIFACT_A,
        MANIFEST_A,
    )
    events.append(event)
    state, event = _apply(
        state,
        "validation_started",
        "a-validation",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
    )
    events.append(event)
    state, event = _apply(
        state,
        "candidate_rejected",
        "a-rejected",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload={"reason": "strategy_regression"},
    )
    events.append(event)

    assert pipeline.reduce_events(TARGET, events) == state
    assert pipeline.canonical_json(pipeline.reduce_events(TARGET, events)) == pipeline.canonical_json(state)
    assert pipeline.validate_projection(state, events=events) == state
    with pytest.raises(pipeline.PipelineContractError, match="complete event replay"):
        pipeline.validate_projection(state, events=events[:-1])


def test_complete_promotion_replay_requires_and_forwards_authority_resolver():
    state = pipeline.initial_projection(TARGET)
    events = []
    state, produced = _queue_and_start(state, "draft-a", "a")
    events.extend(produced)
    state, event = _seal(
        state,
        "draft-a",
        "a",
        "candidate-a",
        ARTIFACT_A,
        MANIFEST_A,
    )
    events.append(event)
    state, event = _apply(
        state,
        "validation_started",
        "a-validation",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
    )
    events.append(event)
    state, event = _apply(
        state,
        "candidate_promoted",
        "a-promoted",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload=_promotion_payload(),
    )
    events.append(event)

    assert pipeline.reduce_events(
        TARGET,
        events,
        promotion_authority_resolver=_promotion_authority_resolver,
    ) == state
    assert pipeline.validate_projection(
        state,
        events=events,
        promotion_authority_resolver=_promotion_authority_resolver,
    ) == state
    with pytest.raises(
        pipeline.PipelineContractError,
        match="promotion authority resolver is required",
    ):
        pipeline.validate_projection(state, events=events)


def test_complete_projection_replay_forwards_mechanical_repair_resolver():
    state = pipeline.initial_projection(TARGET)
    events = []
    state, produced = _queue_and_start(state, "draft-a", "a")
    events.extend(produced)
    state, event = _seal(
        state,
        "draft-a",
        "a",
        "candidate-a",
        ARTIFACT_A,
        MANIFEST_A,
    )
    events.append(event)
    state, event = _apply(
        state,
        "validation_started",
        "a-validation",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
    )
    events.append(event)
    state, event = _apply(
        state,
        "candidate_rejected",
        "a-rejected",
        "draft-a",
        candidate_id="candidate-a",
        artifact_hash=ARTIFACT_A,
        payload={"reason": "mechanical_failure"},
    )
    events.append(event)
    state, repair_event = _apply(
        state,
        "repair_child_created",
        "repair-b-created",
        "repair-b",
        candidate_id="candidate-b",
        artifact_hash=ARTIFACT_B,
        payload={
            "parent_work_item_id": "draft-a",
            "parent_candidate_id": "candidate-a",
            "parent_artifact_hash": ARTIFACT_A,
            "manifest_digest": MANIFEST_B,
            "mechanical_repair_binding": _repair_binding(),
        },
    )
    events.append(repair_event)

    assert pipeline.reduce_events(
        TARGET,
        events,
        mechanical_repair_policy_resolver=_repair_policy_resolver,
    ) == state
    assert pipeline.validate_projection(
        state,
        events=events,
        mechanical_repair_policy_resolver=_repair_policy_resolver,
    ) == state
    with pytest.raises(
        pipeline.PipelineContractError,
        match="artifact resolver is required",
    ):
        pipeline.validate_projection(state, events=events)


def test_projection_digest_or_event_ledger_tamper_is_rejected():
    state, _ = _apply(
        pipeline.initial_projection(TARGET),
        "draft_queued",
        "draft-queued",
        "draft-a",
    )
    bad_digest = deepcopy(state)
    bad_digest["projection_digest"] = ARTIFACT_A
    with pytest.raises(pipeline.PipelineContractError, match="digest mismatch"):
        pipeline.validate_projection(bad_digest)

    bad_ledger = deepcopy(state)
    bad_ledger["event_ids"] = []
    bad_ledger["projection_digest"] = pipeline.content_digest({
        key: value for key, value in bad_ledger.items() if key != "projection_digest"
    })
    with pytest.raises(pipeline.PipelineContractError, match="event id ledger"):
        pipeline.validate_projection(bad_ledger)

    bad_event_digest_ledger = deepcopy(state)
    bad_event_digest_ledger["event_digests"] = []
    bad_event_digest_ledger["projection_digest"] = pipeline.content_digest({
        key: value
        for key, value in bad_event_digest_ledger.items()
        if key != "projection_digest"
    })
    with pytest.raises(pipeline.PipelineContractError, match="event digest ledger"):
        pipeline.validate_projection(bad_event_digest_ledger)

    malformed_event_digest_ledger = deepcopy(state)
    malformed_event_digest_ledger["event_digests"] = [{"not": "a digest"}]
    malformed_event_digest_ledger["projection_digest"] = pipeline.content_digest({
        key: value
        for key, value in malformed_event_digest_ledger.items()
        if key != "projection_digest"
    })
    with pytest.raises(pipeline.PipelineContractError, match="event digest ledger"):
        pipeline.validate_projection(malformed_event_digest_ledger)

    impossible_revision = deepcopy(state)
    impossible_revision["items"][0]["created_revision"] = 99
    impossible_revision["items"][0]["updated_revision"] = 101
    impossible_revision["projection_digest"] = pipeline.content_digest({
        key: value
        for key, value in impossible_revision.items()
        if key != "projection_digest"
    })
    with pytest.raises(pipeline.PipelineContractError, match="exceeds the projection ledger"):
        pipeline.validate_projection(impossible_revision)

    unknown_last_event = deepcopy(state)
    unknown_last_event["items"][0]["last_event_id"] = "never-in-ledger"
    unknown_last_event["items"][0]["last_event_digest"] = "9" * 64
    unknown_last_event["projection_digest"] = pipeline.content_digest({
        key: value for key, value in unknown_last_event.items() if key != "projection_digest"
    })
    with pytest.raises(pipeline.PipelineContractError, match="last event is not ledger-bound"):
        pipeline.validate_projection(unknown_last_event)


def test_event_ledger_order_is_bound_to_each_items_updated_revision():
    state, _ = _queue_and_start(pipeline.initial_projection(TARGET), "draft-a", "a")
    reordered = deepcopy(state)
    reordered["event_ids"].reverse()
    reordered["event_digests"].reverse()
    reordered["projection_digest"] = pipeline.content_digest({
        key: value for key, value in reordered.items() if key != "projection_digest"
    })
    with pytest.raises(pipeline.PipelineContractError, match="last event is not ledger-bound"):
        pipeline.validate_projection(reordered)


def test_same_base_concurrency_exposes_store_cas_fence_and_stale_loser_fails():
    base = pipeline.initial_projection(TARGET)
    precondition = pipeline.projection_cas_precondition(base)
    first = pipeline.build_event(
        base,
        "draft_queued",
        event_id="writer-a",
        work_item_id="draft-a",
    )
    second = pipeline.build_event(
        base,
        "draft_queued",
        event_id="writer-b",
        work_item_id="draft-b",
    )

    # A pure reducer can derive both branches. The durable owner must atomically
    # compare the shared precondition and commit exactly one.
    winner = pipeline.reduce_event(base, first)
    alternate = pipeline.reduce_event(base, second)
    assert winner["queue_revision"] == alternate["queue_revision"] == 1
    assert pipeline.require_projection_cas_precondition(base, **precondition) == base
    with pytest.raises(pipeline.PipelineContractError, match="precondition is stale"):
        pipeline.require_projection_cas_precondition(winner, **precondition)
    with pytest.raises(pipeline.PipelineContractError, match="next projection fence"):
        pipeline.reduce_event(winner, second)
