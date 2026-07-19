"""Pure producer/consumer artifact-pipeline reducer.

This module is deliberately storage-, process-, and poker-semantics free.  It
defines the schema-v1 event/projection contract that a durable owner may later
persist through :mod:`workflow_kernel`.  The reducer never reads a checkpoint,
the filesystem, wall clock, or a live queue.  A mechanical-repair event must be
given a deterministic content-addressed resolver for all three parent/child
executable members.  A promotion event carries only receipt/resolver digests
and likewise requires an independent content-addressed authority resolver;
caller-supplied receipt bytes are never accepted directly.  With the same
resolved evidence, replaying the same events always produces the same
content-addressed projection.

The three macro states belong to one immutable candidate artifact, not to the
existing strict checkpoint ``STAGE_ORDER``:

* ``producing`` -- no sealed artifact exists yet;
* ``awaiting_validation`` -- an immutable artifact is queued/running/retrying;
* ``validation_completed`` -- the artifact is terminal.

An infrastructure retry preserves the exact candidate/artifact identity.  Any
repair is a new sealed child.  Promotion atomically supersedes every other
non-terminal work item for the same canonical target.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Callable, Iterable, Mapping

from mechanical_repair import (
    MechanicalRepairRejected,
    validate_mechanical_repair_receipt_against_artifact_bytes,
)


SCHEMA_VERSION = 1
EVENT_KIND = "producer-consumer-pipeline-event-v1"
PROJECTION_KIND = "producer-consumer-pipeline-projection-v1"
PROJECTION_AUTHORITY = "producer-consumer-pipeline-reducer-v1"
ZERO_DIGEST = "0" * 64
GIT_OBJECT_FORMAT_LENGTHS = {"sha1": 40, "sha256": 64}

MACRO_PRODUCING = "producing"
MACRO_AWAITING_VALIDATION = "awaiting_validation"
MACRO_VALIDATION_COMPLETED = "validation_completed"

PRODUCER_SUBSTATES = frozenset({"queued", "running", "retry", "backpressured"})
VALIDATION_SUBSTATES = frozenset({"queued", "running", "retry", "infra_blocked"})
TERMINAL_SUBSTATES = frozenset({"promoted", "rejected", "quarantined", "superseded"})

EVENT_TYPES = frozenset({
    "draft_queued",
    "producer_started",
    "producer_retry_scheduled",
    "producer_backpressured",
    "producer_backpressure_released",
    "artifact_sealed",
    "validation_started",
    "validation_retry_scheduled",
    "validation_infra_blocked",
    "candidate_promoted",
    "candidate_rejected",
    "candidate_quarantined",
    "candidate_superseded",
    "repair_child_created",
})

_EVENT_KEYS = frozenset({
    "schema_version",
    "kind",
    "event_id",
    "event_type",
    "queue_revision",
    "previous_projection_digest",
    "target_identity",
    "target_identity_digest",
    "work_item_id",
    "candidate_id",
    "artifact_hash",
    "payload",
    "event_digest",
})
_TARGET_KEYS = frozenset({
    "evaluation_epoch",
    "workflow_run_id",
    "target_lease_digest",
    "generation_ordinal",
    "canonical_version",
    "canonical_bot_name",
    "canonical_tag",
})
_PROJECTION_KEYS = frozenset({
    "schema_version",
    "kind",
    "authority",
    "queue_revision",
    "target_identity",
    "target_identity_digest",
    "target_published",
    "promoted_candidate_id",
    "published_identity",
    "event_ids",
    "event_digests",
    "items",
    "projection_digest",
})
_ITEM_KEYS = frozenset({
    "work_item_id",
    "candidate_id",
    "artifact_hash",
    "manifest_digest",
    "macro_state",
    "substate",
    "producer_attempt",
    "validation_attempt",
    "repair_parent",
    "mechanical_repair_binding_digest",
    "mechanical_repair_receipt_digest",
    "mechanical_repair_semantic_digest",
    "mechanical_repair_detector_identity_digest",
    "mechanical_repair_input_policy_sha256",
    "mechanical_repair_output_policy_sha256",
    "superseded_by_candidate_id",
    "superseded_by_artifact_hash",
    "supersession_fence_digest",
    "terminal_reason",
    "created_revision",
    "updated_revision",
    "last_event_id",
    "last_event_digest",
    "allowed_actions",
    "blocked_reasons",
})
_PUBLISHED_IDENTITY_KEYS = frozenset({
    "evaluation_epoch",
    "workflow_run_id",
    "target_lease_digest",
    "generation_ordinal",
    "canonical_version",
    "canonical_bot_name",
    "canonical_tag",
    "candidate_id",
    "artifact_hash",
    "promotion_receipt_digest",
    "official_certificate_digest",
    "git_object_format",
    "commit_oid",
    "tree_digest",
    "completed_digest",
    "annotated_tag_oid",
    "remote_proof_digest",
    "remote_main_oid",
    "high_water_tag_oid",
})
_REPAIR_PARENT_KEYS = frozenset({
    "work_item_id",
    "candidate_id",
    "artifact_hash",
})
_PUBLISHED_PROOF_FIELDS = (
    "promotion_receipt_digest",
    "official_certificate_digest",
    "git_object_format",
    "commit_oid",
    "tree_digest",
    "completed_digest",
    "annotated_tag_oid",
    "remote_proof_digest",
    "remote_main_oid",
    "high_water_tag_oid",
)
_PROMOTION_PAYLOAD_KEYS = frozenset({
    "reason",
    "promotion_receipt_digest",
    "resolver_digest",
})
_PROMOTION_AUTHORITY_RESOLUTION_KEYS = frozenset({
    "authority",
    "official_policy_id",
    "resolver_digest",
    "promotion_receipt",
    "remote_proof",
})
_GIT_OBJECT_KEYS = frozenset({"object_format", "oid"})
_REMOTE_PROOF_KEYS = frozenset({
    "schema",
    "target_identity_digest",
    "object_format",
    "remote_name",
    "remote_main",
    "bot_tag",
    "bot_tag_object",
    "bot_tag_peeled_commit",
    "high_water_tag",
    "high_water_tag_object",
    "high_water_tag_peeled_commit",
    "resolver_digest",
    "verified",
    "commit_reachable_from_remote_main",
    "proof_digest",
})
_PROMOTION_RECEIPT_KEYS = frozenset({
    "schema",
    "target_identity_digest",
    "candidate_id",
    "artifact_hash",
    "official_certificate_digest",
    "object_format",
    "commit",
    "tree_digest",
    "completed_digest",
    "annotated_tag",
    "remote_proof_digest",
    "receipt_digest",
})
_REPAIR_BINDING_KEYS = frozenset({
    "schema",
    "input_artifact_hash",
    "output_artifact_hash",
    "input_manifest_digest",
    "output_manifest_digest",
    "input_policy_sha256",
    "output_policy_sha256",
    "input_national_bot_sha256",
    "output_national_bot_sha256",
    "input_precompute_sha256",
    "output_precompute_sha256",
    "detector_identity_digest",
    "semantic_digest",
    "mechanical_repair_receipt_digest",
    "mechanical_repair_receipt",
    "resolver_digest",
    "binding_digest",
})
_SUPERSESSION_PAYLOAD_KEYS = frozenset({
    "reason",
    "superseded_by_candidate_id",
    "superseded_by_artifact_hash",
    "promotion_fence_digest",
})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,191}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class PipelineContractError(ValueError):
    """An event or projection cannot be accepted without guessing."""


def canonical_json(value: Any) -> str:
    """Return strict canonical JSON or fail on non-JSON/non-finite values."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise PipelineContractError("value is not canonical JSON") from exc


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _json_copy(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _require_exact_keys(value: Any, expected: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(expected):
        raise PipelineContractError(f"{label} fields are not exact")
    return value


def _require_safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise PipelineContractError(f"{label} is invalid")
    return value


def _require_digest(value: Any, label: str, *, allow_zero: bool = False) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise PipelineContractError(f"{label} is not a sha256 digest")
    if not allow_zero and value == ZERO_DIGEST:
        raise PipelineContractError(f"{label} cannot be the zero digest")
    return value


def _require_git_object(value: Any, label: str) -> dict[str, str]:
    observed = _require_exact_keys(value, _GIT_OBJECT_KEYS, label)
    object_format = observed.get("object_format")
    oid = observed.get("oid")
    length = GIT_OBJECT_FORMAT_LENGTHS.get(object_format)
    if (
        length is None
        or not isinstance(oid, str)
        or len(oid) != length
        or re.fullmatch(r"[0-9a-f]+", oid) is None
        or set(oid) == {"0"}
    ):
        raise PipelineContractError(f"{label} is not a valid Git object")
    return {"object_format": str(object_format), "oid": oid}


def _require_git_object_format(value: Any, label: str) -> str:
    if not isinstance(value, str) or value not in GIT_OBJECT_FORMAT_LENGTHS:
        raise PipelineContractError(f"{label} is invalid")
    return value


def validate_target_identity(value: Any) -> dict[str, Any]:
    target = _require_exact_keys(value, _TARGET_KEYS, "target_identity")
    _require_safe_id(target["evaluation_epoch"], "target evaluation_epoch")
    _require_safe_id(target["workflow_run_id"], "target workflow_run_id")
    _require_digest(target["target_lease_digest"], "target lease digest")
    ordinal = target["generation_ordinal"]
    version = target["canonical_version"]
    if (
        isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        or ordinal < 1
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
        or target["canonical_bot_name"] != f"national_v{version}"
        or target["canonical_tag"] != f"national-bot-v{version}"
    ):
        raise PipelineContractError("target_identity is inconsistent")
    return _json_copy(target)


def _projection_unsigned(projection: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in projection.items() if key != "projection_digest"}


def _event_unsigned(event: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key != "event_digest"}


def _item_actions_and_blocks(item: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    macro = item["macro_state"]
    substate = item["substate"]
    if macro == MACRO_PRODUCING:
        actions = {
            "queued": ["claim_production"],
            "running": ["seal_artifact", "schedule_producer_retry"],
            "retry": ["claim_production"],
            "backpressured": ["release_backpressure"],
        }[substate]
        blocks = ["artifact_not_sealed"]
        if substate == "backpressured":
            blocks.append("producer_backpressured")
        return actions, blocks
    if macro == MACRO_AWAITING_VALIDATION:
        actions = {
            "queued": ["claim_validation", "quarantine_candidate", "supersede_candidate"],
            "running": [
                "schedule_validation_retry",
                "mark_validation_infra_blocked",
                "promote_candidate",
                "reject_candidate",
                "quarantine_candidate",
                "supersede_candidate",
            ],
            "retry": ["claim_validation", "quarantine_candidate", "supersede_candidate"],
            "infra_blocked": ["schedule_validation_retry", "quarantine_candidate", "supersede_candidate"],
        }[substate]
        blocks = {
            "queued": ["validation_not_started"],
            "running": ["validation_in_progress"],
            "retry": ["validation_retry_pending"],
            "infra_blocked": ["validation_infrastructure_blocked"],
        }[substate]
        return actions, blocks
    if macro == MACRO_VALIDATION_COMPLETED:
        actions = ["create_repair_child"] if substate == "rejected" else []
        return actions, [f"validation_terminal:{substate}"]
    raise PipelineContractError("item macro state is unknown")


def _refresh_item_projection(item: dict[str, Any]) -> None:
    actions, blocks = _item_actions_and_blocks(item)
    item["allowed_actions"] = actions
    item["blocked_reasons"] = blocks


def _validate_item(value: Any) -> dict[str, Any]:
    item = _require_exact_keys(value, _ITEM_KEYS, "work item")
    _require_safe_id(item["work_item_id"], "work_item_id")
    for key in ("created_revision", "updated_revision", "producer_attempt", "validation_attempt"):
        observed = item[key]
        if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
            raise PipelineContractError(f"work item {key} is invalid")
    if item["created_revision"] < 1 or item["updated_revision"] < item["created_revision"]:
        raise PipelineContractError("work item revision ordering is invalid")
    _require_safe_id(item["last_event_id"], "last_event_id")
    _require_digest(item["last_event_digest"], "last_event_digest")

    macro = item["macro_state"]
    substate = item["substate"]
    candidate = item["candidate_id"]
    artifact = item["artifact_hash"]
    manifest = item["manifest_digest"]
    if macro == MACRO_PRODUCING:
        if substate not in PRODUCER_SUBSTATES or any(
            value is not None for value in (candidate, artifact, manifest)
        ):
            raise PipelineContractError("producing item carries sealed identity")
        if substate in {"running", "retry"} and item["producer_attempt"] < 1:
            raise PipelineContractError("active producer has no attempt")
        if item["validation_attempt"] != 0:
            raise PipelineContractError("unsealed draft has a validation attempt")
    elif macro in {MACRO_AWAITING_VALIDATION, MACRO_VALIDATION_COMPLETED}:
        unsealed_superseded = bool(
            macro == MACRO_VALIDATION_COMPLETED
            and substate == "superseded"
            and candidate is None
            and artifact is None
            and manifest is None
        )
        if (
            substate not in (
                VALIDATION_SUBSTATES
                if macro == MACRO_AWAITING_VALIDATION
                else TERMINAL_SUBSTATES
            )
            or (
                not unsealed_superseded
                and _SAFE_ID.fullmatch(candidate or "") is None
            )
        ):
            raise PipelineContractError("sealed item identity/state is invalid")
        if not unsealed_superseded:
            _require_digest(artifact, "artifact_hash")
            _require_digest(manifest, "manifest_digest")
        if (
            macro == MACRO_AWAITING_VALIDATION
            and substate == "queued"
            and item["validation_attempt"] != 0
        ):
            raise PipelineContractError("queued validation has an attempt")
        if (
            macro == MACRO_AWAITING_VALIDATION
            and substate in {"running", "retry", "infra_blocked"}
            and item["validation_attempt"] < 1
        ):
            raise PipelineContractError("active validation has no attempt")
        if (
            macro == MACRO_VALIDATION_COMPLETED
            and substate in {"promoted", "rejected"}
            and item["validation_attempt"] < 1
        ):
            raise PipelineContractError("validation verdict has no attempt")
    else:
        raise PipelineContractError("work item macro state is invalid")

    parent = item["repair_parent"]
    repair_proof_fields = (
        "mechanical_repair_binding_digest",
        "mechanical_repair_receipt_digest",
        "mechanical_repair_semantic_digest",
        "mechanical_repair_detector_identity_digest",
        "mechanical_repair_input_policy_sha256",
        "mechanical_repair_output_policy_sha256",
    )
    if parent is not None:
        parent = _require_exact_keys(parent, _REPAIR_PARENT_KEYS, "repair_parent")
        _require_safe_id(parent["work_item_id"], "repair parent work_item_id")
        _require_safe_id(parent["candidate_id"], "repair parent candidate_id")
        _require_digest(parent["artifact_hash"], "repair parent artifact_hash")
        if candidate == parent["candidate_id"] or artifact == parent["artifact_hash"]:
            raise PipelineContractError("repair child reused parent identity")
        for field in repair_proof_fields:
            _require_digest(item[field], f"repair child {field}")
    elif any(item[field] is not None for field in repair_proof_fields):
        raise PipelineContractError("non-repair item carries mechanical repair proof")
    superseded_by = item["superseded_by_candidate_id"]
    superseded_by_artifact = item["superseded_by_artifact_hash"]
    supersession_fence = item["supersession_fence_digest"]
    if substate == "superseded":
        _require_safe_id(superseded_by, "superseded_by_candidate_id")
        _require_digest(superseded_by_artifact, "superseded_by_artifact_hash")
        _require_digest(supersession_fence, "supersession_fence_digest")
        if candidate == superseded_by or artifact == superseded_by_artifact:
            raise PipelineContractError("superseded item reused its superseder identity")
    elif any(
        observed is not None
        for observed in (superseded_by, superseded_by_artifact, supersession_fence)
    ):
        raise PipelineContractError("non-superseded item has a supersession fence")
    terminal_reason = item["terminal_reason"]
    if macro == MACRO_VALIDATION_COMPLETED:
        if not isinstance(terminal_reason, str) or not terminal_reason.strip():
            raise PipelineContractError("terminal item has no reason")
    elif terminal_reason is not None:
        raise PipelineContractError("non-terminal item has a terminal reason")
    actions, blocks = _item_actions_and_blocks(item)
    if item["allowed_actions"] != actions or item["blocked_reasons"] != blocks:
        raise PipelineContractError("work item actions/reasons are not reducer-owned")
    return item


def validate_projection(
    value: Any,
    *,
    events: Iterable[Mapping[str, Any]] | None = None,
    mechanical_repair_policy_resolver: (
        Callable[[str, str], Mapping[str, Any]] | None
    ) = None,
    promotion_authority_resolver: (
        Callable[[str, str], Mapping[str, Any]] | None
    ) = None,
) -> dict[str, Any]:
    """Validate a projection, optionally replaying its complete event stream.

    A projection digest is a content identity, not a substitute for an atomic
    durable-store compare-and-swap.  Callers with the complete journal should
    pass ``events`` so every revision and event digest is replay-verified.
    """

    projection = _require_exact_keys(value, _PROJECTION_KEYS, "projection")
    if (
        projection["schema_version"] != SCHEMA_VERSION
        or projection["kind"] != PROJECTION_KIND
        or projection["authority"] != PROJECTION_AUTHORITY
        or isinstance(projection["queue_revision"], bool)
        or not isinstance(projection["queue_revision"], int)
        or projection["queue_revision"] < 0
    ):
        raise PipelineContractError("projection header is invalid")
    target = validate_target_identity(projection["target_identity"])
    target_digest = content_digest(target)
    if projection["target_identity_digest"] != target_digest:
        raise PipelineContractError("projection target identity digest mismatch")
    if not isinstance(projection["target_published"], bool):
        raise PipelineContractError("target_published is invalid")
    if not isinstance(projection["items"], list):
        raise PipelineContractError("projection items are invalid")
    event_ids = projection["event_ids"]
    event_digests = projection["event_digests"]
    if not isinstance(event_ids, list) or len(event_ids) != projection["queue_revision"]:
        raise PipelineContractError("projection event id ledger is invalid")
    if (
        any(
            not isinstance(observed, str) or _SAFE_ID.fullmatch(observed) is None
            for observed in event_ids
        )
        or len(set(event_ids)) != len(event_ids)
    ):
        raise PipelineContractError("projection event id ledger is invalid")
    if not isinstance(event_digests, list) or len(event_digests) != projection["queue_revision"]:
        raise PipelineContractError("projection event digest ledger is invalid")
    if (
        any(
            not isinstance(observed, str)
            or _HEX64.fullmatch(observed) is None
            or observed == ZERO_DIGEST
            for observed in event_digests
        )
        or len(set(event_digests)) != len(event_digests)
    ):
        raise PipelineContractError("projection event digest ledger is invalid")
    items = [_validate_item(item) for item in projection["items"]]
    for item in items:
        created_revision = item["created_revision"]
        updated_revision = item["updated_revision"]
        if created_revision > projection["queue_revision"] or updated_revision > projection["queue_revision"]:
            raise PipelineContractError("work item revision exceeds the projection ledger")
        if (
            event_ids[updated_revision - 1] != item["last_event_id"]
            or event_digests[updated_revision - 1] != item["last_event_digest"]
        ):
            raise PipelineContractError("work item last event is not ledger-bound")
    if len({item["work_item_id"] for item in items}) != len(items):
        raise PipelineContractError("duplicate work_item_id")
    candidate_ids = [item["candidate_id"] for item in items if item["candidate_id"]]
    artifact_hashes = [item["artifact_hash"] for item in items if item["artifact_hash"]]
    if len(set(candidate_ids)) != len(candidate_ids) or len(set(artifact_hashes)) != len(artifact_hashes):
        raise PipelineContractError("sealed candidate/artifact identity is reused")
    expected_order = sorted(items, key=lambda item: (item["created_revision"], item["work_item_id"]))
    if items != expected_order:
        raise PipelineContractError("projection items are not canonically ordered")
    items_by_work_id = {item["work_item_id"]: item for item in items}
    for item in items:
        parent = item["repair_parent"]
        if parent is None:
            continue
        parent_item = items_by_work_id.get(parent["work_item_id"])
        if (
            parent_item is None
            or parent_item["macro_state"] != MACRO_VALIDATION_COMPLETED
            or parent_item["substate"] != "rejected"
            or parent_item["candidate_id"] != parent["candidate_id"]
            or parent_item["artifact_hash"] != parent["artifact_hash"]
        ):
            raise PipelineContractError("repair parent is not a bound rejected artifact")

    promoted = [item for item in items if item["substate"] == "promoted"]
    published = projection["published_identity"]
    if projection["target_published"]:
        if len(promoted) != 1 or projection["promoted_candidate_id"] != promoted[0]["candidate_id"]:
            raise PipelineContractError("published target has no unique promoted candidate")
        published = _require_exact_keys(published, _PUBLISHED_IDENTITY_KEYS, "published_identity")
        for key in (
            "promotion_receipt_digest",
            "official_certificate_digest",
            "tree_digest",
            "completed_digest",
            "remote_proof_digest",
        ):
            _require_digest(published[key], f"published identity {key}")
        commit = _require_git_object({
            "object_format": published.get("git_object_format"),
            "oid": published.get("commit_oid"),
        }, "published commit")
        for key in (
            "annotated_tag_oid",
            "remote_main_oid",
            "high_water_tag_oid",
        ):
            _require_git_object({
                "object_format": commit["object_format"],
                "oid": published.get(key),
            }, f"published identity {key}")
        expected_published = {
            **target,
            "candidate_id": promoted[0]["candidate_id"],
            "artifact_hash": promoted[0]["artifact_hash"],
            **{key: published[key] for key in _PUBLISHED_PROOF_FIELDS},
        }
        if published != expected_published:
            raise PipelineContractError("published identity does not bind promoted artifact")
        if any(item["macro_state"] != MACRO_VALIDATION_COMPLETED for item in items):
            raise PipelineContractError("promotion left another target item live")
    elif promoted or projection["promoted_candidate_id"] is not None or published is not None:
        raise PipelineContractError("unpublished target exposes published identity")

    digest = _require_digest(projection["projection_digest"], "projection_digest")
    if digest != content_digest(_projection_unsigned(projection)):
        raise PipelineContractError("projection digest mismatch")
    validated = _json_copy(projection)
    if events is not None:
        replayed = reduce_events(
            target,
            list(events),
            mechanical_repair_policy_resolver=mechanical_repair_policy_resolver,
            promotion_authority_resolver=promotion_authority_resolver,
        )
        if replayed != validated:
            raise PipelineContractError("projection does not match complete event replay")
    return validated


def initial_projection(target_identity: Mapping[str, Any]) -> dict[str, Any]:
    target = validate_target_identity(dict(target_identity))
    projection = {
        "schema_version": SCHEMA_VERSION,
        "kind": PROJECTION_KIND,
        "authority": PROJECTION_AUTHORITY,
        "queue_revision": 0,
        "target_identity": target,
        "target_identity_digest": content_digest(target),
        "target_published": False,
        "promoted_candidate_id": None,
        "published_identity": None,
        "event_ids": [],
        "event_digests": [],
        "items": [],
    }
    projection["projection_digest"] = content_digest(projection)
    return validate_projection(projection)


def projection_cas_precondition(projection: Mapping[str, Any]) -> dict[str, Any]:
    """Return the fields a durable owner must compare-and-swap atomically.

    Two writers may derive valid next events from the same immutable snapshot.
    Exactly one may commit: the store transaction must compare both returned
    fields with its current row before appending the event and projection.
    """

    current = validate_projection(dict(projection))
    return {
        "expected_queue_revision": current["queue_revision"],
        "expected_projection_digest": current["projection_digest"],
    }


def require_projection_cas_precondition(
    projection: Mapping[str, Any],
    *,
    expected_queue_revision: int,
    expected_projection_digest: str,
) -> dict[str, Any]:
    """Fail closed when a previously captured store CAS fence is stale."""

    if (
        isinstance(expected_queue_revision, bool)
        or not isinstance(expected_queue_revision, int)
        or expected_queue_revision < 0
    ):
        raise PipelineContractError("expected queue revision is invalid")
    _require_digest(expected_projection_digest, "expected projection digest")
    current = validate_projection(dict(projection))
    if (
        current["queue_revision"] != expected_queue_revision
        or current["projection_digest"] != expected_projection_digest
    ):
        raise PipelineContractError("projection compare-and-swap precondition is stale")
    return current


def build_event(
    projection: Mapping[str, Any],
    event_type: str,
    *,
    event_id: str,
    work_item_id: str,
    candidate_id: str | None = None,
    artifact_hash: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a fully bound event; callers still pass it through the reducer."""

    current = validate_projection(dict(projection))
    event = {
        "schema_version": SCHEMA_VERSION,
        "kind": EVENT_KIND,
        "event_id": _require_safe_id(event_id, "event_id"),
        "event_type": event_type,
        "queue_revision": current["queue_revision"] + 1,
        "previous_projection_digest": current["projection_digest"],
        "target_identity": current["target_identity"],
        "target_identity_digest": current["target_identity_digest"],
        "work_item_id": _require_safe_id(work_item_id, "work_item_id"),
        "candidate_id": candidate_id,
        "artifact_hash": artifact_hash,
        "payload": _json_copy(dict(payload or {})),
    }
    event["event_digest"] = content_digest(event)
    return event


def _validate_event(event: Any, current: Mapping[str, Any]) -> dict[str, Any]:
    value = _require_exact_keys(event, _EVENT_KEYS, "event")
    if value["schema_version"] != SCHEMA_VERSION or value["kind"] != EVENT_KIND:
        raise PipelineContractError("event header is invalid")
    if value["event_type"] not in EVENT_TYPES:
        raise PipelineContractError("event type is unknown")
    _require_safe_id(value["event_id"], "event_id")
    _require_safe_id(value["work_item_id"], "work_item_id")
    if not isinstance(value["payload"], dict):
        raise PipelineContractError("event payload is not an object")
    if (
        isinstance(value["queue_revision"], bool)
        or value["queue_revision"] != current["queue_revision"] + 1
        or value["previous_projection_digest"] != current["projection_digest"]
    ):
        raise PipelineContractError("event is missing the next projection fence")
    target = validate_target_identity(value["target_identity"])
    if (
        target != current["target_identity"]
        or value["target_identity_digest"] != current["target_identity_digest"]
        or value["target_identity_digest"] != content_digest(target)
    ):
        raise PipelineContractError("event target identity mismatch")
    if value["candidate_id"] is not None:
        _require_safe_id(value["candidate_id"], "candidate_id")
    if value["artifact_hash"] is not None:
        _require_digest(value["artifact_hash"], "artifact_hash")
    _require_digest(value["event_digest"], "event_digest")
    if value["event_digest"] != content_digest(_event_unsigned(value)):
        raise PipelineContractError("event digest mismatch")
    if value["event_id"] in current["event_ids"]:
        raise PipelineContractError("event_id was already consumed")
    return _json_copy(value)


def _find_item(items: list[dict[str, Any]], work_item_id: str) -> dict[str, Any]:
    matches = [item for item in items if item["work_item_id"] == work_item_id]
    if len(matches) != 1:
        raise PipelineContractError("event work item does not exist uniquely")
    return matches[0]


def _supersession_fence_material(
    projection: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    superseded_by_candidate_id: str,
    superseded_by_artifact_hash: str,
) -> dict[str, Any]:
    return {
        "kind": "producer-consumer-supersession-fence-v1",
        "target_identity_digest": projection["target_identity_digest"],
        "expected_queue_revision": projection["queue_revision"],
        "expected_projection_digest": projection["projection_digest"],
        "superseded_work_item_id": item["work_item_id"],
        "superseded_candidate_id": item["candidate_id"],
        "superseded_artifact_hash": item["artifact_hash"],
        "superseded_by_candidate_id": superseded_by_candidate_id,
        "superseded_by_artifact_hash": superseded_by_artifact_hash,
    }


def supersession_fence_digest(
    projection: Mapping[str, Any],
    *,
    work_item_id: str,
    superseded_by_candidate_id: str,
    superseded_by_artifact_hash: str,
) -> str:
    """Bind an explicit sealed-candidate supersession to one target CAS fence."""

    current = validate_projection(dict(projection))
    item = _find_item(current["items"], _require_safe_id(work_item_id, "work_item_id"))
    if item["macro_state"] != MACRO_AWAITING_VALIDATION:
        raise PipelineContractError("only a sealed validation candidate can be superseded")
    superseder_candidate = _require_safe_id(
        superseded_by_candidate_id,
        "superseded_by_candidate_id",
    )
    superseder_artifact = _require_digest(
        superseded_by_artifact_hash,
        "superseded_by_artifact_hash",
    )
    matches = [
        other
        for other in current["items"]
        if other["work_item_id"] != item["work_item_id"]
        and other["candidate_id"] == superseder_candidate
        and other["artifact_hash"] == superseder_artifact
        and other["macro_state"] == MACRO_AWAITING_VALIDATION
    ]
    if len(matches) != 1:
        raise PipelineContractError("superseder is not one live sealed target candidate")
    return content_digest(
        _supersession_fence_material(
            current,
            item,
            superseded_by_candidate_id=superseder_candidate,
            superseded_by_artifact_hash=superseder_artifact,
        )
    )


def _require_event_identity_matches_item(event: Mapping[str, Any], item: Mapping[str, Any]) -> None:
    if event["candidate_id"] != item["candidate_id"] or event["artifact_hash"] != item["artifact_hash"]:
        raise PipelineContractError("event candidate/artifact identity mismatch")


def _require_reason(payload: Mapping[str, Any]) -> str:
    if set(payload) != {"reason"} or not isinstance(payload["reason"], str) or not payload["reason"].strip():
        raise PipelineContractError("event requires one non-empty reason")
    return payload["reason"].strip()


def _validate_remote_publication_proof(
    value: Any,
    target_identity: Mapping[str, Any],
) -> dict[str, Any]:
    proof = _require_exact_keys(value, _REMOTE_PROOF_KEYS, "remote publication proof")
    target = validate_target_identity(dict(target_identity))
    if proof.get("schema") != "producer-consumer-remote-publication-proof-v1":
        raise PipelineContractError("remote publication proof schema is invalid")
    if proof.get("target_identity_digest") != content_digest(target):
        raise PipelineContractError("remote publication proof target mismatch")
    object_format = _require_git_object_format(
        proof.get("object_format"),
        "remote publication object_format",
    )
    _require_safe_id(proof.get("remote_name"), "remote publication name")
    remote_main = _require_git_object(proof.get("remote_main"), "remote main")
    bot_tag_object = _require_git_object(
        proof.get("bot_tag_object"),
        "remote bot tag object",
    )
    bot_peeled = _require_git_object(
        proof.get("bot_tag_peeled_commit"),
        "remote bot tag peeled commit",
    )
    high_water_object = _require_git_object(
        proof.get("high_water_tag_object"),
        "remote high-water tag object",
    )
    high_water_peeled = _require_git_object(
        proof.get("high_water_tag_peeled_commit"),
        "remote high-water tag peeled commit",
    )
    if proof.get("bot_tag") != target["canonical_tag"]:
        raise PipelineContractError("remote bot tag name mismatch")
    if proof.get("high_water_tag") != (
        f"national-high-water-v{target['canonical_version']}"
    ):
        raise PipelineContractError("remote high-water tag name mismatch")
    formats = {
        object_format,
        remote_main["object_format"],
        bot_tag_object["object_format"],
        bot_peeled["object_format"],
        high_water_object["object_format"],
        high_water_peeled["object_format"],
    }
    if len(formats) != 1:
        raise PipelineContractError("remote publication Git object formats differ")
    if proof.get("verified") is not True:
        raise PipelineContractError("remote publication proof is not verified")
    if proof.get("commit_reachable_from_remote_main") is not True:
        raise PipelineContractError("remote main does not prove commit reachability")
    _require_digest(proof.get("resolver_digest"), "remote proof resolver digest")
    unsigned = {key: item for key, item in proof.items() if key != "proof_digest"}
    if proof.get("proof_digest") != content_digest(unsigned):
        raise PipelineContractError("remote publication proof digest mismatch")
    return _json_copy(proof)


def build_remote_publication_proof(
    *,
    target_identity: Mapping[str, Any],
    object_format: str,
    remote_name: str,
    remote_main: Mapping[str, Any],
    commit: Mapping[str, Any],
    bot_tag_object: Mapping[str, Any],
    high_water_tag_object: Mapping[str, Any],
    resolver_digest: str,
) -> dict[str, Any]:
    """Build one parsed remote-ref proof for either SHA-1 or SHA-256 Git."""

    target = validate_target_identity(dict(target_identity))
    explicit_format = _require_git_object_format(
        object_format,
        "remote publication object_format",
    )
    commit_object = _require_git_object(dict(commit), "commit")
    proof = {
        "schema": "producer-consumer-remote-publication-proof-v1",
        "target_identity_digest": content_digest(target),
        "object_format": explicit_format,
        "remote_name": _require_safe_id(remote_name, "remote publication name"),
        "remote_main": _require_git_object(dict(remote_main), "remote main"),
        "bot_tag": target["canonical_tag"],
        "bot_tag_object": _require_git_object(
            dict(bot_tag_object),
            "remote bot tag object",
        ),
        "bot_tag_peeled_commit": commit_object,
        "high_water_tag": f"national-high-water-v{target['canonical_version']}",
        "high_water_tag_object": _require_git_object(
            dict(high_water_tag_object),
            "remote high-water tag object",
        ),
        "high_water_tag_peeled_commit": commit_object,
        "resolver_digest": _require_digest(
            resolver_digest,
            "remote proof resolver digest",
        ),
        "verified": True,
        "commit_reachable_from_remote_main": True,
    }
    proof["proof_digest"] = content_digest(proof)
    return _validate_remote_publication_proof(proof, target)


def _validate_promotion_receipt(
    value: Any,
    *,
    target_identity: Mapping[str, Any],
    candidate_id: str,
    artifact_hash: str,
    remote_proof: Mapping[str, Any],
) -> dict[str, Any]:
    receipt = _require_exact_keys(value, _PROMOTION_RECEIPT_KEYS, "promotion receipt")
    target = validate_target_identity(dict(target_identity))
    remote = _validate_remote_publication_proof(remote_proof, target)
    if receipt.get("schema") != "producer-consumer-promotion-receipt-v1":
        raise PipelineContractError("promotion receipt schema is invalid")
    if receipt.get("target_identity_digest") != content_digest(target):
        raise PipelineContractError("promotion receipt target mismatch")
    if receipt.get("candidate_id") != candidate_id:
        raise PipelineContractError("promotion receipt candidate mismatch")
    if receipt.get("artifact_hash") != artifact_hash:
        raise PipelineContractError("promotion receipt artifact mismatch")
    _require_safe_id(receipt.get("candidate_id"), "promotion receipt candidate")
    _require_digest(receipt.get("artifact_hash"), "promotion receipt artifact")
    official = _require_digest(
        receipt.get("official_certificate_digest"),
        "promotion receipt official certificate digest",
    )
    tree = _require_digest(receipt.get("tree_digest"), "promotion receipt tree digest")
    completed = _require_digest(
        receipt.get("completed_digest"),
        "promotion receipt completed digest",
    )
    commit = _require_git_object(receipt.get("commit"), "promotion receipt commit")
    object_format = _require_git_object_format(
        receipt.get("object_format"),
        "promotion receipt object_format",
    )
    annotated_tag = _require_git_object(
        receipt.get("annotated_tag"),
        "promotion receipt annotated tag",
    )
    if receipt.get("remote_proof_digest") != remote["proof_digest"]:
        raise PipelineContractError("promotion receipt remote proof mismatch")
    if (
        remote["bot_tag_peeled_commit"] != commit
        or remote["high_water_tag_peeled_commit"] != commit
        or remote["bot_tag_object"] != annotated_tag
    ):
        raise PipelineContractError("promotion receipt Git refs are not cross-bound")
    if remote.get("object_format") != object_format:
        raise PipelineContractError(
            "promotion receipt and remote proof object formats differ"
        )
    if any(
        observed["object_format"] != object_format
        for observed in (
            commit,
            annotated_tag,
            remote["remote_main"],
            remote["high_water_tag_object"],
        )
    ):
        raise PipelineContractError("promotion receipt Git object formats differ")
    unsigned = {key: item for key, item in receipt.items() if key != "receipt_digest"}
    if receipt.get("receipt_digest") != content_digest(unsigned):
        raise PipelineContractError("promotion receipt digest mismatch")
    return {
        "promotion_receipt_digest": receipt["receipt_digest"],
        "official_certificate_digest": official,
        "git_object_format": object_format,
        "commit_oid": commit["oid"],
        "tree_digest": tree,
        "completed_digest": completed,
        "annotated_tag_oid": annotated_tag["oid"],
        "remote_proof_digest": remote["proof_digest"],
        "remote_main_oid": remote["remote_main"]["oid"],
        "high_water_tag_oid": remote["high_water_tag_object"]["oid"],
    }


def build_promotion_receipt(
    *,
    target_identity: Mapping[str, Any],
    candidate_id: str,
    artifact_hash: str,
    official_certificate_digest: str,
    object_format: str,
    commit: Mapping[str, Any],
    tree_digest: str,
    completed_digest: str,
    annotated_tag: Mapping[str, Any],
    remote_proof: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the receipt consumed by the synchronous promotion barrier."""

    target = validate_target_identity(dict(target_identity))
    explicit_format = _require_git_object_format(
        object_format,
        "promotion receipt object_format",
    )
    remote = _validate_remote_publication_proof(dict(remote_proof), target)
    receipt = {
        "schema": "producer-consumer-promotion-receipt-v1",
        "target_identity_digest": content_digest(target),
        "candidate_id": _require_safe_id(candidate_id, "promotion candidate"),
        "artifact_hash": _require_digest(artifact_hash, "promotion artifact"),
        "official_certificate_digest": _require_digest(
            official_certificate_digest,
            "official certificate digest",
        ),
        "object_format": explicit_format,
        "commit": _require_git_object(dict(commit), "promotion commit"),
        "tree_digest": _require_digest(tree_digest, "promotion tree digest"),
        "completed_digest": _require_digest(
            completed_digest,
            "promotion completed digest",
        ),
        "annotated_tag": _require_git_object(
            dict(annotated_tag),
            "promotion annotated tag",
        ),
        "remote_proof_digest": remote["proof_digest"],
    }
    receipt["receipt_digest"] = content_digest(receipt)
    _validate_promotion_receipt(
        receipt,
        target_identity=target,
        candidate_id=candidate_id,
        artifact_hash=artifact_hash,
        remote_proof=remote,
    )
    return receipt


def _validate_promotion_payload(
    payload: Mapping[str, Any],
    *,
    target_identity: Mapping[str, Any],
    candidate_id: str,
    artifact_hash: str,
    promotion_authority_resolver: (
        Callable[[str, str], Mapping[str, Any]] | None
    ),
) -> dict[str, Any]:
    if set(payload) != set(_PROMOTION_PAYLOAD_KEYS):
        raise PipelineContractError("candidate promotion payload is not exact")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise PipelineContractError("candidate promotion reason is invalid")
    receipt_digest = _require_digest(
        payload.get("promotion_receipt_digest"),
        "candidate promotion receipt digest",
    )
    resolver_digest = _require_digest(
        payload.get("resolver_digest"),
        "candidate promotion authority resolver digest",
    )
    if promotion_authority_resolver is None:
        raise PipelineContractError("promotion authority resolver is required")
    try:
        resolved_value = promotion_authority_resolver(
            receipt_digest,
            resolver_digest,
        )
    except Exception as exc:
        raise PipelineContractError("promotion authority resolution failed") from exc
    resolved = _require_exact_keys(
        resolved_value,
        _PROMOTION_AUTHORITY_RESOLUTION_KEYS,
        "promotion authority resolution",
    )
    if resolved.get("authority") != "strict-publication-authority-resolver-v1":
        raise PipelineContractError("promotion authority kind is invalid")
    if resolved.get("official_policy_id") != "official-full-v5":
        raise PipelineContractError("promotion official policy is not official-full-v5")
    if resolved.get("resolver_digest") != resolver_digest:
        raise PipelineContractError("promotion authority resolver identity mismatch")
    remote = _validate_remote_publication_proof(
        resolved.get("remote_proof"),
        target_identity,
    )
    if remote.get("resolver_digest") != resolver_digest:
        raise PipelineContractError("remote proof resolver identity mismatch")
    resolved_receipt = resolved.get("promotion_receipt")
    if (
        not isinstance(resolved_receipt, Mapping)
        or resolved_receipt.get("receipt_digest") != receipt_digest
    ):
        raise PipelineContractError("resolved promotion receipt digest mismatch")
    receipt = _validate_promotion_receipt(
        resolved_receipt,
        target_identity=target_identity,
        candidate_id=candidate_id,
        artifact_hash=artifact_hash,
        remote_proof=remote,
    )
    return {"reason": reason.strip(), **receipt}


def _validate_supersession_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) != set(_SUPERSESSION_PAYLOAD_KEYS):
        raise PipelineContractError("candidate supersession payload is not exact")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise PipelineContractError("candidate supersession reason is invalid")
    return {
        "reason": reason.strip(),
        "superseded_by_candidate_id": _require_safe_id(
            payload.get("superseded_by_candidate_id"),
            "superseded_by_candidate_id",
        ),
        "superseded_by_artifact_hash": _require_digest(
            payload.get("superseded_by_artifact_hash"),
            "superseded_by_artifact_hash",
        ),
        "promotion_fence_digest": _require_digest(
            payload.get("promotion_fence_digest"),
            "promotion_fence_digest",
        ),
    }


def _validate_mechanical_repair_binding(
    value: Any,
    *,
    parent_artifact_hash: str,
    parent_manifest_digest: str,
    output_artifact_hash: str,
    output_manifest_digest: str,
    input_members: dict[str, bytes],
    output_members: dict[str, bytes],
) -> dict[str, str]:
    binding = _require_exact_keys(
        value,
        _REPAIR_BINDING_KEYS,
        "mechanical repair binding",
    )
    if binding.get("schema") != "mechanical-policy-artifact-binding-v1":
        raise PipelineContractError("mechanical repair binding schema is invalid")
    expected = {
        "input_artifact_hash": parent_artifact_hash,
        "output_artifact_hash": output_artifact_hash,
        "input_manifest_digest": parent_manifest_digest,
        "output_manifest_digest": output_manifest_digest,
    }
    for key, expected_value in expected.items():
        if binding.get(key) != expected_value:
            raise PipelineContractError(f"mechanical repair {key} mismatch")
        _require_digest(binding.get(key), f"mechanical repair {key}")
    _require_digest(binding.get("resolver_digest"), "mechanical repair resolver digest")
    try:
        receipt = validate_mechanical_repair_receipt_against_artifact_bytes(
            binding.get("mechanical_repair_receipt"),
            input_members=input_members,
            output_members=output_members,
        )
    except MechanicalRepairRejected as exc:
        raise PipelineContractError(
            "mechanical repair receipt invalid: " + ";".join(exc.errors)
        ) from exc
    identity = receipt["policy_semantic_identity"]
    receipt_bound = {
        "input_policy_sha256": receipt["input"]["policy_sha256"],
        "output_policy_sha256": receipt["output"]["policy_sha256"],
        "input_national_bot_sha256": receipt["input"]["national_bot_sha256"],
        "output_national_bot_sha256": receipt["output"]["national_bot_sha256"],
        "input_precompute_sha256": receipt["input"]["precompute_sha256"],
        "output_precompute_sha256": receipt["output"]["precompute_sha256"],
        "detector_identity_digest": identity["detector_identity_digest"],
        "semantic_digest": identity["semantic_digest"],
        "mechanical_repair_receipt_digest": receipt["receipt_digest"],
    }
    for key, expected_value in receipt_bound.items():
        if binding.get(key) != expected_value:
            raise PipelineContractError(f"mechanical repair {key} mismatch")
        _require_digest(binding.get(key), f"mechanical repair {key}")
    unsigned = {key: item for key, item in binding.items() if key != "binding_digest"}
    if binding.get("binding_digest") != content_digest(unsigned):
        raise PipelineContractError("mechanical repair binding digest mismatch")
    return {
        "mechanical_repair_binding_digest": binding["binding_digest"],
        "mechanical_repair_receipt_digest": receipt["receipt_digest"],
        "mechanical_repair_semantic_digest": identity["semantic_digest"],
        "mechanical_repair_detector_identity_digest": identity[
            "detector_identity_digest"
        ],
        "mechanical_repair_input_policy_sha256": receipt["input"][
            "policy_sha256"
        ],
        "mechanical_repair_output_policy_sha256": receipt["output"][
            "policy_sha256"
        ],
    }


def build_mechanical_repair_binding(
    *,
    parent_artifact_hash: str,
    parent_manifest_digest: str,
    output_artifact_hash: str,
    output_manifest_digest: str,
    mechanical_repair_receipt: Mapping[str, Any],
    resolver_digest: str,
    input_policy_bytes: bytes,
    output_policy_bytes: bytes,
    input_national_bot_bytes: bytes,
    output_national_bot_bytes: bytes,
    input_precompute_bytes: bytes,
    output_precompute_bytes: bytes,
) -> dict[str, Any]:
    """Bind recomputed parent/child policy semantics to exact manifests."""

    try:
        receipt = validate_mechanical_repair_receipt_against_artifact_bytes(
            dict(mechanical_repair_receipt),
            input_members={
                "national_bot.py": input_national_bot_bytes,
                "policy.py": input_policy_bytes,
                "precompute.py": input_precompute_bytes,
            },
            output_members={
                "national_bot.py": output_national_bot_bytes,
                "policy.py": output_policy_bytes,
                "precompute.py": output_precompute_bytes,
            },
        )
    except MechanicalRepairRejected as exc:
        raise PipelineContractError(
            "mechanical repair receipt invalid: " + ";".join(exc.errors)
        ) from exc
    binding = {
        "schema": "mechanical-policy-artifact-binding-v1",
        "input_artifact_hash": _require_digest(
            parent_artifact_hash,
            "mechanical repair parent artifact",
        ),
        "output_artifact_hash": _require_digest(
            output_artifact_hash,
            "mechanical repair output artifact",
        ),
        "input_manifest_digest": _require_digest(
            parent_manifest_digest,
            "mechanical repair parent manifest",
        ),
        "output_manifest_digest": _require_digest(
            output_manifest_digest,
            "mechanical repair output manifest",
        ),
        "input_policy_sha256": receipt["input"]["policy_sha256"],
        "output_policy_sha256": receipt["output"]["policy_sha256"],
        "input_national_bot_sha256": receipt["input"]["national_bot_sha256"],
        "output_national_bot_sha256": receipt["output"]["national_bot_sha256"],
        "input_precompute_sha256": receipt["input"]["precompute_sha256"],
        "output_precompute_sha256": receipt["output"]["precompute_sha256"],
        "detector_identity_digest": receipt["policy_semantic_identity"][
            "detector_identity_digest"
        ],
        "semantic_digest": receipt["policy_semantic_identity"][
            "semantic_digest"
        ],
        "mechanical_repair_receipt_digest": receipt["receipt_digest"],
        "mechanical_repair_receipt": receipt,
        "resolver_digest": _require_digest(
            resolver_digest,
            "mechanical repair resolver digest",
        ),
    }
    binding["binding_digest"] = content_digest(binding)
    _validate_mechanical_repair_binding(
        binding,
        parent_artifact_hash=parent_artifact_hash,
        parent_manifest_digest=parent_manifest_digest,
        output_artifact_hash=output_artifact_hash,
        output_manifest_digest=output_manifest_digest,
        input_members={
            "national_bot.py": input_national_bot_bytes,
            "policy.py": input_policy_bytes,
            "precompute.py": input_precompute_bytes,
        },
        output_members={
            "national_bot.py": output_national_bot_bytes,
            "policy.py": output_policy_bytes,
            "precompute.py": output_precompute_bytes,
        },
    )
    return binding


def _resolve_mechanical_repair_artifact(
    resolver: Callable[[str, str], Mapping[str, Any]] | None,
    *,
    artifact_hash: str,
    manifest_digest: str,
    expected_resolver_digest: str,
    side: str,
) -> dict[str, bytes]:
    if resolver is None:
        raise PipelineContractError(
            "mechanical repair artifact resolver is required"
        )
    try:
        resolution = resolver(artifact_hash, manifest_digest)
    except Exception as exc:
        raise PipelineContractError(
            f"mechanical repair {side} artifact resolver failed:{type(exc).__name__}"
        ) from exc
    if not isinstance(resolution, Mapping) or set(resolution) != {
        "artifact_hash",
        "manifest_digest",
        "members",
        "resolver_digest",
    }:
        raise PipelineContractError(
            f"mechanical repair {side} artifact resolution is not exact"
        )
    if resolution.get("artifact_hash") != artifact_hash:
        raise PipelineContractError(
            f"mechanical repair {side} artifact resolution artifact mismatch"
        )
    if resolution.get("manifest_digest") != manifest_digest:
        raise PipelineContractError(
            f"mechanical repair {side} artifact resolution manifest mismatch"
        )
    members = resolution.get("members")
    if not isinstance(members, Mapping) or set(members) != {
        "national_bot.py",
        "policy.py",
        "precompute.py",
    }:
        raise PipelineContractError(
            f"mechanical repair {side} artifact resolution members mismatch"
        )
    if resolution.get("resolver_digest") != expected_resolver_digest:
        raise PipelineContractError(
            f"mechanical repair {side} artifact resolution resolver mismatch"
        )
    if any(not isinstance(value, bytes) for value in members.values()):
        raise PipelineContractError(
            f"mechanical repair {side} artifact resolver returned non-bytes"
        )
    return dict(members)


def _new_item(event: Mapping[str, Any]) -> dict[str, Any]:
    item = {
        "work_item_id": event["work_item_id"],
        "candidate_id": None,
        "artifact_hash": None,
        "manifest_digest": None,
        "macro_state": MACRO_PRODUCING,
        "substate": "queued",
        "producer_attempt": 0,
        "validation_attempt": 0,
        "repair_parent": None,
        "mechanical_repair_binding_digest": None,
        "mechanical_repair_receipt_digest": None,
        "mechanical_repair_semantic_digest": None,
        "mechanical_repair_detector_identity_digest": None,
        "mechanical_repair_input_policy_sha256": None,
        "mechanical_repair_output_policy_sha256": None,
        "superseded_by_candidate_id": None,
        "superseded_by_artifact_hash": None,
        "supersession_fence_digest": None,
        "terminal_reason": None,
        "created_revision": event["queue_revision"],
        "updated_revision": event["queue_revision"],
        "last_event_id": event["event_id"],
        "last_event_digest": event["event_digest"],
        "allowed_actions": [],
        "blocked_reasons": [],
    }
    _refresh_item_projection(item)
    return item


def _touch(item: dict[str, Any], event: Mapping[str, Any]) -> None:
    item["updated_revision"] = event["queue_revision"]
    item["last_event_id"] = event["event_id"]
    item["last_event_digest"] = event["event_digest"]
    _refresh_item_projection(item)


def reduce_event(
    projection: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    mechanical_repair_policy_resolver: (
        Callable[[str, str], Mapping[str, Any]] | None
    ) = None,
    promotion_authority_resolver: (
        Callable[[str, str], Mapping[str, Any]] | None
    ) = None,
) -> dict[str, Any]:
    """Apply one exact next event, returning a new canonical projection."""

    current = validate_projection(dict(projection))
    value = _validate_event(dict(event), current)
    state = deepcopy(current)
    state.pop("projection_digest", None)
    items: list[dict[str, Any]] = state["items"]
    event_type = value["event_type"]

    if state["target_published"]:
        raise PipelineContractError("published target accepts no further events")

    if event_type == "draft_queued":
        if value["candidate_id"] is not None or value["artifact_hash"] is not None or value["payload"]:
            raise PipelineContractError("draft_queued carries unexpected identity/payload")
        if any(item["work_item_id"] == value["work_item_id"] for item in items):
            raise PipelineContractError("work_item_id is already present")
        items.append(_new_item(value))
    elif event_type == "repair_child_created":
        payload = value["payload"]
        if set(payload) != {
            "parent_work_item_id",
            "parent_candidate_id",
            "parent_artifact_hash",
            "manifest_digest",
            "mechanical_repair_binding",
        }:
            raise PipelineContractError("repair child payload is not exact")
        if any(item["work_item_id"] == value["work_item_id"] for item in items):
            raise PipelineContractError("repair child work_item_id is already present")
        parent = _find_item(items, _require_safe_id(payload["parent_work_item_id"], "parent work_item_id"))
        if parent["macro_state"] != MACRO_VALIDATION_COMPLETED or parent["substate"] != "rejected":
            raise PipelineContractError("repair parent is not a rejected artifact")
        if payload["parent_candidate_id"] != parent["candidate_id"] or payload["parent_artifact_hash"] != parent["artifact_hash"]:
            raise PipelineContractError("repair parent identity mismatch")
        candidate = _require_safe_id(value["candidate_id"], "repair child candidate_id")
        artifact = _require_digest(value["artifact_hash"], "repair child artifact_hash")
        manifest = _require_digest(payload["manifest_digest"], "repair child manifest_digest")
        if candidate == parent["candidate_id"] or artifact == parent["artifact_hash"]:
            raise PipelineContractError("repair child reused parent identity")
        binding_value = payload["mechanical_repair_binding"]
        if not isinstance(binding_value, Mapping):
            raise PipelineContractError("mechanical repair binding is not an object")
        binding_resolver_digest = _require_digest(
            binding_value.get("resolver_digest"),
            "mechanical repair resolver digest",
        )
        input_members = _resolve_mechanical_repair_artifact(
            mechanical_repair_policy_resolver,
            artifact_hash=parent["artifact_hash"],
            manifest_digest=parent["manifest_digest"],
            expected_resolver_digest=binding_resolver_digest,
            side="input",
        )
        output_members = _resolve_mechanical_repair_artifact(
            mechanical_repair_policy_resolver,
            artifact_hash=artifact,
            manifest_digest=manifest,
            expected_resolver_digest=binding_resolver_digest,
            side="output",
        )
        repair_proof = _validate_mechanical_repair_binding(
            binding_value,
            parent_artifact_hash=parent["artifact_hash"],
            parent_manifest_digest=parent["manifest_digest"],
            output_artifact_hash=artifact,
            output_manifest_digest=manifest,
            input_members=input_members,
            output_members=output_members,
        )
        child = _new_item(value)
        child.update({
            "candidate_id": candidate,
            "artifact_hash": artifact,
            "manifest_digest": manifest,
            "macro_state": MACRO_AWAITING_VALIDATION,
            "substate": "queued",
            "repair_parent": {
                "work_item_id": parent["work_item_id"],
                "candidate_id": parent["candidate_id"],
                "artifact_hash": parent["artifact_hash"],
            },
            **repair_proof,
        })
        _refresh_item_projection(child)
        items.append(child)
    else:
        item = _find_item(items, value["work_item_id"])
        if item["macro_state"] == MACRO_VALIDATION_COMPLETED:
            raise PipelineContractError("terminal artifact cannot transition")

        if event_type == "producer_started":
            if item["macro_state"] != MACRO_PRODUCING or item["substate"] not in {"queued", "retry"}:
                raise PipelineContractError("producer cannot start from current state")
            if value["candidate_id"] is not None or value["artifact_hash"] is not None or value["payload"]:
                raise PipelineContractError("producer_started carries unexpected data")
            item["substate"] = "running"
            item["producer_attempt"] += 1
        elif event_type == "producer_retry_scheduled":
            if item["macro_state"] != MACRO_PRODUCING or item["substate"] != "running":
                raise PipelineContractError("producer retry requires a running draft")
            _require_reason(value["payload"])
            item["substate"] = "retry"
        elif event_type == "producer_backpressured":
            if item["macro_state"] != MACRO_PRODUCING or item["substate"] not in {"queued", "retry"}:
                raise PipelineContractError("backpressure cannot interrupt active production")
            _require_reason(value["payload"])
            item["substate"] = "backpressured"
        elif event_type == "producer_backpressure_released":
            if item["macro_state"] != MACRO_PRODUCING or item["substate"] != "backpressured" or value["payload"]:
                raise PipelineContractError("backpressure release is invalid")
            item["substate"] = "queued"
        elif event_type == "artifact_sealed":
            if item["macro_state"] != MACRO_PRODUCING or item["substate"] != "running":
                raise PipelineContractError("only a running producer can seal")
            if set(value["payload"]) != {"manifest_digest"}:
                raise PipelineContractError("artifact seal payload is not exact")
            candidate = _require_safe_id(value["candidate_id"], "candidate_id")
            artifact = _require_digest(value["artifact_hash"], "artifact_hash")
            manifest = _require_digest(value["payload"]["manifest_digest"], "manifest_digest")
            if any(other["candidate_id"] == candidate or other["artifact_hash"] == artifact for other in items if other is not item):
                raise PipelineContractError("sealed identity is already present")
            item.update({
                "candidate_id": candidate,
                "artifact_hash": artifact,
                "manifest_digest": manifest,
                "macro_state": MACRO_AWAITING_VALIDATION,
                "substate": "queued",
            })
        elif event_type in {
            "validation_started",
            "validation_retry_scheduled",
            "validation_infra_blocked",
            "candidate_promoted",
            "candidate_rejected",
            "candidate_quarantined",
            "candidate_superseded",
        }:
            _require_event_identity_matches_item(value, item)
            if event_type == "validation_started":
                if item["macro_state"] != MACRO_AWAITING_VALIDATION or item["substate"] not in {"queued", "retry"} or value["payload"]:
                    raise PipelineContractError("validation cannot start from current state")
                item["substate"] = "running"
                item["validation_attempt"] += 1
            elif event_type == "validation_retry_scheduled":
                if item["macro_state"] != MACRO_AWAITING_VALIDATION or item["substate"] not in {"running", "infra_blocked"}:
                    raise PipelineContractError("validation retry is invalid")
                _require_reason(value["payload"])
                item["substate"] = "retry"
            elif event_type == "validation_infra_blocked":
                if item["macro_state"] != MACRO_AWAITING_VALIDATION or item["substate"] != "running":
                    raise PipelineContractError("infra block requires running validation")
                _require_reason(value["payload"])
                item["substate"] = "infra_blocked"
            elif event_type in {"candidate_promoted", "candidate_rejected"}:
                if item["macro_state"] != MACRO_AWAITING_VALIDATION or item["substate"] != "running":
                    raise PipelineContractError("validation verdict requires running validation")
                outcome = "promoted" if event_type == "candidate_promoted" else "rejected"
                publication = None
                if outcome == "promoted":
                    publication = _validate_promotion_payload(
                        value["payload"],
                        target_identity=state["target_identity"],
                        candidate_id=item["candidate_id"],
                        artifact_hash=item["artifact_hash"],
                        promotion_authority_resolver=promotion_authority_resolver,
                    )
                    reason = publication["reason"]
                else:
                    reason = _require_reason(value["payload"])
                item.update({
                    "macro_state": MACRO_VALIDATION_COMPLETED,
                    "substate": outcome,
                    "terminal_reason": reason,
                })
                if outcome == "promoted":
                    state["target_published"] = True
                    state["promoted_candidate_id"] = item["candidate_id"]
                    state["published_identity"] = {
                        **state["target_identity"],
                        "candidate_id": item["candidate_id"],
                        "artifact_hash": item["artifact_hash"],
                        **{
                            key: publication[key]
                            for key in _PUBLISHED_PROOF_FIELDS
                        },
                    }
                    for other in items:
                        if other is item or other["macro_state"] == MACRO_VALIDATION_COMPLETED:
                            continue
                        other.update({
                            "macro_state": MACRO_VALIDATION_COMPLETED,
                            "substate": "superseded",
                            "superseded_by_candidate_id": item["candidate_id"],
                            "superseded_by_artifact_hash": item["artifact_hash"],
                            "supersession_fence_digest": publication["promotion_receipt_digest"],
                            "terminal_reason": "target_promoted_by_other_candidate",
                        })
                        _touch(other, value)
            elif event_type in {"candidate_quarantined", "candidate_superseded"}:
                outcome = "quarantined" if event_type == "candidate_quarantined" else "superseded"
                if outcome == "quarantined":
                    reason = _require_reason(value["payload"])
                else:
                    if item["macro_state"] != MACRO_AWAITING_VALIDATION:
                        raise PipelineContractError("only a sealed validation candidate can be superseded")
                    supersession = _validate_supersession_payload(value["payload"])
                    expected_fence = supersession_fence_digest(
                        current,
                        work_item_id=item["work_item_id"],
                        superseded_by_candidate_id=supersession["superseded_by_candidate_id"],
                        superseded_by_artifact_hash=supersession["superseded_by_artifact_hash"],
                    )
                    if supersession["promotion_fence_digest"] != expected_fence:
                        raise PipelineContractError("candidate supersession fence mismatch")
                    reason = supersession["reason"]
                item.update({
                    "macro_state": MACRO_VALIDATION_COMPLETED,
                    "substate": outcome,
                    "terminal_reason": reason,
                })
                if outcome == "superseded":
                    item["superseded_by_candidate_id"] = supersession["superseded_by_candidate_id"]
                    item["superseded_by_artifact_hash"] = supersession["superseded_by_artifact_hash"]
                    item["supersession_fence_digest"] = supersession["promotion_fence_digest"]
        else:  # pragma: no cover - EVENT_TYPES and branches are kept exhaustive.
            raise PipelineContractError("event type has no reducer")
        _touch(item, value)

    state["queue_revision"] = value["queue_revision"]
    state["event_ids"].append(value["event_id"])
    state["event_digests"].append(value["event_digest"])
    state["items"] = sorted(items, key=lambda row: (row["created_revision"], row["work_item_id"]))
    state["projection_digest"] = content_digest(state)
    return validate_projection(state)


def reduce_events(
    target_identity: Mapping[str, Any],
    events: Iterable[Mapping[str, Any]],
    *,
    mechanical_repair_policy_resolver: (
        Callable[[str, str], Mapping[str, Any]] | None
    ) = None,
    promotion_authority_resolver: (
        Callable[[str, str], Mapping[str, Any]] | None
    ) = None,
) -> dict[str, Any]:
    """Replay an ordered event stream from the canonical empty projection."""

    state = initial_projection(target_identity)
    for event in events:
        state = reduce_event(
            state,
            event,
            mechanical_repair_policy_resolver=mechanical_repair_policy_resolver,
            promotion_authority_resolver=promotion_authority_resolver,
        )
    return state
