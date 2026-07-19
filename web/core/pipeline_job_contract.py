"""Content-bound job envelopes and receipts for the evolution workflow kernel.

This module owns values only.  It deliberately does not import the workflow
kernel, open files, read clocks, or dispatch work.  A caller freezes every
timestamp and identity before building an envelope; the durable kernel may then
store that envelope as an effect input without creating a circular dependency.

Strength pre-admission is intentionally narrower than receipt validity.
Official EXE and Arena evidence may be retained for compliance or diagnostics,
but only one complete 70-hand native-TCP sample with the exact active identities
can be structurally accepted by :func:`accept_strength_sample`.

This value-only module cannot open the raw replay or reserve an identity in a
durable admission ledger.  Its successful result is therefore deliberately
``rating_eligible=False``.  A future resolver must verify the referenced raw
replay and a persistence boundary must atomically claim the returned stable
``admission_identity_digest`` before any sample can enter rating.
"""

from __future__ import annotations

import json
import math
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from bot_artifact import canonical_digest


JOB_ENVELOPE_SCHEMA_VERSION = 1
JOB_RECEIPT_SCHEMA_VERSION = 1
JOB_ENVELOPE_KIND = "national-evolution-job-envelope-v1"
JOB_RECEIPT_KIND = "national-evolution-job-receipt-v1"

JOB_OUTCOMES = frozenset({
    "success",
    "candidate_failure",
    "infrastructure_failure",
    "cancelled",
})
PRIORITY_RANKS = {
    "recovery": 100,
    "promotion": 90,
    "compliance": 80,
    "producer": 60,
    "rating": 10,
}
RESOURCE_CLASSES = frozenset({
    "llm",
    "cpu",
    "native_match",
    "official_exe",
    "mixed",
})
EVIDENCE_KINDS = frozenset({
    "strength_sample",
    "gate_report",
    "artifact",
    "log",
})
EVIDENCE_AUTHORITIES = frozenset({
    "native_tcp",
    "official_exe",
    "arena",
    "system",
})
ZERO_STRENGTH_AUTHORITIES = frozenset({"official_exe", "arena"})

_BASE_INPUT_REF_KINDS = frozenset({
    "charter",
    "candidate",
    "contract",
    "runtime",
    "repository",
    "executor",
})
_NATIVE_INPUT_REF_KINDS = frozenset({
    "opponent",
    "evaluator",
    "parser",
    "timing-plan",
    "seed-schedule",
    "replay-verifier",
})
_NATIVE_RATING_AUTHORITY_REF_KINDS = frozenset({
    "published-identity",
    "official-certificate",
    "rating-cycle-authority",
})
INPUT_REF_KINDS = frozenset({
    *_BASE_INPUT_REF_KINDS,
    *_NATIVE_INPUT_REF_KINDS,
    *_NATIVE_RATING_AUTHORITY_REF_KINDS,
    "prompt",
    "contract",
    "evidence-cutoff",
})

# The dispatcher, never a candidate or prompt, owns this closed policy table.
# Adding a job kind is a schema change with positive and negative regressions.
JOB_KIND_POLICIES = {
    "quality-static": {
        "priority_class": "compliance",
        "resource_class": "cpu",
        "required_slots": {"cpu_slots": 1},
        "strength_allowed": False,
        "purpose": "quality-static-gate",
        "required_input_ref_kinds": _BASE_INPUT_REF_KINDS,
        "executor_id": "quality-consumer",
    },
    "quality-dynamic": {
        "priority_class": "compliance",
        "resource_class": "cpu",
        "required_slots": {"cpu_slots": 1},
        "strength_allowed": False,
        "purpose": "quality-dynamic-gate",
        "required_input_ref_kinds": _BASE_INPUT_REF_KINDS,
        "executor_id": "quality-consumer",
    },
    "native-admission": {
        "priority_class": "compliance",
        "resource_class": "native_match",
        "required_slots": {"cpu_slots": 1, "match_slots": 1},
        "strength_allowed": True,
        "purpose": "prepublication-native-admission",
        "required_input_ref_kinds": (
            _BASE_INPUT_REF_KINDS | _NATIVE_INPUT_REF_KINDS
        ),
        "executor_id": "native-admission-consumer",
    },
    "native-rating": {
        "priority_class": "rating",
        "resource_class": "native_match",
        "required_slots": {"cpu_slots": 1, "match_slots": 1},
        "strength_allowed": True,
        "purpose": "published-pool-immutable-rating",
        "required_input_ref_kinds": (
            _BASE_INPUT_REF_KINDS
            | _NATIVE_INPUT_REF_KINDS
            | _NATIVE_RATING_AUTHORITY_REF_KINDS
        ),
        "executor_id": "native-rating-consumer",
    },
    "official-certification": {
        "priority_class": "promotion",
        "resource_class": "official_exe",
        "required_slots": {"cpu_slots": 1, "official_slots": 1},
        "strength_allowed": False,
        "purpose": "official-compliance-certification",
        "required_input_ref_kinds": _BASE_INPUT_REF_KINDS,
        "executor_id": "official-certification-consumer",
    },
}
JOB_KIND_POLICIES = MappingProxyType({
    key: MappingProxyType({
        **value,
        "required_slots": MappingProxyType(dict(value["required_slots"])),
    })
    for key, value in JOB_KIND_POLICIES.items()
})

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$")

_INPUT_REF_FIELDS = frozenset({"kind", "subject", "digest"})
_RESOURCE_FIELDS = frozenset({
    "resource_class",
    "cpu_slots",
    "memory_mb",
    "gpu_slots",
    "match_slots",
    "official_slots",
})
_PRIORITY_FIELDS = frozenset({"class", "rank"})
_RETRY_FIELDS = frozenset({
    "max_attempts",
    "initial_backoff_sec",
    "backoff_multiplier",
    "max_backoff_sec",
    "retryable_outcomes",
})
_DEADLINE_FIELDS = frozenset({
    "submitted_at_epoch",
    "not_before_epoch",
    "expires_at_epoch",
})
_ENVELOPE_FIELDS = frozenset({
    "schema_version",
    "kind",
    "job_id",
    "run_id",
    "draft_id",
    "candidate_id",
    "job_kind",
    "purpose",
    "charter_digest",
    "artifact_digest",
    "input_refs",
    "dependency_receipt_digests",
    "idempotency_key",
    "resource_claim",
    "priority",
    "retry_policy",
    "deadline",
    "idempotency_input_digest",
    "envelope_digest",
})
_EXECUTOR_FIELDS = frozenset({
    "executor_id",
    "implementation_digest",
    "version",
})
_EVIDENCE_FIELDS = frozenset({
    "evidence_id",
    "kind",
    "authority",
    "digest",
    "strength_sample_unit",
    "hands",
    "complete",
    "strength_admitted",
    "candidate_artifact_digest",
    "opponent_artifact_digest",
    "evaluator_digest",
    "parser_digest",
    "timing_plan_digest",
    "seed_schedule_digest",
    "settlements",
    "verifier_digest",
    "replay_digest",
    "runtime_digest",
    "repository_digest",
    "executor_digest",
    "executor_subject",
    "job_kind",
    "purpose",
    "published_identity_digest",
    "official_certificate_digest",
    "rating_cycle_authority_digest",
    "admission_identity_digest",
})
_RECEIPT_FIELDS = frozenset({
    "schema_version",
    "kind",
    "job_id",
    "envelope_digest",
    "attempt",
    "lease_epoch",
    "lease_owner",
    "executor",
    "outcome",
    "started_at_epoch",
    "finished_at_epoch",
    "result_digest",
    "evidence",
    "complete_70_hand_sample_ids",
    "error",
    "receipt_digest",
})


class JobContractError(ValueError):
    """A job envelope or receipt is not a valid schema-v1 value."""

    def __init__(self, issues: Iterable[str]):
        self.issues = tuple(dict.fromkeys(str(issue) for issue in issues))
        super().__init__("; ".join(self.issues))


class JobIdempotencyConflict(JobContractError):
    """One idempotency key was reused for a different frozen request."""


def _plain_int(value: Any) -> int | None:
    return value if type(value) is int else None


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _digest_ok(value: Any) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _safe_id_ok(value: Any) -> bool:
    return isinstance(value, str) and _SAFE_ID.fullmatch(value) is not None


def _json_value_issues(
    value: Any,
    *,
    path: str = "value",
    _seen: set[int] | None = None,
    _depth: int = 0,
) -> list[str]:
    """Reject values that Python's permissive JSON encoder would normalize."""

    if _depth > 64:
        return [f"{path}_nesting_too_deep"]
    if value is None or isinstance(value, (str, bool)):
        return []
    if type(value) is int:
        return (
            []
            if -(2**63) <= value <= (2**63 - 1)
            else [f"{path}_integer_out_of_range"]
        )
    if isinstance(value, float):
        return [] if math.isfinite(value) else [f"{path}_non_finite"]
    if isinstance(value, list):
        seen = _seen if _seen is not None else set()
        marker = id(value)
        if marker in seen:
            return [f"{path}_cyclic"]
        seen.add(marker)
        issues: list[str] = []
        for index, item in enumerate(value):
            issues.extend(_json_value_issues(
                item,
                path=f"{path}_{index}",
                _seen=seen,
                _depth=_depth + 1,
            ))
        seen.remove(marker)
        return issues
    if isinstance(value, dict):
        seen = _seen if _seen is not None else set()
        marker = id(value)
        if marker in seen:
            return [f"{path}_cyclic"]
        seen.add(marker)
        issues = []
        for key, item in value.items():
            if not isinstance(key, str):
                issues.append(f"{path}_key_not_string")
                continue
            issues.extend(_json_value_issues(
                item,
                path=f"{path}_{key}",
                _seen=seen,
                _depth=_depth + 1,
            ))
        seen.remove(marker)
        return issues
    return [f"{path}_not_json_value"]


def _frozen_copy(value: Any) -> Any:
    issues = _json_value_issues(value)
    if issues:
        raise JobContractError(issues)
    try:
        return json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise JobContractError(["value_json_freeze_failed"]) from exc


def _field_issues(value: Any, fields: frozenset[str], label: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label}_not_object"]
    if set(value) != fields:
        return [f"{label}_fields_mismatch"]
    return []


def _input_ref_issues(value: Any, index: int) -> list[str]:
    label = f"job_input_ref_{index}"
    issues = _field_issues(value, _INPUT_REF_FIELDS, label)
    if issues:
        return issues
    assert isinstance(value, dict)
    if not _safe_id_ok(value.get("kind")):
        issues.append(f"{label}_kind_invalid")
    elif value.get("kind") not in INPUT_REF_KINDS:
        issues.append(f"{label}_kind_unknown")
    if not _safe_id_ok(value.get("subject")):
        issues.append(f"{label}_subject_invalid")
    if not _digest_ok(value.get("digest")):
        issues.append(f"{label}_digest_invalid")
    return issues


def _resource_claim_issues(value: Any) -> list[str]:
    issues = _field_issues(value, _RESOURCE_FIELDS, "job_resource_claim")
    if issues:
        return issues
    assert isinstance(value, dict)
    resource_class = value.get("resource_class")
    if not isinstance(resource_class, str) or resource_class not in RESOURCE_CLASSES:
        issues.append("job_resource_class_invalid")
    limits = {
        "cpu_slots": (0, 64),
        "memory_mb": (1, 262_144),
        "gpu_slots": (0, 8),
        "match_slots": (0, 28),
        "official_slots": (0, 1),
    }
    parsed_values: dict[str, int | None] = {}
    for field, (minimum, maximum) in limits.items():
        parsed = _plain_int(value.get(field))
        parsed_values[field] = parsed
        if parsed is None or not minimum <= parsed <= maximum:
            issues.append(f"job_resource_{field}_invalid")
    if resource_class == "native_match" and (
        parsed_values["match_slots"] is None
        or parsed_values["match_slots"] < 1
    ):
        issues.append("job_native_match_slot_missing")
    if resource_class in {"cpu", "native_match", "official_exe"} and (
        parsed_values["cpu_slots"] is None
        or parsed_values["cpu_slots"] < 1
    ):
        issues.append("job_cpu_slot_missing")
    if resource_class == "official_exe" and value.get("official_slots") != 1:
        issues.append("job_official_slot_invalid")
    if resource_class not in {"native_match", "mixed"} and value.get("match_slots") != 0:
        issues.append("job_match_slots_scope_invalid")
    if resource_class not in {"official_exe", "mixed"} and value.get("official_slots") != 0:
        issues.append("job_official_slots_scope_invalid")
    if resource_class == "mixed" and not any(
        parsed_values.get(field, 0) not in {None, 0}
        for field in ("cpu_slots", "gpu_slots", "match_slots", "official_slots")
    ):
        issues.append("job_mixed_resource_empty")
    return issues


def _input_refs_by_kind(envelope: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    refs: dict[str, Mapping[str, Any]] = {}
    values = envelope.get("input_refs")
    if not isinstance(values, list):
        return refs
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
            continue
        refs.setdefault(value["kind"], value)
    return refs


def _job_policy_issues(envelope: Mapping[str, Any]) -> list[str]:
    job_kind = envelope.get("job_kind")
    policy = JOB_KIND_POLICIES.get(job_kind) if isinstance(job_kind, str) else None
    if policy is None:
        return ["job_envelope_job_kind_unknown"]

    issues: list[str] = []
    if envelope.get("purpose") != policy["purpose"]:
        issues.append("job_purpose_policy_mismatch")
    priority = envelope.get("priority")
    if isinstance(priority, dict) and priority.get("class") != policy["priority_class"]:
        issues.append("job_priority_class_policy_mismatch")
    claim = envelope.get("resource_claim")
    if isinstance(claim, dict):
        if claim.get("resource_class") != policy["resource_class"]:
            issues.append("job_resource_class_policy_mismatch")
        for field, minimum in policy["required_slots"].items():
            value = _plain_int(claim.get(field))
            if value is None or value < minimum:
                issues.append(f"job_resource_{field}_policy_minimum_missing")

    refs = _input_refs_by_kind(envelope)
    for kind in sorted(policy["required_input_ref_kinds"]):
        if kind not in refs:
            issues.append(f"job_input_ref_{kind.replace('-', '_')}_missing")
    charter_ref = refs.get("charter")
    if (
        isinstance(charter_ref, Mapping)
        and charter_ref.get("digest") != envelope.get("charter_digest")
    ):
        issues.append("job_charter_ref_digest_mismatch")
    candidate_ref = refs.get("candidate")
    if isinstance(candidate_ref, Mapping):
        if candidate_ref.get("digest") != envelope.get("artifact_digest"):
            issues.append("job_candidate_ref_digest_mismatch")
        if candidate_ref.get("subject") != envelope.get("candidate_id"):
            issues.append("job_candidate_ref_subject_mismatch")
    executor_ref = refs.get("executor")
    if (
        isinstance(executor_ref, Mapping)
        and executor_ref.get("subject") != policy["executor_id"]
    ):
        issues.append("job_executor_ref_subject_policy_mismatch")
    return issues


def _priority_issues(value: Any) -> list[str]:
    issues = _field_issues(value, _PRIORITY_FIELDS, "job_priority")
    if issues:
        return issues
    assert isinstance(value, dict)
    priority_class = value.get("class")
    if not isinstance(priority_class, str) or priority_class not in PRIORITY_RANKS:
        issues.append("job_priority_class_invalid")
    elif value.get("rank") != PRIORITY_RANKS[priority_class]:
        issues.append("job_priority_rank_mismatch")
    return issues


def _retry_policy_issues(value: Any) -> list[str]:
    issues = _field_issues(value, _RETRY_FIELDS, "job_retry_policy")
    if issues:
        return issues
    assert isinstance(value, dict)
    maximum = _plain_int(value.get("max_attempts"))
    if maximum is None or not 1 <= maximum <= 20:
        issues.append("job_retry_max_attempts_invalid")
    initial = _finite_number(value.get("initial_backoff_sec"))
    multiplier = _finite_number(value.get("backoff_multiplier"))
    cap = _finite_number(value.get("max_backoff_sec"))
    if initial is None or initial < 0 or initial > 86_400:
        issues.append("job_retry_initial_backoff_invalid")
    if multiplier is None or multiplier < 1 or multiplier > 10:
        issues.append("job_retry_multiplier_invalid")
    if cap is None or cap < 0 or cap > 604_800:
        issues.append("job_retry_max_backoff_invalid")
    if initial is not None and cap is not None and cap < initial:
        issues.append("job_retry_backoff_order_invalid")
    outcomes = value.get("retryable_outcomes")
    if (
        not isinstance(outcomes, list)
        or any(not isinstance(item, str) for item in outcomes)
    ):
        issues.append("job_retryable_outcomes_invalid")
    else:
        if outcomes != sorted(set(outcomes)) or any(
            item != "infrastructure_failure" for item in outcomes
        ):
            issues.append("job_retryable_outcomes_invalid")
        elif maximum is not None:
            if maximum > 1 and outcomes != ["infrastructure_failure"]:
                issues.append("job_retryable_outcome_missing")
            if maximum == 1 and outcomes:
                issues.append("job_single_attempt_retryable_outcome_forbidden")
    return issues


def _deadline_issues(value: Any) -> list[str]:
    issues = _field_issues(value, _DEADLINE_FIELDS, "job_deadline")
    if issues:
        return issues
    assert isinstance(value, dict)
    submitted = _finite_number(value.get("submitted_at_epoch"))
    not_before = _finite_number(value.get("not_before_epoch"))
    expires = _finite_number(value.get("expires_at_epoch"))
    if submitted is None or submitted < 0:
        issues.append("job_deadline_submitted_invalid")
    if not_before is None or not_before < 0:
        issues.append("job_deadline_not_before_invalid")
    if expires is None or expires <= 0:
        issues.append("job_deadline_expires_invalid")
    if (
        submitted is not None
        and not_before is not None
        and submitted > not_before
    ):
        issues.append("job_deadline_submission_order_invalid")
    if not_before is not None and expires is not None and expires <= not_before:
        issues.append("job_deadline_expiry_order_invalid")
    return issues


def _envelope_idempotency_body(envelope: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in envelope.items()
        if key not in {"idempotency_input_digest", "envelope_digest"}
    }


def job_envelope_issues(envelope: Any) -> list[str]:
    issues = _field_issues(envelope, _ENVELOPE_FIELDS, "job_envelope")
    if issues:
        return issues
    assert isinstance(envelope, dict)
    json_issues = _json_value_issues(envelope, path="job_envelope")
    issues.extend(json_issues)
    if json_issues:
        return list(dict.fromkeys(issues))
    if envelope.get("schema_version") != JOB_ENVELOPE_SCHEMA_VERSION:
        issues.append("job_envelope_schema_version_mismatch")
    if envelope.get("kind") != JOB_ENVELOPE_KIND:
        issues.append("job_envelope_kind_mismatch")
    for field in (
        "job_id",
        "run_id",
        "draft_id",
        "candidate_id",
        "job_kind",
        "purpose",
        "idempotency_key",
    ):
        if not _safe_id_ok(envelope.get(field)):
            issues.append(f"job_envelope_{field}_invalid")
    if (
        _safe_id_ok(envelope.get("draft_id"))
        and _safe_id_ok(envelope.get("candidate_id"))
        and envelope.get("draft_id") == envelope.get("candidate_id")
    ):
        issues.append("job_envelope_draft_candidate_identity_collapsed")
    for field in ("charter_digest", "artifact_digest"):
        if not _digest_ok(envelope.get(field)):
            issues.append(f"job_envelope_{field}_invalid")

    input_refs = envelope.get("input_refs")
    if not isinstance(input_refs, list) or not input_refs:
        issues.append("job_envelope_input_refs_missing")
    else:
        for index, value in enumerate(input_refs):
            issues.extend(_input_ref_issues(value, index))
        canonical_order = sorted(
            input_refs,
            key=lambda item: (
                str(item.get("kind") or "") if isinstance(item, dict) else "",
                str(item.get("subject") or "") if isinstance(item, dict) else "",
                str(item.get("digest") or "") if isinstance(item, dict) else "",
            ),
        )
        identities = [
            item.get("kind")
            for item in input_refs
            if isinstance(item, dict)
            and isinstance(item.get("kind"), str)
        ]
        if input_refs != canonical_order:
            issues.append("job_envelope_input_refs_not_canonical")
        if len(identities) != len(set(identities)):
            issues.append("job_envelope_input_refs_duplicate")

    dependencies = envelope.get("dependency_receipt_digests")
    if not isinstance(dependencies, list) or any(
        not isinstance(value, str) for value in dependencies
    ):
        issues.append("job_envelope_dependency_receipts_invalid")
    elif (
        dependencies != sorted(set(dependencies))
        or any(not _digest_ok(value) for value in dependencies)
    ):
        issues.append("job_envelope_dependency_receipts_invalid")
    issues.extend(_resource_claim_issues(envelope.get("resource_claim")))
    issues.extend(_priority_issues(envelope.get("priority")))
    issues.extend(_retry_policy_issues(envelope.get("retry_policy")))
    issues.extend(_deadline_issues(envelope.get("deadline")))
    issues.extend(_job_policy_issues(envelope))

    expected_input_digest = canonical_digest(
        _envelope_idempotency_body(envelope)
    )
    if envelope.get("idempotency_input_digest") != expected_input_digest:
        issues.append("job_envelope_idempotency_input_digest_mismatch")
    unsigned = {key: value for key, value in envelope.items() if key != "envelope_digest"}
    if envelope.get("envelope_digest") != canonical_digest(unsigned):
        issues.append("job_envelope_digest_mismatch")
    return list(dict.fromkeys(issues))


def build_job_envelope(
    *,
    job_id: str,
    run_id: str,
    draft_id: str,
    candidate_id: str,
    job_kind: str,
    charter_digest: str,
    artifact_digest: str,
    input_refs: Iterable[Mapping[str, Any]],
    dependency_receipt_digests: Iterable[str] = (),
    idempotency_key: str,
    resource_claim: Mapping[str, Any],
    priority_class: str,
    retry_policy: Mapping[str, Any],
    deadline: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one canonical immutable job input, or raise before dispatch."""

    frozen_refs = [_frozen_copy(dict(value)) for value in input_refs]
    frozen_refs.sort(key=lambda value: (
        str(value.get("kind") or ""),
        str(value.get("subject") or ""),
        str(value.get("digest") or ""),
    ))
    priority = {
        "class": str(priority_class),
        "rank": PRIORITY_RANKS.get(str(priority_class)),
    }
    envelope = {
        "schema_version": JOB_ENVELOPE_SCHEMA_VERSION,
        "kind": JOB_ENVELOPE_KIND,
        "job_id": job_id,
        "run_id": run_id,
        "draft_id": draft_id,
        "candidate_id": candidate_id,
        "job_kind": job_kind,
        "purpose": (
            JOB_KIND_POLICIES.get(job_kind) or {}
        ).get("purpose"),
        "charter_digest": charter_digest,
        "artifact_digest": artifact_digest,
        "input_refs": frozen_refs,
        "dependency_receipt_digests": _frozen_copy(
            list(dependency_receipt_digests)
        ),
        "idempotency_key": idempotency_key,
        "resource_claim": _frozen_copy(dict(resource_claim)),
        "priority": priority,
        "retry_policy": _frozen_copy(dict(retry_policy)),
        "deadline": _frozen_copy(dict(deadline)),
    }
    dependencies = envelope["dependency_receipt_digests"]
    if isinstance(dependencies, list) and all(
        isinstance(value, str) for value in dependencies
    ):
        envelope["dependency_receipt_digests"] = sorted(dependencies)
    envelope["idempotency_input_digest"] = canonical_digest(envelope)
    envelope["envelope_digest"] = canonical_digest(envelope)
    issues = job_envelope_issues(envelope)
    if issues:
        raise JobContractError(issues)
    return envelope


def assert_idempotent_job_replay(
    existing: Any,
    proposed: Any,
) -> bool:
    """Return true for an exact replay; reject same-key input drift.

    Different keys are different logical jobs and return ``False``.  Reusing a
    key for anything other than the exact envelope is a hard conflict, including
    a different job id, deadline, retry policy, or resource claim.
    """

    existing_issues = job_envelope_issues(existing)
    proposed_issues = job_envelope_issues(proposed)
    if existing_issues or proposed_issues:
        raise JobContractError([
            *(f"existing:{issue}" for issue in existing_issues),
            *(f"proposed:{issue}" for issue in proposed_issues),
        ])
    assert isinstance(existing, dict) and isinstance(proposed, dict)
    if existing["idempotency_key"] != proposed["idempotency_key"]:
        return False
    if (
        existing["idempotency_input_digest"]
        != proposed["idempotency_input_digest"]
        or existing["envelope_digest"] != proposed["envelope_digest"]
        or existing != proposed
    ):
        raise JobIdempotencyConflict(["job_idempotency_key_input_conflict"])
    return True


def _executor_issues(value: Any) -> list[str]:
    issues = _field_issues(value, _EXECUTOR_FIELDS, "job_executor")
    if issues:
        return issues
    assert isinstance(value, dict)
    if not _safe_id_ok(value.get("executor_id")):
        issues.append("job_executor_id_invalid")
    if not _digest_ok(value.get("implementation_digest")):
        issues.append("job_executor_implementation_digest_invalid")
    version = value.get("version")
    if not isinstance(version, str) or not version or len(version) > 128:
        issues.append("job_executor_version_invalid")
    return issues


def _strength_admission_identity(value: Mapping[str, Any]) -> str:
    """Return the retry-stable, complete identity later used by durable CAS.

    Attempt, lease, receipt timestamps *and the produced replay digest* are
    intentionally absent: retrying the exact frozen native match must resolve
    to one logical sample identity even when a retry receipt arrives with a
    different output proof.  ``replay_digest`` remains content-bound in the
    evidence/receipt and is checked by the external replay resolver; the stable
    identity makes the durable CAS detect two different replay payloads for one
    frozen match instead of admitting them as two strength samples.  Every
    executable/verifier identity which can change the meaning of that proof is
    included so a runtime, repository, executor, or verifier upgrade cannot
    collide with old evidence.  Job kind, policy-owned purpose and executor
    subject partition pre-publication admission from post-publication rating;
    rating additionally binds its published identity, signed certificate and
    immutable cycle authority.
    """

    return canonical_digest({
        "kind": "national-native-strength-admission-identity-v1",
        "job_kind": value.get("job_kind"),
        "purpose": value.get("purpose"),
        "candidate_artifact_digest": value.get("candidate_artifact_digest"),
        "opponent_artifact_digest": value.get("opponent_artifact_digest"),
        "evaluator_digest": value.get("evaluator_digest"),
        "parser_digest": value.get("parser_digest"),
        "timing_plan_digest": value.get("timing_plan_digest"),
        "seed_schedule_digest": value.get("seed_schedule_digest"),
        "verifier_digest": value.get("verifier_digest"),
        "runtime_digest": value.get("runtime_digest"),
        "repository_digest": value.get("repository_digest"),
        "executor_digest": value.get("executor_digest"),
        "executor_subject": value.get("executor_subject"),
        "published_identity_digest": value.get("published_identity_digest"),
        "official_certificate_digest": value.get("official_certificate_digest"),
        "rating_cycle_authority_digest": value.get(
            "rating_cycle_authority_digest"
        ),
        "hands": value.get("hands"),
        "settlements": value.get("settlements"),
    })


def _evidence_issues(value: Any, index: int) -> list[str]:
    label = f"job_evidence_{index}"
    issues = _field_issues(value, _EVIDENCE_FIELDS, label)
    if issues:
        return issues
    assert isinstance(value, dict)
    if not _safe_id_ok(value.get("evidence_id")):
        issues.append(f"{label}_id_invalid")
    kind = value.get("kind")
    authority = value.get("authority")
    if not isinstance(kind, str) or kind not in EVIDENCE_KINDS:
        issues.append(f"{label}_kind_invalid")
    if not isinstance(authority, str) or authority not in EVIDENCE_AUTHORITIES:
        issues.append(f"{label}_authority_invalid")
    if not _digest_ok(value.get("digest")):
        issues.append(f"{label}_digest_invalid")
    if type(value.get("complete")) is not bool:
        issues.append(f"{label}_complete_invalid")
    if type(value.get("strength_admitted")) is not bool:
        issues.append(f"{label}_strength_admitted_invalid")

    unit = value.get("strength_sample_unit")
    hands = value.get("hands")
    identity_fields = (
        "candidate_artifact_digest",
        "opponent_artifact_digest",
        "evaluator_digest",
        "parser_digest",
        "timing_plan_digest",
        "seed_schedule_digest",
        "verifier_digest",
        "replay_digest",
        "runtime_digest",
        "repository_digest",
        "executor_digest",
        "admission_identity_digest",
    )
    identity_text_fields = (
        "executor_subject",
        "job_kind",
        "purpose",
    )
    rating_authority_fields = (
        "published_identity_digest",
        "official_certificate_digest",
        "rating_cycle_authority_digest",
    )
    if kind == "strength_sample":
        if unit != "70_hand_match":
            issues.append(f"{label}_sample_unit_invalid")
        parsed_hands = _plain_int(hands)
        if parsed_hands is None or parsed_hands < 0:
            issues.append(f"{label}_hands_invalid")
        settlements = _plain_int(value.get("settlements"))
        if settlements is None or settlements < 0:
            issues.append(f"{label}_settlements_invalid")
        for field in identity_fields:
            if not _digest_ok(value.get(field)):
                issues.append(f"{label}_{field}_invalid")
        for field in identity_text_fields:
            if not _safe_id_ok(value.get(field)):
                issues.append(f"{label}_{field}_invalid")
        job_kind = value.get("job_kind")
        policy = JOB_KIND_POLICIES.get(job_kind)
        if policy is None or not policy["strength_allowed"]:
            issues.append(f"{label}_job_kind_forbidden")
        elif value.get("purpose") != policy["purpose"]:
            issues.append(f"{label}_purpose_job_kind_mismatch")
        if job_kind == "native-rating":
            for field in rating_authority_fields:
                if not _digest_ok(value.get(field)):
                    issues.append(f"{label}_{field}_invalid")
        else:
            for field in rating_authority_fields:
                if value.get(field) is not None:
                    issues.append(f"{label}_{field}_unexpected")
        if (
            _digest_ok(value.get("admission_identity_digest"))
            and value.get("admission_identity_digest")
            != _strength_admission_identity(value)
        ):
            issues.append(f"{label}_admission_identity_digest_mismatch")
        if value.get("strength_admitted") is True:
            if authority != "native_tcp":
                issues.append(f"{label}_strength_authority_forbidden")
            if hands != 70:
                issues.append(f"{label}_strength_hands_not_70")
            if value.get("complete") is not True:
                issues.append(f"{label}_strength_incomplete")
            if value.get("settlements") != 69:
                issues.append(f"{label}_strength_settlements_not_69")
    else:
        if unit is not None or hands is not None or value.get("settlements") is not None:
            issues.append(f"{label}_non_strength_shape_invalid")
        if value.get("strength_admitted") is not False:
            issues.append(f"{label}_non_strength_admission_forbidden")
        for field in identity_fields:
            if value.get(field) is not None:
                issues.append(f"{label}_{field}_unexpected")
        for field in (*identity_text_fields, *rating_authority_fields):
            if value.get(field) is not None:
                issues.append(f"{label}_{field}_unexpected")
    if (
        isinstance(authority, str)
        and authority in ZERO_STRENGTH_AUTHORITIES
        and value.get("strength_admitted") is True
    ):
        issues.append(f"{label}_zero_strength_authority_admitted")
    return issues


def build_evidence_ref(
    *,
    evidence_id: str,
    kind: str,
    authority: str,
    digest: str,
    strength_sample_unit: str | None = None,
    hands: int | None = None,
    complete: bool = False,
    strength_admitted: bool = False,
    candidate_artifact_digest: str | None = None,
    opponent_artifact_digest: str | None = None,
    evaluator_digest: str | None = None,
    parser_digest: str | None = None,
    timing_plan_digest: str | None = None,
    seed_schedule_digest: str | None = None,
    settlements: int | None = None,
    verifier_digest: str | None = None,
    replay_digest: str | None = None,
    runtime_digest: str | None = None,
    repository_digest: str | None = None,
    executor_digest: str | None = None,
    executor_subject: str | None = None,
    job_kind: str | None = None,
    purpose: str | None = None,
    published_identity_digest: str | None = None,
    official_certificate_digest: str | None = None,
    rating_cycle_authority_digest: str | None = None,
    admission_identity_digest: str | None = None,
) -> dict[str, Any]:
    evidence = {
        "evidence_id": evidence_id,
        "kind": kind,
        "authority": authority,
        "digest": digest,
        "strength_sample_unit": strength_sample_unit,
        "hands": hands,
        "complete": complete,
        "strength_admitted": strength_admitted,
        "candidate_artifact_digest": candidate_artifact_digest,
        "opponent_artifact_digest": opponent_artifact_digest,
        "evaluator_digest": evaluator_digest,
        "parser_digest": parser_digest,
        "timing_plan_digest": timing_plan_digest,
        "seed_schedule_digest": seed_schedule_digest,
        "settlements": settlements,
        "verifier_digest": verifier_digest,
        "replay_digest": replay_digest,
        "runtime_digest": runtime_digest,
        "repository_digest": repository_digest,
        "executor_digest": executor_digest,
        "executor_subject": executor_subject,
        "job_kind": job_kind,
        "purpose": purpose,
        "published_identity_digest": published_identity_digest,
        "official_certificate_digest": official_certificate_digest,
        "rating_cycle_authority_digest": rating_cycle_authority_digest,
        "admission_identity_digest": admission_identity_digest,
    }
    if kind == "strength_sample" and admission_identity_digest is None:
        evidence["admission_identity_digest"] = _strength_admission_identity(
            evidence
        )
    issues = _evidence_issues(evidence, 0)
    if issues:
        raise JobContractError(issues)
    return evidence


def job_receipt_issues(
    receipt: Any,
    *,
    envelope: Any | None = None,
) -> list[str]:
    issues = _field_issues(receipt, _RECEIPT_FIELDS, "job_receipt")
    if issues:
        return issues
    assert isinstance(receipt, dict)
    json_issues = _json_value_issues(receipt, path="job_receipt")
    issues.extend(json_issues)
    if json_issues:
        return list(dict.fromkeys(issues))
    if receipt.get("schema_version") != JOB_RECEIPT_SCHEMA_VERSION:
        issues.append("job_receipt_schema_version_mismatch")
    if receipt.get("kind") != JOB_RECEIPT_KIND:
        issues.append("job_receipt_kind_mismatch")
    for field in ("job_id", "lease_owner"):
        if not _safe_id_ok(receipt.get(field)):
            issues.append(f"job_receipt_{field}_invalid")
    for field in ("envelope_digest", "result_digest"):
        if not _digest_ok(receipt.get(field)):
            issues.append(f"job_receipt_{field}_invalid")
    for field in ("attempt", "lease_epoch"):
        parsed = _plain_int(receipt.get(field))
        if parsed is None or parsed < 1:
            issues.append(f"job_receipt_{field}_invalid")
    issues.extend(_executor_issues(receipt.get("executor")))
    outcome = receipt.get("outcome")
    if not isinstance(outcome, str) or outcome not in JOB_OUTCOMES:
        issues.append("job_receipt_outcome_invalid")
    started = _finite_number(receipt.get("started_at_epoch"))
    finished = _finite_number(receipt.get("finished_at_epoch"))
    if started is None or started < 0:
        issues.append("job_receipt_started_at_invalid")
    if finished is None or finished < 0:
        issues.append("job_receipt_finished_at_invalid")
    if started is not None and finished is not None and finished < started:
        issues.append("job_receipt_timing_order_invalid")
    error = receipt.get("error")
    if not isinstance(error, str) or len(error) > 4000:
        issues.append("job_receipt_error_invalid")
    elif outcome == "success" and error:
        issues.append("job_receipt_success_error_forbidden")
    elif outcome != "success" and not error:
        issues.append("job_receipt_failure_error_missing")

    evidence = receipt.get("evidence")
    evidence_ids: list[str] = []
    strength_admission_ids: list[str] = []
    if not isinstance(evidence, list):
        issues.append("job_receipt_evidence_not_list")
        evidence = []
    else:
        for index, value in enumerate(evidence):
            issues.extend(_evidence_issues(value, index))
            if isinstance(value, dict) and isinstance(value.get("evidence_id"), str):
                evidence_ids.append(value["evidence_id"])
            if (
                isinstance(value, dict)
                and value.get("kind") == "strength_sample"
                and isinstance(value.get("admission_identity_digest"), str)
            ):
                strength_admission_ids.append(value["admission_identity_digest"])
        if evidence_ids != sorted(evidence_ids):
            issues.append("job_receipt_evidence_not_canonical")
        if len(evidence_ids) != len(set(evidence_ids)):
            issues.append("job_receipt_evidence_duplicate")
        if len(strength_admission_ids) != len(set(strength_admission_ids)):
            issues.append("job_receipt_strength_admission_identity_duplicate")
    sample_ids = receipt.get("complete_70_hand_sample_ids")
    if not isinstance(sample_ids, list) or any(
        not isinstance(value, str) for value in sample_ids
    ):
        issues.append("job_receipt_complete_70_ids_invalid")
        sample_ids = []
    elif (
        sample_ids != sorted(set(sample_ids))
        or any(not _safe_id_ok(value) for value in sample_ids)
    ):
        issues.append("job_receipt_complete_70_ids_invalid")
        sample_ids = []
    admitted_ids = sorted(
        str(value.get("evidence_id"))
        for value in evidence
        if isinstance(value, dict)
        and value.get("kind") == "strength_sample"
        and value.get("authority") == "native_tcp"
        and value.get("strength_sample_unit") == "70_hand_match"
        and value.get("hands") == 70
        and value.get("complete") is True
        and value.get("strength_admitted") is True
    )
    if sample_ids != admitted_ids:
        issues.append("job_receipt_complete_70_ids_evidence_mismatch")
    if outcome != "success" and sample_ids:
        issues.append("job_receipt_non_success_strength_forbidden")

    if envelope is not None:
        envelope_errors = job_envelope_issues(envelope)
        issues.extend(f"job_receipt_envelope:{item}" for item in envelope_errors)
        if not envelope_errors and isinstance(envelope, dict):
            if receipt.get("job_id") != envelope.get("job_id"):
                issues.append("job_receipt_job_id_mismatch")
            if receipt.get("envelope_digest") != envelope.get("envelope_digest"):
                issues.append("job_receipt_envelope_digest_mismatch")
            policy = JOB_KIND_POLICIES.get(envelope.get("job_kind"))
            if policy is not None and admitted_ids and not policy["strength_allowed"]:
                issues.append("job_receipt_strength_job_kind_forbidden")
            refs = _input_refs_by_kind(envelope)
            for item in evidence:
                if not isinstance(item, dict) or item.get("kind") != "strength_sample":
                    continue
                if item.get("job_kind") != envelope.get("job_kind"):
                    issues.append("job_receipt_strength_job_kind_mismatch")
                if policy is None or item.get("purpose") != envelope.get(
                    "purpose"
                ):
                    issues.append("job_receipt_strength_purpose_mismatch")
                executor_ref_for_sample = refs.get("executor")
                if not isinstance(executor_ref_for_sample, Mapping) or (
                    item.get("executor_subject")
                    != executor_ref_for_sample.get("subject")
                ):
                    issues.append("job_receipt_strength_executor_subject_mismatch")
                authority_refs = {
                    "published_identity_digest": "published-identity",
                    "official_certificate_digest": "official-certificate",
                    "rating_cycle_authority_digest": "rating-cycle-authority",
                }
                for field, ref_kind in authority_refs.items():
                    expected_authority = (
                        (refs.get(ref_kind) or {}).get("digest")
                        if envelope.get("job_kind") == "native-rating"
                        else None
                    )
                    if item.get(field) != expected_authority:
                        issues.append(
                            f"job_receipt_strength_{field}_mismatch"
                        )
            executor = receipt.get("executor")
            executor_ref = refs.get("executor")
            if isinstance(executor, dict) and isinstance(executor_ref, Mapping):
                if executor.get("executor_id") != executor_ref.get("subject"):
                    issues.append("job_receipt_executor_id_ref_mismatch")
                if (
                    executor.get("implementation_digest")
                    != executor_ref.get("digest")
                ):
                    issues.append("job_receipt_executor_digest_ref_mismatch")
            maximum = (envelope.get("retry_policy") or {}).get("max_attempts")
            if _plain_int(receipt.get("attempt")) is not None and (
                receipt["attempt"] > maximum
            ):
                issues.append("job_receipt_attempt_exceeds_policy")
            deadline = envelope.get("deadline") or {}
            if started is not None and started < deadline.get("not_before_epoch", 0):
                issues.append("job_receipt_started_before_window")
            if finished is not None and finished > deadline.get("expires_at_epoch", -1):
                issues.append("job_receipt_finished_after_deadline")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    if receipt.get("receipt_digest") != canonical_digest(unsigned):
        issues.append("job_receipt_digest_mismatch")
    return list(dict.fromkeys(issues))


def build_job_receipt(
    *,
    envelope: Mapping[str, Any],
    attempt: int,
    lease_epoch: int,
    lease_owner: str,
    executor: Mapping[str, Any],
    outcome: str,
    started_at_epoch: float,
    finished_at_epoch: float,
    result_digest: str,
    evidence: Iterable[Mapping[str, Any]] = (),
    complete_70_hand_sample_ids: Iterable[str] = (),
    error: str = "",
) -> dict[str, Any]:
    envelope_issues = job_envelope_issues(envelope)
    if envelope_issues:
        raise JobContractError(envelope_issues)
    frozen_evidence = [_frozen_copy(dict(value)) for value in evidence]
    frozen_evidence.sort(key=lambda value: str(value.get("evidence_id") or ""))
    receipt = {
        "schema_version": JOB_RECEIPT_SCHEMA_VERSION,
        "kind": JOB_RECEIPT_KIND,
        "job_id": envelope.get("job_id"),
        "envelope_digest": envelope.get("envelope_digest"),
        "attempt": attempt,
        "lease_epoch": lease_epoch,
        "lease_owner": lease_owner,
        "executor": _frozen_copy(dict(executor)),
        "outcome": outcome,
        "started_at_epoch": started_at_epoch,
        "finished_at_epoch": finished_at_epoch,
        "result_digest": result_digest,
        "evidence": frozen_evidence,
        "complete_70_hand_sample_ids": _frozen_copy(
            list(complete_70_hand_sample_ids)
        ),
        "error": error,
    }
    sample_ids = receipt["complete_70_hand_sample_ids"]
    if isinstance(sample_ids, list) and all(
        isinstance(value, str) for value in sample_ids
    ):
        receipt["complete_70_hand_sample_ids"] = sorted(sample_ids)
    receipt["receipt_digest"] = canonical_digest(receipt)
    issues = job_receipt_issues(receipt, envelope=envelope)
    if issues:
        raise JobContractError(issues)
    return receipt


def strength_sample_acceptance_issues(
    receipt: Any,
    *,
    envelope: Any,
    sample_id: str,
    expected_candidate_id: str,
    expected_candidate_artifact_digest: str,
    expected_charter_digest: str,
    expected_evaluation_contract_digest: str,
    expected_attempt: int,
    expected_lease_epoch: int,
    expected_lease_owner: str,
    lease_until_epoch: float,
    accepted_at_epoch: float,
    expected_opponent_artifact_digest: str,
    expected_evaluator_digest: str,
    expected_parser_digest: str,
    expected_timing_plan_digest: str,
    expected_seed_schedule_digest: str,
    expected_replay_verifier_digest: str,
    expected_runtime_digest: str,
    expected_repository_digest: str,
    expected_executor_digest: str,
    expected_published_identity_digest: str | None,
    expected_official_certificate_digest: str | None,
    expected_rating_cycle_authority_digest: str | None,
) -> list[str]:
    """Return why one receipt cannot enter external replay/CAS verification."""

    issues = list(job_receipt_issues(receipt, envelope=envelope))
    if issues or not isinstance(receipt, dict) or not isinstance(envelope, dict):
        return list(dict.fromkeys(issues or ["strength_receipt_invalid"]))
    if receipt.get("outcome") != "success":
        issues.append("strength_receipt_outcome_not_success")
    policy = JOB_KIND_POLICIES.get(envelope.get("job_kind"))
    if policy is None or not policy["strength_allowed"]:
        issues.append("strength_job_kind_forbidden")
    if not _safe_id_ok(sample_id):
        issues.append("strength_sample_id_invalid")
    if not _safe_id_ok(expected_candidate_id):
        issues.append("strength_expected_candidate_id_invalid")
    elif envelope.get("candidate_id") != expected_candidate_id:
        issues.append("strength_active_candidate_id_mismatch")
    for field, expected_digest, observed_digest in (
        (
            "candidate_artifact",
            expected_candidate_artifact_digest,
            envelope.get("artifact_digest"),
        ),
        ("charter", expected_charter_digest, envelope.get("charter_digest")),
    ):
        if not _digest_ok(expected_digest):
            issues.append(f"strength_expected_{field}_digest_invalid")
        elif observed_digest != expected_digest:
            issues.append(f"strength_active_{field}_digest_mismatch")
    if _plain_int(expected_attempt) is None or expected_attempt < 1:
        issues.append("strength_expected_attempt_invalid")
    elif receipt.get("attempt") != expected_attempt:
        issues.append("strength_receipt_attempt_stale")
    if _plain_int(expected_lease_epoch) is None or expected_lease_epoch < 1:
        issues.append("strength_expected_lease_epoch_invalid")
    elif receipt.get("lease_epoch") != expected_lease_epoch:
        issues.append("strength_receipt_lease_epoch_stale")
    if not _safe_id_ok(expected_lease_owner):
        issues.append("strength_expected_lease_owner_invalid")
    elif receipt.get("lease_owner") != expected_lease_owner:
        issues.append("strength_receipt_lease_owner_stale")
    lease_until = _finite_number(lease_until_epoch)
    accepted_at = _finite_number(accepted_at_epoch)
    if lease_until is None or accepted_at is None:
        issues.append("strength_acceptance_timing_invalid")
    else:
        if receipt.get("finished_at_epoch", float("inf")) >= lease_until:
            issues.append("strength_receipt_finished_after_lease")
        if accepted_at >= lease_until:
            issues.append("strength_receipt_arrived_after_lease")
        if accepted_at < receipt.get("finished_at_epoch", float("inf")):
            issues.append("strength_acceptance_precedes_receipt")
    if sample_id not in (receipt.get("complete_70_hand_sample_ids") or []):
        issues.append("strength_sample_not_declared_complete_70")
    evidence = next(
        (
            item
            for item in receipt.get("evidence") or []
            if isinstance(item, dict) and item.get("evidence_id") == sample_id
        ),
        None,
    )
    if evidence is None:
        issues.append("strength_sample_evidence_missing")
        return list(dict.fromkeys(issues))
    refs = _input_refs_by_kind(envelope)
    if not _digest_ok(expected_evaluation_contract_digest):
        issues.append("strength_expected_evaluation_contract_digest_invalid")
    else:
        contract_ref = refs.get("contract")
        if (
            not isinstance(contract_ref, Mapping)
            or contract_ref.get("digest")
            != expected_evaluation_contract_digest
        ):
            issues.append("strength_active_evaluation_contract_digest_mismatch")
    active_expected = {
        "opponent": expected_opponent_artifact_digest,
        "evaluator": expected_evaluator_digest,
        "parser": expected_parser_digest,
        "timing-plan": expected_timing_plan_digest,
        "seed-schedule": expected_seed_schedule_digest,
        "replay-verifier": expected_replay_verifier_digest,
        "runtime": expected_runtime_digest,
        "repository": expected_repository_digest,
        "executor": expected_executor_digest,
    }
    for kind, expected_digest in active_expected.items():
        issue_kind = kind.replace("-", "_")
        if not _digest_ok(expected_digest):
            issues.append(f"strength_expected_{issue_kind}_digest_invalid")
            continue
        ref = refs.get(kind)
        if not isinstance(ref, Mapping) or ref.get("digest") != expected_digest:
            issues.append(f"strength_active_{issue_kind}_digest_mismatch")
    rating_authority_expected = {
        "published-identity": expected_published_identity_digest,
        "official-certificate": expected_official_certificate_digest,
        "rating-cycle-authority": expected_rating_cycle_authority_digest,
    }
    if envelope.get("job_kind") == "native-rating":
        for kind, expected_digest in rating_authority_expected.items():
            issue_kind = kind.replace("-", "_")
            if not _digest_ok(expected_digest):
                issues.append(f"strength_expected_{issue_kind}_digest_invalid")
                continue
            ref = refs.get(kind)
            if not isinstance(ref, Mapping) or ref.get("digest") != expected_digest:
                issues.append(f"strength_active_{issue_kind}_digest_mismatch")
    elif any(value is not None for value in rating_authority_expected.values()):
        issues.append("strength_prepublication_rating_authority_unexpected")
    expected = {
        "kind": "strength_sample",
        "authority": "native_tcp",
        "strength_sample_unit": "70_hand_match",
        "hands": 70,
        "settlements": 69,
        "complete": True,
        "strength_admitted": True,
        "candidate_artifact_digest": envelope.get("artifact_digest"),
        "opponent_artifact_digest": (refs.get("opponent") or {}).get("digest"),
        "evaluator_digest": (refs.get("evaluator") or {}).get("digest"),
        "parser_digest": (refs.get("parser") or {}).get("digest"),
        "timing_plan_digest": (refs.get("timing-plan") or {}).get("digest"),
        "seed_schedule_digest": (refs.get("seed-schedule") or {}).get("digest"),
        "verifier_digest": (refs.get("replay-verifier") or {}).get("digest"),
        "runtime_digest": (refs.get("runtime") or {}).get("digest"),
        "repository_digest": (refs.get("repository") or {}).get("digest"),
        "executor_digest": (refs.get("executor") or {}).get("digest"),
        "executor_subject": (refs.get("executor") or {}).get("subject"),
        "job_kind": envelope.get("job_kind"),
        "purpose": envelope.get("purpose"),
        "published_identity_digest": (
            (refs.get("published-identity") or {}).get("digest")
            if envelope.get("job_kind") == "native-rating"
            else None
        ),
        "official_certificate_digest": (
            (refs.get("official-certificate") or {}).get("digest")
            if envelope.get("job_kind") == "native-rating"
            else None
        ),
        "rating_cycle_authority_digest": (
            (refs.get("rating-cycle-authority") or {}).get("digest")
            if envelope.get("job_kind") == "native-rating"
            else None
        ),
    }
    for field, value in expected.items():
        if evidence.get(field) != value:
            issues.append(f"strength_sample_{field}_mismatch")
    expected_admission_identity = _strength_admission_identity(evidence)
    if evidence.get("admission_identity_digest") != expected_admission_identity:
        issues.append("strength_sample_admission_identity_digest_mismatch")
    return list(dict.fromkeys(issues))


def accept_strength_sample(
    receipt: Any,
    **expected: Any,
) -> dict[str, Any]:
    """Structurally pre-admit; raw replay verification and CAS remain mandatory."""

    issues = strength_sample_acceptance_issues(receipt, **expected)
    if issues:
        return {
            "accepted": False,
            "issues": issues,
            "sample": None,
            "admission_identity_digest": None,
            "rating_eligible": False,
            "pending_external_gates": [
                "raw_replay_resolver",
                "durable_admission_identity_cas",
            ],
        }
    sample_id = expected["sample_id"]
    sample = next(
        item for item in receipt["evidence"] if item["evidence_id"] == sample_id
    )
    return {
        "accepted": True,
        "issues": [],
        "sample": _frozen_copy(sample),
        "receipt_digest": receipt["receipt_digest"],
        "admission_identity_digest": sample["admission_identity_digest"],
        "rating_eligible": False,
        "pending_external_gates": [
            "raw_replay_resolver",
            "durable_admission_identity_cas",
        ],
    }


__all__ = [
    "JOB_ENVELOPE_KIND",
    "JOB_ENVELOPE_SCHEMA_VERSION",
    "JOB_RECEIPT_KIND",
    "JOB_RECEIPT_SCHEMA_VERSION",
    "JOB_KIND_POLICIES",
    "INPUT_REF_KINDS",
    "JobContractError",
    "JobIdempotencyConflict",
    "PRIORITY_RANKS",
    "ZERO_STRENGTH_AUTHORITIES",
    "accept_strength_sample",
    "assert_idempotent_job_replay",
    "build_evidence_ref",
    "build_job_envelope",
    "build_job_receipt",
    "job_envelope_issues",
    "job_receipt_issues",
    "strength_sample_acceptance_issues",
]
