"""Canonical infrastructure-failure overlay for resumable pipeline gates."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any


INFRA_FAILURE_SCHEMA_VERSION = 1
DEFAULT_INFRA_MAX_ATTEMPTS = 3
INFRA_OWNER_STAGES = {
    "run_crossover": ("crossover_running",),
    "run_master": ("direction_audited",),
    "execute_workers": ("master_planned", "repair_planned", "rework_running"),
    "run_quality_gates": ("workers_done",),
    "run_review": ("quality_passed",),
    "run_critic": ("reviewed",),
    "commit_bot": ("verified", "official_certifying"),
}
INFRA_OWNER_TOOLS = frozenset(INFRA_OWNER_STAGES)


def _canonical_digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def infrastructure_attempt_key(
    *,
    component: str,
    candidate_fingerprint: str = "",
    source_fingerprint: str = "",
    harness_identity: str = "",
    contract_identity: str = "",
    extra: dict[str, Any] | None = None,
) -> str:
    """Bind retries to the exact candidate and trusted harness identities."""
    return _canonical_digest({
        "schema_version": INFRA_FAILURE_SCHEMA_VERSION,
        "component": str(component),
        "candidate_fingerprint": str(candidate_fingerprint),
        "source_fingerprint": str(source_fingerprint),
        "harness_identity": str(harness_identity),
        "contract_identity": str(contract_identity),
        "extra": extra or {},
    })


def _identity_payload(failure: dict[str, Any]) -> dict[str, Any]:
    return {
        key: failure.get(key)
        for key in (
            "schema_version",
            "failure_class",
            "component",
            "code",
            "owner_tool",
            "resume_stage",
            "attempt_key",
            "attempt",
            "max_attempts",
            "retryable",
            "exhausted",
            "action",
            "issues",
            "first_seen_at",
            "last_seen_at",
            "metadata",
        )
    }


def infrastructure_failure_digest(failure: dict[str, Any] | None) -> str:
    if not isinstance(failure, dict):
        return ""
    return _canonical_digest(_identity_payload(failure))


def validate_infrastructure_failure(failure: dict[str, Any] | None) -> list[str]:
    if not isinstance(failure, dict):
        return ["infra_failure_not_object"]
    errors: list[str] = []
    if failure.get("schema_version") != INFRA_FAILURE_SCHEMA_VERSION:
        errors.append("infra_failure_schema_version")
    if failure.get("failure_class") != "infrastructure":
        errors.append("infra_failure_class")
    for key in ("component", "code", "owner_tool", "resume_stage", "attempt_key"):
        if not str(failure.get(key) or "").strip():
            errors.append(f"infra_failure_missing_{key}")
    if failure.get("owner_tool") not in INFRA_OWNER_TOOLS:
        errors.append("infra_failure_owner_tool")
    elif failure.get("resume_stage") not in INFRA_OWNER_STAGES[failure["owner_tool"]]:
        errors.append("infra_failure_owner_stage_mismatch")
    try:
        attempt = int(failure.get("attempt"))
        max_attempts = int(failure.get("max_attempts"))
        if attempt < 1 or max_attempts < 1 or attempt > max_attempts:
            errors.append("infra_failure_attempt_range")
    except (TypeError, ValueError):
        errors.append("infra_failure_attempt_type")
        attempt = 0
        max_attempts = 0
    exhausted = bool(max_attempts and attempt >= max_attempts)
    if bool(failure.get("exhausted")) != exhausted:
        errors.append("infra_failure_exhausted_mismatch")
    if bool(failure.get("retryable")) == exhausted:
        errors.append("infra_failure_retryable_mismatch")
    expected_action = "abandon_generation" if exhausted else "retry_same_tool"
    if failure.get("action") != expected_action:
        errors.append("infra_failure_action_mismatch")
    issues = failure.get("issues")
    if not isinstance(issues, list) or not issues:
        errors.append("infra_failure_issues")
    stored_digest = str(failure.get("identity_digest") or "")
    computed_digest = infrastructure_failure_digest(failure)
    if not stored_digest or stored_digest != computed_digest:
        errors.append("infra_failure_identity_digest_mismatch")
    return errors


def build_infrastructure_failure(
    previous: dict[str, Any] | None,
    *,
    component: str,
    code: str,
    owner_tool: str,
    resume_stage: str,
    attempt_key: str,
    issues: list[Any],
    max_attempts: int = DEFAULT_INFRA_MAX_ATTEMPTS,
    metadata: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Create or advance one identity-bound infrastructure retry overlay."""
    max_attempts = max(1, int(max_attempts))
    now = float(time.time() if now is None else now)
    same_identity = bool(
        isinstance(previous, dict)
        and not validate_infrastructure_failure(previous)
        and previous.get("component") == component
        and previous.get("code") == code
        and previous.get("owner_tool") == owner_tool
        and previous.get("resume_stage") == resume_stage
        and previous.get("attempt_key") == attempt_key
    )
    previous_attempt = int(previous.get("attempt") or 0) if same_identity else 0
    attempt = min(max_attempts, previous_attempt + 1)
    exhausted = attempt >= max_attempts
    failure = {
        "schema_version": INFRA_FAILURE_SCHEMA_VERSION,
        "failure_class": "infrastructure",
        "component": str(component),
        "code": str(code),
        "owner_tool": str(owner_tool),
        "resume_stage": str(resume_stage),
        "attempt_key": str(attempt_key),
        "attempt": attempt,
        "max_attempts": max_attempts,
        "retryable": not exhausted,
        "exhausted": exhausted,
        "action": "abandon_generation" if exhausted else "retry_same_tool",
        "issues": [str(item)[:500] for item in issues[:16]] or ["unspecified_infrastructure_failure"],
        "first_seen_at": float(previous.get("first_seen_at")) if same_identity else now,
        "last_seen_at": now,
        "metadata": dict(metadata or {}),
    }
    failure["identity_digest"] = infrastructure_failure_digest(failure)
    errors = validate_infrastructure_failure(failure)
    if errors:
        raise ValueError("invalid infrastructure failure: " + "; ".join(errors))
    return failure


def infrastructure_route(checkpoint: dict[str, Any] | None) -> dict[str, Any] | None:
    failure = checkpoint.get("infra_failure") if isinstance(checkpoint, dict) else None
    if failure is None:
        return None
    errors = validate_infrastructure_failure(failure)
    if errors:
        return {
            "next_tool": None,
            "allowed_tools": [],
            "intent": "infra_invalid",
            "action": "repair_checkpoint",
            "failure_class": "infrastructure",
            "directive": (
                "The checkpoint infrastructure overlay is invalid. Do not run bot workers; "
                "repair checkpoint infrastructure state first: " + "; ".join(errors[:5])
            ),
            "infra_failure": failure if isinstance(failure, dict) else None,
        }
    if failure["exhausted"]:
        return {
            "next_tool": failure["owner_tool"],
            "allowed_tools": [failure["owner_tool"]],
            "intent": "infra_abandon",
            "action": "abandon_generation",
            "failure_class": "infrastructure",
            "directive": (
                f"{failure['component']} infrastructure failed {failure['attempt']}/"
                f"{failure['max_attempts']} times for the same identity. Call "
                f"{failure['owner_tool']} once to execute centralized safe abandonment; "
                "do not edit bot code."
            ),
            "infra_failure": failure,
        }
    return {
        "next_tool": failure["owner_tool"],
        "allowed_tools": [failure["owner_tool"]],
        "intent": "infra_retry",
        "action": "retry_same_tool",
        "failure_class": "infrastructure",
        "directive": (
            f"Retry {failure['owner_tool']} for {failure['component']} infrastructure "
            f"attempt {failure['attempt'] + 1}/{failure['max_attempts']}. Preserve the "
            "candidate and do not call execute_workers."
        ),
        "infra_failure": failure,
    }


def normalize_checkpoint_infrastructure(checkpoint: dict[str, Any] | None) -> dict[str, Any] | None:
    """Translate pre-overlay quality/reviewer/critic checkpoints in memory."""
    if not isinstance(checkpoint, dict):
        return checkpoint
    result = dict(checkpoint)
    if "infra_failure" in result and result.get("infra_failure") is not None:
        return result
    stage = str(result.get("stage") or "")
    gates = result.get("gate_results") or {}
    component = code = owner_tool = resume_stage = ""
    previous_attempt = 0
    max_attempts = DEFAULT_INFRA_MAX_ATTEMPTS
    issues: list[str] = []
    if stage in {"quality_infra_retry", "quality_inconclusive"}:
        quality = gates.get("quality") or {}
        legacy = quality.get("quality_infrastructure") or {}
        component = "national_runtime_probe"
        code = "legacy_quality_infrastructure"
        owner_tool = "run_quality_gates"
        resume_stage = "workers_done"
        try:
            previous_attempt = int(
                legacy.get("attempt")
                or (max_attempts if stage == "quality_inconclusive" else 1)
            )
        except (TypeError, ValueError):
            previous_attempt = 1
        try:
            max_attempts = max(1, int(legacy.get("max_attempts") or max_attempts))
        except (TypeError, ValueError):
            max_attempts = DEFAULT_INFRA_MAX_ATTEMPTS
        issues = [str(item) for item in legacy.get("issues") or quality.get("failed_gates") or [code]]
        result["stage"] = resume_stage
    elif stage == "quality_passed" and isinstance(gates.get("review"), dict) and gates["review"].get("llm_failed"):
        component = "reviewer_llm"
        code = "reviewer_llm_infrastructure"
        owner_tool = "run_review"
        resume_stage = "quality_passed"
        try:
            previous_attempt = int(gates["review"].get("review_infra_retry") or 1)
        except (TypeError, ValueError):
            previous_attempt = 1
        issues = [str(gates["review"].get("error") or code)]
    elif stage == "reviewed" and isinstance(gates.get("critic"), dict) and gates["critic"].get("llm_failed"):
        component = "critic_llm"
        code = "critic_llm_infrastructure"
        owner_tool = "run_critic"
        resume_stage = "reviewed"
        try:
            previous_attempt = int(gates["critic"].get("critic_infra_retry") or 1)
        except (TypeError, ValueError):
            previous_attempt = 1
        issues = [str(gates["critic"].get("error") or code)]
    if not component:
        return result
    attempt_key = infrastructure_attempt_key(
        component=component,
        candidate_fingerprint=str((gates.get("quality") or {}).get("code_fingerprint") or "legacy"),
        extra={"next_v": result.get("next_v"), "source_v": result.get("source_v")},
    )
    failure = build_infrastructure_failure(
        None,
        component=component,
        code=code,
        owner_tool=owner_tool,
        resume_stage=resume_stage,
        attempt_key=attempt_key,
        issues=issues,
        max_attempts=max_attempts,
        now=float(result.get("last_update_ts") or 0.0),
    )
    # Preserve the already-consumed legacy attempt without reclassifying it as
    # a new failure observation.
    failure["attempt"] = min(max_attempts, max(1, previous_attempt))
    failure["exhausted"] = failure["attempt"] >= max_attempts
    failure["retryable"] = not failure["exhausted"]
    failure["action"] = "abandon_generation" if failure["exhausted"] else "retry_same_tool"
    failure["identity_digest"] = infrastructure_failure_digest(failure)
    result["infra_failure"] = failure
    return result
