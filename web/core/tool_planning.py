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
    return _lp._literature_checkpoint_identity(checkpoint, origin_revision)


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
    return _lp._expected_literature_dispatch(next_v, source_v, weakness, stagnation_info)


def _issue_literature_rendered_prompt(
    *,
    next_v: int,
    source_v: int,
    weakness: str,
    stagnation_info: str,
):

    """Delegate to tool_planning_literature_probe."""
    return _lp._issue_literature_rendered_prompt(next_v, source_v, weakness, stagnation_info)


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
    return _lp._expected_literature_candidate_id(proposal, checkpoint_identity, terminal_output_sha256)


def _literature_translation_receipt(
    proposal: dict | None,
    *,
    next_v: int,
    candidate_id,
    checkpoint_identity: str,
    terminal_output_sha256: str | None,
) -> dict:

    """Delegate to tool_planning_literature_probe."""
    return _lp._literature_translation_receipt(proposal, next_v, candidate_id, checkpoint_identity, terminal_output_sha256)


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
    return _lp._build_literature_probe_payload(payload, checkpoint, receipt_binding, rendered_prompt, terminal_output)


def _literature_probe_payload_errors(
    data: dict | None,
    *,
    checkpoint: dict | None,
    receipt_binding: dict | None,
    require_origin_checkpoint: bool,
) -> list[str]:

    """Delegate to tool_planning_literature_probe."""
    return _lp._literature_probe_payload_errors(data, checkpoint, receipt_binding, require_origin_checkpoint)


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
    return _lp._read_literature_probe_cache(next_v, source_v, h2h_weakness, stagnation_info, receipt_binding, checkpoint)


def _normalize_literature_probe_result(
    data: dict,
    next_v: int | str,
    *,
    checkpoint: dict | None = None,
    receipt_binding: dict | None = None,
    cached: str = "",
) -> dict | None:

    """Delegate to tool_planning_literature_probe."""
    return _lp._normalize_literature_probe_result(data, next_v, checkpoint, receipt_binding, cached)


def _read_literature_probe_checkpoint(
    next_v: int | str,
    *,
    source_v: int | str | None = None,
    h2h_weakness: str = "",
    stagnation_info: str = "",
    receipt_binding: dict | None = None,
) -> dict | None:

    """Delegate to tool_planning_literature_probe."""
    return _lp._read_literature_probe_checkpoint(next_v, source_v, h2h_weakness, stagnation_info, receipt_binding)


def _persist_literature_probe_result(
    next_v: int | str,
    source_v: int | str | None,
    payload: dict,
    *,
    receipt_binding: dict | None = None,
) -> bool:

    """Delegate to tool_planning_literature_probe."""
    return _lp._persist_literature_probe_result(next_v, source_v, payload, receipt_binding)


def _write_literature_probe_cache(
    next_v: int | str,
    payload: dict,
    *,
    checkpoint: dict | None = None,
    receipt_binding: dict | None = None,
) -> dict:

    """Delegate to tool_planning_literature_probe."""
    return _lp._write_literature_probe_cache(next_v, payload, checkpoint, receipt_binding)


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
    """Clear a Master-stuck generation from the tool layer itself.

    The orchestrator is intentionally LLM-driven, so returning a plain text
    "please abandon" directive is not a reliable control plane. All Master
    retry-budget exhaustion paths route here and perform the cleanup directly.
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
        abandon_result = await _do_abandon_generation(
            reason=reason,
            **abandon_identity,
        )
    except Exception as exc:
        abandon_result = {"abandoned": False, "error": str(exc)}
    result = {
        "error": error,
        "fail_count": fail_count,
        **payload,
        **abandon_result,
        "directive": directive or (
            "Master planning exhausted its retry budget and this generation "
            "was abandoned by the tool layer. Start a fresh generation; do not "
            "call run_master again for the abandoned candidate."
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
    """Rewrite parent bot paths in a Master plan to the target bot path.

    Master can inspect the source bot, but worker edit and verification paths
    must point at the prepared target directory. Keep the rewrite path-scoped so
    prose such as "national_v206 is weak vs underbets" remains intact.
    """
    meta = {
        "source_v": source_v,
        "next_v": next_v,
        "replacements": 0,
        "fields": [],
    }
    if not isinstance(plan, (dict, list)) or source_v is None or next_v is None:
        return plan, meta
    try:
        source_i = int(source_v)
        next_i = int(next_v)
    except (TypeError, ValueError):
        return plan, meta
    if source_i == next_i:
        return plan, meta

    source_bot = bot_name(source_i)
    target_bot = bot_name(next_i)
    rel_source = f"bots/{source_bot}"
    rel_target = f"bots/{target_bot}"
    win_source = f"bots\\{source_bot}"
    win_target = f"bots\\{target_bot}"
    abs_source = str(PROJECT_ROOT / "bots" / source_bot)
    abs_target = str(PROJECT_ROOT / "bots" / target_bot)
    abs_win_source = abs_source.replace("/", "\\")
    abs_win_target = abs_target.replace("/", "\\")

    literal_replacements = [
        (rel_source + "/", rel_target + "/"),
        (win_source + "\\", win_target + "\\"),
        (abs_source + "/", abs_target + "/"),
        (abs_win_source + "\\", abs_win_target + "\\"),
    ]
    quoted_dirs = [
        (rel_source, rel_target),
        (win_source, win_target),
        (abs_source, abs_target),
        (abs_win_source, abs_win_target),
    ]

    def replace_text(text):
        changed = 0
        out = text
        for src, dst in literal_replacements:
            n = out.count(src)
            if n:
                out = out.replace(src, dst)
                changed += n
        for src, dst in quoted_dirs:
            pattern = re.compile(rf"(?P<q>['\"]){re.escape(src)}(?P=q)")

            def _quoted(match, replacement=dst):
                return f"{match.group('q')}{replacement}{match.group('q')}"

            out, n = pattern.subn(_quoted, out)
            changed += n

            cd_pattern = re.compile(
                rf"(?P<prefix>\bcd\s+){re.escape(src)}"
                rf"(?P<suffix>\s*(?:&&|;|\||\n|$))"
            )
            out, n = cd_pattern.subn(
                lambda m, replacement=dst: (
                    f"{m.group('prefix')}{replacement}{m.group('suffix')}"
                ),
                out,
            )
            changed += n
        return out, changed

    def walk(value, path):
        if isinstance(value, str):
            new_value, count = replace_text(value)
            if count:
                meta["replacements"] += count
                meta["fields"].append(path)
            return new_value
        if isinstance(value, list):
            return [walk(item, f"{path}[{idx}]") for idx, item in enumerate(value)]
        if isinstance(value, dict):
            return {key: walk(item, f"{path}.{key}") for key, item in value.items()}
        return value

    if isinstance(plan, dict):
        normalized = dict(plan)
        if "tasks" in normalized:
            normalized["tasks"] = walk(normalized["tasks"], "plan.tasks")
    else:
        normalized = walk(plan, "plan")
    if meta["fields"]:
        meta["fields"] = sorted(set(meta["fields"]))
    return normalized, meta


def _normalize_and_log_master_plan_paths(plan, source_v, next_v):
    normalized, meta = _normalize_master_plan_paths(plan, source_v, next_v)
    if meta.get("replacements", 0) > 0:
        try:
            log_system_event(
                "pipeline.master_plan_paths_normalized", "warn",
                f"Normalized {meta['replacements']} parent-path reference(s) "
                f"in Master plan v{next_v}: {bot_relpath(source_v)} -> "
                f"{bot_relpath(next_v)}",
                meta,
            )
        except Exception:
            pass
    return normalized


_TUNER_STRUCTURAL_PATTERNS = [
    "add parameter", "add a parameter", "function signature",
    "add function", "new function", "add method",
    "add class", "new class",
    "add import", "new import",
    "before the clamp", "after the existing",
]


# A4 (evidence_gate, evolution-plan-refresh-jun21): citation patterns the agents use
# to reference spotlight hands. Anchored form (G3H25#9a3f1c02) is preferred but the
# bare form (G3H25) is what fabricated citations usually look like.
_CITATION_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:G\d+H\d+|H\d+)(?:#[0-9a-fA-F]{8})?(?![A-Za-z0-9_])"
)


def _load_replay_anchor_map(next_v=None):
    """Load the generation snapshot citations as ``{base_id: anchor}``.

    Returns:
        dict mapping citation base ID (e.g. "G3H25") to anchor string, or
        ``None`` only for context-free utility calls with no generation id.
        A missing/corrupt generation snapshot returns an empty map so any
        citation fails closed.
    """
    if next_v is None:
        return None
    try:
        from evidence_snapshot import load_generation_evaluation_snapshot

        frozen = load_generation_evaluation_snapshot(int(next_v))
        if not frozen.get("available"):
            return {}
        spotlight = frozen.get("replay_spotlight")
        if not isinstance(spotlight, dict):
            return {}
    except Exception:
        return {}

    anchor_map = {}
    for citation in spotlight.get("citations", []):
        if not isinstance(citation, dict):
            continue
        base = str(citation.get("id") or "")
        anchor = str(citation.get("anchor") or "")
        if base and anchor:
            anchor_map[base] = anchor
    return anchor_map


def _check_citations(text_list, anchor_map):
    """Check text list for fabricated GxHx#anchor citations.

    Args:
        text_list: List of strings to check for citation patterns.
        anchor_map: Manifest of valid anchors.
                   None  = no manifest loaded (skip check, return []).
                   {}    = manifest loaded but empty = ALL citations fabricated.
                   {id: anchor, ...} = normal validation.

    Returns:
        List of error messages for fabricated citations.
    """
    if anchor_map is None:
        return []  # No manifest loaded, skip
    errors = []
    for text in text_list:
        for match in _CITATION_RE.finditer(text):
            ref = match.group(0)
            base = ref.split("#", 1)[0] if "#" in ref else ref
            if base not in anchor_map:
                errors.append(
                    f"FABRICATED_EVIDENCE: '{ref}' is NOT in the spotlight manifest "
                    f"(no such hand exists in recent replays). Only cite hands "
                    f"verbatim from the injected Replay Spotlight section "
                    f"(format: G<game>H<hand>#<anchor>)."
                )
            elif "#" in ref:
                cited_anchor = ref.split("#", 1)[1]
                expected = anchor_map.get(base, "")
                if expected and cited_anchor.lower() != expected.lower():
                    errors.append(
                        f"FABRICATED_EVIDENCE: '{ref}' anchor mismatch "
                        f"(expected #{expected}). Possible hallucination or "
                        f"tampering with a real hand id."
                    )
    return errors


def _sanitize_unverified_replay_citations(text, anchor_map):
    """Remove stale replay hand IDs from Master side context.

    The current replay spotlight is the only authoritative citation source for
    a generation. Direction-audit, match-analysis, research, or other advisory text
    can mention historical GxHy IDs from prior generations; if injected as-is,
    Master tends to repeat them and the evidence gate correctly rejects the
    plan. Keep valid current IDs, fix stale anchors, and redact invalid IDs
    before the text reaches Master.
    """
    if anchor_map is None or not isinstance(text, str) or not text:
        return text, 0

    count = 0

    def repl(match):
        nonlocal count
        ref = match.group(0)
        base = ref.split("#", 1)[0] if "#" in ref else ref
        if base not in anchor_map:
            count += 1
            return "unverified-replay-ref"
        if "#" in ref:
            cited_anchor = ref.split("#", 1)[1]
            expected = anchor_map.get(base, "")
            if expected and cited_anchor.lower() != expected.lower():
                count += 1
                return f"{base}#{expected}"
        return ref

    return _CITATION_RE.sub(repl, text), count


def _verify_cited_replays(plan, *, next_v=None):
    """A4 (evidence_gate): reject Master/Worker replay citations that don't
    correspond to any real replay hand in the spotlight manifest.

    Historical agents invented GxHx IDs that did not exist in the bound replay
    set. The pure spotlight builder now stores every emitted citation directly
    inside the immutable generation evidence snapshot; this function
    cross-checks the plan only against that snapshot.

    Returns a list of BLOCKING error strings. Fabricated evidence must not
    reach Workers.
    """
    anchor_map = _load_replay_anchor_map(next_v)
    tasks = plan if isinstance(plan, list) else (
        plan.get("tasks", []) if isinstance(plan, dict) else []
    )
    texts = []
    for i, task in enumerate(tasks or []):
        if not isinstance(task, dict):
            continue
        texts.append(" ".join([
            str(task.get("worker_prompt", "")),
            str(task.get("instruction", "")),
            str(task.get("targeted_failure", "")),
        ]))
    return _check_citations(texts, anchor_map)


def _validate_master_plan(
    plan,
    next_v=None,
):
    """Validate master plan constraints before dispatching workers.

    Returns (errors, warnings) — only errors block plan storage.
    Boundary warnings are logged but non-blocking; the reviewer/critic
    enforce actual role boundaries during code review.

    """
    errors = []
    warnings = []
    tasks = plan.get("tasks", [])
    if len(tasks) > MASTER_PLAN_MAX_TASKS:
        errors.append(
            f"Too many tasks: {len(tasks)} > {MASTER_PLAN_MAX_TASKS}"
        )
    for i, task in enumerate(tasks):
        targets = task.get("target_files", [])
        files_allowed = task.get("files_allowed", []) or []
        if len(targets) > WORKER_TASK_MAX_TARGET_FILES:
            errors.append(
                f"Task {i}: too many target_files "
                f"({len(targets)} > {WORKER_TASK_MAX_TARGET_FILES})"
            )
        prompt = task.get("worker_prompt", "")
        if len(prompt) > WORKER_PROMPT_MAX_CHARS:
            errors.append(
                f"Task {i}: worker_prompt too long "
                f"({len(prompt)} > {WORKER_PROMPT_MAX_CHARS} chars)"
            )
        layer = str(task.get("skill_layer", "") or "").strip()
        errors.extend(_runtime_contract_errors(task, i, layer))
        declared_rels = {
            (
                _target_rel(item, next_v)
                if next_v is not None
                else Path(str(item)).name
            )
            for item in [*targets, *files_allowed]
            if str(item).strip()
        }
        if declared_rels != _ACTIVE_CANDIDATE_WRITABLE_FILES:
            errors.append(
                f"Task {i}: national_tcp_policy_v1 writable scope must be exactly "
                f"['policy.py']; got {sorted(declared_rels)}. System files, helper "
                "modules, candidate-owned assets, and unbound external assets are not "
                "Worker targets."
            )
        role = str(task.get("role", ""))
        if normalize_worker_role(role) == "tuner":
            # All roles share the sole candidate artifact; Tuner scope is
            # semantic (existing numeric values only), not a separate module.
            tuner_only_files = _ACTIVE_CANDIDATE_WRITABLE_FILES
            declared_files = list(targets) + list(files_allowed)
            non_tuner_files = [t for t in declared_files if Path(str(t)).name not in tuner_only_files]
            if non_tuner_files:
                errors.append(
                    f"Task {i}: Hyperparameter Tuner declares non-policy file(s) {non_tuner_files}; "
                    "all candidate edits must remain in policy.py."
                )
            prompt_lower = prompt.lower()
            # Skip structural keywords that appear in constraint/negative contexts
            _skip_contexts = ("do not", "don't", "must not", "never", "preserve",
                              "keep", "unchanged", "maintain", "no new", "forbidden",
                              "avoid", "except", "aside from", "other than",
                              "should not", "cannot", "do not change", "do not add")
            for kw in _TUNER_STRUCTURAL_PATTERNS:
                # Find the keyword in context — skip if it's in a constraint sentence
                idx = prompt_lower.find(kw)
                if idx >= 0:
                    # Check surrounding context (200 chars before) for negative cues
                    context_before = prompt_lower[max(0, idx - 200):idx]
                    if any(cue in context_before for cue in _skip_contexts):
                        continue
                    # Keyword found in an affirmative (structural) context — warn only
                    warnings.append(
                        f"Task {i} boundary warning: Hyperparameter Tuner prompt contains structural instruction "
                        f"'{kw}' — Tuner should only change numeric constants. "
                        f"The reviewer/critic will enforce this boundary."
                    )
                    break

    # tasks 校验之后：禁止 Master 自行指定 source override 字段。
    # Source ancestor 由系统在 prepare_generation (generation_scheduler._decide_strategy)
    # 决定，Master 不得设置；否则为永不生效的死字段（写 checkpoint 后从不读取）。
    # 注意：本检查必须在 Pydantic (MasterPlan.model_validate, extra='ignore')
    # 剥离 branch_from 之前对原始 dict 调用，否则该键已被丢弃、检查永不命中。
    # 见 _run_master_analysis (agent_master.py) 中 validate_agent_output 之前的
    # 原始 dict 预检。
    source_override_fields = ("branch_from", "source_override", "source_v_override")
    offending = [f for f in source_override_fields if plan.get(f)]
    if offending:
        errors.append(
            f"Master plan must not set source-override field(s) {offending}. "
            f"Source ancestor selection is decided automatically in "
            f"prepare_generation (generation_scheduler._decide_strategy); "
            f"Master must not set branch_from."
        )

    # Check target_files overlap between workers.
    # Architect-Tuner overlap on any file is a hard error (causes boundary false positives).
    # Other overlaps are informational — workers execute sequentially so overlap is safe,
    # but different files make each worker's scope clearer.
    architect_targets = {}
    tuner_targets = {}
    all_targets = {}
    for i, task in enumerate(tasks):
        role = str(task.get("role", ""))
        _role_kind = normalize_worker_role(role)
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v) if next_v else target.strip()
            if _role_kind == "architect":
                architect_targets.setdefault(rel, []).append(i)
            elif _role_kind == "tuner":
                tuner_targets.setdefault(rel, []).append(i)
            if rel in all_targets:
                warnings.append(
                    f"Tasks {all_targets[rel]} and {i} share target_file '{target}'. "
                    f"This is safe (sequential execution) but consider splitting for clarity."
                )
            else:
                all_targets[rel] = i

    # All active tasks necessarily share policy.py. Per-worker snapshots isolate
    # boundary review, and the executor serializes overlapping tasks.
    overlap = set(architect_targets.keys()) & set(tuner_targets.keys())
    if overlap:
        warnings.append(
            f"Architect and Tuner share the sole policy target {sorted(overlap)}; "
            "execute sequentially and audit each worker against its own snapshot."
        )

    # BLOCKING: reject replay hands that do not exist in this generation's
    # digest-bound spotlight payload. A plan built on invented evidence must
    # not reach Workers.
    try:
        errors.extend(_verify_cited_replays(plan, next_v=next_v))
    except Exception:
        pass  # never let the gate itself crash the pipeline

    try:
        from runtime_architecture_policy import validate_plan_architecture_focus
        errors.extend(validate_plan_architecture_focus(plan))
    except Exception as exc:
        if isinstance(plan, dict) and isinstance(plan.get("architecture_policy"), dict):
            errors.append(
                f"Architecture focus validation failed closed: {type(exc).__name__}: {str(exc)[:200]}"
            )

    return errors, warnings


def _runtime_contract_errors(task: dict, index: int, layer: str) -> list[str]:
    """Return hard Master-plan errors for runtime-architecture task contracts."""
    focus_id = str(task.get("architecture_focus_id") or "").strip()
    if not runtime_contract_is_required(layer, focus_id):
        return []

    contract = task.get("runtime_contract")
    if not isinstance(contract, dict):
        return [
            f"Task {index}: runtime_contract is required for skill_layer={layer!r}. "
            "Declare decision, precompute_artifacts, match_memory, and "
            "official_feedback_refs as applicable, and mirror "
            "the concrete work into worker_prompt."
        ]

    try:
        validated = RuntimeContract.model_validate(contract)
    except Exception as exc:
        details: list[str] = []
        if hasattr(exc, "errors"):
            for item in exc.errors()[:8]:
                location = ".".join(str(part) for part in item.get("loc") or [])
                details.append(f"{location}: {item.get('msg')}")
        else:
            details.append(str(exc))
        return [
            f"Task {index}: runtime_contract schema invalid: {'; '.join(details)}"
        ]

    required_sections = runtime_contract_required_sections(layer, focus_id)
    missing = runtime_contract_missing_sections(validated, required_sections)
    if missing:
        return [
            f"Task {index}: runtime_contract for skill_layer={layer!r} is missing "
            f"{', '.join(missing)}"
        ]

    writable_scope = {
        Path(str(item)).name
        for item in [
            *(task.get("target_files") or []),
            *(task.get("files_allowed") or []),
        ]
        if str(item).strip()
    }
    read_only_scope = {
        Path(str(item)).name
        for item in task.get("read_only_dependencies") or []
        if str(item).strip()
    }
    overlap = sorted(writable_scope.intersection(read_only_scope))
    if overlap:
        return [
            f"Task {index}: read_only_dependencies overlap writable "
            f"target_files/files_allowed: {overlap}"
        ]
    owners = []
    if validated.match_memory is not None:
        owners.append(validated.match_memory.owner_file)
    owners.extend(item.owner_file for item in validated.precompute_artifacts)
    invalid_precompute_owners = sorted({
        item.owner_file
        for item in validated.precompute_artifacts
        if item.owner_file != "precompute.py"
    })
    if invalid_precompute_owners:
        return [
            f"Task {index}: precompute artifacts must be existing read-only "
            f"precompute.py objects, got owners {invalid_precompute_owners}."
        ]
    if (
        validated.match_memory is not None
        and validated.match_memory.owner_file != "national_bot.py"
    ):
        return [
            f"Task {index}: match memory is owned by read-only national_bot.py, "
            f"got {validated.match_memory.owner_file!r}."
        ]
    missing_owners = sorted({
        owner
        for owner in owners
        if owner not in writable_scope and owner not in read_only_scope
    })
    if missing_owners:
        return [
            f"Task {index}: runtime_contract owner file(s) {missing_owners} are outside "
            "the declared writable/read-only scope: "
            f"writable={sorted(writable_scope)}, read_only={sorted(read_only_scope)}."
        ]
    # national_bot.py and precompute.py can only be declared read-only; their
    # content failures are system/infrastructure failures, never Worker repairs.

    state_learning = validated.state_learning
    if state_learning is not None:
        missing_checks = sorted(
            set(state_learning.primary_checks()).difference(
                str(item) for item in task.get("checks_required") or []
            )
        )
        if missing_checks:
            return [
                f"Task {index}: state_learning primary innovation "
                f"{state_learning.primary_innovation()!r} requires checks_required "
                f"{missing_checks}."
            ]
        if (
            state_learning.work_primitive == "sample_counted_candidate_batch"
            and validated.decision is None
        ):
            return [
                f"Task {index}: sample_counted_candidate_batch requires a decision contract."
            ]
        if state_learning.work_primitive is not None:
            from strategy_reference_pack import validate_reference_task

            reference_errors = validate_reference_task(
                validated.reference_pack_id,
                state_learning.primary_innovation(),
                target_files=[
                    *(task.get("target_files") or []),
                    *(task.get("files_allowed") or []),
                ],
                worker_prompt=str(task.get("worker_prompt", task.get("instruction", ""))),
            )
            if reference_errors:
                return [f"Task {index}: {error}" for error in reference_errors]

    prompt = str(task.get("worker_prompt", task.get("instruction", ""))).lower()
    contract_terms = runtime_contract_worker_prompt_terms(validated)
    missing_terms = [term for term in contract_terms if term not in prompt]
    if missing_terms:
        return [
            f"Task {index}: runtime_contract is declared but worker_prompt does not "
            f"mention required execution term(s) {missing_terms}. Mirror every contract "
            "boundary into the worker instructions so it reaches the implementation."
        ]
    return []


def _build_generation_architecture_policy(
    source_v: int,
    *,
    prepared_capability_snapshot: dict | None = None,
    prepared_dir: Path | None = None,
    allow_lineage_only_source: bool = False,
) -> dict:
    """Assess and build the system-owned policy for a native source artifact."""

    from workflow_profiles import get_workflow_profile

    profile = get_workflow_profile()
    if getattr(profile, "national_execution_mode", "") != "native_tcp":
        return {"outcome": "skipped", "policy": None, "capabilities": None}
    if allow_lineage_only_source:
        # The only path-free lineage exception is the one-time v142 -> v143
        # empty-pool bootstrap.  Do not establish it by checking whether a
        # historical directory happens to exist: stale local debris must have
        # exactly zero influence.
        from bot_namespace import (
            ARCHIVED_VERSION_HIGH_WATER,
            FIRST_STRICT_POLICY_VERSION,
        )
        from runtime_architecture_policy import (
            build_lineage_only_architecture_policy,
            lineage_only_capabilities,
            validate_prepared_capability_snapshot,
        )

        target_dir = Path(prepared_dir) if prepared_dir is not None else None
        source_identity = bot_name(source_v)
        snapshot_errors = []
        if int(source_v) != int(ARCHIVED_VERSION_HIGH_WATER):
            snapshot_errors.append("lineage_only_source_not_archived_high_water")
        if target_dir is None or target_dir.name != bot_name(
            FIRST_STRICT_POLICY_VERSION
        ):
            snapshot_errors.append("lineage_only_target_not_first_strict")
        if not isinstance(prepared_capability_snapshot, dict):
            snapshot_errors.append("lineage_only_prepared_snapshot_missing")
        else:
            snapshot_errors.extend(validate_prepared_capability_snapshot(
                prepared_capability_snapshot,
                lineage_parent_bot=source_identity,
                prepared_bot_dir=target_dir,
            ))
        if snapshot_errors:
            return {
                "outcome": "infrastructure_failure",
                "policy": None,
                "capabilities": None,
                "infrastructure_failures": [{
                    "component": "fresh_bootstrap_architecture_policy",
                    "failure_class": "internal_infrastructure",
                    "issues": list(dict.fromkeys(snapshot_errors))[:20],
                }],
            }
        try:
            policy = build_lineage_only_architecture_policy(
                source_identity,
                prepared_capability_snapshot=prepared_capability_snapshot,
            )
        except Exception as exc:
            return {
                "outcome": "infrastructure_failure",
                "policy": None,
                "capabilities": None,
                "infrastructure_failures": [{
                    "component": "fresh_bootstrap_architecture_policy",
                    "failure_class": "internal_infrastructure",
                    "issues": [f"{type(exc).__name__}: {str(exc)[:300]}"],
                }],
            }
        return {
            "outcome": "passed",
            "policy": policy,
            "capabilities": lineage_only_capabilities(),
        }

    source_dir = get_bot_dir(source_v)
    if not (source_dir / "national_bot.py").exists():
        return {
            "outcome": "source_invalid",
            "policy": None,
            "capabilities": None,
            "issues": [f"{source_dir.name}/national_bot.py is missing"],
        }
    from national_capability_contract import evaluate_national_capabilities
    from runtime_architecture_policy import build_architecture_policy

    try:
        capabilities = evaluate_national_capabilities(source_dir)
    except Exception as exc:
        return {
            "outcome": "infrastructure_failure",
            "policy": None,
            "capabilities": None,
            "infrastructure_failures": [{
                "component": "national_runtime_probe",
                "failure_class": "internal_infrastructure",
                "issues": [f"{type(exc).__name__}: {str(exc)[:300]}"],
            }],
        }
    infrastructure_failures = capabilities.get("infrastructure_failures") or []
    if capabilities.get("outcome") == "infrastructure_failure" or infrastructure_failures:
        return {
            "outcome": "infrastructure_failure",
            "policy": None,
            "capabilities": capabilities,
            "infrastructure_failures": infrastructure_failures or [{
                "component": "national_runtime_probe",
                "failure_class": "internal_infrastructure",
                "issues": ["source capability probe was inconclusive"],
            }],
        }
    try:
        policy = build_architecture_policy(
            source_dir,
            source_capabilities=capabilities,
            prepared_capability_snapshot=prepared_capability_snapshot,
        )
    except Exception as exc:
        return {
            "outcome": "infrastructure_failure",
            "policy": None,
            "capabilities": capabilities,
            "infrastructure_failures": [{
                "component": "runtime_architecture_policy",
                "failure_class": "internal_infrastructure",
                "issues": [f"{type(exc).__name__}: {str(exc)[:300]}"],
            }],
        }
    return {"outcome": "passed", "policy": policy, "capabilities": capabilities}


def _master_snapshot_binding_errors(checkpoint, next_v):
    """Verify every post-selection Master read uses the selected cutoff."""
    if not isinstance(checkpoint, dict):
        return ["master_checkpoint_missing"]
    audit_context = checkpoint.get("audit_context") or {}
    selection = audit_context.get("selection") or {}
    if selection.get("bootstrap_without_strength_evidence") is True:
        errors = []
        receipt = audit_context.get("protocol_bootstrap")
        try:
            from evolution_infra import get_active_bots
            active_bots = list(get_active_bots())
            if (
                isinstance(receipt, dict)
                and receipt.get("mode") == "fresh_national_policy_bootstrap"
            ):
                from system_strict_bootstrap import validate_fresh_bootstrap_receipt

                errors.extend(validate_fresh_bootstrap_receipt(
                    receipt, active_bots=active_bots
                ))
            else:
                from bot_artifact import canonical_digest

                unsigned = {
                    key: value for key, value in (receipt or {}).items()
                    if key != "receipt_digest"
                }
                if not isinstance(receipt, dict) or receipt.get(
                    "receipt_digest"
                ) != canonical_digest(unsigned):
                    errors.append("policy_bootstrap_receipt_digest_mismatch")
                if sorted((receipt or {}).get("active_bots") or []) != sorted(active_bots):
                    errors.append("policy_bootstrap_active_pool_mismatch")
        except Exception as exc:
            errors.append(
                f"protocol_bootstrap_validation_error:{type(exc).__name__}:"
                f"{str(exc)[:160]}"
            )
        prepare = audit_context.get("protocol_bootstrap_prepare")
        if not isinstance(prepare, dict):
            errors.append("protocol_bootstrap_prepare_receipt_missing")
            return errors
        if not isinstance(receipt, dict) or prepare.get("receipt_digest") != receipt.get(
            "receipt_digest"
        ):
            errors.append("protocol_bootstrap_prepare_receipt_digest_mismatch")
        candidate_dir = get_bot_dir(next_v)
        entry = candidate_dir / "national_bot.py"
        try:
            actual_entry_hash = hashlib.sha256(entry.read_bytes()).hexdigest()
        except OSError as exc:
            errors.append(f"protocol_bootstrap_runtime_unreadable:{type(exc).__name__}")
        else:
            if prepare.get("national_bot_sha256") != actual_entry_hash:
                errors.append("protocol_bootstrap_runtime_hash_mismatch")
        if (
            isinstance(receipt, dict)
            and receipt.get("mode") == "fresh_national_policy_bootstrap"
            and prepare.get("system_runtime_replaced") is not True
        ):
            errors.append("protocol_bootstrap_system_runtime_not_replaced")
        try:
            from bot_namespace import (
                NATIONAL_RUNTIME_MANIFEST,
                POLICY_EPOCH_RECEIPT,
                epoch_receipt_errors,
                runtime_manifest_errors,
            )
            runtime_manifest = json.loads(
                (candidate_dir / NATIONAL_RUNTIME_MANIFEST).read_text(encoding="utf-8")
            )
            epoch_receipt = json.loads(
                (candidate_dir / POLICY_EPOCH_RECEIPT).read_text(encoding="utf-8")
            )
            errors.extend(
                "protocol_bootstrap_candidate_contract:" + item
                for item in [
                    *runtime_manifest_errors(candidate_dir, runtime_manifest),
                    *epoch_receipt_errors(
                        candidate_dir, int(next_v), runtime_manifest, epoch_receipt
                    ),
                ]
            )
        except Exception as exc:
            errors.append(
                f"protocol_bootstrap_candidate_contract_error:{type(exc).__name__}"
            )
        return errors
    formal_binding = bool(
        audit_context.get("master_context") is not None
        or selection.get("evaluation_evidence") is not None
        or selection.get("h2h_snapshot_manifest_digest")
    )
    if not formal_binding:
        # Legacy fixtures/checkpoints have no selected-evidence contract. The
        # strict downstream loader still cannot create a replacement cutoff.
        return []
    try:
        from evidence_snapshot import load_generation_snapshot_identity

        snapshot = load_generation_snapshot_identity(next_v)
    except Exception as exc:
        return [f"generation_snapshot_read_failed:{type(exc).__name__}"]
    if not snapshot.get("available"):
        return [
            "generation_snapshot_unavailable:"
            f"{snapshot.get('reason', 'unknown')}"
        ]
    errors = []
    expected_manifest = str(selection.get("h2h_snapshot_manifest_digest") or "")
    expected_sha = str(selection.get("h2h_snapshot_sha256") or "")
    evidence_cutoffs = (selection.get("evaluation_evidence") or {}).get("cutoffs") or {}
    expected_cycle = str(evidence_cutoffs.get("cycle_manifest_digest") or "")
    if not expected_manifest:
        errors.append("checkpoint_snapshot_manifest_digest_missing")
    elif expected_manifest != str(snapshot.get("manifest_digest") or ""):
        errors.append("checkpoint_snapshot_manifest_digest_mismatch")
    if not expected_sha:
        errors.append("checkpoint_snapshot_h2h_sha256_missing")
    elif expected_sha != str(snapshot.get("sha256") or ""):
        errors.append("checkpoint_snapshot_h2h_sha256_mismatch")
    actual_cycle = str((snapshot.get("cycle") or {}).get("manifest_digest") or "")
    if not expected_cycle:
        errors.append("checkpoint_cycle_manifest_digest_missing")
    elif expected_cycle != actual_cycle:
        errors.append("checkpoint_cycle_manifest_digest_mismatch")
    return errors


@tool("run_master", "Run Master Architect analysis to plan the next generation. Returns a task plan with worker assignments.", {"source_v": int, "next_v": int, "stagnation_info": str, "match_analysis": str, "performance_verification": str, "direction_audit": str, "research_proposals": str})
async def run_master(args):
    _t0 = time.time()
    source_v = args.get("source_v")
    next_v = args.get("next_v")
    if source_v is None or next_v is None:
        _v, source_v = _resolve_version_args(args)
        next_v = next_v or _v
    if source_v is None or next_v is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Missing source_v/next_v and no active checkpoint"})}]}
    # B1 (v125 retry-storm fix): unify next_v with the checkpoint's authoritative
    # value. The Master-failure counter (audit_attempt) is keyed on checkpoint
    # next_v; both the top-of-function circuit-breaker guard (below, ~line 336)
    # and _bump_master_fail_count gate on `checkpoint.next_v == next_v` and
    # SILENTLY zero the count on mismatch. When the orchestrator LLM passes a
    # stale next_v (e.g. from a PreCompact-injected context snapshot), every
    # failure is silently dropped and the breaker never trips — which is exactly
    # how v125 retried 47× without ever hitting MAX_MASTER_TOTAL_FAILURES.
    # Fix: if an ACTIVE checkpoint exists with a different next_v, trust the
    # checkpoint (system-authoritative) and surface the mismatch. A timed-out
    # checkpoint remains authoritative until its canonical abandon transaction
    # completes; only truly inactive archived/abandoned states are ignored.
    try:
        from evolution_infra import read_pipeline_checkpoint
        _entry_ckpt = read_pipeline_checkpoint() or {}
        _entry_next_v = _entry_ckpt.get("next_v")
        _entry_stage = _entry_ckpt.get("stage")
        _dead_stages = (None, "archived", "abandoned")
        if (_entry_next_v is not None and _entry_next_v != next_v
                and _entry_stage not in _dead_stages):
            _log.warning(
                "run_master: LLM passed next_v=%s but active checkpoint is "
                "next_v=%s (stage=%s) — aligning to checkpoint to keep the "
                "Master-failure counter consistent (v125 bypass fix).",
                next_v, _entry_next_v, _entry_stage,
            )
            try:
                log_system_event(
                    "pipeline.master_next_v_mismatch", "warn",
                    f"run_master next_v={next_v} aligned to checkpoint next_v={_entry_next_v} "
                    f"(stage={_entry_stage}) — LLM passed a stale version",
                    {"args_next_v": next_v, "ckpt_next_v": _entry_next_v,
                     "source_v": source_v, "stage": _entry_stage},
                )
            except Exception:
                pass
            next_v = _entry_next_v
            if _entry_ckpt.get("source_v") is not None:
                source_v = _entry_ckpt["source_v"]
    except Exception:
        pass
    _master_entry_ckpt = _matching_checkpoint(next_v, source_v)
    protocol_bootstrap_receipt = (
        (_master_entry_ckpt.get("audit_context") or {}).get("protocol_bootstrap")
        if isinstance(_master_entry_ckpt, dict)
        else None
    )
    protocol_bootstrap_no_strength = isinstance(protocol_bootstrap_receipt, dict)
    _master_infra, _master_infra_error = _owned_infrastructure_failure(
        _master_entry_ckpt,
        "run_master",
    )
    if _master_infra_error:
        return _state_blocked(
            _master_infra_error,
            next_v,
            source_v,
            _master_entry_ckpt,
        )
    _master_exhausted = await _execute_exhausted_infrastructure_failure(
        next_v,
        source_v,
        owner_tool="run_master",
    )
    if _master_exhausted is not None:
        return _json_tool_result(_master_exhausted)
    _persisted_identity_replan = (
        (_master_entry_ckpt.get("audit_context") or {}).get(
            "architecture_policy_identity_replan"
        )
        if isinstance(_master_entry_ckpt, dict)
        else None
    )
    if isinstance(_persisted_identity_replan, dict):
        try:
            persisted_identity_recovery = (
                _recover_persisted_architecture_policy_identity_replan(
                    _master_entry_ckpt,
                    get_bot_dir(next_v),
                    get_bot_dir(source_v),
                )
            )
        except Exception as exc:
            log_system_event(
                "pipeline.architecture_policy_identity_replan_recovery_failed",
                "error",
                (
                    f"Could not recover persisted identity replan for v{next_v}: "
                    f"{type(exc).__name__}: {str(exc)[:240]}"
                ),
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": _master_entry_ckpt.get("stage"),
                },
            )
            return _json_tool_result({
                "error": "ARCHITECTURE_POLICY_IDENTITY_REPLAN_RECOVERY_FAILED",
                "failure_class": "control_plane",
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                "directive": (
                    "The journaled identity migration did not close. Retry "
                    "run_master to recover the same content-CAS operation; do "
                    "not edit the checkpoint or candidate directory by hand."
                ),
            })
        if persisted_identity_recovery is not None:
            return persisted_identity_recovery
    if (
        isinstance(_master_entry_ckpt, dict)
        and _master_entry_ckpt.get("stage") == "direction_audited"
    ):
        from prepared_baseline_contract import validate_prepared_artifact_contract

        prepared_artifact_contract = (
            (_master_entry_ckpt.get("audit_context") or {}).get(
                "prepared_artifact_contract"
            )
        )
        prepared_artifact_errors = validate_prepared_artifact_contract(
            prepared_artifact_contract,
            prepared_dir=get_bot_dir(next_v),
            source_v=source_v,
            next_v=next_v,
            verify_live_content=True,
        )
        if prepared_artifact_errors:
            log_system_event(
                "pipeline.master_prepared_artifact_drift",
                "error",
                f"Master refused drifted prepared artifact v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "errors": prepared_artifact_errors,
                },
            )
            return _json_tool_result({
                "error": "PREPARED_ARTIFACT_CONTRACT_INVALID",
                "next_v": next_v,
                "source_v": source_v,
                "validation_errors": prepared_artifact_errors,
                "next_tool": "abandon_generation",
                "directive": (
                    "The candidate changed after prepare/crossover and before Master. "
                    "Abandon and rebuild from a fresh scheduler-owned baseline."
                ),
            })
    # fix-4: idempotency guard — if master already planned for this (next_v, source_v),
    # return cached result instead of re-running (LLM intermittently violates
    # orchestrator.md:43, causing duplicate run_master calls in the same cycle).
    _ckpt_idempotent = _matching_checkpoint(next_v, source_v)
    if _ckpt_idempotent and _ckpt_idempotent.get("stage") in (
        "master_planned", "workers_done", "quality_failed", "quality_passed",
        "reviewed", "critic_checked", "verified", "archived",
    ):
        _existing_plan = _ckpt_idempotent.get("master_plan")
        if _existing_plan:
            if (_ckpt_idempotent.get("parent2_v")
                    and isinstance(_existing_plan, dict)
                    and _existing_plan.get("strategy") == "crossover"):
                log_system_event(
                    "pipeline.crossover_master_call_blocked", "warn",
                    f"run_master called after crossover already produced v{next_v}; proceed to quality/retry workers, not Master",
                    {"next_v": next_v, "source_v": source_v,
                     "parent2_v": _ckpt_idempotent.get("parent2_v"),
                     "stage": _ckpt_idempotent.get("stage"),
                     "has_synthetic_plan": True},
                )
                return _json_tool_result({
                    "error": "CROSSOVER_ALREADY_DONE",
                    "next_v": next_v,
                    "source_v": source_v,
                    "parent2_v": _ckpt_idempotent.get("parent2_v"),
                    "stage": _ckpt_idempotent.get("stage"),
                    "directive": (
                        "Crossover already produced the target bot. Do NOT call run_master. "
                        "If stage=workers_done call run_quality_gates; if stage=quality_failed "
                        "call execute_workers with exact gate feedback or abandon_generation."
                    ),
                })
            log_system_event("pipeline.master_idempotent", "info",
                             f"run_master for v{next_v}: plan already exists "
                             f"(stage={_ckpt_idempotent.get('stage')}), returning cached",
                             {"next_v": next_v, "source_v": source_v})
            ui = _get_ui()
            ui.log_history("Master plan already exists — returning cached (idempotent).", "info")
            return _json_tool_result({"plan": _existing_plan, "logs": ui.get_output(),
                                      "idempotent_cache": True})
        if _ckpt_idempotent.get("parent2_v") and _ckpt_idempotent.get("stage") in (
            "workers_done", "quality_failed", "quality_passed",
            "reviewed", "critic_checked", "verified", "archived",
        ):
            log_system_event(
                "pipeline.crossover_master_call_blocked", "warn",
                f"run_master called after crossover already produced v{next_v}; proceed to quality/retry workers, not Master",
                {"next_v": next_v, "source_v": source_v,
                 "parent2_v": _ckpt_idempotent.get("parent2_v"),
                 "stage": _ckpt_idempotent.get("stage")},
            )
            return _json_tool_result({
                "error": "CROSSOVER_ALREADY_DONE",
                "next_v": next_v,
                "source_v": source_v,
                "parent2_v": _ckpt_idempotent.get("parent2_v"),
                "stage": _ckpt_idempotent.get("stage"),
                "directive": (
                    "Crossover already produced the target bot. Do NOT call run_master. "
                    "If stage=workers_done call run_quality_gates; if stage=quality_failed "
                    "call execute_workers with the exact quality failure feedback or abandon_generation."
                ),
            })

    stagnation_info = args.get("stagnation_info", "No stagnation detected. Continue from latest version.")
    match_analysis = args.get("match_analysis", "")
    performance_verification = args.get("performance_verification", "")
    direction_audit_str = args.get("direction_audit", "")
    research_proposals = args.get("research_proposals", "")

    # The scheduler owns the evidence bundle.  The outer orchestrator sees the
    # same text but is not a trusted serializer for it: on v146 it converted a
    # missing-literal validation error into a contradictory instruction to omit
    # the valid reference-card id.  Prefer the digest-bound checkpoint copy and
    # make caller mismatches observable.
    _checkpoint_master_context = (
        ((_master_entry_ckpt.get("audit_context") or {}).get("master_context"))
        if isinstance(_master_entry_ckpt, dict)
        else None
    )
    if _checkpoint_master_context is not None:
        from master_context_contract import validate_master_context

        _context_errors = validate_master_context(
            _checkpoint_master_context,
            next_v=next_v,
            source_v=source_v,
        )
        if _context_errors:
            log_system_event(
                "pipeline.master_context_invalid",
                "error",
                f"Master context contract invalid for v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "errors": _context_errors,
                },
            )
            return _json_tool_result({
                "error": "MASTER_CONTEXT_CONTRACT_INVALID",
                "next_v": next_v,
                "source_v": source_v,
                "validation_errors": _context_errors,
                "directive": (
                    "Do not run Master with caller-reconstructed evidence. Repair or "
                    "restart the scheduler-owned master context checkpoint first."
                ),
            })
        _incoming_context = {
            "stagnation_info": str(stagnation_info or ""),
            "match_analysis": str(match_analysis or ""),
            "performance_verification": str(performance_verification or ""),
        }
        _context_mismatches = [
            field
            for field, supplied in _incoming_context.items()
            if supplied and supplied != _checkpoint_master_context[field]
        ]
        stagnation_info = _checkpoint_master_context["stagnation_info"]
        match_analysis = _checkpoint_master_context["match_analysis"]
        performance_verification = _checkpoint_master_context["performance_verification"]
        if _context_mismatches:
            log_system_event(
                "pipeline.master_context_caller_mismatch",
                "warn",
                f"Ignored caller-reconstructed Master context for v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "mismatched_fields": _context_mismatches,
                    "context_digest": _checkpoint_master_context.get("context_digest"),
                },
            )

    _snapshot_binding_errors = _master_snapshot_binding_errors(
        _master_entry_ckpt,
        next_v,
    )
    if _snapshot_binding_errors:
        log_system_event(
            "pipeline.master_snapshot_binding_invalid",
            "error",
            f"Master v{next_v} blocked by generation evidence drift",
            {
                "next_v": next_v,
                "source_v": source_v,
                "errors": _snapshot_binding_errors,
            },
        )
        return _json_tool_result({
            "error": "GENERATION_EVIDENCE_BINDING_INVALID",
            "next_v": next_v,
            "source_v": source_v,
            "validation_errors": _snapshot_binding_errors,
            "next_tool": "abandon_generation",
            "directive": (
                "The selected generation snapshot is missing or no longer matches "
                "its checkpoint. Do not recreate a cutoff or run Master; abandon "
                "and re-prepare from a fresh coherent evaluation cycle."
            ),
        })

    # Literature-probe output is checkpoint-owned and producer-receipt bound.
    # Caller text and legacy four-field receipts have no injection authority.
    from pipeline_state import (
        literature_probe_receipt_binding,
        literature_probe_required,
    )

    _literature_required = literature_probe_required(_master_entry_ckpt)
    _literature_binding = None
    _literature_binding_errors = []
    if _literature_required:
        _literature_binding, _literature_binding_errors = (
            literature_probe_receipt_binding(_master_entry_ckpt)
        )
    _validated_literature_probe = None
    if isinstance(_master_entry_ckpt, dict):
        _probe = _master_entry_ckpt.get("literature_probe")
        if _probe is not None:
            _validated_literature_probe = _normalize_literature_probe_result(
                _probe,
                next_v,
                checkpoint=_master_entry_ckpt,
                receipt_binding=_literature_binding,
            )
            canonical_research = (
                str(_validated_literature_probe.get("inject_text") or "")
                if isinstance(_validated_literature_probe, dict)
                else ""
            )
            if research_proposals and research_proposals != canonical_research:
                log_system_event(
                    "pipeline.master_research_caller_mismatch",
                    "warn",
                    f"Ignored caller-reconstructed research proposal for v{next_v}",
                    {"next_v": next_v, "source_v": source_v},
                )
            research_proposals = canonical_research
        elif _checkpoint_master_context is not None:
            # New scheduler-owned checkpoints can distinguish "no probe" from
            # a lost caller argument.  Legacy checkpoints without this marker
            # retain their old caller-proposal fallback for crash compatibility.
            if research_proposals:
                log_system_event(
                    "pipeline.master_research_without_checkpoint_probe",
                    "warn",
                    f"Ignored research proposal without checkpoint probe for v{next_v}",
                    {"next_v": next_v, "source_v": source_v},
                )
            research_proposals = ""

    if protocol_bootstrap_no_strength:
        # Existing literature receipts were designed around an H2H weakness.
        # Until a separately typed non-result literature receipt exists, omit
        # them rather than laundering match conclusions through research prose.
        stagnation_info = _PROTOCOL_BOOTSTRAP_NO_STRENGTH
        match_analysis = _PROTOCOL_BOOTSTRAP_NO_STRENGTH
        performance_verification = _PROTOCOL_BOOTSTRAP_NO_STRENGTH
        research_proposals = ""

    # A terminal attempt is satisfied only by the exact schema-v2 producer
    # receipt. The route helper's legacy four-field compatibility cannot grant
    # Master prompt authority.
    if _literature_required and _validated_literature_probe is None:
        _probe_present = isinstance(
            (_master_entry_ckpt or {}).get("literature_probe"), dict
        )
        log_system_event(
            "pipeline.master_blocked_invalid_literature_probe"
            if _probe_present
            else "pipeline.master_blocked_missing_literature_probe",
            "error",
            f"Master v{next_v} blocked: mandatory literature probe receipt is "
            + ("invalid" if _probe_present else "missing"),
            {
                "next_v": next_v,
                "source_v": source_v,
                "stage": (_master_entry_ckpt or {}).get("stage"),
                "binding_errors": _literature_binding_errors,
            },
        )
        return _json_tool_result({
            "error": (
                "LITERATURE_PROBE_RECEIPT_INVALID"
                if _probe_present
                else "LITERATURE_PROBE_REQUIRED"
            ),
            "next_v": next_v,
            "source_v": source_v,
            "next_tool": (
                "abandon_generation" if _probe_present else "run_literature_probe"
            ),
            "validation_errors": _literature_binding_errors,
            "directive": (
                "The mandatory literature stage requires an exact schema-v2 "
                "checkpoint/dispatch/output/translation-gate producer receipt. "
                + (
                    "Use governed abandon/reprepare; never rewrite an existing receipt."
                    if _probe_present
                    else "Call run_literature_probe before run_master."
                )
            ),
        })

    prepared_baseline = None
    prepared_capability_snapshot = None
    if isinstance(_master_entry_ckpt, dict) and _master_entry_ckpt.get("parent2_v") is not None:
        prepared_baseline = (
            (_master_entry_ckpt.get("audit_context") or {}).get(
                "prepared_baseline_contract"
            )
        )
        from prepared_baseline_contract import validate_prepared_baseline_contract

        baseline_errors = validate_prepared_baseline_contract(
            prepared_baseline,
            parent_a_dir=get_bot_dir(source_v),
            parent_b_dir=get_bot_dir(_master_entry_ckpt.get("parent2_v")),
            prepared_dir=get_bot_dir(next_v),
            source_v=source_v,
            parent2_v=_master_entry_ckpt.get("parent2_v"),
            next_v=next_v,
            verify_live_content=True,
        )
        try:
            from evidence_snapshot import load_generation_snapshot_identity

            live_h2h_identity = load_generation_snapshot_identity(next_v)
            bound_h2h_identity = (
                prepared_baseline.get("h2h_snapshot_identity")
                if isinstance(prepared_baseline, dict)
                else {}
            ) or {}
            if not live_h2h_identity.get("available"):
                baseline_errors.append(
                    "prepared_baseline_h2h_snapshot_unavailable:"
                    f"{live_h2h_identity.get('reason', 'unknown')}"
                )
            for field in ("manifest_digest", "sha256"):
                expected_value = str(bound_h2h_identity.get(field) or "")
                if expected_value and expected_value != str(
                    live_h2h_identity.get(field) or ""
                ):
                    baseline_errors.append(
                        f"prepared_baseline_h2h_{field}_mismatch"
                    )
        except Exception as exc:
            baseline_errors.append(
                "prepared_baseline_h2h_snapshot_error:"
                f"{type(exc).__name__}:{str(exc)[:200]}"
            )
        if baseline_errors:
            log_system_event(
                "pipeline.master_prepared_baseline_invalid",
                "error",
                f"Master v{next_v} blocked by invalid prepared crossover baseline",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "parent2_v": _master_entry_ckpt.get("parent2_v"),
                    "errors": baseline_errors[:20],
                },
            )
            return _json_tool_result({
                "error": "CROSSOVER_PREPARED_BASELINE_INVALID",
                "next_v": next_v,
                "source_v": source_v,
                "parent2_v": _master_entry_ckpt.get("parent2_v"),
                "validation_errors": baseline_errors,
                "next_tool": "abandon_generation",
                "directive": (
                    "The digest-bound prepared crossover child no longer matches "
                    "its checkpoint contract. Do not reconstruct it from Parent A or "
                    "run Workers; abandon and rerun crossover from a fresh baseline."
                ),
            })
        prepared_capability_snapshot = prepared_baseline.get(
            "capability_snapshot"
        )

    fresh_empty_pool_bootstrap = _is_fresh_empty_pool_bootstrap(
        _master_entry_ckpt
    )
    architecture_source_dir = None
    if protocol_bootstrap_no_strength:
        try:
            if fresh_empty_pool_bootstrap:
                from runtime_architecture_policy import (
                    build_lineage_only_prepared_capability_snapshot,
                )

                prepared_capability_snapshot = (
                    build_lineage_only_prepared_capability_snapshot(
                        bot_name(source_v),
                        get_bot_dir(next_v),
                    )
                )
            else:
                from runtime_architecture_policy import (
                    build_prepared_capability_snapshot,
                )

                architecture_source_dir = get_bot_dir(source_v)
                prepared_capability_snapshot = build_prepared_capability_snapshot(
                    architecture_source_dir,
                    get_bot_dir(next_v),
                )
            architecture_assessment = _build_generation_architecture_policy(
                source_v,
                prepared_capability_snapshot=prepared_capability_snapshot,
                prepared_dir=get_bot_dir(next_v),
                allow_lineage_only_source=fresh_empty_pool_bootstrap,
            )
        except Exception as exc:
            architecture_assessment = {
                "outcome": "infrastructure_failure",
                "policy": None,
                "capabilities": None,
                "infrastructure_failures": [{
                    "component": "protocol_bootstrap_capability_snapshot",
                    "failure_class": "internal_infrastructure",
                    "issues": [f"{type(exc).__name__}: {str(exc)[:300]}"],
                }],
            }
    elif prepared_capability_snapshot is None:
        # Preserve the legacy/single-parent call shape for test and plugin
        # adapters that replace this helper with a one-argument provider.
        architecture_assessment = _build_generation_architecture_policy(source_v)
    else:
        architecture_assessment = _build_generation_architecture_policy(
            source_v,
            prepared_capability_snapshot=prepared_capability_snapshot,
        )
    if architecture_assessment.get("outcome") == "infrastructure_failure":
        from national_runtime_probe import RUNTIME_PROBE_IDENTITY_DIGEST
        from pipeline_infrastructure import infrastructure_attempt_key

        source_fingerprint = _master_source_fingerprint(
            _master_entry_ckpt,
            source_v,
        )
        failures = architecture_assessment.get("infrastructure_failures") or []
        infra_component = str(
            failures[0].get("component")
            if failures and isinstance(failures[0], dict)
            else "national_runtime_probe"
        )
        issues = [
            f"{item.get('component', 'national_runtime_probe')}: "
            + ", ".join(str(issue) for issue in (item.get("issues") or [])[:8])
            for item in failures
            if isinstance(item, dict)
        ] or ["source national runtime capability probe was inconclusive"]
        attempt_key = infrastructure_attempt_key(
            component=infra_component,
            source_fingerprint=source_fingerprint,
            harness_identity=RUNTIME_PROBE_IDENTITY_DIGEST,
            extra={"source_v": source_v, "next_v": next_v, "phase": "master_policy"},
        )
        infra_result = await _record_infrastructure_failure(
            next_v,
            source_v,
            owner_tool="run_master",
            resume_stage="direction_audited",
            component=infra_component,
            code=f"{infra_component}_infrastructure_failure",
            attempt_key=attempt_key,
            issues=issues,
            max_attempts=3,
            metadata={
                "source_fingerprint": source_fingerprint,
                "runtime_probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
                "phase": "master_policy",
            },
        )
        attempt = (infra_result.get("infra_failure") or {}).get("attempt")
        log_system_event(
            "pipeline.architecture_policy_infrastructure",
            "error" if infra_result.get("action") == "abandon_generation" else "warn",
            f"Source runtime probe unavailable for v{next_v} policy (attempt {attempt or '?'}/3)",
            {
                "source_v": source_v,
                "next_v": next_v,
                "issues": issues,
                **infra_result,
            },
        )
        return _json_tool_result({
            **infra_result,
            "error": "ARCHITECTURE_POLICY_INFRASTRUCTURE",
            "source_v": source_v,
            "next_v": next_v,
            "directive": (
                "Source capability infrastructure retry exhausted; generation was safely abandoned."
                if infra_result.get("action") == "abandon_generation"
                else "Retry run_master for the same generation; do not execute workers."
            ),
        })
    if architecture_assessment.get("outcome") == "source_invalid":
        ui = _get_ui()
        return await _abandon_master_generation(
            next_v,
            source_v,
            error="ARCHITECTURE_POLICY_SOURCE_INVALID",
            fail_count=0,
            reason=f"architecture_source_invalid v{source_v}",
            event_type="pipeline.architecture_policy_source_invalid",
            event_message=(
                f"Native architecture source v{source_v} is invalid; abandoning v{next_v}"
            ),
            ui=ui,
            payload={"issues": architecture_assessment.get("issues") or []},
            directive=(
                "The selected native source lacks the required national entry. The generation "
                "was abandoned; repair source eligibility instead of running workers."
            ),
        )
    architecture_policy = architecture_assessment.get("policy")
    if prepared_baseline is not None:
        stored_prepared_policy = (
            ((_master_entry_ckpt.get("audit_context") or {}).get("crossover") or {})
            .get("prepared_architecture_policy")
        )
        if not isinstance(stored_prepared_policy, dict) or (
            stored_prepared_policy.get("policy_digest")
            != (architecture_policy or {}).get("policy_digest")
        ):
            return _json_tool_result({
                "error": "CROSSOVER_PREPARED_POLICY_IDENTITY_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "parent2_v": _master_entry_ckpt.get("parent2_v"),
                "stored_policy_digest": (
                    (stored_prepared_policy or {}).get("policy_digest")
                    if isinstance(stored_prepared_policy, dict)
                    else ""
                ),
                "current_policy_digest": (
                    (architecture_policy or {}).get("policy_digest")
                ),
                "next_tool": "abandon_generation",
                "directive": (
                    "The prepared child policy no longer matches the current "
                    "system contract. Fail closed and rerun crossover; never reset "
                    "the child to Parent A while retaining two-parent lineage."
                ),
            })
    if (
        _master_infra is not None
        and _master_infra.get("component")
        not in {"master_llm", "master_plan_audit_llm"}
    ):
        from pipeline_infrastructure import infrastructure_failure_digest

        cleared = write_pipeline_checkpoint(
            next_v,
            source_v,
            "direction_audited",
            clear_infra_failure=True,
            infra_failure_owner="run_master",
            expected_infra_failure_digest=infrastructure_failure_digest(_master_infra),
            touch_stage_timestamp=True,
        )
        if not cleared:
            return _state_blocked(
                "source runtime probe recovered but its infrastructure overlay could not be cleared",
                next_v,
                source_v,
                _matching_checkpoint(next_v, source_v),
            )

    _set_pipeline_status(f"Master planning for v{next_v}")
    _touch_master_checkpoint(next_v, source_v, phase="run_master_start")

    # Hard cap: refuse to re-burn Master LLM budget if it has already failed
    # (plan-JSON collapse or audit rejection) MAX_MASTER_TOTAL_FAILURES times
    # this generation. See MAX_MASTER_TOTAL_FAILURES docstring.
    try:
        from evolution_infra import read_pipeline_checkpoint
        _ckpt_m = read_pipeline_checkpoint() or {}
        _master_fails = int(_ckpt_m.get("audit_attempt") or 0) if _ckpt_m.get("next_v") == next_v else 0
    except Exception:
        _master_fails = 0
    if _master_fails >= MAX_MASTER_TOTAL_FAILURES:
        _ui = _get_ui()
        return await _abandon_master_generation(
            next_v,
            source_v,
            error="MASTER_EXHAUSTED",
            fail_count=_master_fails,
            reason=f"master_exhausted ({_master_fails} fails)",
            event_type="pipeline.master_exhausted",
            event_message=(
                f"Master exhausted {_master_fails} attempts for v{next_v} — "
                "refusing retry and abandoning"
            ),
            ui=_ui,
            directive=(
                f"Master planning failed {_master_fails} times for v{next_v}. "
                "This generation has been abandoned (checkpoint cleared, incomplete "
                "dir removed, session cleared). The next cycle must start fresh."
            ),
        )

    # Direction audit is persisted by its owning tool. The caller copy cannot
    # override the checkpoint verdict or rewrite mandatory directions.
    direction_audit = None
    _checkpoint_direction_audit = (
        _master_entry_ckpt.get("direction_audit")
        if isinstance(_master_entry_ckpt, dict)
        else None
    )
    _caller_direction_audit = None
    if direction_audit_str:
        try:
            _caller_direction_audit = (
                json.loads(direction_audit_str)
                if isinstance(direction_audit_str, str)
                else direction_audit_str
            )
        except (json.JSONDecodeError, TypeError):
            pass
    if isinstance(_checkpoint_direction_audit, dict):
        direction_audit = deepcopy(_checkpoint_direction_audit)
        if (
            isinstance(_caller_direction_audit, dict)
            and _caller_direction_audit != _checkpoint_direction_audit
        ):
            log_system_event(
                "pipeline.direction_audit_caller_mismatch",
                "warn",
                f"Ignored caller-reconstructed direction audit for v{next_v}",
                {"next_v": next_v, "source_v": source_v},
            )
    elif isinstance(_caller_direction_audit, dict):
        direction_audit = _caller_direction_audit

    # Inject only the current, checkpoint-bound Direction audit constraint when
    # it found repetition.
    if protocol_bootstrap_no_strength:
        # Direction Audit remains a durable stage receipt, but its historical
        # match/critic conclusions are not evidence for the first strict plan.
        direction_audit = None
    elif direction_audit and direction_audit.get("llm_failed"):
        _log.warning(
            "Direction audit for v%s reported LLM infrastructure failure — "
            "skipping audit mandatory_constraints injection (untrustworthy).",
            next_v,
        )
        try:
            log_system_event(
                "pipeline.direction_audit_infra", "warn",
                f"Direction audit for v{next_v} unavailable (LLM infra error). "
                "Skipping audit constraints.",
                {"next_v": next_v, "source_v": source_v},
            )
        except Exception:
            pass
    elif direction_audit and direction_audit.get("repetition_detected") and direction_audit.get("mandatory_constraints"):
        constraint_block = (
            f"\n\n# Direction Audit Constraints (MANDATORY)\n"
            f"The Direction Auditor detected that recent generations are stuck repeating the same approach.\n"
            f"**DO NOT repeat these exhausted directions:** {', '.join(direction_audit.get('exhausted_directions', []))}\n"
            f"**Mandatory constraint:** {direction_audit['mandatory_constraints']}\n"
        )
        if direction_audit.get("suggested_direction"):
            constraint_block += f"**Suggested alternative:** {direction_audit['suggested_direction']}\n"
        constraint_block += "\nYou MUST comply with these constraints. A plan that repeats an exhausted direction will be rejected.\n"
        performance_verification = (performance_verification or "") + constraint_block

    ui = _get_ui()

    # --- Extract replay_spotlight for Master prompt ---
    replay_spotlight = _PROTOCOL_BOOTSTRAP_NO_STRENGTH
    if not protocol_bootstrap_no_strength:
        replay_spotlight = ""
        try:
            from evidence_snapshot import load_generation_evaluation_snapshot

            _replay_evidence = load_generation_evaluation_snapshot(next_v)
            if not _replay_evidence.get("available"):
                raise RuntimeError("generation replay evidence unavailable")
            _spotlight_payload = _replay_evidence.get("replay_spotlight")
            if not isinstance(_spotlight_payload, dict):
                raise RuntimeError("generation replay spotlight payload missing")
            if _spotlight_payload.get("bot") != bot_name(source_v):
                raise RuntimeError("generation replay spotlight bot mismatch")
            if _spotlight_payload.get("evaluation_identity_digest") != (
                (_replay_evidence.get("manifest") or {}).get(
                    "evaluation_identity_digest"
                )
            ):
                raise RuntimeError("generation replay spotlight identity mismatch")
            replay_spotlight = str(_spotlight_payload.get("text") or "")
        except Exception:
            pass

    # The Replay Spotlight below is authoritative for current-generation hand
    # IDs. Side contexts can carry historical GxHy references from old audits,
    # research proposals, or match summaries; redact those before Master sees
    # them so the hard fabricated-evidence gate can remain strict.
    _anchor_map = (
        None
        if protocol_bootstrap_no_strength
        else _load_replay_anchor_map(next_v)
    )
    _citation_sanitized = {}
    for _name, _value in (
        ("stagnation_info", stagnation_info),
        ("match_analysis", match_analysis),
        ("performance_verification", performance_verification),
        ("research_proposals", research_proposals),
    ):
        _clean, _count = _sanitize_unverified_replay_citations(_value, _anchor_map)
        if _count:
            _citation_sanitized[_name] = _count
        if _name == "stagnation_info":
            stagnation_info = _clean
        elif _name == "match_analysis":
            match_analysis = _clean
        elif _name == "performance_verification":
            performance_verification = _clean
        elif _name == "research_proposals":
            research_proposals = _clean
    if _citation_sanitized:
        try:
            log_system_event(
                "pipeline.master_context_citations_sanitized",
                "warn",
                f"Master v{next_v} context had stale replay IDs redacted",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "counts": _citation_sanitized,
                },
            )
        except Exception:
            pass

    # --- Read frozen action diagnostics for Master prompt ---
    _master_evaluation = {}
    if not protocol_bootstrap_no_strength:
        try:
            from evidence_snapshot import load_generation_evaluation_snapshot

            _master_evaluation = load_generation_evaluation_snapshot(next_v)
            if not _master_evaluation.get("available"):
                _master_evaluation = {}
        except Exception:
            _master_evaluation = {}
    bot_action_stats = (
        _PROTOCOL_BOOTSTRAP_NO_STRENGTH
        if protocol_bootstrap_no_strength
        else ""
    )
    try:
        _all_stats = _master_evaluation.get("action_stats") or {}
        if _all_stats:
            _source_bot_name = bot_name(source_v)
            _bot_stats = _all_stats.get(_source_bot_name)
            if _bot_stats:
                # Format as compact text for prompt injection
                _parts = []
                _native_trackers = _bot_stats.get("opponent_trackers") or {}
                for _street in ("preflop", "flop", "turn", "river"):
                    if _native_trackers:
                        _semantic = {}
                        for _tracker in _native_trackers.values():
                            _counts = (
                                (_tracker.get("semantic_street_actions") or {}).get(
                                    _street
                                )
                                or {}
                            )
                            for _action, _count in _counts.items():
                                _semantic[_action] = _semantic.get(_action, 0) + int(
                                    _count or 0
                                )
                        _total = sum(_semantic.values())
                        if _total > 0:
                            _parts.append(
                                f"{_street}: fold={_semantic.get('fold', 0)/_total:.1%} "
                                f"match_call={_semantic.get('call', 0)/_total:.1%} "
                                f"check={_semantic.get('check', 0)/_total:.1%} "
                                f"pass={_semantic.get('pass', 0)/_total:.1%} "
                                f"raise={_semantic.get('raise', 0)/_total:.1%} "
                                f"allin={_semantic.get('allin', 0)/_total:.1%} "
                                f"(semantic_n={_total})"
                            )
                        continue
                    _st = _bot_stats.get(_street)
                    if _st and _st.get("total", 0) > 0:
                        _total = _st["total"]
                        _parts.append(
                            f"{_street}: fold={_st.get('fold', 0)/_total:.1%} "
                            f"call={_st.get('call', 0)/_total:.1%} "
                            f"raise={_st.get('raise', 0)/_total:.1%} "
                            f"(n={_total})"
                        )
                if _parts:
                    bot_action_stats = (
                        f"Action frequencies for {_source_bot_name}:\n"
                        + "\n".join(_parts)
                    )
    except Exception:
        pass

    # --- Phase 3: per-opponent behavior profiles for Master prompt ---
    # Reads the nested per-opponent breakdown (bot_action_stats_per_opp.json,
    # written by elo_daemon alongside the flat file). For the source bot we
    # surface its most lopsided matchups (by h2h win_rate) with a compact
    # aggression / fold-to-bet / cbet / barrel line each, so the Master can
    # plan opponent-specific adaptations. Advisory: read failure -> "".
    opponent_profiles = (
        _PROTOCOL_BOOTSTRAP_NO_STRENGTH
        if protocol_bootstrap_no_strength
        else ""
    )
    try:
        _per_opp_all = _master_evaluation.get("action_stats_per_opp") or {}
        if _per_opp_all:
            _source_bot = bot_name(source_v)
            _opp_map = {}
            # Rank opponents by h2h win_rate (most-beaten and most-beating) to
            # avoid prompt bloat: keep the K most extreme matchups. Strength
            # ordering comes only from the strict generation snapshot; the live
            # top-level H2H file may have advanced since source selection.
            _h2h_for_rank = {}
            try:
                from tool_helpers import _h2h_stats

                _evaluation = _master_evaluation
                if not _evaluation.get("available"):
                    raise RuntimeError("generation evidence snapshot unavailable")
                _h2h = _evaluation.get("h2h") or {}
                _active_opponents = set(
                    (_evaluation.get("selection") or {}).get("active_bots") or []
                )
                _opp_map = {
                    _opp: ((_per_opp_all.get(_opp) or {}).get(_source_bot) or {})
                    for _opp in _active_opponents
                    if _opp != _source_bot
                    and isinstance((_per_opp_all.get(_opp) or {}).get(_source_bot), dict)
                }
                for _opp in _opp_map:
                    _st = _h2h_stats(_source_bot, _opp, _h2h)
                    if _st:
                        _h2h_for_rank[_opp] = _st
            except Exception:
                # No frozen active-pool/H2H authority means no profile ranking;
                # do not silently fall back to live or inactive opponents.
                _opp_map = {}
            _PROFILES_K = 6
            _action_sample = lambda _opp: sum(
                int(_opp_map[_opp].get(_street, {}).get("total", 0) or 0)
                for _street in ("preflop", "flop", "turn", "river")
            )
            _adequate = {
                _opp: _stats
                for _opp, _stats in _h2h_for_rank.items()
                if int(_stats.get("games", 0) or 0) >= 10
            }
            if _adequate:
                _ranked = sorted(
                    _adequate.items(), key=lambda kv: kv[1]["win_rate"]
                )
                _selected = [o for o, _ in _ranked[:_PROFILES_K // 2]]
                _selected += [o for o, _ in _ranked[-(_PROFILES_K // 2):]]
                # Dedup while preserving order.
                _seen = set()
                _selected = [o for o in _selected if not (o in _seen or _seen.add(o))]
                if len(_selected) < _PROFILES_K:
                    _selected.extend(
                        _opp
                        for _opp in sorted(
                            (_opp for _opp in _opp_map if _opp not in _seen),
                            key=_action_sample,
                            reverse=True,
                        )[:_PROFILES_K - len(_selected)]
                    )
            else:
                # No adequate H2H signal: use observation volume, but every H2H
                # label below remains explicitly sparse/advisory.
                _selected = sorted(
                    _opp_map,
                    key=_action_sample,
                    reverse=True,
                )[:_PROFILES_K]
            _lines = []
            for _opp in _selected:
                _ostats = _opp_map.get(_opp, {})
                _matchup = _h2h_for_rank.get(_opp)
                _wr_str = " h2h=unobserved"
                if _matchup is not None:
                    _games = int(_matchup.get("games", 0) or 0)
                    _wr = float(_matchup.get("win_rate", 0.5) or 0.5)
                    _sample_class = (
                        "confirmed_weakness"
                        if _games >= 10 and _wr < 0.40
                        else "confirmed_strength"
                        if _games >= 10 and _wr > 0.60
                        else "adequate_context"
                        if _games >= 10
                        else "sparse_advisory"
                    )
                    _wr_str = (
                        f" h2h_games={_games} wins={int(_matchup.get('wins', 0) or 0)} "
                        f"losses={int(_matchup.get('losses', 0) or 0)} "
                        f"draws={int(_matchup.get('draws', 0) or 0)} "
                        f"h2h_wr={_wr:.2f} sample_class={_sample_class}"
                    )
                _n = (
                    sum(_ostats.get(s, {}).get("total", 0)
                        for s in ("preflop", "flop", "turn", "river"))
                )
                if _n == 0:
                    continue
                _street_bits = []
                _tracker = _ostats.get("opponent_tracker") or {}
                _is_native_tracker = (
                    _tracker.get("source")
                    == "national_native_opponent_tracker"
                )
                for _street in ("preflop", "flop", "turn", "river"):
                    _st = _ostats.get(_street, {})
                    _tot = _st.get("total", 0)
                    if _tot <= 0:
                        continue
                    _calls = _st.get("call", 0)
                    _raises = _st.get("raise", 0)
                    _folds = _st.get("fold", 0)
                    _ftb = _st.get("fold_to_bet", 0)
                    _cbet = _st.get("cbet", 0)
                    _barrel = _st.get("barrel", 0)
                    _af = (_raises / _calls) if _calls > 0 else None
                    _af_str = f"{_af:.1f}" if _af is not None else "n/a"
                    if _is_native_tracker:
                        _semantic = (
                            (_tracker.get("semantic_street_actions") or {}).get(
                                _street
                            )
                            or {}
                        )
                        _semantic_total = sum(
                            int(value or 0) for value in _semantic.values()
                        )
                        if _semantic_total <= 0:
                            continue
                        _street_bits.append(
                            f"{_street}: fold={int(_semantic.get('fold', 0) or 0)/_semantic_total:.1%} "
                            f"match_call={int(_semantic.get('call', 0) or 0)/_semantic_total:.1%} "
                            f"check={int(_semantic.get('check', 0) or 0)/_semantic_total:.1%} "
                            f"pass={int(_semantic.get('pass', 0) or 0)/_semantic_total:.1%} "
                            f"raise={int(_semantic.get('raise', 0) or 0)/_semantic_total:.1%} "
                            f"allin={int(_semantic.get('allin', 0) or 0)/_semantic_total:.1%} "
                            f"(semantic_n={_semantic_total})"
                        )
                    else:
                        _street_bits.append(
                            f"{_street}: AF={_af_str} "
                            f"ftb={_ftb}/{_folds}f cbet={_cbet} "
                            f"barrel={_barrel} (n={_tot})"
                        )
                _terminal = _tracker.get("terminal_response") or {}
                if _terminal:
                    _ftr = _terminal.get("fold_to_raise")
                    _ftj = _terminal.get("fold_to_jam")
                    _roc = _terminal.get("river_overcall")
                    _raise_n = int(
                        (_terminal.get("facing_raise") or {}).get(
                            "opportunities", 0
                        )
                        or 0
                    )
                    _jam_n = int(
                        (_terminal.get("facing_allin") or {}).get(
                            "opportunities", 0
                        )
                        or 0
                    )
                    _street_bits.append(
                        "terminal: "
                        f"fold_to_raise={_ftr:.1%} (n={_raise_n}) "
                        if isinstance(_ftr, (int, float))
                        else f"terminal: fold_to_raise=unknown (n={_raise_n}) "
                    )
                    _street_bits[-1] += (
                        f"fold_to_jam={_ftj:.1%} (n={_jam_n}) "
                        if isinstance(_ftj, (int, float))
                        else f"fold_to_jam=unknown (n={_jam_n}) "
                    )
                    _street_bits[-1] += (
                        f"river_overcall={_roc:.1%} "
                        f"(n={int(_terminal.get('river_overcall_samples', 0) or 0)})"
                        if isinstance(_roc, (int, float))
                        else "river_overcall=unknown"
                    )
                _showdown = _tracker.get("showdown_range") or {}
                if int(_showdown.get("samples", 0) or 0) > 0:
                    _street_bits.append(
                        "showdown_range: "
                        f"samples={int(_showdown.get('samples', 0) or 0)} "
                        f"buckets={json.dumps(_showdown.get('bucket_counts') or {}, sort_keys=True)}"
                    )
                if _street_bits:
                    _lines.append(
                        f"vs {_opp}{_wr_str} (n={_n}): " + "; ".join(_street_bits)
                    )
            if _lines:
                opponent_profiles = (
                    f"Opponent behavior observed against {_source_bot} "
                    f"(adequate frozen H2H extremes first; sparse rows are advisory):\n"
                    + "\n".join(_lines)
                )
    except Exception:
        pass

    _system_bootstrap_master = False
    _system_bootstrap_master_receipt = None
    from system_strict_bootstrap import is_declared_native_bootstrap

    if is_declared_native_bootstrap(_master_entry_ckpt):
        from system_strict_bootstrap import (
            validate_bootstrap_checkpoint,
        )

        _system_errors = validate_bootstrap_checkpoint(
            _master_entry_ckpt,
            architecture_policy=architecture_policy,
            candidate_dir=get_bot_dir(next_v),
            require_direction_audit=True,
        )
        if _system_errors:
            return await _abandon_master_generation(
                next_v,
                source_v,
                error="SYSTEM_STRICT_BOOTSTRAP_AUTHORITY_INVALID",
                fail_count=0,
                reason=(
                    "system_strict_bootstrap_authority_invalid:"
                    + ";".join(_system_errors[:8])
                ),
                event_type="pipeline.system_strict_bootstrap_invalid",
                event_message=(
                    f"System strict bootstrap authority drifted for v{next_v}; "
                    "abandoning without falling back to an LLM"
                ),
                ui=ui,
                payload={"validation_errors": _system_errors},
                directive=(
                    "The exact first-migration receipt, prepared artifact, checked-in "
                    "blueprint, or strict-pool state changed. The generation was "
                    "abandoned; repair the control-plane contract and re-prepare."
                ),
            )
        _system_bootstrap_master = True
        log_system_event(
            "pipeline.system_strict_bootstrap_authority_verified",
            "info",
            f"Verified deterministic Worker authority before Master for v{next_v}",
            {
                "next_v": next_v,
                "source_v": source_v,
                "executor": "system_policy_bootstrap_v1",
                "master_governance": "three_proposals_two_anonymous_ballots",
            },
        )

    # The deterministic package owns implementation bytes only.  Governance is
    # unchanged: Master must still produce three independent proposals, collect
    # two anonymous criterion ballots, and publish one schema-valid plan.
    try:
        data = await _run_master_analysis(
            source_v, next_v, stagnation_info, ui,
            match_analysis=match_analysis,
            performance_verification=performance_verification,
            replay_spotlight=replay_spotlight,
            bot_action_stats=bot_action_stats,
            opponent_profiles=opponent_profiles,
            research_proposals=research_proposals,
            architecture_policy=architecture_policy,
            prepared_baseline=prepared_baseline,
            **(
                {"protocol_bootstrap": protocol_bootstrap_receipt}
                if isinstance(protocol_bootstrap_receipt, dict)
                else {}
            ),
        )
    except LLMAvailabilityBlocked:
        # Availability pauses are attempt-neutral and must park at Direction.
        _clear_master_runtime_heartbeat(next_v, source_v)
        raise
    except Exception as exc:
        from agent_master import (
            MasterAuthorityError,
            MasterEnsembleInfrastructureParked,
            MasterInfrastructureError,
        )
        from strict_authority_workflow import StrictAuthorityError

        if isinstance(exc, StrictAuthorityError):
            return await _abandon_strict_master_authority(
                next_v,
                source_v,
                error=exc,
                ui=ui,
            )
        if isinstance(exc, MasterAuthorityError):
            return await _block_master_authority(
                next_v,
                source_v,
                error=exc,
                ui=ui,
            )
        if isinstance(exc, MasterEnsembleInfrastructureParked):
            return _handle_master_ensemble_provider_parked(
                next_v,
                source_v,
                ui,
                exc,
            )
        if not isinstance(exc, MasterInfrastructureError):
            raise
        return await _handle_master_llm_infrastructure(
            next_v,
            source_v,
            ui,
            component="master_llm",
            issue=exc.issue,
            prompt_digest=exc.prompt_digest,
        )

    if data is None:
        return await _handle_master_analysis_failure(
            next_v,
            source_v,
            ui,
            message="Master failed to produce a valid plan after retries or LLM failure",
            reason=f"master_analysis_failed v{next_v}",
        )
    if architecture_policy is not None:
        data["architecture_policy"] = architecture_policy

    async def _compile_and_hard_validate_master_plan(plan, *, phase: str):
        """Normalize, compile, and hard-validate a Master plan before LLM audit."""
        plan = _normalize_and_log_master_plan_paths(plan, source_v, next_v)
        compiler_errors = []
        try:
            from plan_compiler import compile_master_plan
            plan, _compile_meta = compile_master_plan(
                plan,
                next_v=next_v,
                target_dir=get_bot_dir(next_v),
                project_root=PROJECT_ROOT,
            )
            if _compile_meta.get("compiled"):
                log_system_event(
                    "pipeline.master_plan_compaction_forbidden",
                    "error",
                    f"Master plan v{next_v}: rejected unexpected Worker-prompt compaction",
                    {"next_v": next_v, "source_v": source_v, "phase": phase, "compiler": _compile_meta},
                )
                # A Master plan accepted through the selected-proposal binding
                # must remain losslessly inline.  Replacing it with a generated
                # task brief changes the authority/replay shape; reject it here
                # before a checkpoint, Worker lease, or bootstrap receipt.
                compiler_errors.append(
                    "master_plan_worker_prompt_externalization_forbidden"
                )
            _contract_binding = _compile_meta.get("contract_binding") or {}
            if any(_contract_binding.get(key) for key in (
                "invalid_contract_tasks",
                "invalid_prompt_tasks",
                "overflow_tasks",
            )):
                compiler_errors.append(
                    "master_plan_system_contract_binding_invalid:"
                    + json.dumps(
                        _contract_binding,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )[:2000]
                )
        except Exception as _compile_exc:
            compiler_errors.append(
                "master_plan_compile_failed:"
                f"{type(_compile_exc).__name__}:{str(_compile_exc)[:500]}"
            )
            log_system_event(
                "pipeline.master_plan_compile_failed",
                "error",
                f"Master plan compiler failed for v{next_v}: {_compile_exc}",
                {"next_v": next_v, "source_v": source_v, "phase": phase, "error": str(_compile_exc)[:500]},
            )

        plan_errors, plan_warnings = _validate_master_plan(plan, next_v=next_v)
        plan_errors = list(dict.fromkeys([*compiler_errors, *plan_errors]))
        if plan_warnings:
            try:
                log_system_event(
                    "pipeline.master_boundary",
                    "warning",
                    f"Master plan boundary warnings for v{next_v}: {plan_warnings}",
                    {"next_v": next_v, "source_v": source_v, "phase": phase, "warnings": plan_warnings},
                )
            except Exception:
                pass
        if not plan_errors:
            from runtime_architecture_policy import attach_runtime_contract_ledger

            return attach_runtime_contract_ledger(plan, replace=True), None

        _validation_ctx = {
            "master_validation": {
                "phase": phase,
                "errors": plan_errors,
                "warnings": plan_warnings,
                "plan_analysis": plan.get("analysis", "")[:1000]
                if isinstance(plan, dict) else "",
            }
        }
        _nf = _bump_master_fail_count(
            next_v,
            source_v,
            audit_context=_validation_ctx,
        )
        _severity = "error" if _nf >= MAX_MASTER_TOTAL_FAILURES else "warn"
        try:
            log_system_event(
                "pipeline.master_validation_failed",
                _severity,
                f"Master plan validation failed before audit for v{next_v} "
                f"(fail_count={_nf}): {'; '.join(plan_errors[:3])}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "phase": phase,
                    "fail_count": _nf,
                    "validation_errors": plan_errors,
                    "validation_warnings": plan_warnings,
                },
            )
        except Exception:
            pass
        try:
            ui.log_history(
                "Master plan validation failed before audit: " + "; ".join(plan_errors[:5]),
                "error",
            )
        except Exception:
            pass

        if _nf >= MAX_MASTER_TOTAL_FAILURES:
            return plan, await _abandon_master_generation(
                next_v,
                source_v,
                error="MASTER_VALIDATION_EXHAUSTED",
                fail_count=_nf,
                reason=(
                    f"master_validation_failed v{next_v}: "
                    f"{'; '.join(plan_errors[:3])[:300]}"
                ),
                event_type="pipeline.master_validation_exhausted_abandon",
                event_message=(
                    f"Master plan validation failed {_nf} times for v{next_v} — "
                    "abandoning invalid generation"
                ),
                ui=ui,
                payload={
                    "validation_errors": plan_errors,
                    "validation_warnings": plan_warnings,
                },
                directive=(
                    "Master plan validation failed too many times and this "
                    "generation was abandoned. Start a fresh generation; do "
                    "not execute workers from the invalid plan."
                ),
            )

        return plan, _json_tool_result({
            "error": "MASTER_VALIDATION_FAILED",
            "fail_count": _nf,
            "validation_errors": plan_errors,
            "validation_warnings": plan_warnings,
            "invalid_plan_preview": {
                "analysis": str(plan.get("analysis", ""))[:1000]
                if isinstance(plan, dict) else "",
                "tasks": [
                    {
                        "worker_id": task.get("worker_id"),
                        "role": task.get("role"),
                        "target_files": task.get("target_files", []),
                        "worker_prompt_chars": len(str(task.get("worker_prompt", ""))),
                    }
                    for task in (plan.get("tasks", []) if isinstance(plan, dict) else [])[:3]
                    if isinstance(task, dict)
                ],
            },
            "directive": (
                "The Master plan failed hard validation before LLM audit. "
                "Do NOT execute workers from this plan. If retrying Master, "
                "the next plan must explicitly fix these validation_errors; "
                "after repeated failures the generation will be abandoned."
            ),
            "logs": ui.get_output(),
        })

    data, _early_validation_result = await _compile_and_hard_validate_master_plan(
        data, phase="master_plan_ready"
    )
    if _early_validation_result is not None:
        return _early_validation_result
    if _system_bootstrap_master:
        from system_strict_bootstrap import (
            SystemStrictBootstrapError,
            build_master_receipt,
        )

        try:
            _system_bootstrap_master_receipt = build_master_receipt(
                _matching_checkpoint(next_v, source_v) or _master_entry_ckpt,
                data,
                architecture_policy=architecture_policy,
                candidate_dir=get_bot_dir(next_v),
            )
        except SystemStrictBootstrapError as exc:
            return await _abandon_master_generation(
                next_v,
                source_v,
                error="SYSTEM_STRICT_BOOTSTRAP_MASTER_RECEIPT_INVALID",
                fail_count=0,
                reason="system_strict_bootstrap_master_receipt_invalid:" + str(exc)[:300],
                event_type="pipeline.system_strict_bootstrap_receipt_invalid",
                event_message=(
                    f"System strict bootstrap receipt failed closed for v{next_v}"
                ),
                ui=ui,
                payload={"validation_errors": list(exc.errors)},
                directive=(
                    "The compiled plan or its authority chain drifted. The generation "
                    "was abandoned without invoking an LLM fallback."
                ),
            )
        except Exception as exc:
            return await _abandon_master_generation(
                next_v,
                source_v,
                error="SYSTEM_STRICT_BOOTSTRAP_MASTER_RECEIPT_ERROR",
                fail_count=0,
                reason=(
                    "system_strict_bootstrap_master_receipt_error:"
                    f"{type(exc).__name__}:{str(exc)[:260]}"
                ),
                event_type="pipeline.system_strict_bootstrap_receipt_error",
                event_message=(
                    f"System strict bootstrap receipt errored for v{next_v}"
                ),
                ui=ui,
                payload={"exception_type": type(exc).__name__},
                directive=(
                    "The system receipt controller failed unexpectedly. The "
                    "generation was abandoned; never fall back to an LLM Worker."
                ),
            )
    _touch_master_checkpoint(next_v, source_v, phase="master_plan_ready")

    # --- P0-1: Post-Master Plan Verification Audit ---
    # Capped retry loop: on audit rejection, re-plan AND re-audit only while the
    # unified Master budget still allows it. The audit_attempt counter is
    # persisted in the checkpoint so a crash-resume does not re-burn the budget.
    master_audit_ctx = None
    try:
        from evolution_infra import read_pipeline_checkpoint
        _ckpt0 = read_pipeline_checkpoint() or {}
        # `or 0` defends against a stored null: prepare_next_gen writes the
        # checkpoint with audit_attempt=None (default), and across the next_v
        # change the merge guard fails so it serializes as JSON null. A bare
        # .get("audit_attempt", 0) returns the stored None (not the default),
        # and int(None) raises TypeError that the surrounding try/except would
        # swallow — silently disabling the audit on every normal generation.
        _audit_attempt = int(_ckpt0.get("audit_attempt") or 0)

        for _audit_iter in range(MAX_MASTER_AUDIT_RETRIES + 1):
            _touch_master_checkpoint(
                next_v,
                source_v,
                phase="master_plan_audit_start",
                audit_attempt=_audit_attempt,
            )
            if protocol_bootstrap_no_strength:
                _h2h_citation_errors = []
                _h2h_repair_guidance = ""
                audit_result = _protocol_bootstrap_master_audit(data)
            else:
                try:
                    from evidence_snapshot import (
                        h2h_citation_repair_guidance,
                        validate_h2h_citations_against_snapshot,
                    )
                    _h2h_citation_errors = validate_h2h_citations_against_snapshot(data, next_v)
                    _h2h_repair_guidance = h2h_citation_repair_guidance(
                        next_v,
                        _h2h_citation_errors,
                        source_v=source_v,
                    )
                except Exception:
                    _h2h_citation_errors = []
                    _h2h_repair_guidance = ""
            if not protocol_bootstrap_no_strength and _h2h_citation_errors:
                audit_result = {
                    "plan_coherent": False,
                    "contradiction_found": True,
                    "contradictions": _h2h_citation_errors[:10],
                    "evidence_alignment": "misaligned",
                    "direction_novelty": "incremental",
                    "overall_pass": False,
                    "feedback": _h2h_citation_audit_feedback(
                        next_v,
                        _h2h_citation_errors,
                        _h2h_repair_guidance,
                    ),
                    "retry_recommended": True,
                    "deterministic_h2h_snapshot_check": True,
                    "repair_guidance": _h2h_repair_guidance,
                }
            elif not protocol_bootstrap_no_strength:
                from audit_agents import _run_master_plan_audit

                try:
                    audit_result = await _run_master_plan_audit(data, source_v, ui, next_v=next_v)
                except TypeError as _audit_te:
                    if "next_v" not in str(_audit_te) and "keyword" not in str(_audit_te):
                        raise
                    audit_result = await _run_master_plan_audit(data, source_v, ui)
            master_audit_ctx = audit_result  # Save for audit_context chain
            if (
                not isinstance(audit_result, dict)
                or audit_result.get("llm_failed")
                or audit_result.get("parse_failed")
            ):
                issue = (
                    str((audit_result or {}).get("error") or "master plan audit output unavailable")
                    if isinstance(audit_result, dict)
                    else f"master_plan_audit_not_object:{type(audit_result).__name__}"
                )
                return await _handle_master_llm_infrastructure(
                    next_v,
                    source_v,
                    ui,
                    component="master_plan_audit_llm",
                    issue=issue,
                    prompt_digest=hashlib.sha256(
                        json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                )
            if audit_result.get("overall_pass", True):
                break  # plan passed audit
            # Rejected
            log_system_event("pipeline.master_audit_rejected", "warn",
                             f"Master plan audit rejected for v{next_v} (attempt {_audit_attempt + 1}): {audit_result.get('feedback', '')[:200]}",
                             {"next_v": next_v, "audit": audit_result, "audit_attempt": _audit_attempt + 1})
            if _audit_attempt + 1 > MAX_MASTER_AUDIT_RETRIES:
                _nf = _bump_master_fail_count(next_v, source_v, value=_audit_attempt + 1)
                return await _abandon_master_generation(
                    next_v,
                    source_v,
                    error="MASTER_AUDIT_REJECTED",
                    fail_count=_nf,
                    reason=f"master_audit_rejected v{next_v}: {audit_result.get('feedback', '')[:300]}",
                    event_type="pipeline.master_audit_exhausted_abandon",
                    event_message=(
                        f"Master audit exhausted {MAX_MASTER_AUDIT_RETRIES} retries "
                        f"for v{next_v} — blocking plan and abandoning"
                    ),
                    ui=ui,
                    payload={"audit": audit_result},
                    directive=(
                        "Master plan audit is blocking. This generation was abandoned "
                        "after the corrective re-plan budget was exhausted. Start a "
                        "fresh generation; do not execute workers from the rejected plan."
                    ),
                )
            # Re-plan with rejection feedback, then re-audit the new plan
            _audit_attempt += 1
            _rejection_ckpt = read_pipeline_checkpoint() or {}
            _rejection_written = write_pipeline_checkpoint(
                next_v,
                source_v,
                _rejection_ckpt.get("stage") or "direction_audited",
                audit_attempt=_audit_attempt,
                audit_context={"master_audit_rejection": master_audit_ctx},
                touch_stage_timestamp=True,
                expected_checkpoint_revision=int(
                    _rejection_ckpt.get("checkpoint_revision") or 0
                ),
                expected_checkpoint_stage=str(
                    _rejection_ckpt.get("stage") or "direction_audited"
                ),
                expected_workflow_run_id=str(
                    _rejection_ckpt.get("workflow_run_id") or ""
                ),
            )
            if not _rejection_written:
                return _state_blocked(
                    "master audit rejection checkpoint CAS was refused",
                    next_v,
                    source_v,
                    _matching_checkpoint(next_v, source_v),
                )
            _touch_master_checkpoint(
                next_v,
                source_v,
                phase="master_audit_rejected",
                audit_attempt=_audit_attempt,
                audit_context={"master_audit_rejection": master_audit_ctx},
            )
            log_system_event("pipeline.master_audit_blocked", "error",
                             f"Master plan audit blocked v{next_v}; retrying Master attempt {_audit_attempt}",
                             {"next_v": next_v, "source_v": source_v,
                              "audit_attempt": _audit_attempt, "audit": audit_result})
            performance_verification += (
                f"\n\n# PLAN AUDIT REJECTION (attempt {_audit_attempt})\n"
                f"The previous plan was rejected by the Plan Verification Auditor.\n"
                f"Issues: {audit_result.get('feedback', '')}\n"
                f"Contradictions: {', '.join(audit_result.get('contradictions', []))}\n"
                f"Direction assessment: {audit_result.get('direction_novelty', 'unknown')}\n"
                f"You MUST address these issues in your new plan.\n"
            )
            performance_verification, _retry_sanitized = _sanitize_unverified_replay_citations(
                performance_verification, _anchor_map
            )
            if _retry_sanitized:
                try:
                    log_system_event(
                        "pipeline.master_retry_context_citations_sanitized",
                        "warn",
                        f"Master retry v{next_v} context had stale replay IDs redacted",
                        {
                            "next_v": next_v,
                            "source_v": source_v,
                            "count": _retry_sanitized,
                        },
                    )
                except Exception:
                    pass
            try:
                data = await _run_master_analysis(
                    source_v, next_v, stagnation_info, ui,
                    match_analysis=match_analysis,
                    performance_verification=performance_verification,
                    replay_spotlight=replay_spotlight,
                    bot_action_stats=bot_action_stats,
                    opponent_profiles=opponent_profiles,
                    research_proposals=research_proposals,
                    architecture_policy=architecture_policy,
                    prepared_baseline=prepared_baseline,
                    **(
                        {"protocol_bootstrap": protocol_bootstrap_receipt}
                        if isinstance(protocol_bootstrap_receipt, dict)
                        else {}
                    ),
                )
            except LLMAvailabilityBlocked:
                _clear_master_runtime_heartbeat(next_v, source_v)
                raise
            except Exception as exc:
                from agent_master import (
                    MasterAuthorityError,
                    MasterEnsembleInfrastructureParked,
                    MasterInfrastructureError,
                )
                from strict_authority_workflow import StrictAuthorityError

                if isinstance(exc, StrictAuthorityError):
                    return await _abandon_strict_master_authority(
                        next_v,
                        source_v,
                        error=exc,
                        ui=ui,
                    )
                if isinstance(exc, MasterAuthorityError):
                    return await _block_master_authority(
                        next_v,
                        source_v,
                        error=exc,
                        ui=ui,
                    )
                if isinstance(exc, MasterEnsembleInfrastructureParked):
                    return _handle_master_ensemble_provider_parked(
                        next_v,
                        source_v,
                        ui,
                        exc,
                    )
                if not isinstance(exc, MasterInfrastructureError):
                    raise
                return await _handle_master_llm_infrastructure(
                    next_v,
                    source_v,
                    ui,
                    component="master_llm",
                    issue=exc.issue,
                    prompt_digest=exc.prompt_digest,
                )
            if data is None:
                return await _handle_master_analysis_failure(
                    next_v,
                    source_v,
                    ui,
                    message="Master failed after audit retry",
                    reason=f"master_analysis_failed_after_audit_retry v{next_v}",
                    payload={"audit_attempt": _audit_attempt},
                )
            if architecture_policy is not None:
                data["architecture_policy"] = architecture_policy
            data, _early_validation_result = await _compile_and_hard_validate_master_plan(
                data, phase="master_retry_plan_ready"
            )
            if _early_validation_result is not None:
                return _early_validation_result
            _touch_master_checkpoint(
                next_v,
                source_v,
                phase="master_retry_plan_ready",
                audit_attempt=_audit_attempt,
            )
            log_system_event("pipeline.master_audit_retry", "info",
                             f"Master re-planned after audit rejection for v{next_v} (attempt {_audit_attempt})",
                             {"next_v": next_v})
    except LLMAvailabilityBlocked:
        _clear_master_runtime_heartbeat(next_v, source_v)
        raise
    except Exception as e:
        _log.warning("Master plan audit infrastructure error: %s", e)
        try:
            log_system_event('pipeline.master_audit_error', 'warn',
                f'Master plan audit error for v{next_v}: {e}',
                {"next_v": next_v, "source_v": source_v, "error": str(e)})
        except Exception:
            pass
        return await _handle_master_llm_infrastructure(
            next_v,
            source_v,
            ui,
            component="master_plan_audit_llm",
            issue=f"{type(e).__name__}: {str(e)[:400]}",
            prompt_digest=hashlib.sha256(
                json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        )

    # A singleton no-strength successor now uses the same six-slot durable
    # Master journal as the fresh bootstrap. Before projecting master_planned,
    # re-open all three Scouts, both Ballots, final Master and all five bound
    # invocation logs, then replay the production compiler. A sealed final role
    # alone is insufficient authority if any sibling receipt/log was lost or
    # changed across a crash.
    if protocol_bootstrap_no_strength and not _system_bootstrap_master:
        from agent_master import MasterAuthorityError
        from strict_authority_workflow import (
            StrictAuthorityError,
            validate_master_final_projection,
        )

        _projection_checkpoint = (
            _matching_checkpoint(next_v, source_v) or _master_entry_ckpt
        )
        try:
            _projection_proof, _projection_errors = (
                validate_master_final_projection(
                    _projection_checkpoint,
                    data,
                    candidate_dir=get_bot_dir(next_v),
                    project_root=PROJECT_ROOT,
                    require_no_other_accepted=True,
                )
            )
        except StrictAuthorityError as exc:
            _projection_errors = list(exc.errors)
        except Exception as exc:
            _projection_errors = [
                "singleton_master_projection_unavailable:"
                f"{type(exc).__name__}:{str(exc)[:240]}"
            ]
        if _projection_errors:
            return await _block_master_authority(
                next_v,
                source_v,
                error=MasterAuthorityError(
                    source_v,
                    next_v,
                    hashlib.sha256(
                        json.dumps(
                            {
                                "plan": data,
                                "errors": _projection_errors,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    _projection_errors,
                ),
                ui=ui,
            )

    # Persist master plan to checkpoint so it survives crashes between master and workers
    _ckpt = _matching_checkpoint(next_v, source_v)
    existing_audit = _ckpt.get("direction_audit") if _ckpt else direction_audit
    # Mark direction_audit as resolved now that Master has produced a plan
    if existing_audit and existing_audit.get("repetition_detected"):
        existing_audit["resolved"] = True
    checkpoint_kwargs = {}
    current_master_infra = (_ckpt or {}).get("infra_failure")
    if isinstance(current_master_infra, dict):
        from pipeline_infrastructure import infrastructure_failure_digest

        checkpoint_kwargs = {
            "clear_infra_failure": True,
            "infra_failure_owner": "run_master",
            "expected_infra_failure_digest": infrastructure_failure_digest(current_master_infra),
        }
    recorded = write_pipeline_checkpoint(
        next_v,
        source_v,
        "master_planned",
        master_plan=data,
        direction_audit=existing_audit,
        worker_failure_count=_ckpt.get("worker_failure_count", 0) if _ckpt else 0,
        audit_context={
            **({"master_audit": master_audit_ctx} if master_audit_ctx else {}),
            **(
                {"system_strict_bootstrap": _system_bootstrap_master_receipt}
                if _system_bootstrap_master_receipt is not None
                else {}
            ),
        } or None,
        reset_generation_attempt=True,
        reset_audit_attempt=True,
        **checkpoint_kwargs,
    )
    if not recorded:
        return _state_blocked(
            "Master plan passed but checkpoint publication was rejected",
            next_v,
            source_v,
            _matching_checkpoint(next_v, source_v),
        )

    try:
        log_system_event("pipeline.master_done", "info", f"Master planned v{next_v}: {len(data.get('tasks', []))} tasks",
                         {"next_v": next_v, "source_v": source_v, "num_tasks": len(data.get("tasks", [])),
                          "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass

    result = {
        "plan": data,
        "logs": ui.get_output(),
        **(
            {
                "implementation_executor": "system_policy_bootstrap_v1",
                "master_ensemble_invoked": True,
            }
            if _system_bootstrap_master
            else {}
        ),
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


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
    """Incremental reset: overwrite files present in source (undo worker edits to
    existing files), PRESERVE worker-created NEW files (absent from source). Returns
    the list of preserved NEW filenames.

    Invariants after this call:
      - files in both source+next -> identical to source (authoritative overwrite)
      - files only in next (worker-created NEW) -> untouched (survive the reset)
      - files only in source -> created
      - parent .completed sentinels are removed; commit_bot is the only writer
        allowed to mark a candidate complete
    """
    from evolution_infra import candidate_copy_ignore, is_candidate_copy_ignored_name

    source_names = {
        item.name
        for item in source_dir.iterdir()
        if not is_candidate_copy_ignored_name(item.name)
    }
    preserved = []
    # Walk next_dir entries: clean stale bytecode, preserve NEW files, remove files
    # that exist in source so the source copy overwrites authoritatively.
    for item in next_dir.iterdir():
        if is_candidate_copy_ignored_name(item.name):
            # Clean parent/runtime artifacts. .task_context is generated per
            # current plan by plan_compiler and must not survive resets.
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        elif item.name not in source_names:
            # Worker-created NEW file absent from source: PRESERVE it.
            preserved.append(item.name)
        else:
            # Exists in source: remove so source copy overwrites authoritatively.
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
    # Copy all source entries into next_dir (skip parent/runtime artifacts).
    # Source files are recreated/overwritten; NEW files preserved above are untouched.
    for item in source_dir.iterdir():
        if is_candidate_copy_ignored_name(item.name):
            continue
        if item.is_dir():
            shutil.copytree(item, next_dir / item.name,
                            ignore=candidate_copy_ignore)
        else:
            shutil.copy2(item, next_dir / item.name)
    return preserved


def _clear_compiled_task_context(next_dir):
    """Remove the system-owned Worker brief after a successful batch.

    ``.task_context`` is control-plane input, not part of the bot artifact.
    Keeping it through quality/official certification could hide an accidental
    runtime dependency because publication intentionally excludes the brief.
    """
    from candidate_hygiene import cleanup_transient_candidate_artifacts

    cleanup_transient_candidate_artifacts(
        next_dir,
        include_task_context=True,
    )


def _cleanup_worker_transients_before_identity_refresh(next_dir):
    """Remove host-owned compile caches before rebuilding strict identity.

    The Worker contract permits only an exact-file ``py_compile`` probe and
    explicitly denies cache cleanup to the model.  ``py_compile`` nevertheless
    creates ``__pycache__`` beside ``policy.py``.  Snapshot/delta accounting
    intentionally excludes that transient output, while the strict five-file
    identity validator correctly rejects it.  Close that work-phase boundary
    here: after the Worker write audit has passed, the host removes only the
    centrally defined transient cache surface and deliberately retains the
    compiler-owned ``.task_context`` until the refreshed identity is bound.

    The shared hygiene helper rejects symlinks and non-regular entries before
    removing anything.  Arbitrary extra files/directories remain untouched and
    therefore continue to fail the strict layout check below.
    """
    from candidate_hygiene import cleanup_transient_candidate_artifacts

    return cleanup_transient_candidate_artifacts(
        next_dir,
        include_task_context=False,
    )


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
    """Rebuild and publish one single-parent prepared identity transaction.

    Candidate bytes are projected with the existing journaled
    ``RENAME_EXCHANGE`` content CAS.  The checkpoint then uses its independent
    revision/stage/workflow CAS.  If that second CAS loses, the exact immutable
    preimage is restored; a crash inside an unfinished exchange is recovered
    from the destination journal before a fresh forward operation is opened.
    """

    from bot_artifact import canonical_digest, hash_path
    from bot_namespace import (
        POLICY_EPOCH_RECEIPT,
        policy_identity_document_errors,
        refresh_policy_identity_documents,
        strict_lineage_parent_versions,
    )
    from candidate_hygiene import sanitize_candidate_dir
    from evolution_infra import RESULTS_DIR, copy_bot_tree_for_candidate
    from prepared_baseline_contract import build_prepared_artifact_contract
    from worker_workflow import WorkerArtifactStore

    next_dir = Path(next_dir)
    source_dir = Path(source_dir)
    next_v = int(ckpt.get("next_v"))
    source_v = int(ckpt.get("source_v"))
    if ckpt.get("parent2_v") is not None:
        raise RuntimeError("identity replan cannot reconstruct crossover lineage")
    if not source_dir.is_dir() or source_dir.is_symlink():
        raise RuntimeError("identity replan source is missing or nonregular")
    if not next_dir.is_dir() or next_dir.is_symlink():
        raise RuntimeError("identity replan candidate is missing or nonregular")
    if (
        ckpt.get("publication_intent") is not None
        or ckpt.get("official_job") is not None
        or ckpt.get("infra_failure") is not None
    ):
        raise RuntimeError("identity replan has an incompatible durable overlay")

    try:
        source_receipt = json.loads(
            (source_dir / POLICY_EPOCH_RECEIPT).read_text(encoding="utf-8")
        )
        source_parents = tuple(
            int(item)
            for item in ((source_receipt.get("lineage") or {}).get("parent_versions") or [])
        )
    except Exception as exc:
        raise RuntimeError(
            f"identity replan source receipt unavailable:{type(exc).__name__}"
        ) from exc
    source_identity_errors = policy_identity_document_errors(
        source_dir,
        source_v,
        parent_versions=source_parents,
    )
    if source_identity_errors:
        raise RuntimeError(
            "identity replan source identity invalid:"
            + ";".join(source_identity_errors[:8])
        )
    source_hash = hash_path(source_dir)
    parent_identities = (
        (ckpt.get("epoch_binding") or {}).get("published_parent_identities")
        or []
    )
    source_bindings = [
        item
        for item in parent_identities
        if isinstance(item, dict) and item.get("version") == source_v
    ]
    if (
        len(source_bindings) != 1
        or source_bindings[0].get("tag_artifact_hash") != source_hash
    ):
        raise RuntimeError("identity replan source tag artifact binding mismatch")

    next_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{next_dir.name}.identity-replan-build-",
        dir=next_dir.parent,
    ) as temporary:
        staged_dir = Path(temporary) / next_dir.name
        copy_bot_tree_for_candidate(source_dir, staged_dir)
        lineage = strict_lineage_parent_versions(next_v, source_v, None)
        refreshed_identity = refresh_policy_identity_documents(
            staged_dir,
            next_v,
            parent_versions=lineage,
        )
        sanitize_candidate_dir(staged_dir, require_native_tcp=True)
        staged_errors = policy_identity_document_errors(
            staged_dir,
            next_v,
            parent_versions=lineage,
        )
        if staged_errors:
            raise RuntimeError(
                "identity replan target identity invalid:"
                + ";".join(staged_errors[:8])
            )
        prepared_contract = build_prepared_artifact_contract(
            staged_dir,
            source_v=source_v,
            next_v=next_v,
        )
        prepared_hash = str(prepared_contract["prepared_artifact_hash"])
        current_hash = hash_path(next_dir)
        if recover_persisted_reset:
            legacy_errors = _legacy_identity_replan_receipt_errors(
                ckpt,
                source_hash=source_hash,
                current_hash=current_hash,
                prepared_contract=prepared_contract,
            )
            if legacy_errors:
                return _json_tool_result({
                    "error": "ARCHITECTURE_POLICY_IDENTITY_REPLAN_RECOVERY_INVALID",
                    "failure_class": "state_migration",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "validation_errors": legacy_errors,
                    "candidate_overwritten": False,
                    "directive": (
                        "The persisted identity-replan preimage is not the exact "
                        "legacy transaction produced by the system. Preserve all "
                        "bytes and use canonical checkpoint reconciliation; never "
                        "rewrite JSON or copy a parent by hand."
                    ),
                })

        artifact_store = WorkerArtifactStore(
            Path(RESULTS_DIR) / "workflow" / "artifacts"
        )
        preimage_snapshot = artifact_store.capture(next_dir)
        prepared_snapshot = artifact_store.capture(staged_dir)
        if preimage_snapshot != current_hash or prepared_snapshot != prepared_hash:
            raise RuntimeError("identity replan immutable snapshot mismatch")

    operation_id = _identity_replan_operation_id(ckpt, prepared_hash)
    materialization = artifact_store.materialize(
        prepared_snapshot,
        next_dir,
        expected_destination_digest=current_hash,
        operation_id=operation_id,
    )
    if hash_path(next_dir) != prepared_hash:
        raise RuntimeError("identity replan materialization hash mismatch")

    prior_audit = ckpt.get("audit_context") or {}
    policy_errors = (
        (prior_audit.get("architecture_policy_identity_replan") or {}).get(
            "identity_errors"
        )
        if recover_persisted_reset
        else _checkpoint_architecture_policy_identity_errors(ckpt)
    ) or []
    if materialization.installed:
        materialization_proof = artifact_store.verify_materialization_receipt(
            materialization.operation_id,
            destination=next_dir,
            digest=prepared_hash,
            expected_destination_digest=current_hash,
            receipt_digest=materialization.receipt_digest,
        )
    else:
        materialization_proof = (
            artifact_store.find_installed_materialization_receipt(
                destination=next_dir,
                digest=prepared_hash,
            )
        )
        if materialization_proof is None:
            raise RuntimeError(
                "identity replan lacks installed materialization proof"
            )

    replan_receipt = {
        "schema_version": 2,
        "kind": "single-parent-architecture-policy-identity-replan-v2",
        "source_v": source_v,
        "next_v": next_v,
        "workflow_run_id": str(ckpt.get("workflow_run_id") or ""),
        "checkpoint_preimage_revision": int(
            ckpt.get("checkpoint_revision") or 0
        ),
        "checkpoint_preimage_stage": str(ckpt.get("stage") or ""),
        "source_stage": str(
            (prior_audit.get("architecture_policy_identity_replan") or {}).get(
                "source_stage"
            )
            if recover_persisted_reset
            else ckpt.get("stage")
        ),
        "recovery_mode": (
            "legacy_parent_copy_recovery"
            if recover_persisted_reset
            else "quality_identity_replan"
        ),
        "identity_errors": [str(item) for item in policy_errors],
        "source_artifact_hash": source_hash,
        "replaced_artifact_hash": materialization_proof[
            "expected_destination_digest"
        ],
        "prepared_artifact_hash": prepared_hash,
        "prepared_artifact_contract_digest": prepared_contract["contract_digest"],
        "runtime_manifest_digest": refreshed_identity["runtime_manifest_digest"],
        "epoch_receipt_digest": refreshed_identity["epoch_receipt_digest"],
        "runtime_manifest_file_sha256": next(
            str(item.get("sha256") or "")
            for item in prepared_contract["prepared_artifact_manifest"]["entries"]
            if item.get("type") == "file"
            and item.get("path") == "national_runtime_manifest.json"
        ),
        "epoch_receipt_file_sha256": next(
            str(item.get("sha256") or "")
            for item in prepared_contract["prepared_artifact_manifest"]["entries"]
            if item.get("type") == "file"
            and item.get("path") == "policy_epoch_receipt.json"
        ),
        "materialization_operation_id": materialization_proof["operation_id"],
        "materialization_expected_destination_digest": materialization_proof[
            "expected_destination_digest"
        ],
        "materialization_receipt_digest": materialization_proof[
            "receipt_digest"
        ],
        "candidate_reset_to_source": True,
        "target_identity_refreshed": True,
        "stale_worker_gate_identity_cleared": True,
    }
    replan_receipt["receipt_digest"] = canonical_digest(replan_receipt)
    replacement_audit = {
        key: deepcopy(prior_audit[key])
        for key in _IDENTITY_REPLAN_AUDIT_KEYS
        if key in prior_audit
    }
    replacement_audit.update({
        "prepared_artifact_contract": prepared_contract,
        "architecture_policy_identity_replan": replan_receipt,
    })

    old_stage = str(ckpt.get("stage") or "")
    reset_ledger = old_stage != "direction_audited"
    write_kwargs = {}
    if reset_ledger:
        write_kwargs = {
            "reset_runtime_contract_ledger": True,
            "expected_runtime_contract_ledger_digest": (
                _checkpoint_runtime_contract_ledger_digest(ckpt)
            ),
            "runtime_contract_ledger_reset_reason": (
                "architecture_policy_identity_replan"
            ),
        }
    written = write_pipeline_checkpoint(
        next_v,
        source_v,
        "direction_audited",
        master_plan={},
        direction_audit=ckpt.get("direction_audit"),
        audit_context=replacement_audit,
        replace_audit_context=True,
        audit_context_replacement_reason=(
            "architecture_policy_identity_replan"
        ),
        worker_failure_count=0,
        clear_reviewer_feedback=True,
        reset_generation_attempt=True,
        reset_audit_attempt=True,
        reset_precommit_attempt=True,
        precommit_rework_count=0,
        official_rework_count=0,
        clear_repair_baseline_artifact_hash=True,
        touch_stage_timestamp=True,
        expected_checkpoint_revision=int(ckpt.get("checkpoint_revision") or 0),
        expected_checkpoint_stage=old_stage,
        expected_workflow_run_id=str(ckpt.get("workflow_run_id") or ""),
        **write_kwargs,
    )
    if not written:
        current = _matching_checkpoint(next_v, source_v) or {}
        current_prepared = (
            (current.get("audit_context") or {}).get("prepared_artifact_contract")
        )
        current_replan = (
            (current.get("audit_context") or {}).get(
                "architecture_policy_identity_replan"
            )
        )
        current_replan_unsigned = (
            {
                key: value
                for key, value in current_replan.items()
                if key != "receipt_digest"
            }
            if isinstance(current_replan, dict)
            else {}
        )
        if (
            current.get("stage") == "direction_audited"
            and current_prepared == prepared_contract
            and isinstance(current_replan, dict)
            and current_replan.get("schema_version") == 2
            and current_replan.get("receipt_digest")
            == canonical_digest(current_replan_unsigned)
            and current_replan.get("prepared_artifact_hash")
            == prepared_hash
            and current_replan.get("prepared_artifact_contract_digest")
            == prepared_contract.get("contract_digest")
            and current_replan.get("target_identity_refreshed") is True
            and current_replan.get("stale_worker_gate_identity_cleared") is True
            and str(current.get("workflow_run_id") or "")
            == str(ckpt.get("workflow_run_id") or "")
            and hash_path(next_dir) == prepared_hash
        ):
            return _json_tool_result({
                "success": True,
                "recovered": True,
                "idempotent_checkpoint_projection": True,
                "next_v": next_v,
                "source_v": source_v,
                "next_tool": "run_master",
            })
        # Candidate and checkpoint publication are two fenced CAS operations,
        # not one filesystem transaction.  Rollback is safe only while the
        # checkpoint authority is still the exact preimage this invocation
        # read.  A concurrent recovery may have published revision N+1 and a
        # second run_master may already have bound the prepared bytes in N+2;
        # rolling those bytes back merely because N+2 is no longer the
        # direction_audited idempotency shape would corrupt the successor.
        checkpoint_preimage_unchanged = current == ckpt
        if not checkpoint_preimage_unchanged:
            return _json_tool_result({
                "error": (
                    "ARCHITECTURE_POLICY_IDENTITY_REPLAN_"
                    "CHECKPOINT_CONCURRENTLY_ADVANCED"
                ),
                "failure_class": "control_plane",
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "candidate_forward_preserved": (
                    hash_path(next_dir) == prepared_hash
                ),
                "candidate_preimage_restored": False,
                "expected_checkpoint_revision": int(
                    ckpt.get("checkpoint_revision") or 0
                ),
                "current_checkpoint_revision": current.get(
                    "checkpoint_revision"
                ),
                "expected_checkpoint_stage": old_stage,
                "current_checkpoint_stage": current.get("stage"),
                "expected_workflow_run_id": str(
                    ckpt.get("workflow_run_id") or ""
                ),
                "current_workflow_run_id": str(
                    current.get("workflow_run_id") or ""
                ),
                "directive": (
                    "Checkpoint authority changed after candidate content-CAS. "
                    "The forward prepared bytes were preserved because a "
                    "successor may already bind them. Re-read the canonical "
                    "route; never roll back or edit the candidate by hand."
                ),
            })
        if materialization.installed and current_hash != prepared_hash:
            rollback_id = f"{operation_id}-rollback"
            artifact_store.materialize(
                preimage_snapshot,
                next_dir,
                expected_destination_digest=prepared_hash,
                operation_id=rollback_id,
            )
        return _json_tool_result({
            "error": "ARCHITECTURE_POLICY_IDENTITY_REPLAN_CHECKPOINT_CAS_FAILED",
            "failure_class": "control_plane",
            "action": "operator_reconcile",
            "next_v": next_v,
            "source_v": source_v,
            "candidate_preimage_restored": hash_path(next_dir) == current_hash,
        })

    log_system_event(
        "pipeline.architecture_policy_identity_replan",
        "error",
        (
            f"Rebuilt target-version prepared identity for v{next_v} from "
            f"strict parent v{source_v}; fresh Master plan required"
        ),
        {
            "next_v": next_v,
            "source_v": source_v,
            "source_stage": old_stage,
            "prepared_artifact_hash": prepared_hash,
            "prepared_artifact_contract_digest": prepared_contract[
                "contract_digest"
            ],
            "receipt_digest": replan_receipt["receipt_digest"],
            "legacy_recovery": recover_persisted_reset,
        },
    )
    return _json_tool_result({
        "error": "ARCHITECTURE_POLICY_IDENTITY_REPLAN",
        "recovered": True,
        "next_v": next_v,
        "source_v": source_v,
        "identity_errors": list(policy_errors),
        "candidate_reset_to_source": True,
        "target_identity_refreshed": True,
        "prepared_artifact_hash": prepared_hash,
        "prepared_artifact_contract_digest": prepared_contract[
            "contract_digest"
        ],
        "replan_receipt_digest": replan_receipt["receipt_digest"],
        "next_tool": "run_master",
        "directive": (
            "The stale Worker/gate identity was cleared and the exact strict "
            "parent was rematerialized as a target-version prepared artifact. "
            "Call run_master again to build a fresh policy-bound plan."
        ),
    })


def _checkpoint_architecture_policy_identity_errors(ckpt):
    if not isinstance(ckpt, dict):
        return []
    quality = (ckpt.get("gate_results") or {}).get("quality") or {}
    transition = quality.get("national_architecture_transition") or {}
    if not isinstance(transition, dict):
        return []
    return [str(item) for item in transition.get("policy_identity_errors") or [] if str(item)]


# Identity replan circuit breaker. A deterministic identity error that is not
# actually fixed by the replan path (e.g. a frozen-vs-recomputed digest
# mismatch caused by non-determinism, as observed in the v152 loop) would
# otherwise retrigger the same recovery forever, burning LLM budget with zero
# possibility of success. Track consecutive identical error fingerprints; once
# the threshold is crossed, abandon the generation and surface to the operator
# instead of looping. Distinct fingerprints reset the count, so genuine
# progressive repair is unaffected.
IDENTITY_REPLAN_ABANDON_THRESHOLD = 3


def _identity_replan_fingerprint(errors):
    """Stable, deduplicated string key for the identity error set.

    Serialized to a single string so the value round-trips through JSON
    checkpoint storage and string comparisons in the circuit breaker.
    """
    items = sorted(set(str(item) for item in (errors or []) if str(item)))
    return "|".join(items)


def _identity_replan_counts(ckpt):
    """Return the history list of recorded replan fingerprints (strings).

    Stored under checkpoint key ``identity_replan_history``. Only the trailing
    run identical to the most recent entry matters for the circuit breaker,
    but the full list is kept for diagnostics.
    """
    if not isinstance(ckpt, dict):
        return []
    history = ckpt.get("identity_replan_history")
    return [str(item) for item in (history or []) if isinstance(item, str)]


def _identity_replan_consecutive_count(history, fingerprint):
    """Count trailing history entries equal to ``fingerprint``."""
    if not fingerprint:
        return 0
    count = 0
    for item in reversed(history):
        if item == fingerprint:
            count += 1
        else:
            break
    return count


def _record_identity_replan_attempt(ckpt, fingerprint):
    """Record one replan attempt and return the updated history list.

    A different fingerprint from the prior attempt resets the consecutive run
    (progressive repair). Caller is responsible for writing the checkpoint.
    """
    if not isinstance(ckpt, dict) or not fingerprint:
        return _identity_replan_counts(ckpt)
    history = _identity_replan_counts(ckpt)
    if history and history[-1] != fingerprint:
        history = []
    history.append(fingerprint)
    ckpt["identity_replan_history"] = list(history)
    return history



def _checkpoint_runtime_contract_ledger_digest(ckpt):
    ledger = ckpt.get("runtime_contract_ledger") if isinstance(ckpt, dict) else None
    if ledger is None and isinstance(ckpt, dict):
        master_plan = ckpt.get("master_plan")
        if isinstance(master_plan, dict):
            ledger = master_plan.get("runtime_contract_ledger")
    return str((ledger or {}).get("ledger_digest") or "")


def _recover_architecture_policy_identity(ckpt, next_dir, source_dir):
    """Discard stale-policy code and route through a fresh system-owned Master plan."""
    errors = _checkpoint_architecture_policy_identity_errors(ckpt)
    if not errors:
        return None
    next_v = ckpt.get("next_v")
    source_v = ckpt.get("source_v")
    parent2_v = ckpt.get("parent2_v")
    if parent2_v is not None:
        # Resetting a crossover child to Parent A while retaining parent2_v and
        # crossover metadata fabricates a two-parent lineage.  The prepared
        # child is itself the authoritative baseline; once its policy identity
        # is stale there is no trusted single-parent reconstruction path.
        log_system_event(
            "pipeline.crossover_policy_identity_fail_closed",
            "error",
            f"Crossover v{next_v} policy identity is stale; refusing Parent-A reset",
            {
                "next_v": next_v,
                "source_v": source_v,
                "parent2_v": parent2_v,
                "source_stage": ckpt.get("stage"),
                "identity_errors": errors,
            },
        )
        return _json_tool_result({
            "error": "CROSSOVER_ARCHITECTURE_POLICY_IDENTITY_STALE",
            "next_v": next_v,
            "source_v": source_v,
            "parent2_v": parent2_v,
            "identity_errors": errors,
            "candidate_reset_to_source": False,
            "next_tool": "abandon_generation",
            "directive": (
                "Fail closed: do not reset this two-parent child to Parent A while "
                "claiming crossover lineage. Abandon this generation, then rerun "
                "crossover from a fresh selected checkpoint under the current policy."
            ),
        })
    return _materialize_identity_replan_candidate(
        ckpt,
        next_dir,
        source_dir,
        recover_persisted_reset=False,
    )


def _recover_persisted_architecture_policy_identity_replan(
    ckpt,
    next_dir,
    source_dir,
):
    """Repair the exact direction_audited state emitted by the retired reset.

    A valid new receipt with a matching prepared artifact is already complete.
    Any other Direction checkpoint is outside this migration and remains under
    the ordinary prepared-artifact drift gate.
    """

    if not isinstance(ckpt, dict) or ckpt.get("stage") != "direction_audited":
        return None
    audit = ckpt.get("audit_context") or {}
    receipt = audit.get("architecture_policy_identity_replan")
    if not isinstance(receipt, dict):
        return None
    from prepared_baseline_contract import validate_prepared_artifact_contract
    from evolution_infra import (
        RESULTS_DIR,
        _identity_replan_live_materialization_errors,
        _identity_replan_replacement_contract_errors,
    )

    prepared = audit.get("prepared_artifact_contract")
    prepared_errors = validate_prepared_artifact_contract(
        prepared,
        prepared_dir=next_dir,
        source_v=ckpt.get("source_v"),
        next_v=ckpt.get("next_v"),
        verify_live_content=True,
    )
    if receipt.get("schema_version") == 2:
        try:
            receipt_revision = int(receipt.get("checkpoint_preimage_revision"))
        except (TypeError, ValueError):
            receipt_revision = -1
        receipt_stage = str(receipt.get("checkpoint_preimage_stage") or "")
        replan_errors = _identity_replan_replacement_contract_errors(
            replacement=audit,
            next_v=ckpt.get("next_v"),
            source_v=ckpt.get("source_v"),
            workflow_run_id=str(ckpt.get("workflow_run_id") or ""),
            checkpoint_revision=receipt_revision,
            checkpoint_stage=receipt_stage,
            epoch_binding=ckpt.get("epoch_binding"),
        )
        if not replan_errors:
            replan_errors.extend(
                _identity_replan_live_materialization_errors(
                    audit,
                    candidate_dir=next_dir,
                    artifact_root=Path(RESULTS_DIR) / "workflow" / "artifacts",
                )
            )
        if int(ckpt.get("checkpoint_revision") or 0) != receipt_revision + 1:
            replan_errors.append(
                "identity_replan_checkpoint_projection_revision_mismatch"
            )
        if not prepared_errors and not replan_errors:
            return None
        return _json_tool_result({
            "error": "ARCHITECTURE_POLICY_IDENTITY_REPLAN_RECOVERY_INVALID",
            "failure_class": "state_migration",
            "action": "operator_reconcile",
            "next_v": ckpt.get("next_v"),
            "source_v": ckpt.get("source_v"),
            "validation_errors": list(dict.fromkeys([
                *prepared_errors,
                *replan_errors,
            ])),
            "candidate_overwritten": False,
            "directive": (
                "The schema-2 identity-replan projection is not the exact "
                "closed receipt published by its checkpoint CAS. Preserve all "
                "bytes and reconcile canonical authority; never rewrite JSON "
                "or copy a parent by hand."
            ),
        })
    return _materialize_identity_replan_candidate(
        ckpt,
        next_dir,
        source_dir,
        recover_persisted_reset=True,
    )




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
