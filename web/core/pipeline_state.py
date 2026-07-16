"""Authoritative pipeline state machine helpers.

Stages remain persisted as strings for backward compatibility. Centralizing the
stage order, route policy, and generic-abandon guard prevents the orchestrator
prompt, MCP tools, and recovery code from drifting into contradictory rules.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid

from failure_classification import classify_precommit_gate


PIPELINE_RUNTIME_HEARTBEAT_SCHEMA = 1
# Runtime-only liveness belongs outside results/evidence identity.  ``web/logs``
# is gitignored and never copied into an evaluation snapshot.
PIPELINE_RUNTIME_HEARTBEAT_FILE = (
    Path(__file__).resolve().parents[1] / "logs" / "pipeline_runtime_heartbeat.json"
)


def _checkpoint_runtime_identity(checkpoint):
    return hashlib.sha256(
        json.dumps(
            checkpoint or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _process_start_token(pid):
    """Return Linux PID start ticks so PID reuse cannot validate stale state."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        closing = raw.rfind(")")
        if closing < 0:
            return ""
        # Fields following ``comm`` begin at proc-stat field 3; starttime is 22.
        fields = raw[closing + 2:].split()
        return str(fields[19]) if len(fields) > 19 else ""
    except Exception:
        return ""


def write_pipeline_runtime_heartbeat(
    checkpoint,
    *,
    phase,
    audit_attempt=None,
    audit_context=None,
):
    """Atomically publish non-semantic in-process liveness for one checkpoint."""
    if not isinstance(checkpoint, dict) or not checkpoint:
        return False
    pid = os.getpid()
    process_start_token = _process_start_token(pid)
    if not process_start_token:
        return False
    now = time.time()
    payload = {
        "schema_version": PIPELINE_RUNTIME_HEARTBEAT_SCHEMA,
        "checkpoint_identity": _checkpoint_runtime_identity(checkpoint),
        "workflow_run_id": str(
            checkpoint.get("workflow_run_id")
            or checkpoint.get("run_id")
            or ""
        ),
        "checkpoint_revision": int(checkpoint.get("checkpoint_revision") or 0),
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "stage": str(checkpoint.get("stage") or ""),
        "phase": str(phase),
        "audit_attempt": (
            int(audit_attempt) if audit_attempt is not None else None
        ),
        "audit_context_digest": (
            hashlib.sha256(
                json.dumps(
                    audit_context,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            if audit_context is not None
            else ""
        ),
        "pid": pid,
        "process_start_token": process_start_token,
        "written_at": now,
    }
    path = PIPELINE_RUNTIME_HEARTBEAT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{pid}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        with open(temporary, "x", encoding="utf-8") as writer:
            writer.write(encoded)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def read_pipeline_runtime_heartbeat(checkpoint, *, now=None, max_age=None):
    """Read a live, identity-bound heartbeat; reject restart/PID/stage debris."""
    if not isinstance(checkpoint, dict) or not checkpoint:
        return None
    try:
        payload = json.loads(
            PIPELINE_RUNTIME_HEARTBEAT_FILE.read_text(encoding="utf-8")
        )
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != PIPELINE_RUNTIME_HEARTBEAT_SCHEMA:
            return None
        if payload.get("checkpoint_identity") != _checkpoint_runtime_identity(checkpoint):
            return None
        if int(payload.get("checkpoint_revision") or 0) != int(
            checkpoint.get("checkpoint_revision") or 0
        ):
            return None
        if str(payload.get("stage") or "") != str(checkpoint.get("stage") or ""):
            return None
        pid = int(payload.get("pid") or 0)
        if not pid or payload.get("process_start_token") != _process_start_token(pid):
            return None
        written_at = float(payload.get("written_at") or 0.0)
        current = time.time() if now is None else float(now)
        if (
            not math.isfinite(written_at)
            or written_at <= 0
            or written_at > current + 5.0
        ):
            return None
        if max_age is not None and current - written_at > float(max_age):
            return None
        return payload
    except Exception:
        return None


def pipeline_runtime_activity_ts(checkpoint, *, now=None, max_age=None):
    heartbeat = read_pipeline_runtime_heartbeat(
        checkpoint,
        now=now,
        max_age=max_age,
    )
    return float((heartbeat or {}).get("written_at") or 0.0)


def clear_pipeline_runtime_heartbeat(checkpoint=None):
    """Clear only this checkpoint's sidecar, never another generation's."""
    path = PIPELINE_RUNTIME_HEARTBEAT_FILE
    if checkpoint is not None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return True
        except Exception:
            payload = None
        if (
            not isinstance(payload, dict)
            or payload.get("checkpoint_identity")
            != _checkpoint_runtime_identity(checkpoint)
        ):
            return False
    try:
        path.unlink(missing_ok=True)
        return True
    except Exception:
        return False


STAGE_ORDER = [
    "selected",
    "preparing",
    "prepared",
    "crossover_running",
    "direction_audited",
    "master_planned",
    "workers_done",
    "quality_failed",
    "quality_passed",
    "reviewed",
    "critic_checked",
    "precommit_failed",
    "repair_planned",
    "rework_running",
    "verified",
    "official_bootstrap_required",
    "official_certifying",
    "official_failed",
    "official_inconclusive",
    "publishing",
    "archived",
]

# Checkpoints in these stages contain enough durable state to resume with a
# fresh orchestrator conversation.  Keep this classification next to the
# authoritative stage machine so newly-added stages cannot silently drift from
# startup watchdog and stale-checkpoint handling.
SESSION_RECOVERABLE_STAGES = frozenset(
    stage
    for stage in STAGE_ORDER
    if stage not in {"official_bootstrap_required", "official_inconclusive", "archived"}
) | frozenset({"timed_out", "infra_timed_out"})

# A plain cycle timeout may terminalize only stages whose ordinary generic
# abandon policy is disposable.  Later gates, repairs, certification and
# publication must retain their exact stage and resume their canonical owner;
# rewriting them to ``timed_out`` would erase the stage-specific safety rule.
TIMEOUT_ABANDONABLE_STAGES = frozenset({
    "selected",
    "preparing",
    "prepared",
    "crossover_running",
    "direction_audited",
    "master_planned",
    "workers_done",
    "quality_failed",
})

STAGE_GATE_ALLOWLIST = {
    "selected": set(),
    "preparing": set(),
    "prepared": set(),
    "crossover_running": set(),
    "direction_audited": set(),
    "master_planned": set(),
    "workers_done": set(),
    "quality_failed": {"quality"},
    "quality_passed": {"quality", "review"},
    "reviewed": {"quality", "review", "critic"},
    "critic_checked": {"quality", "review", "critic"},
    "precommit_failed": {"quality", "review", "critic", "precommit_eval"},
    "repair_planned": {"quality", "review", "critic", "precommit_eval"},
    "rework_running": {"quality", "review", "critic", "precommit_eval"},
    "verified": {"quality", "review", "critic", "precommit_eval", "official_full"},
    "official_bootstrap_required": {"quality", "review", "critic", "precommit_eval", "official_full"},
    "official_certifying": {"quality", "review", "critic", "precommit_eval", "official_full"},
    "official_failed": {"quality", "review", "critic", "precommit_eval", "official_full"},
    "official_inconclusive": {"quality", "review", "critic", "precommit_eval", "official_full"},
    "publishing": {"quality", "review", "critic", "precommit_eval", "official_full"},
    "archived": {"quality", "review", "critic", "precommit_eval", "official_full"},
}

NEXT_TOOL_BY_STAGE = {
    "selected": "prepare_next_gen or run_crossover",
    "preparing": "prepare_next_gen",
    "prepared": "run_direction_audit",
    "crossover_running": "run_crossover",
    "direction_audited": "run_master",
    "master_planned": "execute_workers",
    "workers_done": "run_quality_gates",
    "quality_failed": "execute_workers",
    "quality_passed": "run_review",
    "reviewed": "run_critic",
    "critic_checked": "run_precommit_eval",
    "precommit_failed": "execute_workers",
    "repair_planned": "execute_workers",
    "rework_running": "execute_workers",
    "verified": "commit_bot",
    "official_bootstrap_required": None,
    "official_certifying": "commit_bot",
    "official_failed": "execute_workers",
    "official_inconclusive": None,
    "publishing": "commit_bot",
    "archived": "run_archivist",
    "timed_out": "abandon_generation",
    "infra_timed_out": "run_precommit_eval",
}

_STAGE_RANK = {stage: idx for idx, stage in enumerate(STAGE_ORDER)}


# The prepare/crossover branch has two materializers that converge at
# ``prepared``.  STAGE_ORDER is deliberately retained for compatibility with
# historical checkpoints, but it is not a license to enter the crossover
# materializer after a single-parent baseline has already been prepared.
_EARLY_GENERATION_STAGES = frozenset(
    {"selected", "preparing", "crossover_running", "prepared"}
)
_EARLY_GENERATION_EDGES = {
    ("selected", "preparing"): "prepare_started",
    ("selected", "crossover_running"): "crossover_started",
    ("preparing", "prepared"): "prepare_baseline_completed",
    ("crossover_running", "prepared"): "prepare_baseline_completed",
    ("prepared", "direction_audited"): "direction_audit_ready",
}

LITERATURE_PROBE_REQUIREMENT_SCHEMA_VERSION = "literature-probe-requirement-v1"


def session_recoverable_stages() -> frozenset[str]:
    """Return stages safe to resume with a new orchestrator session."""
    return SESSION_RECOVERABLE_STAGES


HEAD_DRIFT_RESUME_POLICY = {
    "selected": {
        "allowed_tools": ("prepare_next_gen", "run_crossover"),
        "resume_kind": "selected",
        "warning_suffix": "selected",
        "requires_target": False,
        "requires_contract_unchanged": False,
        "branch_alias_allowed": True,
    },
    "timed_out": {
        "allowed_tools": ("abandon_generation",),
        "resume_kind": "timeout_abandon",
        "warning_suffix": "timed_out",
        "requires_target": False,
        "requires_contract_unchanged": False,
        "branch_alias_allowed": True,
    },
    "infra_timed_out": {
        "allowed_tools": ("run_precommit_eval",),
        "resume_kind": "infra_precommit_retry",
        "warning_suffix": "infra_timed_out",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
    "prepared": {
        "allowed_tools": ("run_direction_audit",),
        "resume_kind": "pre_master",
        "warning_suffix": "pre_master",
        "requires_target": True,
        "requires_contract_unchanged": False,
        "branch_alias_allowed": True,
    },
    "crossover_running": {
        "allowed_tools": ("run_crossover",),
        "resume_kind": "crossover",
        "warning_suffix": "crossover",
        "requires_target": True,
        "requires_contract_unchanged": False,
        "branch_alias_allowed": True,
    },
    "direction_audited": {
        "allowed_tools": ("run_literature_probe", "run_master"),
        "resume_kind": "pre_master",
        "warning_suffix": "pre_master",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
    "master_planned": {
        "allowed_tools": ("execute_workers",),
        "resume_kind": "initial_workers",
        "warning_suffix": "initial_workers",
        "requires_target": True,
        # The persisted master_plan already contains the exact worker prompts
        # for the first mutating step. If HEAD changed before or during planning,
        # execute_workers can still run from that saved plan; downstream quality
        # and precommit gates validate the resulting candidate on the current
        # codebase before it can be committed.
        "requires_contract_unchanged": False,
        "branch_alias_allowed": True,
    },
    "workers_done": {
        "allowed_tools": ("run_quality_gates",),
        "resume_kind": "gate",
        "warning_suffix": "gate",
        "requires_target": True,
        "requires_contract_unchanged": False,
        "branch_alias_allowed": True,
    },
    "quality_failed": {
        "allowed_tools": ("execute_workers",),
        "resume_kind": "repair",
        "warning_suffix": "repair",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
    "quality_passed": {
        "allowed_tools": ("run_review",),
        "resume_kind": "post_quality",
        "warning_suffix": "post_quality",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
    "reviewed": {
        "allowed_tools": ("run_critic",),
        "resume_kind": "post_quality",
        "warning_suffix": "post_quality",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
    "critic_checked": {
        "allowed_tools": ("run_precommit_eval",),
        "resume_kind": "post_quality",
        "warning_suffix": "post_quality",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
    "precommit_failed": {
        "allowed_tools": ("execute_workers",),
        "resume_kind": "repair",
        "warning_suffix": "repair",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
    "repair_planned": {
        "allowed_tools": ("execute_workers",),
        "resume_kind": "repair",
        "warning_suffix": "repair",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
    "rework_running": {
        "allowed_tools": ("execute_workers",),
        "resume_kind": "repair",
        "warning_suffix": "repair",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
    "verified": {
        "allowed_tools": ("commit_bot",),
        "resume_kind": "post_quality",
        "warning_suffix": "post_quality",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": False,
    },
    "official_certifying": {
        "allowed_tools": ("commit_bot",),
        "resume_kind": "official_certifying",
        "warning_suffix": "official_certifying",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": False,
    },
    "publishing": {
        "allowed_tools": ("commit_bot",),
        "resume_kind": "publishing",
        "warning_suffix": "publishing",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": False,
    },
    "official_failed": {
        "allowed_tools": ("execute_workers",),
        "resume_kind": "repair",
        "warning_suffix": "official_repair",
        "requires_target": True,
        "requires_contract_unchanged": True,
        "branch_alias_allowed": True,
    },
}


def head_drift_resume_policy(stage: str | None) -> dict | None:
    """Return the authoritative HEAD-drift recovery policy for a stage."""
    policy = HEAD_DRIFT_RESUME_POLICY.get(stage)
    return dict(policy) if policy else None


def head_drift_resume_stages() -> set[str]:
    return set(HEAD_DRIFT_RESUME_POLICY)


def head_drift_allowed_tools(stage: str | None) -> set[str]:
    policy = head_drift_resume_policy(stage)
    if not policy:
        return set()
    return set(policy.get("allowed_tools") or ())


def stage_requires_target_for_head_resume(stage: str | None) -> bool:
    policy = head_drift_resume_policy(stage)
    return True if not policy else bool(policy.get("requires_target", True))


def _canonical_digest(payload: object) -> str:
    """Return a deterministic digest or raise when an owned record is malformed."""
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _literature_probe_requirement_reasons(checkpoint: dict | None) -> tuple[str, ...]:
    """Return the scheduler-owned facts that make literature mandatory."""
    if not isinstance(checkpoint, dict):
        return ()
    audit_context = checkpoint.get("audit_context") or {}
    if isinstance(audit_context, dict) and isinstance(
        audit_context.get("protocol_bootstrap"), dict
    ):
        # Bootstrap Master planning is deliberately free of match-result
        # evidence. The existing literature probe is H2H-weakness driven, so it
        # cannot become mandatory until a separately typed non-result probe
        # contract exists.
        return ()
    master_context = (
        audit_context.get("master_context")
        if isinstance(audit_context, dict)
        else None
    )
    stagnation_info = str(
        (master_context or {}).get("stagnation_info")
        if isinstance(master_context, dict)
        else ""
    )
    normalized = stagnation_info.lower().replace(" ", "")
    reasons: list[str] = []
    if (
        "stagnation_detected" in normalized
        or '"is_stagnant":true' in normalized
        or "'is_stagnant':true" in normalized
    ):
        reasons.append("stagnation")
    direction_audit = checkpoint.get("direction_audit") or {}
    if isinstance(direction_audit, dict) and direction_audit.get("repetition_detected"):
        reasons.append("direction_repetition")
    return tuple(reasons)


def literature_probe_required(checkpoint: dict | None) -> bool:
    """Return whether canonical scheduler/auditor evidence mandates research."""
    return bool(_literature_probe_requirement_reasons(checkpoint))


def literature_probe_receipt_binding(
    checkpoint: dict | None,
) -> tuple[dict | None, list[str]]:
    """Build the immutable context that a mandatory probe receipt must bind.

    A next/source pair alone is too weak: the weak outer model could reuse an
    old ``governed_skip`` after the scheduler refreshed its Master evidence or
    the Direction Auditor changed the mandatory constraint.  Bind both owned
    digests and the exact route-requirement context instead.
    """
    errors: list[str] = []
    if not isinstance(checkpoint, dict):
        return None, ["literature_probe_checkpoint_missing_or_not_object"]

    try:
        next_v = int(checkpoint.get("next_v"))
        source_v = int(checkpoint.get("source_v"))
    except (TypeError, ValueError):
        return None, ["literature_probe_checkpoint_identity_invalid"]

    reasons = _literature_probe_requirement_reasons(checkpoint)
    if not reasons:
        errors.append("literature_probe_not_required")

    audit_context = checkpoint.get("audit_context") or {}
    master_context = (
        audit_context.get("master_context")
        if isinstance(audit_context, dict)
        else None
    )
    try:
        from master_context_contract import validate_master_context

        master_errors = validate_master_context(
            master_context,
            next_v=next_v,
            source_v=source_v,
        )
    except Exception as exc:
        master_errors = [
            "literature_probe_master_context_validation_error:"
            f"{type(exc).__name__}"
        ]
    if master_errors:
        errors.extend(f"literature_probe_{error}" for error in master_errors)

    direction_audit = checkpoint.get("direction_audit")
    if not isinstance(direction_audit, dict):
        errors.append("literature_probe_direction_audit_missing_or_not_object")

    if errors:
        return None, errors

    try:
        requirement_context = {
            "schema_version": LITERATURE_PROBE_REQUIREMENT_SCHEMA_VERSION,
            "next_v": next_v,
            "source_v": source_v,
            "master_context_digest": str(master_context["context_digest"]),
            "direction_audit_digest": _canonical_digest(direction_audit),
            "requirement_reasons": list(reasons),
        }
        return {
            "master_context_digest": requirement_context["master_context_digest"],
            "direction_audit_digest": requirement_context["direction_audit_digest"],
            "requirement_context": requirement_context,
            "requirement_context_digest": _canonical_digest(requirement_context),
        }, []
    except Exception as exc:
        return None, [
            "literature_probe_requirement_context_digest_error:"
            f"{type(exc).__name__}"
        ]


def literature_probe_receipt_present(checkpoint: dict | None) -> bool:
    """Return whether a mandatory probe attempt is bound to current evidence.

    A governed skip, timeout, or provider failure counts as an attempt, but
    only for the exact Master context and Direction Auditor requirement that
    requested it.  Legacy/mismatched receipts deliberately fail closed.
    """
    if not isinstance(checkpoint, dict):
        return False
    receipt = checkpoint.get("literature_probe")
    if not isinstance(receipt, dict):
        return False
    try:
        if int(receipt.get("next_v")) != int(checkpoint.get("next_v")):
            return False
        if int(receipt.get("source_v")) != int(checkpoint.get("source_v")):
            return False
    except (TypeError, ValueError):
        return False
    if not isinstance(receipt.get("reason"), str) or not receipt.get("reason").strip():
        return False

    binding, _errors = literature_probe_receipt_binding(checkpoint)
    if binding is None:
        return False
    return all(
        receipt.get(field) == binding[field]
        for field in (
            "master_context_digest",
            "direction_audit_digest",
            "requirement_context",
            "requirement_context_digest",
        )
    )


def _active_workflow_profile_info() -> tuple[str, str]:
    from workflow_profiles import get_workflow_profile

    profile = get_workflow_profile()
    return (
        getattr(profile, "profile_id", ""),
        getattr(profile, "national_execution_mode", "native_tcp"),
    )


def _gate_matches_active_workflow(gate: dict | None, *, require_native_contract: bool = False) -> bool:
    active_profile_id, active_execution_mode = _active_workflow_profile_info()
    if not active_profile_id:
        return True
    gate = gate or {}
    gate_profile_id = str(gate.get("workflow_profile_id") or gate.get("profile_id") or "")
    gate_execution_mode = str(gate.get("national_execution_mode") or "")
    if active_profile_id == "default":
        return (
            gate_profile_id in {"", "default"}
            and gate_execution_mode in {"", active_execution_mode}
        )
    if gate_profile_id != active_profile_id:
        return False
    if gate_execution_mode != active_execution_mode:
        return False
    if (
        require_native_contract
        and active_execution_mode == "native_tcp"
        and gate.get("national_native_contract_ok") is not True
    ):
        return False
    return True


def _quality_gate_matches_active_workflow(gate_results: dict) -> bool:
    quality = (gate_results or {}).get("quality") or {}
    return (
        quality.get("all_passed") is True
        and quality.get("critical_scenarios_passed") is True
        and _gate_matches_active_workflow(quality, require_native_contract=True)
    )


def _precommit_gate_matches_active_workflow(gate_results: dict) -> bool:
    precommit = (gate_results or {}).get("precommit_eval") or {}
    return precommit.get("passed") is True and _gate_matches_active_workflow(precommit)


def _critic_gate_passed(gate_results: dict) -> bool:
    critic = (gate_results or {}).get("critic") or {}
    if not critic:
        return False
    if critic.get("llm_failed") or critic.get("parse_failed"):
        return False
    return (
        critic.get("approved") is True
        and critic.get("llm_invoked") is True
        and critic.get("critic_llm_executed") is True
        and critic.get("schema_valid") is True
    )


def validate_stage_transition(current_stage, proposed_stage):
    """Validate that a pipeline stage transition is legal.

    Returns ``(is_valid, reason)``. Unknown stages remain allowed for backward
    compatibility with old checkpoints, but known backward transitions are
    blocked unless they are explicit retry/recovery paths.
    """
    if proposed_stage is None or current_stage is None:
        return True, "no_guard"
    if proposed_stage == current_stage:
        return True, "same_stage"
    if current_stage == "publishing":
        return False, "publication_transaction_is_durable"
    if current_stage == "official_bootstrap_required":
        if proposed_stage == "verified":
            return True, "official_bootstrap_certificate_validated"
        return False, "operator_bootstrap_pause_is_durable"
    if proposed_stage == "timed_out":
        if current_stage in TIMEOUT_ABANDONABLE_STAGES:
            return True, "timeout_override"
        return False, (
            "timeout_cannot_erase_stage_authority: "
            f"{current_stage}"
        )
    if proposed_stage == "infra_timed_out":
        if current_stage == "critic_checked":
            return True, "infra_timeout_override"
        return False, (
            "infra_timeout_requires_critic_checked: "
            f"{current_stage}"
        )
    if proposed_stage == "selected" and current_stage == "archived":
        return True, "fresh_generation_selection"
    if current_stage == "timed_out":
        return False, (
            "timed_out_requires_canonical_abandon: "
            f"{proposed_stage}"
        )
    if current_stage == "infra_timed_out":
        if proposed_stage == "critic_checked":
            return True, "infra_precommit_retry_recovery"
        return False, f"infra_timed_out_recovery_requires_critic_checked: {proposed_stage}"
    if (
        current_stage in _EARLY_GENERATION_STAGES
        or proposed_stage in _EARLY_GENERATION_STAGES
    ):
        reason = _EARLY_GENERATION_EDGES.get((current_stage, proposed_stage))
        if reason:
            return True, reason
        return False, (
            "early_generation_transition_not_allowed: "
            f"{current_stage} -> {proposed_stage}"
        )
    if current_stage == "official_certifying" and proposed_stage == "verified":
        return True, "official_profile_refresh"
    if current_stage == "official_certifying" and proposed_stage in {
        "quality_failed",
        "quality_passed",
        "precommit_failed",
    }:
        return True, "official_profile_refresh"

    retry_sources = {
        "workers_done",
        "quality_failed",
        "quality_passed",
        "reviewed",
        "critic_checked",
        "precommit_failed",
        "verified",
        "official_certifying",
        "official_failed",
    }
    if proposed_stage == "master_planned" and current_stage in retry_sources:
        return True, "retry_reset"
    if current_stage == "master_planned" and proposed_stage == "direction_audited":
        return True, "master_plan_rejected_replan"
    if (
        current_stage in {"quality_failed", "repair_planned", "rework_running"}
        and proposed_stage == "direction_audited"
    ):
        return True, "architecture_policy_identity_replan"
    if proposed_stage == "repair_planned" and current_stage in retry_sources:
        return True, "rework_planned"
    if proposed_stage == "repair_planned" and current_stage == "rework_running":
        return True, "rework_retry_planned"
    if proposed_stage == "rework_running" and current_stage in retry_sources | {"repair_planned"}:
        return True, "rework_running"
    if proposed_stage == "workers_done" and current_stage == "precommit_failed":
        return True, "precommit_rework_done"
    if proposed_stage == "workers_done" and current_stage in {"repair_planned", "rework_running"}:
        return True, "rework_done"
    if proposed_stage == "workers_done" and current_stage == "verified":
        return True, "verified_rework_reset"
    if proposed_stage == "workers_done" and current_stage == "official_certifying":
        return True, "official_certification_rework_reset"
    if proposed_stage == "workers_done" and current_stage == "official_failed":
        return True, "official_rework_done"
    if current_stage == "critic_checked" and proposed_stage == "reviewed":
        return True, "review_recheck"

    if current_stage in STAGE_ORDER and proposed_stage in STAGE_ORDER:
        current_idx = _STAGE_RANK[current_stage]
        proposed_idx = _STAGE_RANK[proposed_stage]
        if proposed_idx > current_idx:
            return True, "forward_progression"
        return False, f"backward_transition: {current_stage} -> {proposed_stage}"

    return True, "unknown_stage"


def validate_runtime_contract_ledger_reset(current_stage, proposed_stage):
    """Authorize the narrow rollback transitions that may discard a plan ledger.

    Runtime-contract ledgers are append-only during ordinary repair work.  They
    may be reset only when the state machine rejects the entire Master plan and
    routes back to ``direction_audited`` for a genuinely fresh plan.  Keeping
    this decision next to the stage-transition authority prevents a generic
    checkpoint write from turning into an implicit ledger-erasure escape hatch.
    """
    valid, reason = validate_stage_transition(current_stage, proposed_stage)
    if not valid:
        return False, reason
    if reason not in {
        "master_plan_rejected_replan",
        "architecture_policy_identity_replan",
    }:
        return False, f"runtime_contract_ledger_reset_forbidden:{reason}"
    return True, reason


def is_rework_reset_transition(current_stage: str | None, proposed_stage: str | None) -> bool:
    """True when stage movement implies bot code is being regenerated."""
    if not current_stage or not proposed_stage:
        return False
    if current_stage not in _STAGE_RANK or proposed_stage not in _STAGE_RANK:
        return False
    if proposed_stage in {"repair_planned", "rework_running"} and current_stage in {
        "workers_done",
        "quality_failed",
        "quality_passed",
        "reviewed",
        "critic_checked",
        "precommit_failed",
        "verified",
        "official_certifying",
    }:
        return True
    old_rank = _STAGE_RANK[current_stage]
    new_rank = _STAGE_RANK[proposed_stage]
    return new_rank < old_rank and new_rank <= _STAGE_RANK["workers_done"]


def invalidates_official_job_transition(
    current_stage: str | None,
    proposed_stage: str | None,
) -> bool:
    """True when a workflow-profile refresh invalidates an attached EXE job."""
    return bool(
        current_stage == "official_certifying"
        and proposed_stage in {"quality_failed", "quality_passed", "precommit_failed", "verified"}
    )


def route_policy(checkpoint: dict | None) -> dict:
    """Return the single authoritative route decision for a checkpoint.

    The orchestrator prompt, compact/resume context, and MCP refusal payloads
    should describe the next step from this function rather than maintaining
    separate stage-hint tables.
    """
    if not checkpoint:
        return {
            "stage": None,
            "next_tool": None,
            "allowed_tools": [],
            "intent": "none",
            "directive": "No active checkpoint. Start or select a generation before calling pipeline tools.",
        }

    if checkpoint.get("post_publication_handoff_route") is True:
        try:
            from post_publication_handoff import pending_handoff_route

            pending = pending_handoff_route()
        except Exception as exc:
            pending = {
                "status": "blocked",
                "issues": [f"handoff_route_validation_error:{type(exc).__name__}"],
            }
        exact = bool(
            pending.get("status") == "pending"
            and pending.get("identity_digest")
            == checkpoint.get("post_publication_handoff_identity_digest")
            and pending.get("publication_id") == checkpoint.get("post_publication_id")
            and pending.get("version") == checkpoint.get("next_v")
            and pending.get("source_v") == checkpoint.get("source_v")
        )
        if not exact:
            return {
                "stage": "archived",
                "next_tool": None,
                "allowed_tools": [],
                "intent": "post_publication_handoff_blocked",
                "directive": (
                    "The post-publication handoff route is missing, ambiguous, "
                    "or changed; do not start another generation."
                ),
                "issues": pending.get("issues") or [
                    "post_publication_handoff_route_identity_mismatch"
                ],
            }
        return {
            "stage": "archived",
            "next_v": int(pending["version"]),
            "source_v": int(pending["source_v"]),
            "parent2_v": None,
            "next_tool": "run_archivist",
            "allowed_tools": ["run_archivist"],
            "intent": "post_publication_handoff",
            "directive": (
                "Call run_archivist for the exact durable handoff before "
                "preparing another generation."
            ),
        }

    # Stage names survived the policy-epoch reset, but their old payloads did
    # not.  Validate the system-owned epoch envelope before infrastructure
    # normalization or any stage lookup so a legacy ``direction_audited``
    # checkpoint can never route into the current Master.
    from checkpoint_schema import (
        checkpoint_epoch_errors,
        checkpoint_epoch_reset_route,
    )

    epoch_errors = checkpoint_epoch_errors(checkpoint)
    if epoch_errors:
        return checkpoint_epoch_reset_route(checkpoint, epoch_errors)

    from pipeline_infrastructure import (
        infrastructure_route,
        normalize_checkpoint_infrastructure,
    )

    checkpoint = normalize_checkpoint_infrastructure(checkpoint)
    infra_route = infrastructure_route(checkpoint)
    if infra_route is not None:
        return {
            "stage": checkpoint.get("stage"),
            "next_v": checkpoint.get("next_v"),
            "source_v": checkpoint.get("source_v"),
            "parent2_v": checkpoint.get("parent2_v"),
            **infra_route,
        }

    stage = checkpoint.get("stage")
    next_v = checkpoint.get("next_v")
    source_v = checkpoint.get("source_v")
    parent2_v = checkpoint.get("parent2_v")
    gate_results = checkpoint.get("gate_results") or {}
    failure_class = None
    profile_refresh_needed = False
    precommit_gate = gate_results.get("precommit_eval") or {}
    try:
        from system_strict_bootstrap import is_declared_native_bootstrap

        system_bootstrap_precommit_terminal = bool(
            stage == "precommit_failed"
            and is_declared_native_bootstrap(checkpoint)
            and precommit_gate.get("failure_class")
            == "system_bootstrap_regression"
        )
    except Exception:
        system_bootstrap_precommit_terminal = False

    if system_bootstrap_precommit_terminal:
        next_tool = "abandon_generation"
        intent = "system_bootstrap_abandon"
        failure_class = "system_bootstrap_regression"
    elif (
        stage in {"quality_passed", "reviewed", "critic_checked", "precommit_failed", "verified", "official_certifying", "official_failed"}
        and "quality" in gate_results
        and not _quality_gate_matches_active_workflow(gate_results)
    ):
        next_tool = "run_quality_gates"
        intent = "quality_profile_refresh"
        profile_refresh_needed = True
    elif (
        stage in {"verified", "official_certifying"}
        and not _precommit_gate_matches_active_workflow(gate_results)
    ):
        next_tool = "run_precommit_eval"
        intent = "precommit_profile_refresh"
        profile_refresh_needed = True
    elif stage == "selected":
        next_tool = "run_crossover" if parent2_v is not None else "prepare_next_gen"
        intent = "crossover_prepare" if parent2_v is not None else "prepare"
    elif (
        stage == "direction_audited"
        and literature_probe_required(checkpoint)
        and not literature_probe_receipt_present(checkpoint)
    ):
        next_tool = "run_literature_probe"
        intent = "mandatory_literature_probe"
    elif (
        stage in {"reviewed", "critic_checked"}
        and "critic" in gate_results
        and not _critic_gate_passed(gate_results)
    ):
        # Critic verdicts are advisory.  An incomplete/legacy Critic record may
        # require the role to run again, but it can never authorize Worker
        # strategy rework or replace the native-TCP precommit hard gate.
        next_tool = "run_critic"
        intent = "critic_retry"
    elif stage == "critic_checked":
        gate = gate_results.get("precommit_eval")
        failure_class = classify_precommit_gate(gate)
        if failure_class in {"regression", "failed_unknown"}:
            next_tool = "execute_workers"
            intent = "precommit_rework"
        else:
            next_tool = "run_precommit_eval"
            intent = "precommit_eval"
    else:
        next_tool = NEXT_TOOL_BY_STAGE.get(stage)
        if stage in {"quality_failed", "repair_planned", "rework_running"}:
            intent = "quality_rework"
        elif stage == "precommit_failed":
            intent = "precommit_rework"
        elif stage == "official_failed":
            intent = "official_rework"
        elif stage == "official_bootstrap_required":
            intent = "operator_bootstrap"
        elif stage == "official_certifying":
            intent = "official_poll"
        elif stage == "publishing":
            intent = "publication_resume"
        elif stage in {"quality_passed", "reviewed"}:
            intent = "gate"
        elif stage == "master_planned":
            intent = "initial_workers"
        elif stage == "workers_done":
            intent = "quality"
        else:
            intent = "pipeline"

    directive_map = {
        "prepare_next_gen": "Call prepare_next_gen with the checkpoint source/target.",
        "run_crossover": "Call run_crossover with the checkpoint parents/target.",
        "run_direction_audit": "Call run_direction_audit before planning workers.",
        "run_literature_probe": "Call run_literature_probe and persist its receipt before Master planning.",
        "run_master": "Call run_master to produce worker tasks.",
        "execute_workers": "Call execute_workers with the checkpoint task plan and exact failure feedback when present.",
        "run_quality_gates": "Call run_quality_gates; it owns compile, national, decision, size, and scope validation.",
        "run_review": "Call run_review. Do not rerun workers unless the reviewer returns a code rejection.",
        "run_critic": "Call run_critic; its score is advisory and native-TCP precommit is the final strategy gate.",
        "run_precommit_eval": "Call run_precommit_eval unless the precommit gate already recorded a regression.",
        "abandon_generation": "Abandon the rejected deterministic first-migration generation.",
        "commit_bot": "Call commit_bot only after all gates are passed.",
        "run_archivist": "Call run_archivist to finish post-commit cleanup.",
    }
    directive = directive_map.get(next_tool, "Inspect checkpoint context and continue with the matching MCP pipeline tool.")
    if system_bootstrap_precommit_terminal:
        directive = (
            "The content-bound first-migration control rejected this candidate. "
            "Call abandon_generation; ordinary Worker rework and unchanged "
            "precommit retries are forbidden."
        )
    elif stage == "quality_failed":
        directive = (
            "Quality failed. Call execute_workers using the exact quality-gate failures; "
            "do not call run_master. The rework will be tracked as repair_planned/rework_running."
        )
    elif stage == "precommit_failed":
        directive = (
            "Precommit failed. Call execute_workers with the exact precommit blockers; "
            "do not retry precommit on unchanged code."
        )
    elif stage == "official_failed":
        directive = (
            "Official EXE full certification found a deterministic bot-side compliance, "
            "state-machine, or obvious decision blocker. Call execute_workers with the "
            "official_full evidence; do not retry commit_bot on unchanged code."
        )
    elif stage == "official_bootstrap_required":
        directive = (
            "No published full-v5 opponent exists. The candidate is parked for the explicit "
            "one-time first-strict control bootstrap; the orchestrator must stop and must "
            "never authorize or consume it. After bootstrap-first-strict succeeds, only the "
            "operator-only finalize-first-strict command may publish it; the complete signed "
            "certificate and completed bootstrap authorization remain mandatory."
        )
    elif stage == "official_certifying":
        directive = (
            "Official EXE 5+3x70 certification is running as a durable job. "
            "Call commit_bot only to poll the attached job; do not edit bot code or start another EXE suite."
        )
    elif stage == "official_inconclusive":
        directive = (
            "Official EXE full certification is inconclusive because the platform/harness "
            "evidence is insufficient. Do not call commit_bot or edit bot code; fix the "
            "official harness/runtime evidence path, then rerun certification."
        )
    elif stage == "publishing":
        directive = (
            "A content-bound publication transaction is in progress. Call "
            "commit_bot to reconcile the existing intent; do not rerun official "
            "certification, edit the candidate, or enter a Worker/rework path."
        )
    elif intent == "critic_retry":
        directive = (
            "The mandatory Critic execution receipt is incomplete or invalid. "
            "Call run_critic again for the same candidate; its strategic verdict "
            "remains advisory and must not create Worker rework."
        )
    elif stage in {"repair_planned", "rework_running"}:
        directive = (
            "Rework is already planned/running. Continue execute_workers with the saved task plan; "
            "do not restart planning or crossover."
        )
    elif stage == "selected" and parent2_v is not None:
        directive = "Crossover selected. Call run_crossover; do not call prepare_next_gen."
    elif intent == "mandatory_literature_probe":
        directive = (
            "Canonical stagnation/repetition evidence requires run_literature_probe. "
            "Persist a success, governed-skip, timeout, or failure receipt before run_master; "
            "the outer model cannot waive this stage."
        )
    elif stage == "master_planned":
        directive = "Master plan is saved. Call execute_workers with the saved tasks."
    elif profile_refresh_needed and next_tool == "run_quality_gates":
        directive = (
            "Cached quality gate was produced under a different workflow profile "
            "or national execution mode. Call run_quality_gates to revalidate "
            "the current candidate under the active workflow."
        )
    elif profile_refresh_needed and next_tool == "run_precommit_eval":
        directive = (
            "Cached precommit gate was produced under a different workflow "
            "profile or national execution mode. Call run_precommit_eval to "
            "revalidate under the active workflow."
        )

    allowed_tools = []
    if next_tool:
        allowed_tools.append(next_tool)
    return {
        "stage": stage,
        "next_v": next_v,
        "source_v": source_v,
        "parent2_v": parent2_v,
        "next_tool": next_tool,
        "allowed_tools": allowed_tools,
        "intent": intent,
        "failure_class": failure_class,
        "directive": directive,
    }


def next_tool_for_checkpoint(checkpoint: dict | None) -> str | None:
    if not checkpoint:
        return None
    return route_policy(checkpoint).get("next_tool")


def generic_abandon_block(checkpoint: dict | None, *,
                          reason: str = "abandon_generation",
                          max_precommit_retries: int = 3) -> dict | None:
    """Return a refusal payload when generic abandon is unsafe.

    Forced abandon callers use explicit reason/stage allowlists. Publication,
    certification and finalization stages are never disposable. For precommit
    regression failures, abandon is only allowed after the configured hard
    limit; before that, the state machine requires worker rework using the
    exact precommit feedback.
    """
    if not checkpoint:
        return None

    stage = checkpoint.get("stage")
    precommit_attempt = int(checkpoint.get("precommit_attempt") or 0)
    gate = (checkpoint.get("gate_results") or {}).get("precommit_eval")
    failure_class = classify_precommit_gate(gate)

    block = False
    route = route_policy(checkpoint)
    next_tool = route.get("next_tool")
    if route.get("intent") == "operator_reconcile_checkpoint":
        return {
            "abandoned": False,
            "blocked": True,
            "reason": "checkpoint_epoch_requires_operator_reconciliation",
            "stage": stage,
            "next_v": checkpoint.get("next_v"),
            "source_v": checkpoint.get("source_v"),
            "next_tool": None,
            "operator_action": route.get("operator_action"),
            "operator_command": route.get("operator_command"),
            "epoch_issues": route.get("epoch_issues") or [],
            "directive": route.get("directive"),
        }
    if route.get("intent") == "system_bootstrap_abandon":
        return None
    never_disposable = {
        "verified",
        "official_bootstrap_required",
        "official_certifying",
        "official_inconclusive",
        "publishing",
        "archived",
    }
    if stage in never_disposable:
        return {
            "abandoned": False,
            "blocked": True,
            "reason": "publication_or_certification_stage_not_disposable",
            "stage": stage,
            "next_v": checkpoint.get("next_v"),
            "source_v": checkpoint.get("source_v"),
            "next_tool": next_tool,
            "directive": (
                f"Refusing abandon for v{checkpoint.get('next_v')} at "
                f"non-disposable stage '{stage}'. Resume/reconcile the exact "
                "certification or publication transaction."
            ),
        }

    if reason not in {"abandon_generation", "cleanup_incomplete_exact_workflow"}:
        broad_infra_stages = {
            "selected", "preparing", "prepared", "crossover_running",
            "direction_audited", "master_planned", "workers_done",
            "quality_failed", "quality_passed", "reviewed", "critic_checked",
            "precommit_failed", "repair_planned", "rework_running",
            "official_failed",
        }
        forced_rules = (
            ("infrastructure_exhausted:", broad_infra_stages),
            ("cycle_timeout_master_stuck", {"direction_audited"}),
            ("master_", {"direction_audited"}),
            ("system_strict_authority_invalid:", {"direction_audited"}),
            ("crossover_", {"preparing", "prepared", "crossover_running"}),
            ("worker_circuit_breaker", {"master_planned", "workers_done", "quality_failed"}),
            ("worker_infrastructure_exhausted", {"master_planned", "workers_done", "quality_failed", "repair_planned", "rework_running"}),
            ("worker_workflow_abandoned", {"master_planned", "workers_done", "quality_failed", "precommit_failed", "repair_planned", "rework_running", "official_failed"}),
            ("worker_terminal_abandon", {"master_planned", "workers_done", "quality_failed", "precommit_failed", "repair_planned", "rework_running", "official_failed"}),
            ("frozen_worker", {"master_planned", "workers_done", "quality_failed", "repair_planned", "rework_running"}),
            ("frozen_rework_", {"precommit_failed", "repair_planned", "rework_running", "official_failed"}),
            ("durable_initial_worker_", {"master_planned"}),
            ("durable_worker_", {"master_planned", "repair_planned", "rework_running"}),
            ("precommit_rework_circuit_breaker", {"precommit_failed", "repair_planned", "rework_running"}),
            ("official_rework_circuit_breaker", {"official_failed", "repair_planned", "rework_running"}),
            ("stale_blueprint_rejection", {"selected", "preparing", "prepared", "direction_audited"}),
        )
        allowed = next(
            (stages for prefix, stages in forced_rules if str(reason).startswith(prefix)),
            None,
        )
        if allowed is None or stage not in allowed:
            return {
                "abandoned": False,
                "blocked": True,
                "reason": "forced_abandon_reason_stage_not_allowed",
                "stage": stage,
                "next_v": checkpoint.get("next_v"),
                "source_v": checkpoint.get("source_v"),
                "next_tool": next_tool,
                "directive": (
                    f"Forced abandon reason '{reason}' is not authorized for "
                    f"stage '{stage}'. Preserve the checkpoint and candidate."
                ),
            }
        return None
    explanation = "This generation has passed earlier gates; continue the state machine"

    if stage in {
        "quality_passed",
        "reviewed",
        "repair_planned",
        "rework_running",
        "verified",
        "publishing",
    }:
        block = True
        if stage == "publishing":
            explanation = (
                "Publication has crossed its durable intent boundary; reconcile "
                "the same transaction"
            )
    elif stage == "critic_checked":
        block = True
        if "critic" in (checkpoint.get("gate_results") or {}) and not _critic_gate_passed(checkpoint.get("gate_results") or {}):
            next_tool = "run_critic"
            explanation = (
                "Critic execution evidence is incomplete; rerun the advisory "
                "role without changing candidate code"
            )
        elif failure_class in {"regression", "failed_unknown"}:
            next_tool = "execute_workers"
            explanation = "Precommit already failed for this code; rework the bot with exact precommit feedback"
        else:
            next_tool = "run_precommit_eval"
    elif stage == "precommit_failed" and precommit_attempt < max_precommit_retries:
        block = True
        next_tool = "execute_workers"
        explanation = "Precommit failed below the hard limit; rework the bot before abandoning"

    if not block:
        return None

    next_v = checkpoint.get("next_v")
    source_v = checkpoint.get("source_v")
    directive = (
        f"Refusing generic abandon_generation for v{next_v} at stage '{stage}'. "
        f"{explanation} with {next_tool}."
    )
    return {
        "abandoned": False,
        "blocked": True,
        "reason": "forward_only_stage",
        "stage": stage,
        "next_v": next_v,
        "source_v": source_v,
        "next_tool": next_tool,
        "failure_class": failure_class,
        "directive": directive,
    }
