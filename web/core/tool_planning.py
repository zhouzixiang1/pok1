"""Pipeline tools: direction audit, master planning, and worker execution."""

import ast
from copy import deepcopy
import io
import json
import math
import os
import py_compile
import re
import hashlib
import shutil
import stat
import tempfile
import time
import tokenize
import uuid
from dataclasses import dataclass
from pathlib import Path

from bot_namespace import bot_name, bot_relpath
from tool_runtime_guard import tool

from logging_config import get_logger
_log = get_logger("planning")

from evolution_core import (
    get_bot_dir,
    _run_master_analysis,
    _run_direction_audit,
    _execute_workers,
    write_pipeline_checkpoint,
    check_code_size,
    MAX_PRECOMMIT_REWORK_ROUNDS,
    MAX_OFFICIAL_REWORK_ROUNDS,
)
from tool_helpers import (
    _get_ui, _json_tool_result,
    _matching_checkpoint, _state_blocked,
    _execute_exhausted_infrastructure_failure, _owned_infrastructure_failure,
    _record_infrastructure_failure,
    _validate_worker_boundaries,
    _target_rel, _py_files_changed_between, _resolve_version_args,
    PROJECT_ROOT,
    _set_pipeline_status,
    normalize_worker_role,
)
from system_log import log_system_event
from pipeline_state import route_policy
from llm_availability import LLMAvailabilityBlocked
from output_schema import (
    MASTER_PLAN_MAX_TASKS,
    NATIONAL_POLICY_FOCUS_ID,
    POLICY_CONTEXT_SCHEMA_VERSION,
    POLICY_CONTEXT_TOP_LEVEL_FIELDS,
    POLICY_ENTRYPOINTS,
    POLICY_INTENT_KINDS,
    PRECOMPUTE_KEY_SHAPE_PATTERN,
    PRECOMPUTE_MAX_BUILD_MS,
    PRECOMPUTE_MAX_BYTES,
    PRECOMPUTE_MAX_ENTRIES,
    RuntimeContract,
    WORKER_PROMPT_MAX_CHARS,
    WORKER_TASK_MAX_TARGET_FILES,
    runtime_contract_missing_sections,
    runtime_contract_is_required,
    runtime_contract_required_sections,
    runtime_contract_worker_prompt_terms,
)

# Governed literature probe subsystem extracted into its own module. Kept as a
# private alias so the thin delegate shells below can forward to it. Imported
# after the rest of the top-level block above so the companion's own
# ``import tool_planning as _tp`` resolves to this (fully loaded) module.
import tool_planning_literature_probe as _lp  # noqa: E402,F401

# Master-plan validation subsystem extracted into its own module. Same pattern
# as _lp above: thin delegate shells below forward to it.
import tool_planning_master_plan_validation as _mpv  # noqa: E402,F401
# Master dispatch subsystem: the run_master tool body (giant inline
# plan/audit/compile/validation loop) lives in this companion. The @tool
# decoration stays here so run_master.handler resolves on tool_planning.
import tool_planning_master_dispatch as _md  # noqa: E402,F401
import tool_planning_identity_replan as _tpi  # noqa: E402,F401  (identity-replan cluster)
from tool_planning_identity_replan import IDENTITY_REPLAN_ABANDON_THRESHOLD  # noqa: E402,F401


_PROTOCOL_BOOTSTRAP_NO_STRENGTH = (
    "PROTOCOL BOOTSTRAP NO-STRENGTH: no current-cycle strength evidence exists. "
    "Use only the digest-bound strict prepared artifact, repository-pinned "
    "protocol evidence, and bootstrap receipt supplied by the system."
)

# national_tcp_policy_v1 has one candidate-owned source artifact. System
# runtime/precompute bytes, helpers, candidate-owned assets, and unbound
# external assets are never Worker targets. A future system-asset broker is a
# separate infrastructure profile, not a writable candidate path.
_ACTIVE_CANDIDATE_WRITABLE_FILES = frozenset({"policy.py"})


def _render_literature_provider_prompt(inputs):
    """Delegate to tool_planning_literature_probe."""
    return _lp._render_literature_provider_prompt(inputs)


def _is_fresh_empty_pool_bootstrap(checkpoint: dict | None) -> bool:
    """Return whether source_v is numeric high-water rather than an artifact."""

    receipt = (
        (checkpoint.get("audit_context") or {}).get("protocol_bootstrap")
        if isinstance(checkpoint, dict)
        else None
    )
    return bool(
        isinstance(receipt, dict)
        and receipt.get("mode") == "fresh_national_policy_bootstrap"
        and receipt.get("source_artifact_inherited") is False
    )


def _master_source_fingerprint(checkpoint: dict | None, source_v: int) -> str:
    """Bind Master retries without resolving a numeric-only source path."""

    if _is_fresh_empty_pool_bootstrap(checkpoint):
        receipt = (checkpoint.get("audit_context") or {}).get(
            "protocol_bootstrap"
        ) or {}
        payload = {
            "kind": "numeric-high-water-lineage-only",
            "source_v": int(source_v),
            "receipt_digest": str(receipt.get("receipt_digest") or ""),
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    return _complete_artifact_fingerprint(get_bot_dir(source_v))


def _protocol_bootstrap_direction_audit(
    checkpoint: dict,
    *,
    source_v: int,
    next_v: int,
) -> dict | None:
    """Build the deterministic no-strength Direction receipt for bootstrap.

    The ordinary Direction auditor is explicitly a historical-performance
    consumer.  A protocol-bootstrap generation has no admissible strength
    history, so invoking that auditor (even if its output were later ignored)
    would violate the quarantine boundary.
    """
    audit_context = checkpoint.get("audit_context") or {}
    receipt = audit_context.get("protocol_bootstrap")
    if not isinstance(receipt, dict):
        return None
    prepared = audit_context.get("prepared_artifact_contract") or {}
    prepare_receipt = audit_context.get("protocol_bootstrap_prepare") or {}
    from prepared_baseline_contract import validate_prepared_artifact_contract

    prepared_errors = validate_prepared_artifact_contract(
        prepared,
        source_v=int(source_v),
        next_v=int(next_v),
        verify_live_content=False,
    )
    prepared_hash = str(prepared.get("prepared_artifact_hash") or "")
    prepared_contract_digest = str(prepared.get("contract_digest") or "")
    if prepared_errors or not re.fullmatch(r"[0-9a-f]{64}", prepared_hash):
        detail = ",".join(prepared_errors[:8]) or "prepared_artifact_hash_invalid"
        raise RuntimeError(
            "protocol bootstrap Direction requires the exact prepared artifact "
            f"contract: {detail}"
        )
    payload = {
        "repetition_detected": False,
        "exhausted_directions": [],
        "mandatory_constraints": None,
        "suggested_direction": None,
        "confidence": "not_applicable",
        "resolved": False,
        "llm_failed": False,
        "protocol_bootstrap_no_strength": True,
        "evidence_policy": _PROTOCOL_BOOTSTRAP_NO_STRENGTH,
        "source_v": int(source_v),
        "next_v": int(next_v),
        "protocol_bootstrap_receipt_digest": str(
            receipt.get("receipt_digest") or ""
        ),
        "protocol_bootstrap_prepare_receipt_digest": str(
            prepare_receipt.get("receipt_digest") or ""
        ),
        "prepared_artifact_hash": prepared_hash,
        "prepared_artifact_contract_digest": prepared_contract_digest,
    }
    from bot_artifact import canonical_digest

    payload["receipt_digest"] = canonical_digest(payload)
    return payload


def _protocol_bootstrap_master_audit(plan: dict) -> dict:
    """Return the no-history post-plan receipt for a bootstrap Master run."""
    from bot_artifact import canonical_digest

    payload = {
        "plan_coherent": True,
        "contradiction_found": False,
        "contradictions": [],
        "evidence_alignment": "not_applicable",
        "direction_novelty": "not_applicable",
        "overall_pass": True,
        "feedback": "",
        "retry_recommended": False,
        "protocol_bootstrap_no_strength": True,
        "evidence_policy": _PROTOCOL_BOOTSTRAP_NO_STRENGTH,
        "plan_digest": canonical_digest(plan),
    }
    payload["receipt_digest"] = canonical_digest(payload)
    return payload


def _literature_probe_cache_path(next_v: int | str) -> Path:

    """Delegate to tool_planning_literature_probe."""
    return _lp._literature_probe_cache_path(next_v)


def _complete_artifact_fingerprint(root) -> str:

    """Delegate to tool_planning_literature_probe."""
    return _lp._complete_artifact_fingerprint(root)


def _literature_probe_context_fingerprint(
    source_v: int | str | None,
    h2h_weakness: str = "",
    stagnation_info: str = "",
) -> str:

    """Delegate to tool_planning_literature_probe."""
    return _lp._literature_probe_context_fingerprint(source_v, h2h_weakness, stagnation_info)


_LITERATURE_PROBE_BINDING_FIELDS = (
    "master_context_digest",
    "direction_audit_digest",
    "requirement_context",
    "requirement_context_digest",
)

_LITERATURE_PROBE_PAYLOAD_SCHEMA = "national_tcp_literature_probe_payload_v2"
_LITERATURE_PROBE_PRODUCER_SCHEMA = "national_tcp_literature_probe_producer_v2"
_LITERATURE_PROBE_CHECKPOINT_BINDING_SCHEMA = (
    "national_tcp_literature_probe_checkpoint_binding_v1"
)
_LITERATURE_PROBE_TRANSLATION_SCHEMA = (
    "national_tcp_literature_probe_translation_gate_v1"
)
_LITERATURE_PROBE_CACHE_SCHEMA = "national_tcp_literature_probe_cache_v2"
_LITERATURE_PROBE_CACHE_MAX_BYTES = 1_048_576
_LITERATURE_PROBE_REASONS = frozenset({
    "completed",
    "governed_skip",
    "literature_probe_timeout",
    "literature_probe_failed",
})
_LITERATURE_PROPOSAL_FIELDS = (
    "claim",
    "source_url",
    "numeric_claim",
    "target_fn",
    "proposed_change",
    "pseudocode",
    "firing_tuple",
    "h2h_weakness_addressed",
)
_LITERATURE_PROBE_BODY_FIELDS = (
    "schema",
    "next_v",
    "source_v",
    "reason",
    "skipped",
    "weakness",
    "stagnation_info",
    "proposal",
    "candidate_id",
    "gated_out",
    "elapsed_sec",
    "timeout_s",
    "error",
    "context_fingerprint",
    *_LITERATURE_PROBE_BINDING_FIELDS,
    "inject_text",
)
_LITERATURE_PROBE_PAYLOAD_FIELDS = frozenset({
    *_LITERATURE_PROBE_BODY_FIELDS,
    "canonical_payload_digest",
    "producer_receipt",
})


def _literature_canonical_json(value) -> str:

    """Delegate to tool_planning_literature_probe."""
    return _lp._literature_canonical_json(value)


def _literature_digest(value) -> str:

    """Delegate to tool_planning_literature_probe."""
    return _lp._literature_digest(value)


def _literature_checkpoint_identity(
    checkpoint: dict,
    *,
    origin_revision: int | None = None,
) -> str:

    """Delegate to tool_planning_literature_probe."""
    return _lp._literature_checkpoint_identity(checkpoint, origin_revision=origin_revision)


def _literature_checkpoint_binding(
    checkpoint: dict,
    receipt_binding: dict,
) -> dict:

    """Delegate to tool_planning_literature_probe."""
    return _lp._literature_checkpoint_binding(checkpoint, receipt_binding)


def _literature_dispatch_projection(rendered_prompt) -> dict | None:

    """Delegate to tool_planning_literature_probe."""
    return _lp._literature_dispatch_projection(rendered_prompt)


def _expected_literature_dispatch(
    *,
    next_v: int,
    source_v: int,
    weakness: str,
    stagnation_info: str,
) -> dict:

    """Delegate to tool_planning_literature_probe."""
    return _lp._expected_literature_dispatch(next_v=next_v, source_v=source_v, weakness=weakness, stagnation_info=stagnation_info)


def _issue_literature_rendered_prompt(
    *,
    next_v: int,
    source_v: int,
    weakness: str,
    stagnation_info: str,
):

    """Delegate to tool_planning_literature_probe."""
    return _lp._issue_literature_rendered_prompt(
        next_v=next_v, source_v=source_v, weakness=weakness, stagnation_info=stagnation_info
    )


def _normalize_literature_proposal(proposal) -> dict | None:

    """Delegate to tool_planning_literature_probe."""
    return _lp._normalize_literature_proposal(proposal)


def _literature_candidate_submission(
    proposal: dict | None,
    next_v: int,
    submitted_candidate_id: str | None = None,
) -> dict | None:

    """Delegate to tool_planning_literature_probe."""
    return _lp._literature_candidate_submission(proposal, next_v, submitted_candidate_id)


def _expected_literature_candidate_id(
    proposal: dict | None,
    *,
    checkpoint_identity: str,
    terminal_output_sha256: str | None,
) -> str | None:

    """Delegate to tool_planning_literature_probe."""
    return _lp._expected_literature_candidate_id(proposal, checkpoint_identity=checkpoint_identity, terminal_output_sha256=terminal_output_sha256)


def _literature_translation_receipt(
    proposal: dict | None,
    *,
    next_v: int,
    candidate_id,
    checkpoint_identity: str,
    terminal_output_sha256: str | None,
) -> dict:

    """Delegate to tool_planning_literature_probe."""
    return _lp._literature_translation_receipt(proposal, next_v=next_v, candidate_id=candidate_id, checkpoint_identity=checkpoint_identity, terminal_output_sha256=terminal_output_sha256)


def _literature_probe_stale_result(next_v: int | str, source_v: int | str | None) -> dict:

    """Delegate to tool_planning_literature_probe."""
    return _lp._literature_probe_stale_result(next_v, source_v)


def _h2h_citation_audit_feedback(next_v, errors, repair_guidance=""):

    """Delegate to tool_planning_literature_probe."""
    return _lp._h2h_citation_audit_feedback(next_v, errors, repair_guidance)


def _literature_probe_inject_text(payload: dict) -> str:

    """Delegate to tool_planning_literature_probe."""
    return _lp._literature_probe_inject_text(payload)


def _build_literature_probe_payload(
    payload: dict,
    *,
    checkpoint: dict,
    receipt_binding: dict,
    rendered_prompt=None,
    terminal_output: str | None = None,
) -> dict:

    """Delegate to tool_planning_literature_probe."""
    return _lp._build_literature_probe_payload(payload, checkpoint=checkpoint, receipt_binding=receipt_binding, rendered_prompt=rendered_prompt, terminal_output=terminal_output)


def _literature_probe_payload_errors(
    data: dict | None,
    *,
    checkpoint: dict | None,
    receipt_binding: dict | None,
    require_origin_checkpoint: bool,
) -> list[str]:

    """Delegate to tool_planning_literature_probe."""
    return _lp._literature_probe_payload_errors(data, checkpoint=checkpoint, receipt_binding=receipt_binding, require_origin_checkpoint=require_origin_checkpoint)


def _json_without_duplicate_keys(raw: bytes):

    """Delegate to tool_planning_literature_probe."""
    return _lp._json_without_duplicate_keys(raw)


def _open_directory_nofollow(path: Path) -> int:

    """Delegate to tool_planning_literature_probe."""
    return _lp._open_directory_nofollow(path)


def _read_regular_single_link_json(path: Path):

    """Delegate to tool_planning_literature_probe."""
    return _lp._read_regular_single_link_json(path)


def _write_regular_single_link_json(path: Path, value: dict) -> None:

    """Delegate to tool_planning_literature_probe."""
    return _lp._write_regular_single_link_json(path, value)


def _read_literature_probe_cache(
    next_v: int | str,
    *,
    source_v: int | str | None = None,
    h2h_weakness: str = "",
    stagnation_info: str = "",
    receipt_binding: dict | None = None,
    checkpoint: dict | None = None,
) -> dict | None:

    """Delegate to tool_planning_literature_probe."""
    return _lp._read_literature_probe_cache(next_v, source_v=source_v, h2h_weakness=h2h_weakness, stagnation_info=stagnation_info, receipt_binding=receipt_binding, checkpoint=checkpoint)


def _normalize_literature_probe_result(
    data: dict,
    next_v: int | str,
    *,
    checkpoint: dict | None = None,
    receipt_binding: dict | None = None,
    cached: str = "",
) -> dict | None:

    """Delegate to tool_planning_literature_probe."""
    return _lp._normalize_literature_probe_result(data, next_v=next_v, checkpoint=checkpoint, receipt_binding=receipt_binding, cached=cached)


def _read_literature_probe_checkpoint(
    next_v: int | str,
    *,
    source_v: int | str | None = None,
    h2h_weakness: str = "",
    stagnation_info: str = "",
    receipt_binding: dict | None = None,
) -> dict | None:

    """Delegate to tool_planning_literature_probe."""
    return _lp._read_literature_probe_checkpoint(next_v, source_v=source_v, h2h_weakness=h2h_weakness, stagnation_info=stagnation_info, receipt_binding=receipt_binding)


def _persist_literature_probe_result(
    next_v: int | str,
    source_v: int | str | None,
    payload: dict,
    *,
    receipt_binding: dict | None = None,
) -> bool:

    """Delegate to tool_planning_literature_probe."""
    return _lp._persist_literature_probe_result(next_v, source_v, payload, receipt_binding=receipt_binding)


def _write_literature_probe_cache(
    next_v: int | str,
    payload: dict,
    *,
    checkpoint: dict | None = None,
    receipt_binding: dict | None = None,
) -> dict:

    """Delegate to tool_planning_literature_probe."""
    return _lp._write_literature_probe_cache(next_v, payload, checkpoint=checkpoint, receipt_binding=receipt_binding)


# ──────────────────────────────────────────────
# Direction Audit Stage (pre-Master)
# ──────────────────────────────────────────────

@tool("run_direction_audit", "Audit recent generation directions for repetition. Returns exhausted directions and mandatory constraints for the Master.", {"source_v": int, "next_v": int})
async def run_direction_audit(args):
    source_v = args.get("source_v")
    next_v = args.get("next_v")
    if source_v is None or next_v is None:
        _v, source_v = _resolve_version_args(args)
        next_v = next_v or _v
    if source_v is None or next_v is None:
        return _json_tool_result({"error": "Missing source_v/next_v and no active checkpoint"})

    _existing = _matching_checkpoint(next_v, source_v)
    if not isinstance(_existing, dict):
        return _json_tool_result({
            "error": "DIRECTION_AUDIT_CHECKPOINT_REQUIRED",
            "next_v": next_v,
            "source_v": source_v,
            "directive": (
                "Direction audit may only consume the active prepared checkpoint; "
                "do not reconstruct or audit a stale generation."
            ),
        })

    # A completed audit is the one narrow idempotent path.  It returns the
    # owned result without re-running the LLM or mutating a later checkpoint.
    if (
        _existing.get("stage") == "direction_audited"
        and isinstance(_existing.get("direction_audit"), dict)
    ):
        ui = _get_ui()
        ui.log_history("Direction audit: using cached result (already completed)", "info")
        return _json_tool_result({
            "direction_audit": _existing["direction_audit"],
            "logs": ui.get_output(),
            "idempotent_cache": True,
        })
    if _existing.get("stage") != "prepared":
        route = route_policy(_existing)
        return _json_tool_result({
            "error": "DIRECTION_AUDIT_WRONG_STAGE",
            "next_v": next_v,
            "source_v": source_v,
            "checkpoint_stage": _existing.get("stage"),
            "expected_stage": "prepared",
            "next_tool": route.get("next_tool"),
            "allowed_tools": route.get("allowed_tools"),
            "directive": (
                "Do not run or overwrite direction audit outside prepared. "
                "Follow the checkpoint-owned next tool instead."
            ),
        })

    _set_pipeline_status(f"Auditing directions for v{next_v}")

    ui = _get_ui()
    neutral_bootstrap_audit = _protocol_bootstrap_direction_audit(
        _existing,
        source_v=int(source_v),
        next_v=int(next_v),
    )
    # Never call the historical-performance LLM auditor during protocol
    # bootstrap.  The deterministic receipt is the complete Direction result.
    result = (
        neutral_bootstrap_audit
        if neutral_bootstrap_audit is not None
        else await _run_direction_audit(source_v, ui)
    )

    repetition = result.get("repetition_detected", False)
    exhausted = result.get("exhausted_directions", [])
    constraints = result.get("mandatory_constraints")
    suggested = result.get("suggested_direction")
    confidence = result.get("confidence", "low")
    llm_failed = result.get("llm_failed", False)

    direction_audit_payload = (
        dict(neutral_bootstrap_audit)
        if neutral_bootstrap_audit is not None
        else {
            "repetition_detected": repetition,
            "exhausted_directions": exhausted,
            "mandatory_constraints": constraints,
            "suggested_direction": suggested,
            "confidence": confidence,
            "resolved": False,
            # Propagate the infra marker so run_master can skip injecting the
            # (untrustworthy, empty) audit mandatory_constraints block.
            "llm_failed": llm_failed,
        }
    )

    # Re-check after the LLM call.  A late response must never refresh audit
    # data after another controller advanced the generation.
    _ckpt = _matching_checkpoint(next_v, source_v)
    if not isinstance(_ckpt, dict) or _ckpt.get("stage") != "prepared":
        route = route_policy(_ckpt) if isinstance(_ckpt, dict) else {}
        return _json_tool_result({
            "error": "DIRECTION_AUDIT_STALE_RESULT",
            "next_v": next_v,
            "source_v": source_v,
            "checkpoint_stage": _ckpt.get("stage") if isinstance(_ckpt, dict) else None,
            "next_tool": route.get("next_tool"),
            "directive": (
                "The prepared checkpoint changed while direction audit was running. "
                "Its result was discarded and no checkpoint was overwritten."
            ),
        })
    recorded = write_pipeline_checkpoint(
        next_v, source_v, "direction_audited",
        direction_audit=direction_audit_payload,
        master_plan=_ckpt.get("master_plan"),
        worker_failure_count=_ckpt.get("worker_failure_count", 0),
        expected_checkpoint_revision=_existing.get("checkpoint_revision"),
        expected_checkpoint_stage="prepared",
        expected_workflow_run_id=_existing.get("workflow_run_id"),
    )
    if not recorded:
        return _json_tool_result({
            "error": "DIRECTION_AUDIT_CHECKPOINT_WRITE_REJECTED",
            "next_v": next_v,
            "source_v": source_v,
            "directive": (
                "The prepared checkpoint changed before the Direction receipt "
                "could be committed. The result was discarded; follow the "
                "current checkpoint-owned route."
            ),
        })

    if neutral_bootstrap_audit is not None:
        event_type = "pipeline.direction_audit_protocol_bootstrap_neutral"
        severity = "success"
        msg = (
            f"Direction audit: deterministic no-strength bootstrap receipt for v{next_v}"
        )
    elif llm_failed:
        # Infra failure is neither "warning" (repetition) nor "passed" (clean).
        # Log it as a distinct event so the orchestrator can see the audit was
        # untrustworthy; run_master also emits its own pipeline.direction_audit_infra.
        event_type = "pipeline.direction_audit_infra"
        severity = "warn"
        msg = (f"Direction audit: LLM infrastructure failure for v{next_v} — "
               "verdict untrustworthy, proceeding with mechanical backstop only")
    else:
        event_type = "pipeline.direction_audit_warning" if repetition else "pipeline.direction_audit_passed"
        severity = "warn" if repetition else "success"
        msg = (f"Direction audit: repetition detected ({', '.join(exhausted)})" if repetition
               else "Direction audit: no repetition detected")
    log_system_event(event_type, severity, msg, {
        "next_v": next_v, "source_v": source_v,
        "repetition_detected": repetition,
        "exhausted_directions": exhausted,
        "llm_failed": llm_failed,
        "protocol_bootstrap_no_strength": neutral_bootstrap_audit is not None,
        "receipt_digest": direction_audit_payload.get("receipt_digest"),
    })

    return _json_tool_result({
        "direction_audit": direction_audit_payload,
        "logs": ui.get_output(),
    })


# ──────────────────────────────────────────────
# Master Stage
# ──────────────────────────────────────────────

# Hard cap on total Master-stage failures per generation. A generation gets the
# initial Master plan and one corrective re-plan. After that, the tool abandons
# the generation itself instead of returning a directive that the orchestrator
# might ignore and re-call. audit_attempt in the checkpoint doubles as the
# counter (reset to 0 on successful master_planned write).
MAX_MASTER_TOTAL_FAILURES = 2
MAX_MASTER_AUDIT_RETRIES = max(0, MAX_MASTER_TOTAL_FAILURES - 1)
LITERATURE_PROBE_TIMEOUT = int(os.environ.get("POK_LITERATURE_PROBE_TIMEOUT", "600"))


def _bump_master_fail_count(next_v, source_v, value=None, audit_context=None):
    """Increment (or set) the Master-stage failure counter in the checkpoint.

    Reuses the audit_attempt field: both plan-JSON-collapse (data is None) and
    audit-rejection are "Master-stage failures", and run_master's hard cap at
    the top of the function counts them together to stop token-burning loops.
    Returns the new count (0 on any error / mismatched generation).
    """
    try:
        from evolution_infra import read_pipeline_checkpoint
        ckpt = read_pipeline_checkpoint() or {}
        if ckpt.get("next_v") != next_v:
            return 0
        cur = int(ckpt.get("audit_attempt") or 0)
        new = cur + 1 if value is None else int(value)
        write_pipeline_checkpoint(
            next_v, source_v, ckpt.get("stage") or "direction_audited",
            audit_attempt=new, touch_stage_timestamp=True,
            audit_context=audit_context,
        )
        return new
    except Exception:
        return 0


def _touch_master_checkpoint(next_v, source_v, *, phase, audit_attempt=None, audit_context=None):
    """Publish runtime liveness while Master/audit LLM work is progressing.

    `run_master` can legitimately spend many minutes inside Master retries and
    plan-audit loops before it reaches the real `master_planned` stage.  This is
    deliberately a gitignored sidecar, not a semantic checkpoint write: billing
    or authentication pauses must leave pipeline_state bytes/revision unchanged.
    """
    try:
        from evolution_infra import read_pipeline_checkpoint
        from pipeline_state import write_pipeline_runtime_heartbeat

        ckpt = read_pipeline_checkpoint() or {}
        if ckpt.get("next_v") != next_v:
            return False
        stage = ckpt.get("stage") or "direction_audited"
        if stage in {"timed_out", "infra_timed_out", "archived", "abandoned"}:
            return False
        ok = write_pipeline_runtime_heartbeat(
            ckpt,
            phase=phase,
            audit_attempt=audit_attempt,
            audit_context=audit_context,
        )
        if ok:
            log_system_event(
                "pipeline.master_checkpoint_heartbeat",
                "info",
                f"Master checkpoint heartbeat for v{next_v} ({phase})",
                {
                    "next_v": next_v,
                    "source_v": ckpt.get("source_v", source_v),
                    "stage": stage,
                    "phase": phase,
                    "audit_attempt": audit_attempt,
                },
            )
        return bool(ok)
    except Exception as exc:
        _log.debug("Master runtime heartbeat failed (%s): %s", phase, exc)
        return False


def _clear_master_runtime_heartbeat(next_v, source_v):
    """Remove only the current generation's liveness sidecar."""
    try:
        from evolution_infra import read_pipeline_checkpoint
        from pipeline_state import clear_pipeline_runtime_heartbeat

        checkpoint = read_pipeline_checkpoint() or {}
        if (
            checkpoint.get("next_v") == next_v
            and checkpoint.get("source_v") == source_v
        ):
            return clear_pipeline_runtime_heartbeat(checkpoint)
    except Exception:
        pass
    return False


async def _abandon_master_generation(next_v, source_v, *, error, fail_count, reason,
                                     event_type, event_message, ui=None,
                                     payload=None, directive=None):
    """Signal a Master-stuck generation abandon for the orchestrator to finalize.

    The orchestrator is intentionally LLM-driven, so returning a plain text
    "please abandon" directive is not a reliable control plane. All Master
    retry-budget exhaustion paths route here.

    This function used to call ``_do_abandon_generation`` *inline* (from inside
    a tool dispatch, while the orchestrator loop was still running).  That raced
    the loop's concurrent ``checkpoint_revision`` bumps: the canonical abandon's
    CAS revalidation refused with ``expected_checkpoint_identity_mismatch``,
    the ``abandoned: False`` result was ignored, and the orchestrator re-entered
    ``run_master`` every ~30 s — burning LLM budget forever (the v161 / v106
    livelock class).  See ``master_abandon_signal.py`` for the full background.

    The fix mirrors the HTTP ``POST /api/control/abandon`` "stop-then-abandon"
    pattern: instead of running the publication-authority transaction from the
    tool layer against a moving checkpoint, we just *signal* the request
    (``master_abandon_signal.request_abandon``) and return a terminal tool
    result.  The orchestrator loop, which is the sole owner of the publication
    lifecycle *between* cycles, finalizes the abandon right after
    ``_run_one_cycle`` returns — when the checkpoint is guaranteed quiescent —
    by re-reading the live checkpoint for a fresh ``expected_abandon_identity``
    and calling ``_do_abandon_generation`` with ``_bypass_rate_limit=True``.
    """
    payload = dict(payload or {})
    event_data = {"next_v": next_v, "source_v": source_v, "fail_count": fail_count}
    event_data.update(payload)
    try:
        log_system_event(event_type, "error", event_message, event_data)
    except Exception:
        pass
    if ui:
        try:
            ui.log_history(event_message, "error")
        except Exception:
            pass
    # Signal the orchestrator loop to finalize this abandon against a quiescent
    # checkpoint.  We do NOT pass an identity snapshot: by the time the loop
    # consumes the signal the checkpoint is static, and the loop re-reads the
    # live checkpoint for a fresh CAS identity (carrying a stale snapshot here
    # would re-introduce the exact race this fix closes).
    try:
        from master_abandon_signal import request_abandon
        request_abandon(reason)
    except Exception:
        pass
    try:
        from orchestrator_session import _clear_orchestrator_session
        _clear_orchestrator_session()
    except Exception:
        pass
    result = {
        "error": error,
        "fail_count": fail_count,
        **payload,
        # The abandon is pending finalization by the orchestrator loop.  We do
        # not claim ``abandoned: True`` here (the transaction has not run yet),
        # but we tag the result so any observability layer can distinguish a
        # signaled-but-pending abandon from a normal tool completion.
        "abandon_signaled": True,
        "abandon_reason": reason,
        "directive": directive or (
            "Master planning exhausted its retry budget and this generation "
            "was signaled for abandonment. The orchestrator will finalize the "
            "abandon against a quiescent checkpoint and start a fresh "
            "generation; do not call run_master again for this candidate."
        ),
        "logs": ui.get_output() if ui else "",
    }
    return _json_tool_result(result)


async def _abandon_strict_master_authority(
    next_v,
    source_v,
    *,
    error,
    ui,
):
    """Classify durable strict-authority drift as control-plane failure."""

    validation_errors = list(getattr(error, "errors", ()) or (str(error),))
    return await _abandon_master_generation(
        next_v,
        source_v,
        error="SYSTEM_STRICT_AUTHORITY_INVALID",
        fail_count=0,
        reason=(
            "system_strict_authority_invalid:"
            + ";".join(validation_errors)[:700]
        ),
        event_type="pipeline.system_strict_authority_invalid",
        event_message=(
            f"Strict provider/evidence authority failed closed for v{next_v}"
        ),
        ui=ui,
        payload={
            "failure_class": "control_plane",
            "validation_errors": validation_errors,
        },
        directive=(
            "The strict authority journal, prompt, context, or invocation evidence "
            "drifted. The generation was canonically abandoned; prepare a fresh "
            "v143 workflow and do not consume an LLM infrastructure retry."
        ),
    )


async def _block_master_authority(
    next_v,
    source_v,
    *,
    error,
    ui,
):
    """Stop on deterministic system-authority drift without spending labels."""

    validation_errors = list(getattr(error, "errors", ()) or (str(error),))
    message = (
        f"Master authority is recovery-blocked for v{next_v}; provider retry "
        "and automatic generation abandonment are forbidden"
    )
    try:
        log_system_event(
            "pipeline.master_authority_recovery_blocked",
            "error",
            message,
            {
                "next_v": int(next_v),
                "source_v": int(source_v),
                "failure_class": "control_plane",
                "validation_errors": validation_errors,
            },
        )
    except Exception:
        pass
    if ui:
        try:
            ui.log_history(message, "error")
        except Exception:
            pass
    return _json_tool_result({
        "error": "MASTER_AUTHORITY_RECOVERY_BLOCKED",
        "failure_class": "control_plane",
        "recovery_blocked": True,
        "retryable": False,
        "action": "repair_master_authority_contract",
        "next_v": int(next_v),
        "source_v": int(source_v),
        "validation_errors": validation_errors,
        "directive": (
            "Preserve this checkpoint and candidate. Repair and validate the "
            "system-owned checkpoint/evidence/allocation contract, synchronize "
            "through origin/main, then resume the same canonical target. Do not "
            "retry an LLM or abandon/allocate another label."
        ),
    })


async def _force_abandon_official_rework_generation(
    next_v,
    source_v,
    *,
    actor_lock_owned=False,
):
    """End a non-converging formal-repair loop in the tool control plane."""
    try:
        from orchestrator_session import _clear_orchestrator_session
        _clear_orchestrator_session()
    except Exception:
        pass
    try:
        from evolution_core import read_pipeline_checkpoint
        from tool_bot_management import (
            _do_abandon_generation,
            expected_abandon_identity,
        )
        abandon_identity = expected_abandon_identity(read_pipeline_checkpoint())
        return await _do_abandon_generation(
            reason="official_rework_circuit_breaker",
            _actor_lock_owned=actor_lock_owned,
            **abandon_identity,
        )
    except Exception as exc:
        return {
            "abandoned": False,
            "error": f"official rework abandon failed: {type(exc).__name__}: {exc}",
            "next_v": next_v,
            "source_v": source_v,
        }


async def _force_abandon_frozen_worker_generation(
    next_v,
    source_v,
    reason,
    *,
    actor_lock_owned=False,
):
    """Fail closed when a frozen Worker transaction cannot be reproduced."""
    try:
        from orchestrator_session import _clear_orchestrator_session
        _clear_orchestrator_session()
    except Exception:
        pass
    try:
        from evolution_core import read_pipeline_checkpoint
        from tool_bot_management import (
            _do_abandon_generation,
            expected_abandon_identity,
        )
        abandon_identity = expected_abandon_identity(read_pipeline_checkpoint())
        return await _do_abandon_generation(
            reason=reason,
            _actor_lock_owned=actor_lock_owned,
            **abandon_identity,
        )
    except Exception as exc:
        return {
            "abandoned": False,
            "error": f"frozen Worker abandon failed: {type(exc).__name__}: {exc}",
            "next_v": next_v,
            "source_v": source_v,
        }


async def _handle_master_analysis_failure(next_v, source_v, ui, *, message,
                                          reason, payload=None):
    """Count a Master analysis collapse against the same budget as bad plans.

    `_run_master_analysis` returns None for malformed output after retries and
    for role-level LLM failures such as total timeouts. Treat both as Master-stage
    failures so the orchestrator cannot keep re-calling `run_master` forever.
    """
    payload = dict(payload or {})
    audit_context = {
        "master_analysis": {
            "error": message,
            **payload,
        }
    }
    fail_count = _bump_master_fail_count(
        next_v,
        source_v,
        audit_context=audit_context,
    )
    if fail_count >= MAX_MASTER_TOTAL_FAILURES:
        return await _abandon_master_generation(
            next_v,
            source_v,
            error="MASTER_ANALYSIS_EXHAUSTED",
            fail_count=fail_count,
            reason=reason,
            event_type="pipeline.master_analysis_exhausted_abandon",
            event_message=(
                f"Master analysis failed {fail_count} times for v{next_v} — "
                "abandoning invalid generation"
            ),
            ui=ui,
            payload=payload,
            directive=(
                "Master analysis failed too many times and this generation was "
                "abandoned. Start a fresh generation; do not call run_master "
                "again for the abandoned candidate."
            ),
        )
    try:
        log_system_event(
            "pipeline.master_analysis_failed",
            "warn",
            f"Master analysis failed for v{next_v} (fail_count={fail_count}): {message}",
            {
                "next_v": next_v,
                "source_v": source_v,
                "fail_count": fail_count,
                **payload,
            },
        )
    except Exception:
        pass
    return _json_tool_result({
        "error": "MASTER_ANALYSIS_FAILED",
        "fail_count": fail_count,
        "directive": (
            "Master failed to produce a valid plan. If run_master keeps failing, "
            "do NOT retry indefinitely; start a fresh generation or fix the "
            "Master prompt/tooling failure."
        ),
        "logs": ui.get_output() if ui else "",
        **payload,
    })


async def _handle_master_llm_infrastructure(
    next_v,
    source_v,
    ui,
    *,
    component,
    issue,
    prompt_digest,
):
    """Persist a neutral, identity-bound retry for Master-side LLM transport."""
    from pipeline_infrastructure import infrastructure_attempt_key

    checkpoint = _matching_checkpoint(next_v, source_v) or {}
    backend_contract = {
        key: os.environ.get(key, "")
        for key in (
            "ANTHROPIC_MODEL",
            "CLAUDE_MODEL",
            "POK_LLM_MODEL",
            "ANTHROPIC_BASE_URL",
        )
    }
    attempt_key = infrastructure_attempt_key(
        component=component,
        candidate_fingerprint=_complete_artifact_fingerprint(get_bot_dir(next_v)),
        source_fingerprint=_master_source_fingerprint(checkpoint, source_v),
        harness_identity=prompt_digest,
        contract_identity=str(
            ((checkpoint.get("runtime_contract_ledger") or {}).get("ledger_digest") or "")
        ),
        extra={"backend_contract": backend_contract},
    )
    infra_result = await _record_infrastructure_failure(
        next_v,
        source_v,
        owner_tool="run_master",
        resume_stage="direction_audited",
        component=component,
        code=f"{component}_unavailable",
        attempt_key=attempt_key,
        issues=[issue],
        # A transport/stall failure is not evidence that the candidate or its
        # strategy direction is invalid. Six bounded outer attempts give the
        # generation-scoped role journal enough opportunities to fill only its
        # missing slots while avoiding an unbounded paid retry loop. Existing
        # attempt=2/3 overlays upgrade in place to attempt=3/6; they are never
        # reset or hand-edited.
        max_attempts=6,
        metadata={
            "prompt_digest": prompt_digest,
            "backend_contract": backend_contract,
        },
    )
    return _json_tool_result({
        **infra_result,
        "llm_failed": True,
        "directive": (
            "Master-side LLM infrastructure exhausted and the generation was abandoned."
            if infra_result.get("abandoned")
            else "Retry run_master for the same generation; do not count this as an invalid plan."
        ),
        "logs": ui.get_output() if ui else "",
    })


def _handle_master_ensemble_provider_parked(next_v, source_v, ui, error):
    """Return an attempt-neutral retry for one missing journaled Master role."""

    _clear_master_runtime_heartbeat(next_v, source_v)
    needs_attention = bool(error.needs_attention)
    payload = {
        "error": (
            "MASTER_ENSEMBLE_PROVIDER_ATTENTION_REQUIRED"
            if needs_attention
            else "MASTER_ENSEMBLE_PROVIDER_PARKED"
        ),
        "failure_class": "infrastructure",
        "pending": not needs_attention,
        "action": (
            "operator_attention_required"
            if needs_attention
            else "retry_same_tool"
        ),
        "retry_after_sec": float(error.retry_after_sec),
        "abandoned": False,
        "checkpoint_preserved": True,
        "slot": error.slot,
        "role_attempt": int(error.role_attempt),
        "accepted_slots": list(error.accepted_slots),
        "pending_slots": list(error.pending_slots),
        "authority_run_id": error.authority_run_id,
        "needs_attention": needs_attention,
        "recovery_blocked": needs_attention,
        "validation_errors": (
            [
                "master_ensemble_provider_role_retry_exhausted:"
                f"{error.slot}:attempt_{error.role_attempt}"
            ]
            if needs_attention
            else []
        ),
        "issue": error.issue,
        "directive": (
            (
                "The same journaled role failed three or more provider "
                "attempts. Preserve this generation and inspect provider health; "
                "do not abandon or allocate a successor label."
                if needs_attention
                else "Retry run_master for the same generation after the bounded "
                "cooldown. Reuse accepted journal slots and dispatch only missing "
                "roles; do not abandon or allocate a successor label."
            )
        ),
        "logs": ui.get_output() if ui else "",
    }
    try:
        log_system_event(
            "pipeline.master_ensemble_provider_parked",
            "warn" if not error.needs_attention else "error",
            (
                f"Master v{next_v} parked missing role {error.slot} "
                f"(role attempt {error.role_attempt})"
            ),
            {
                key: value
                for key, value in payload.items()
                if key not in {"logs", "directive"}
            },
        )
    except Exception:
        pass
    return _json_tool_result(payload)


def _normalize_master_plan_paths(plan, source_v, next_v):

    """Delegate to tool_planning_master_plan_validation."""
    return _mpv._normalize_master_plan_paths(plan, source_v, next_v)


def _normalize_and_log_master_plan_paths(plan, source_v, next_v):

    """Delegate to tool_planning_master_plan_validation."""
    return _mpv._normalize_and_log_master_plan_paths(plan, source_v, next_v)


# Citation patterns the agents use to reference spotlight hands, and the
# Tuner structural-instruction keyword list. Authoritative copies now live in
# tool_planning_master_plan_validation (only the moved functions consume them);
# a lazy module-level __getattr__ (see bottom of file) re-exposes them on
# ``tool_planning._CITATION_RE`` / ``tool_planning._TUNER_STRUCTURAL_PATTERNS``
# for any external reference without forcing an eager read at import time
# (which would create a circular import when this module is imported first).


def _load_replay_anchor_map(next_v=None):

    """Delegate to tool_planning_master_plan_validation."""
    return _mpv._load_replay_anchor_map(next_v)


def _check_citations(text_list, anchor_map):

    """Delegate to tool_planning_master_plan_validation."""
    return _mpv._check_citations(text_list, anchor_map)


def _sanitize_unverified_replay_citations(text, anchor_map):

    """Delegate to tool_planning_master_plan_validation."""
    return _mpv._sanitize_unverified_replay_citations(text, anchor_map)


def _verify_cited_replays(plan, *, next_v=None):

    """Delegate to tool_planning_master_plan_validation."""
    return _mpv._verify_cited_replays(plan, next_v=next_v)


def _validate_master_plan(
    plan,
    next_v=None,
):

    """Delegate to tool_planning_master_plan_validation."""
    return _mpv._validate_master_plan(plan, next_v=next_v)


def _runtime_contract_errors(task: dict, index: int, layer: str) -> list[str]:

    """Delegate to tool_planning_master_plan_validation."""
    return _mpv._runtime_contract_errors(task, index, layer)


def _build_generation_architecture_policy(
    source_v: int,
    *,
    prepared_capability_snapshot: dict | None = None,
    prepared_dir: Path | None = None,
    allow_lineage_only_source: bool = False,
) -> dict:

    """Delegate to tool_planning_master_plan_validation."""
    return _mpv._build_generation_architecture_policy(
        source_v,
        prepared_capability_snapshot=prepared_capability_snapshot,
        prepared_dir=prepared_dir,
        allow_lineage_only_source=allow_lineage_only_source,
    )


def _master_snapshot_binding_errors(checkpoint, next_v):

    """Delegate to tool_planning_master_plan_validation."""
    return _mpv._master_snapshot_binding_errors(checkpoint, next_v)


@tool("run_master", "Run Master Architect analysis to plan the next generation. Returns a task plan with worker assignments.", {"source_v": int, "next_v": int, "stagnation_info": str, "match_analysis": str, "performance_verification": str, "direction_audit": str, "research_proposals": str})
async def run_master(args):
    """Delegate to tool_planning_master_dispatch.

    The @tool decoration stays here so ``run_master.handler`` and the
    runtime git/worktree guard resolve on ``tool_planning`` exactly as
    before; the full plan/audit/compile/validation body lives in the
    companion and routes parent symbols through ``_tp``.
    """
    return await _md.run_master_impl(args)


# ──────────────────────────────────────────────
# Literature Probe Stage (A5, evolution-plan-refresh-jun21)
# ──────────────────────────────────────────────

@tool("run_literature_probe", "Deep-research a specific H2H weakness via web search (Exa) and synthesize ONE codable strategy proposal. Governed by research_governance (cooldown/blacklist/translation gate). Stagnation-triggered. Output is a HYPOTHESIS for run_master — it does NOT modify bot code directly.", {"source_v": int, "next_v": int, "h2h_weakness": str, "stagnation_info": str})
async def run_literature_probe(args):
    """Delegate to tool_planning_literature_probe."""
    return await _lp.run_literature_probe(args)


# ──────────────────────────────────────────────
# Worker Stage
# ──────────────────────────────────────────────

def _incremental_reset_next_dir(next_dir, source_dir):
    """Delegate to tool_planning_identity_replan."""
    return _tpi._incremental_reset_next_dir(next_dir, source_dir)


def _clear_compiled_task_context(next_dir):
    """Delegate to tool_planning_identity_replan."""
    return _tpi._clear_compiled_task_context(next_dir)


def _cleanup_worker_transients_before_identity_refresh(next_dir):
    """Delegate to tool_planning_identity_replan."""
    return _tpi._cleanup_worker_transients_before_identity_refresh(next_dir)


_IDENTITY_REPLAN_AUDIT_KEYS = frozenset({
    "selection",
    "master_context",
    "protocol_bootstrap",
    "protocol_bootstrap_prepare",
})
_LEGACY_IDENTITY_REPLAN_KEYS = frozenset({
    "source_stage",
    "identity_errors",
    "candidate_reset_to_source",
    "runtime_contract_ledger_reset",
    "previous_runtime_contract_ledger_digest",
    "directive",
})


def _identity_replan_operation_id(ckpt, prepared_hash: str) -> str:
    payload = {
        "workflow_run_id": str(ckpt.get("workflow_run_id") or ""),
        "checkpoint_revision": int(ckpt.get("checkpoint_revision") or 0),
        "source_v": int(ckpt.get("source_v")),
        "next_v": int(ckpt.get("next_v")),
        "prepared_artifact_hash": str(prepared_hash),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]
    # A completed forward projection may be rolled back when checkpoint CAS
    # loses.  Its immutable completion receipt must never be reused for a later
    # forward retry, because that receipt correctly proves that *that* exchange
    # already finished.  A fresh suffix creates a new content-CAS operation;
    # an actual crash inside an unfinished exchange is still recovered first by
    # WorkerArtifactStore's destination journal.
    return (
        f"identity-replan-{int(ckpt['next_v'])}-{digest}-"
        f"{uuid.uuid4().hex[:12]}"
    )


def _legacy_identity_replan_receipt_errors(
    ckpt,
    *,
    source_hash: str,
    current_hash: str,
    prepared_contract: dict,
) -> list[str]:
    """Validate the exact post-540a broken-state recovery preimage.

    The retired reset wrote no digest of its own.  Recovery therefore accepts
    it only when every independently checkable boundary agrees: the checkpoint
    is the empty Direction replan, the candidate is either the exact copied
    parent or the deterministic target-version preparation, and the old
    prepared contract is byte-for-byte the one rebuilt from that parent.
    """

    errors: list[str] = []
    audit = ckpt.get("audit_context") or {}
    receipt = audit.get("architecture_policy_identity_replan")
    if not isinstance(receipt, dict):
        return ["identity_replan_receipt_missing_or_not_object"]
    if set(receipt) != _LEGACY_IDENTITY_REPLAN_KEYS:
        errors.append("identity_replan_receipt_fields_mismatch")
    if receipt.get("source_stage") not in {
        "quality_failed",
        "repair_planned",
        "rework_running",
    }:
        errors.append("identity_replan_source_stage_invalid")
    identity_errors = receipt.get("identity_errors")
    if (
        not isinstance(identity_errors, list)
        or not identity_errors
        or any(not isinstance(item, str) or not item for item in identity_errors)
    ):
        errors.append("identity_replan_policy_errors_invalid")
    if receipt.get("candidate_reset_to_source") is not True:
        errors.append("identity_replan_parent_reset_not_proven")
    if receipt.get("runtime_contract_ledger_reset") is not True:
        errors.append("identity_replan_ledger_reset_not_proven")
    prior_ledger = str(receipt.get("previous_runtime_contract_ledger_digest") or "")
    if prior_ledger and re.fullmatch(r"[0-9a-f]{64}", prior_ledger) is None:
        errors.append("identity_replan_previous_ledger_digest_invalid")
    if ckpt.get("stage") != "direction_audited":
        errors.append("identity_replan_checkpoint_stage_invalid")
    if ckpt.get("parent2_v") is not None:
        errors.append("identity_replan_crossover_forbidden")
    if ckpt.get("master_plan") not in ({}, None):
        errors.append("identity_replan_master_plan_not_empty")
    if ckpt.get("runtime_contract_ledger") is not None:
        errors.append("identity_replan_runtime_contract_ledger_not_cleared")
    if ckpt.get("gate_results") not in ({}, None):
        errors.append("identity_replan_gate_results_not_cleared")
    if ckpt.get("publication_intent") is not None:
        errors.append("identity_replan_publication_intent_present")
    if ckpt.get("official_job") is not None:
        errors.append("identity_replan_official_job_present")
    if ckpt.get("infra_failure") is not None:
        errors.append("identity_replan_infrastructure_overlay_present")
    if current_hash not in {
        str(source_hash),
        str(prepared_contract.get("prepared_artifact_hash") or ""),
    }:
        errors.append("identity_replan_candidate_preimage_mismatch")
    if audit.get("prepared_artifact_contract") != prepared_contract:
        errors.append("identity_replan_prepared_contract_not_reproducible")
    return list(dict.fromkeys(errors))


def _materialize_identity_replan_candidate(
    ckpt,
    next_dir,
    source_dir,
    *,
    recover_persisted_reset: bool,
):
    """Delegate to tool_planning_identity_replan."""
    return _tpi._materialize_identity_replan_candidate(
        ckpt, next_dir, source_dir, recover_persisted_reset=recover_persisted_reset
    )


def _checkpoint_architecture_policy_identity_errors(ckpt):
    """Delegate to tool_planning_identity_replan."""
    return _tpi._checkpoint_architecture_policy_identity_errors(ckpt)


# IDENTITY_REPLAN_ABANDON_THRESHOLD moved to tool_planning_identity_replan.


def _identity_replan_fingerprint(errors):
    """Delegate to tool_planning_identity_replan."""
    return _tpi._identity_replan_fingerprint(errors)


def _identity_replan_counts(ckpt):
    """Delegate to tool_planning_identity_replan."""
    return _tpi._identity_replan_counts(ckpt)


def _identity_replan_consecutive_count(history, fingerprint):
    """Delegate to tool_planning_identity_replan."""
    return _tpi._identity_replan_consecutive_count(history, fingerprint)


def _record_identity_replan_attempt(ckpt, fingerprint):
    """Delegate to tool_planning_identity_replan."""
    return _tpi._record_identity_replan_attempt(ckpt, fingerprint)



def _checkpoint_runtime_contract_ledger_digest(ckpt):
    """Delegate to tool_planning_identity_replan."""
    return _tpi._checkpoint_runtime_contract_ledger_digest(ckpt)


def _recover_architecture_policy_identity(ckpt, next_dir, source_dir):
    """Delegate to tool_planning_identity_replan."""
    return _tpi._recover_architecture_policy_identity(ckpt, next_dir, source_dir)


def _recover_persisted_architecture_policy_identity_replan(
    ckpt,
    next_dir,
    source_dir,
):
    """Delegate to tool_planning_identity_replan."""
    return _tpi._recover_persisted_architecture_policy_identity_replan(ckpt, next_dir, source_dir)




# ---------------------------------------------------------------------------
# Worker execution and quality/repair contracts (groups E and F) now live in
# tool_planning_worker.py.  Re-exported here so every existing
# ``from tool_planning import X`` and ``tool_planning.X`` reference resolves
# to the same object as before the split.
# ---------------------------------------------------------------------------
from tool_planning_worker import (
    _ARCHITECTURE_CHECK_FILES,
    _ARCHITECTURE_FOCUS_LAYERS,
    _DeferredWorkerActivity,
    _PRECOMMIT_PROTOCOL_EVIDENCE_MARKERS,
    _PRECOMMIT_PROTOCOL_REPAIR_FILES,
    _PRECOMMIT_STRATEGY_REPAIR_FILES,
    _REVERT_FEEDBACK_MARKERS,
    _SCOPE_DRIFT_FEEDBACK_MARKERS,
    _STATE_LEARNING_ORACLE_REFS,
    _apply_mechanical_file_size_trims,
    _architecture_contracts,
    _architecture_default_runtime_contract,
    _architecture_repair_context,
    _architecture_transition_failure_ids,
    _architecture_transition_repair_files,
    _authoritative_rework_tasks,
    _candidate_consumed_precompute_contracts,
    _canonical_tasks_digest,
    _checkpoint_master_plan,
    _checkpoint_master_task_authority_errors,
    _checkpoint_plan_with_tasks,
    _checkpoint_repair_baseline_fingerprint,
    _checkpoint_rework_feedback,
    _checkpoint_work_item,
    _critic_advisory_rework_refusal,
    _declared_scope_violation_files,
    _default_state_learning_contract,
    _detected_artifact_consumer,
    _docstring_line_ranges,
    _durable_checkpoint_contract_matches,
    _durable_output_already_projected,
    _execute_workers_command,
    _expected_worker_backend_contract,
    _extract_quality_failure_files,
    _feedback_quality_contracts,
    _flatten_text_items,
    _format_position_details,
    _frozen_rework_task_authority_errors,
    _generic_quality_contracts,
    _has_legacy_critic_repair_contract,
    _has_scope_drift_marker,
    _int_or_none,
    _is_declared_scope_failure_text,
    _is_file_size_repair_task,
    _is_national_native_contract_failure_text,
    _is_official_rework_checkpoint,
    _is_official_smoke_protocol_failure_text,
    _is_position_semantics_failure_text,
    _is_precommit_rework_checkpoint,
    _is_review_rework_checkpoint,
    _is_runtime_architecture_failure_text,
    _limit_precommit_repair_targets,
    _line_count_contracts,
    _load_worker_prompt_template,
    _mechanical_trim_python_file,
    _mechanically_trim_python_text,
    _merge_runtime_contract_floor,
    _national_native_contracts,
    _normalize_repair_blocker,
    _official_deterministic_failure_items,
    _official_failure_is_protocol,
    _official_failure_items,
    _official_repair_target_files,
    _official_repair_tasks,
    _official_smoke_contracts,
    _order_quality_repair_tasks,
    _plan_repair_scope_files,
    _plan_with_accumulated_repair_scope,
    _position_contracts,
    _precommit_changed_python_files,
    _precommit_failure_items,
    _precommit_filter_repair_targets,
    _precommit_protocol_compliance_failure,
    _precommit_repair_target_files,
    _precommit_repair_task,
    _precommit_repair_task_refresh_reason,
    _precommit_repair_tasks,
    _primary_feedback_file,
    _project_durable_worker_failure,
    _project_durable_worker_output,
    _quality_contract_signature,
    _quality_contract_signatures,
    _quality_contract_task,
    _quality_failure_items,
    _quality_failure_target_files,
    _quality_repair_contracts,
    _quality_rework_skipper,
    _quality_task_contract_refresh_reason,
    _repair_contract_signature,
    _review_feedback_items,
    _review_primary_feedback_text,
    _review_repair_target_files,
    _review_repair_task_refresh_reason,
    _run_durable_worker_effect,
    _scope_drift_feedback_files,
    _should_reset_before_rework,
    _split_reviewer_quality_feedback,
    _stale_quality_task_reason,
    _synthesize_rework_tasks_from_checkpoint,
    _task_declared_scope_files,
    _task_id_suffix,
    _task_matches_quality_blocker,
    _task_must_change_filenames,
    _task_quality_contract_signatures,
    _task_quality_recheck_blockers,
    _task_target_filenames,
    _task_write_scope_errors,
    _text_line_count,
    _tokenized_comment_and_string_lines,
    _transport_equivalent_feedback,
    _worker_availability_resume_receipt_errors,
    _worker_backend_contract,
    _worker_execution_task_digest,
    execute_workers,
)


# Lazy re-export of the two module-level constants that moved into
# ``tool_planning_master_plan_validation``. They are only consumed by the moved
# functions, but this fallback keeps any ``tool_planning._CITATION_RE`` /
# ``tool_planning._TUNER_STRUCTURAL_PATTERNS`` reference resolving without an
# eager read at import time (which would create a circular import when this
# module is imported before the companion).
_MASTER_PLAN_VALIDATION_LAZY_CONSTANTS = (
    "_CITATION_RE",
    "_TUNER_STRUCTURAL_PATTERNS",
)


def __getattr__(name):
    if name in _MASTER_PLAN_VALIDATION_LAZY_CONSTANTS:
        try:
            value = getattr(_mpv, name)
        except AttributeError:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
