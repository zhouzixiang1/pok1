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
import time
import tokenize
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


_PROTOCOL_BOOTSTRAP_NO_STRENGTH = (
    "PROTOCOL BOOTSTRAP NO-STRENGTH: no current-cycle strength evidence exists. "
    "Use only the digest-bound strict prepared artifact, repository-pinned "
    "protocol evidence, and bootstrap receipt supplied by the system."
)

# national_tcp_policy_v1 has one candidate-owned source artifact.  System
# runtime/precompute bytes and any extra helper/asset are never Worker targets.
_ACTIVE_CANDIDATE_WRITABLE_FILES = frozenset({"policy.py"})


def _render_literature_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    expected = {"source_v", "next_v", "weakness", "stagnation_info"}
    if not isinstance(inputs, dict) or set(inputs) != expected:
        raise ValueError("Literature renderer input contract mismatch")
    source_v = int(inputs["source_v"])
    probe_template = (
        Path(__file__).resolve().parent / "prompts" / "literature_probe_prompt.md"
    ).read_text(encoding="utf-8")
    weakness = str(inputs["weakness"]).strip() or (
        "General postflop stack-off leak: 0%-fold facing river all-ins (made_strength "
        "0.40-0.50 always calls). Need optimal fold frequency vs polarized jam."
    )
    stagnation_info = str(inputs["stagnation_info"] or "")
    text = (
        f"{probe_template}\n\n"
        f"## Current H2H weakness to research\n{weakness}\n\n"
        f"## Stagnation context\n"
        f"{stagnation_info or 'Stagnation detected — current axis exhausted.'}\n\n"
        f"## Source bot version\nv{source_v}\n\n"
        f"Now execute the 4 steps (PLAN → SEARCH → REFLECT → WRITE) and return the final "
        f"WRITE-step JSON. You have web search tools available — use them for the SEARCH step."
    )
    brief_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    return LLMRenderedMaterial(
        text=text,
        evidence_kind="governed_literature_brief",
        evidence_provenance={
            "source_v": source_v,
            "next_v": int(inputs["next_v"]),
            "brief_digest": brief_digest,
        },
    )


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
    from evolution_infra import RESULTS_DIR
    return RESULTS_DIR / "research_proposals" / f"v{int(next_v)}.json"


def _complete_artifact_fingerprint(root) -> str:
    """Safe complete-artifact identity for control-plane receipts/retries."""
    try:
        from bot_artifact import hash_path

        return hash_path(Path(root))
    except Exception:
        return ""


def _literature_probe_context_fingerprint(
    source_v: int | str | None,
    h2h_weakness: str = "",
    stagnation_info: str = "",
) -> str:
    payload = {
        "source_v": int(source_v) if source_v is not None else None,
        "h2h_weakness": " ".join((h2h_weakness or "").split()),
        "stagnation_info": " ".join((stagnation_info or "").split()),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _literature_digest(value) -> str:
    return hashlib.sha256(
        _literature_canonical_json(value).encode("utf-8")
    ).hexdigest()


def _literature_checkpoint_identity(
    checkpoint: dict,
    *,
    origin_revision: int | None = None,
) -> str:
    """Digest the semantic checkpoint preimage across the receipt CAS write."""

    projection = deepcopy(checkpoint)
    projection.pop("literature_probe", None)
    for field in ("timestamp", "last_update_ts", "last_stage_change_ts"):
        projection.pop(field, None)
    projection["checkpoint_revision"] = (
        int(origin_revision)
        if origin_revision is not None
        else int(checkpoint["checkpoint_revision"])
    )
    return _literature_digest(projection)


def _literature_checkpoint_binding(
    checkpoint: dict,
    receipt_binding: dict,
) -> dict:
    if not isinstance(checkpoint, dict):
        raise ValueError("literature checkpoint is not an object")
    workflow_run_id = str(checkpoint.get("workflow_run_id") or "").strip()
    revision = checkpoint.get("checkpoint_revision")
    if not workflow_run_id or type(revision) is not int or revision < 1:
        raise ValueError("literature checkpoint has no durable workflow identity")
    if checkpoint.get("stage") != "direction_audited":
        raise ValueError("literature checkpoint is not direction_audited")
    if not isinstance(receipt_binding, dict):
        raise ValueError("literature requirement binding is missing")
    return {
        "schema": _LITERATURE_PROBE_CHECKPOINT_BINDING_SCHEMA,
        "checkpoint_identity": _literature_checkpoint_identity(checkpoint),
        "checkpoint_revision": revision,
        "workflow_run_id": workflow_run_id,
        "stage": "direction_audited",
        "next_v": int(checkpoint["next_v"]),
        "source_v": int(checkpoint["source_v"]),
        "requirement_context_digest": str(
            receipt_binding["requirement_context_digest"]
        ),
    }


def _literature_dispatch_projection(rendered_prompt) -> dict | None:
    receipt = getattr(rendered_prompt, "dispatch_receipt", None)
    if receipt is None:
        return None
    return {
        "schema": str(receipt.schema),
        "role_id": str(receipt.role_id),
        "runtime_role": str(receipt.runtime_role),
        "model": str(receipt.model),
        "dispatch_receipt_digest": str(receipt.receipt_digest),
        "renderer_receipt_digest": str(receipt.renderer.receipt_digest),
        "rendered_prompt_sha256": str(
            receipt.renderer.rendered_prompt_sha256
        ),
        "evidence_receipt_digest": str(receipt.evidence.receipt_digest),
        "evidence_provenance_sha256": str(
            receipt.evidence.provenance_sha256
        ),
        "mcp_receipt_digest": str(receipt.mcp.receipt_digest),
        "mcp_config_sha256": str(receipt.mcp.config_sha256),
    }


def _expected_literature_dispatch(
    *,
    next_v: int,
    source_v: int,
    weakness: str,
    stagnation_info: str,
) -> dict:
    rendered = _issue_literature_rendered_prompt(
        next_v=next_v,
        source_v=source_v,
        weakness=weakness,
        stagnation_info=stagnation_info,
    )
    projection = _literature_dispatch_projection(rendered)
    if projection is None:
        raise ValueError("literature dispatch receipt was not issued")
    return projection


def _issue_literature_rendered_prompt(
    *,
    next_v: int,
    source_v: int,
    weakness: str,
    stagnation_info: str,
):
    from llm_query import render_llm_prompt

    return render_llm_prompt(
        f"LITERATURE_PROBE (v{int(next_v)})",
        producer=_render_literature_provider_prompt,
        renderer_inputs={
            "source_v": int(source_v),
            "next_v": int(next_v),
            "weakness": str(weakness),
            "stagnation_info": str(stagnation_info),
        },
    )


def _normalize_literature_proposal(proposal) -> dict | None:
    if not isinstance(proposal, dict) or proposal.get("claim") is None:
        return None
    normalized = {
        field: str(proposal.get(field) or "")
        for field in _LITERATURE_PROPOSAL_FIELDS
    }
    if not normalized["claim"]:
        return None
    if any(len(value) > 32_768 for value in normalized.values()):
        raise ValueError("literature proposal field exceeds bounded schema")
    return normalized


def _literature_candidate_submission(
    proposal: dict | None,
    next_v: int,
    submitted_candidate_id: str | None = None,
) -> dict | None:
    if proposal is None:
        return None
    result = {
        "claim": proposal["claim"],
        "source_url": proposal["source_url"],
        "numeric_claim": proposal["numeric_claim"],
        "target_fn": proposal["target_fn"],
        "proposed_change": proposal["proposed_change"],
        "pseudocode": proposal["pseudocode"],
        "firing_tuple": proposal["firing_tuple"],
        "born_gen": int(next_v),
    }
    if submitted_candidate_id is not None:
        result["id"] = str(submitted_candidate_id)
    return result


def _expected_literature_candidate_id(
    proposal: dict | None,
    *,
    checkpoint_identity: str,
    terminal_output_sha256: str | None,
) -> str | None:
    if proposal is None:
        return None
    identity = {
        "checkpoint_identity": str(checkpoint_identity),
        "terminal_output_sha256": str(terminal_output_sha256 or ""),
        "proposal_digest": _literature_digest(proposal),
    }
    return f"wc_lit_{_literature_digest(identity)[:32]}"


def _literature_translation_receipt(
    proposal: dict | None,
    *,
    next_v: int,
    candidate_id,
    checkpoint_identity: str,
    terminal_output_sha256: str | None,
) -> dict:
    submitted_candidate_id = _expected_literature_candidate_id(
        proposal,
        checkpoint_identity=checkpoint_identity,
        terminal_output_sha256=terminal_output_sha256,
    )
    submission = _literature_candidate_submission(
        proposal,
        next_v,
        submitted_candidate_id,
    )
    if submission is None:
        return {
            "schema": _LITERATURE_PROBE_TRANSLATION_SCHEMA,
            "status": "not_applicable",
            "eligible": False,
            "submitted_candidate_id": None,
            "candidate_id": None,
            "gated_out": False,
            "candidate_submission_digest": None,
        }
    from research_governance import translation_gate

    eligible = bool(translation_gate(deepcopy(submission)))
    normalized_candidate_id = (
        str(candidate_id).strip()
        if isinstance(candidate_id, str) and str(candidate_id).strip()
        else None
    )
    if normalized_candidate_id not in {None, submitted_candidate_id}:
        raise ValueError("literature governance returned a non-deterministic candidate id")
    return {
        "schema": _LITERATURE_PROBE_TRANSLATION_SCHEMA,
        "status": "accepted" if normalized_candidate_id is not None else "rejected",
        "eligible": eligible,
        "submitted_candidate_id": submitted_candidate_id,
        "candidate_id": normalized_candidate_id,
        "gated_out": normalized_candidate_id is None,
        "candidate_submission_digest": _literature_digest(submission),
    }


def _literature_probe_stale_result(next_v: int | str, source_v: int | str | None) -> dict:
    return {
        "error": "LITERATURE_PROBE_STALE_RESULT",
        "next_v": next_v,
        "source_v": source_v,
        "directive": (
            "The checkpoint changed while literature research was running. "
            "No stale receipt was written; follow the current checkpoint route."
        ),
    }


def _h2h_citation_audit_feedback(next_v, errors, repair_guidance=""):
    """Render the deterministic citation failure without format ambiguity."""

    guidance = f"\n\n{repair_guidance}" if repair_guidance else ""
    return (
        "Master plan H2H citations disagree with the stable generation "
        "H2H snapshot. Correct the cited raw games/a_wins/b_wins/draws counts "
        f"against web/core/results/v{int(next_v)}/evidence_snapshot/"
        "head_to_head.json: "
        f"{'; '.join(str(item) for item in list(errors)[:6])}{guidance}"
    )


def _literature_probe_inject_text(payload: dict) -> str:
    proposal = payload.get("proposal") if isinstance(payload, dict) else None
    candidate_id = payload.get("candidate_id") if isinstance(payload, dict) else None
    gated_out = bool(payload.get("gated_out")) if isinstance(payload, dict) else False
    reason = payload.get("reason", "") if isinstance(payload, dict) else ""

    if proposal and candidate_id:
        return (
            "## Research Proposal (web-derived hypothesis, verify before using)\n"
            f"- claim: {proposal.get('claim','')}\n"
            f"- target_fn: {proposal.get('target_fn','')}\n"
            f"- numeric_claim: {proposal.get('numeric_claim','')}\n"
            f"- firing_tuple: {proposal.get('firing_tuple','')}\n"
            f"- source: {proposal.get('source_url','')}\n"
            f"- pseudocode: {proposal.get('pseudocode','')}\n"
            "NOTE: this is a hypothesis from web research. It must pass all quality gates "
            "(decision tests >=70%, precommit eval). If precommit fails, this pattern is "
            "auto-blacklisted by research_governance."
        )
    if reason == "literature_probe_timeout":
        return (
            "## Research Proposal\n"
            "No codable proposal was produced because the web research stage timed out. "
            "Proceed with run_master using frozen direction, H2H, replay, and identity-bound native memory evidence."
        )
    if reason and reason != "completed":
        return (
            "## Research Proposal\n"
            f"No codable proposal is available for this generation ({reason}). "
            "Proceed with run_master without a web hypothesis."
        )
    return (
        "## Research Proposal\nNo codable proposal survived the reflect/translation gate "
        f"this generation (gated_out={gated_out}). Proceed with run_master without a web hypothesis."
    )


def _build_literature_probe_payload(
    payload: dict,
    *,
    checkpoint: dict,
    receipt_binding: dict,
    rendered_prompt=None,
    terminal_output: str | None = None,
) -> dict:
    """Create the sole durable literature outcome from system-owned inputs."""

    reason = str(payload.get("reason") or "").strip()
    if reason not in _LITERATURE_PROBE_REASONS:
        raise ValueError(f"unsupported literature terminal reason: {reason!r}")
    next_v = int(checkpoint["next_v"])
    source_v = int(checkpoint["source_v"])
    if int(payload.get("next_v")) != next_v or int(payload.get("source_v")) != source_v:
        raise ValueError("literature terminal identity drift")

    proposal = (
        _normalize_literature_proposal(payload.get("proposal"))
        if reason == "completed"
        else None
    )
    checkpoint_binding = _literature_checkpoint_binding(
        checkpoint,
        receipt_binding,
    )
    terminal_output_sha256 = (
        hashlib.sha256(terminal_output.encode("utf-8")).hexdigest()
        if isinstance(terminal_output, str)
        else None
    )
    translation = _literature_translation_receipt(
        proposal,
        next_v=next_v,
        candidate_id=payload.get("candidate_id"),
        checkpoint_identity=checkpoint_binding["checkpoint_identity"],
        terminal_output_sha256=terminal_output_sha256,
    )
    elapsed = payload.get("elapsed_sec", 0.0)
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
        raise ValueError("literature elapsed_sec is not numeric")
    elapsed = float(elapsed)
    if not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("literature elapsed_sec is not finite and non-negative")
    timeout_s = payload.get("timeout_s") if reason == "literature_probe_timeout" else None
    if timeout_s is not None:
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
            raise ValueError("literature timeout_s is not numeric")
        timeout_s = float(timeout_s)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("literature timeout_s is not positive")

    body = {
        "schema": _LITERATURE_PROBE_PAYLOAD_SCHEMA,
        "next_v": next_v,
        "source_v": source_v,
        "reason": reason,
        "skipped": reason != "completed",
        "weakness": str(payload.get("weakness") or ""),
        "stagnation_info": str(payload.get("stagnation_info") or ""),
        "proposal": proposal,
        "candidate_id": translation["candidate_id"],
        "gated_out": translation["gated_out"],
        "elapsed_sec": elapsed,
        "timeout_s": timeout_s,
        "error": (
            str(payload.get("error") or "")[:1000]
            if reason == "literature_probe_failed"
            else ""
        ),
        "context_fingerprint": _literature_probe_context_fingerprint(
            source_v,
            str(payload.get("weakness") or ""),
            str(payload.get("stagnation_info") or ""),
        ),
    }
    for field in _LITERATURE_PROBE_BINDING_FIELDS:
        body[field] = deepcopy(receipt_binding[field])
    body["inject_text"] = _literature_probe_inject_text(body)
    canonical_payload_digest = _literature_digest(body)

    llm_dispatch = _literature_dispatch_projection(rendered_prompt)
    if reason == "governed_skip":
        if llm_dispatch is not None or terminal_output is not None:
            raise ValueError("governed skip cannot claim a provider dispatch")
    elif llm_dispatch is None:
        raise ValueError("provider terminal outcome has no dispatch receipt")
    if reason == "completed" and not isinstance(terminal_output, str):
        raise ValueError("completed literature outcome has no terminal output")

    producer_receipt = {
        "schema": _LITERATURE_PROBE_PRODUCER_SCHEMA,
        "producer_kind": reason,
        "checkpoint_binding": checkpoint_binding,
        "requirement_context_digest": str(
            receipt_binding["requirement_context_digest"]
        ),
        "llm_dispatch_receipt": llm_dispatch,
        "terminal_output": terminal_output if reason == "completed" else None,
        "terminal_output_sha256": terminal_output_sha256,
        "parsed_proposal_digest": _literature_digest(proposal),
        "translation_gate": translation,
        "canonical_payload_digest": canonical_payload_digest,
    }
    producer_receipt["receipt_digest"] = _literature_digest(producer_receipt)
    return {
        **body,
        "canonical_payload_digest": canonical_payload_digest,
        "producer_receipt": producer_receipt,
    }


def _literature_probe_payload_errors(
    data: dict | None,
    *,
    checkpoint: dict | None,
    receipt_binding: dict | None,
    require_origin_checkpoint: bool,
) -> list[str]:
    """Validate cache/checkpoint bytes without trusting any derived text field."""

    errors: list[str] = []
    if not isinstance(data, dict):
        return ["literature_payload_missing_or_not_object"]
    if set(data) != _LITERATURE_PROBE_PAYLOAD_FIELDS:
        return ["literature_payload_schema_fields_mismatch"]
    if data.get("schema") != _LITERATURE_PROBE_PAYLOAD_SCHEMA:
        errors.append("literature_payload_schema_invalid")
    if not isinstance(checkpoint, dict):
        errors.append("literature_checkpoint_missing")
        return errors
    if not isinstance(receipt_binding, dict):
        errors.append("literature_requirement_binding_missing")
        return errors

    try:
        next_v = int(checkpoint["next_v"])
        source_v = int(checkpoint["source_v"])
    except (KeyError, TypeError, ValueError):
        errors.append("literature_checkpoint_identity_invalid")
        return errors
    if type(data.get("next_v")) is not int or data.get("next_v") != next_v:
        errors.append("literature_payload_next_v_mismatch")
    if type(data.get("source_v")) is not int or data.get("source_v") != source_v:
        errors.append("literature_payload_source_v_mismatch")
    reason = data.get("reason")
    if reason not in _LITERATURE_PROBE_REASONS:
        errors.append("literature_payload_reason_invalid")
    if type(data.get("skipped")) is not bool or data.get("skipped") != (
        reason != "completed"
    ):
        errors.append("literature_payload_skipped_invalid")
    for field in ("weakness", "stagnation_info", "error", "inject_text"):
        if not isinstance(data.get(field), str):
            errors.append(f"literature_payload_{field}_invalid")
    elapsed = data.get("elapsed_sec")
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(float(elapsed))
        or float(elapsed) < 0
    ):
        errors.append("literature_payload_elapsed_invalid")
    timeout_s = data.get("timeout_s")
    if reason == "literature_probe_timeout":
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(float(timeout_s))
            or float(timeout_s) <= 0
        ):
            errors.append("literature_payload_timeout_invalid")
    elif timeout_s is not None:
        errors.append("literature_payload_unexpected_timeout")
    if reason == "literature_probe_failed":
        if not isinstance(data.get("error"), str) or not data.get("error"):
            errors.append("literature_payload_failure_error_missing")
    elif data.get("error") != "":
        errors.append("literature_payload_unexpected_error")

    proposal = data.get("proposal")
    try:
        normalized_proposal = _normalize_literature_proposal(proposal)
    except Exception:
        normalized_proposal = None
        errors.append("literature_payload_proposal_invalid")
    if proposal is not None and (
        not isinstance(proposal, dict)
        or set(proposal) != set(_LITERATURE_PROPOSAL_FIELDS)
        or normalized_proposal != proposal
    ):
        errors.append("literature_payload_proposal_schema_invalid")
    if reason != "completed" and proposal is not None:
        errors.append("literature_payload_unexpected_proposal")
    if data.get("candidate_id") is not None and (
        not isinstance(data.get("candidate_id"), str)
        or not data.get("candidate_id").strip()
    ):
        errors.append("literature_payload_candidate_id_invalid")
    if type(data.get("gated_out")) is not bool:
        errors.append("literature_payload_gated_out_invalid")

    for field in _LITERATURE_PROBE_BINDING_FIELDS:
        if data.get(field) != receipt_binding.get(field):
            errors.append(f"literature_payload_{field}_mismatch")
    expected_context_fingerprint = _literature_probe_context_fingerprint(
        source_v,
        data.get("weakness") if isinstance(data.get("weakness"), str) else "",
        data.get("stagnation_info")
        if isinstance(data.get("stagnation_info"), str)
        else "",
    )
    if data.get("context_fingerprint") != expected_context_fingerprint:
        errors.append("literature_payload_context_fingerprint_mismatch")
    expected_inject = _literature_probe_inject_text(data)
    if data.get("inject_text") != expected_inject:
        errors.append("literature_payload_inject_text_not_canonical")

    body = {field: deepcopy(data.get(field)) for field in _LITERATURE_PROBE_BODY_FIELDS}
    canonical_payload_digest = _literature_digest(body)
    if data.get("canonical_payload_digest") != canonical_payload_digest:
        errors.append("literature_payload_digest_mismatch")

    producer = data.get("producer_receipt")
    producer_fields = {
        "schema",
        "producer_kind",
        "checkpoint_binding",
        "requirement_context_digest",
        "llm_dispatch_receipt",
        "terminal_output",
        "terminal_output_sha256",
        "parsed_proposal_digest",
        "translation_gate",
        "canonical_payload_digest",
        "receipt_digest",
    }
    if not isinstance(producer, dict) or set(producer) != producer_fields:
        errors.append("literature_producer_receipt_schema_fields_mismatch")
        return errors
    if producer.get("schema") != _LITERATURE_PROBE_PRODUCER_SCHEMA:
        errors.append("literature_producer_receipt_schema_invalid")
    if producer.get("producer_kind") != reason:
        errors.append("literature_producer_kind_mismatch")
    if producer.get("requirement_context_digest") != receipt_binding.get(
        "requirement_context_digest"
    ):
        errors.append("literature_producer_requirement_binding_mismatch")
    if producer.get("canonical_payload_digest") != canonical_payload_digest:
        errors.append("literature_producer_payload_digest_mismatch")
    if producer.get("parsed_proposal_digest") != _literature_digest(
        normalized_proposal
    ):
        errors.append("literature_producer_proposal_digest_mismatch")

    checkpoint_binding = producer.get("checkpoint_binding")
    checkpoint_binding_fields = {
        "schema",
        "checkpoint_identity",
        "checkpoint_revision",
        "workflow_run_id",
        "stage",
        "next_v",
        "source_v",
        "requirement_context_digest",
    }
    if (
        not isinstance(checkpoint_binding, dict)
        or set(checkpoint_binding) != checkpoint_binding_fields
        or checkpoint_binding.get("schema")
        != _LITERATURE_PROBE_CHECKPOINT_BINDING_SCHEMA
    ):
        errors.append("literature_producer_checkpoint_binding_invalid")
    else:
        current_workflow = str(checkpoint.get("workflow_run_id") or "")
        current_revision = checkpoint.get("checkpoint_revision")
        if checkpoint_binding.get("workflow_run_id") != current_workflow:
            errors.append("literature_producer_workflow_mismatch")
        if checkpoint_binding.get("stage") != "direction_audited" or checkpoint.get(
            "stage"
        ) != "direction_audited":
            errors.append("literature_producer_stage_mismatch")
        if checkpoint_binding.get("next_v") != next_v:
            errors.append("literature_producer_next_v_mismatch")
        if checkpoint_binding.get("source_v") != source_v:
            errors.append("literature_producer_source_v_mismatch")
        if checkpoint_binding.get("requirement_context_digest") != receipt_binding.get(
            "requirement_context_digest"
        ):
            errors.append("literature_producer_checkpoint_requirement_mismatch")
        origin_revision = checkpoint_binding.get("checkpoint_revision")
        if type(origin_revision) is not int or origin_revision < 1:
            errors.append("literature_producer_checkpoint_revision_invalid")
        elif type(current_revision) is not int:
            errors.append("literature_current_checkpoint_revision_invalid")
        elif require_origin_checkpoint:
            if current_revision != origin_revision:
                errors.append("literature_cache_checkpoint_revision_mismatch")
            if checkpoint.get("literature_probe") is not None:
                errors.append("literature_cache_origin_already_has_receipt")
        elif current_revision < origin_revision + 1:
            errors.append("literature_checkpoint_receipt_revision_precedes_producer")
        if (
            not isinstance(checkpoint_binding.get("checkpoint_identity"), str)
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                checkpoint_binding.get("checkpoint_identity", ""),
            )
        ):
            errors.append("literature_producer_checkpoint_identity_invalid")
        elif type(origin_revision) is int and checkpoint_binding.get(
            "checkpoint_identity"
        ) != _literature_checkpoint_identity(
            checkpoint,
            origin_revision=origin_revision,
        ):
            errors.append("literature_checkpoint_semantic_identity_mismatch")

    translation = producer.get("translation_gate")
    try:
        expected_translation = _literature_translation_receipt(
            normalized_proposal,
            next_v=next_v,
            candidate_id=data.get("candidate_id"),
            checkpoint_identity=(
                checkpoint_binding.get("checkpoint_identity", "")
                if isinstance(checkpoint_binding, dict)
                else ""
            ),
            terminal_output_sha256=producer.get("terminal_output_sha256"),
        )
    except Exception:
        expected_translation = None
        errors.append("literature_translation_gate_replay_failed")
    if translation != expected_translation:
        errors.append("literature_translation_gate_receipt_mismatch")
    elif isinstance(translation, dict) and (
        translation.get("gated_out") != data.get("gated_out")
        or translation.get("candidate_id") != data.get("candidate_id")
    ):
        errors.append("literature_translation_gate_payload_mismatch")

    dispatch = producer.get("llm_dispatch_receipt")
    if reason == "governed_skip":
        if (
            dispatch is not None
            or producer.get("terminal_output") is not None
            or producer.get("terminal_output_sha256") is not None
        ):
            errors.append("literature_governed_skip_claims_provider_output")
    else:
        try:
            expected_dispatch = _expected_literature_dispatch(
                next_v=next_v,
                source_v=source_v,
                weakness=str(data.get("weakness") or ""),
                stagnation_info=str(data.get("stagnation_info") or ""),
            )
        except Exception:
            expected_dispatch = None
            errors.append("literature_dispatch_receipt_replay_failed")
        if dispatch != expected_dispatch:
            errors.append("literature_dispatch_receipt_mismatch")
        output_digest = producer.get("terminal_output_sha256")
        if reason == "completed":
            terminal_output = producer.get("terminal_output")
            if (
                not isinstance(terminal_output, str)
                or len(terminal_output.encode("utf-8"))
                > _LITERATURE_PROBE_CACHE_MAX_BYTES // 2
            ):
                errors.append("literature_terminal_output_invalid")
            elif (
                not isinstance(output_digest, str)
                or output_digest
                != hashlib.sha256(terminal_output.encode("utf-8")).hexdigest()
            ):
                errors.append("literature_terminal_output_digest_invalid")
            else:
                try:
                    from llm_query import parse_json_output_with_mode

                    replayed, _mode = parse_json_output_with_mode(terminal_output)
                    replayed_proposal = _normalize_literature_proposal(replayed)
                except Exception:
                    replayed_proposal = None
                    errors.append("literature_terminal_output_parse_failed")
                if replayed_proposal != normalized_proposal:
                    errors.append("literature_terminal_output_proposal_mismatch")
        elif (
            producer.get("terminal_output") is not None
            or output_digest is not None
        ):
            errors.append("literature_unexpected_terminal_output_digest")

    claimed_receipt_digest = producer.get("receipt_digest")
    producer_body = {
        key: deepcopy(value)
        for key, value in producer.items()
        if key != "receipt_digest"
    }
    if claimed_receipt_digest != _literature_digest(producer_body):
        errors.append("literature_producer_receipt_digest_mismatch")
    return list(dict.fromkeys(errors))


def _json_without_duplicate_keys(raw: bytes):
    def _object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    return json.loads(raw.decode("utf-8"), object_pairs_hook=_object)


def _open_directory_nofollow(path: Path) -> int:
    absolute = Path(path).absolute()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(os.sep, flags)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_regular_single_link_json(path: Path):
    directory_fd = None
    descriptor = None
    try:
        directory_fd = _open_directory_nofollow(path.parent)
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            return None
        if before.st_size < 2 or before.st_size > _LITERATURE_PROBE_CACHE_MAX_BYTES:
            return None
        chunks = []
        remaining = _LITERATURE_PROBE_CACHE_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if (
            before_identity != after_identity
            or after.st_nlink != 1
            or len(raw) != before.st_size
        ):
            return None
        return _json_without_duplicate_keys(raw)
    except (FileNotFoundError, NotADirectoryError, OSError, UnicodeError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_fd is not None:
            os.close(directory_fd)


def _write_regular_single_link_json(path: Path, value: dict) -> None:
    encoded = (_literature_canonical_json(value) + "\n").encode("utf-8")
    if len(encoded) > _LITERATURE_PROBE_CACHE_MAX_BYTES:
        raise ValueError("literature cache exceeds bounded size")
    path.parent.mkdir(parents=True, exist_ok=True)
    directory_fd = _open_directory_nofollow(path.parent)
    temporary_name = f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    descriptor = None
    try:
        try:
            existing = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
        ):
            raise OSError("literature cache target is not a single-link regular file")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _read_literature_probe_cache(
    next_v: int | str,
    *,
    source_v: int | str | None = None,
    h2h_weakness: str = "",
    stagnation_info: str = "",
    receipt_binding: dict | None = None,
    checkpoint: dict | None = None,
) -> dict | None:
    path = _literature_probe_cache_path(next_v)
    envelope = _read_regular_single_link_json(path)
    envelope_fields = {
        "schema",
        "checkpoint_identity",
        "payload",
        "payload_digest",
        "producer_receipt_digest",
        "cache_digest",
    }
    if not isinstance(envelope, dict) or set(envelope) != envelope_fields:
        return None
    if envelope.get("schema") != _LITERATURE_PROBE_CACHE_SCHEMA:
        return None
    cache_body = {
        key: deepcopy(value)
        for key, value in envelope.items()
        if key != "cache_digest"
    }
    if envelope.get("cache_digest") != _literature_digest(cache_body):
        return None
    data = envelope.get("payload")
    if not isinstance(data, dict):
        return None
    producer = data.get("producer_receipt") or {}
    checkpoint_binding = producer.get("checkpoint_binding") or {}
    if envelope.get("checkpoint_identity") != checkpoint_binding.get(
        "checkpoint_identity"
    ):
        return None
    if envelope.get("payload_digest") != data.get("canonical_payload_digest"):
        return None
    if envelope.get("producer_receipt_digest") != producer.get("receipt_digest"):
        return None
    if source_v is not None and data.get("source_v") != int(source_v):
        return None
    if data.get("next_v") != int(next_v):
        return None
    if data.get("weakness") != str(h2h_weakness or ""):
        return None
    if data.get("stagnation_info") != str(stagnation_info or ""):
        return None
    if _literature_probe_payload_errors(
        data,
        checkpoint=checkpoint,
        receipt_binding=receipt_binding,
        require_origin_checkpoint=True,
    ):
        return None
    return deepcopy(data)


def _normalize_literature_probe_result(
    data: dict,
    next_v: int | str,
    *,
    checkpoint: dict | None = None,
    receipt_binding: dict | None = None,
    cached: str = "",
) -> dict | None:
    if not isinstance(data, dict) or data.get("next_v") != int(next_v):
        return None
    if _literature_probe_payload_errors(
        data,
        checkpoint=checkpoint,
        receipt_binding=receipt_binding,
        require_origin_checkpoint=False,
    ):
        return None
    result = deepcopy(data)
    if cached:
        result["cached"] = True
        result["cache_source"] = cached
    return result


def _read_literature_probe_checkpoint(
    next_v: int | str,
    *,
    source_v: int | str | None = None,
    h2h_weakness: str = "",
    stagnation_info: str = "",
    receipt_binding: dict | None = None,
) -> dict | None:
    """Return this generation's already-completed literature probe from checkpoint.

    The checkpoint is generation-authoritative.  Mandatory callers pass the
    digest-bound receipt context, so an older result is reusable only when the
    current Master/Audit requirement is identical; a changed requirement must
    get a fresh governed outcome instead of silently reusing stale research.
    """
    try:
        from evolution_infra import read_pipeline_checkpoint
        ckpt = read_pipeline_checkpoint() or {}
    except Exception:
        return None
    try:
        if int(ckpt.get("next_v")) != int(next_v):
            return None
        if source_v is not None and int(ckpt.get("source_v")) != int(source_v):
            return None
    except (TypeError, ValueError):
        return None
    payload = ckpt.get("literature_probe")
    result = _normalize_literature_probe_result(
        payload,
        next_v,
        checkpoint=ckpt,
        receipt_binding=receipt_binding,
        cached="checkpoint",
    )
    if not result:
        return None
    if result.get("weakness") != str(h2h_weakness or ""):
        return None
    if result.get("stagnation_info") != str(stagnation_info or ""):
        return None
    return result


def _persist_literature_probe_result(
    next_v: int | str,
    source_v: int | str | None,
    payload: dict,
    *,
    receipt_binding: dict | None = None,
) -> bool:
    """Persist only an outcome still owned by the mandatory probe route.

    The web/LLM request can finish after a weak controller has moved the
    checkpoint.  Re-read and verify the exact route before writing so a late
    result cannot overwrite a later stage or revive an obsolete receipt.
    """
    try:
        from evolution_infra import read_pipeline_checkpoint
        ckpt = read_pipeline_checkpoint() or {}
        if int(ckpt.get("next_v")) != int(next_v):
            return False
        if source_v is not None and int(ckpt.get("source_v")) != int(source_v):
            return False
        if ckpt.get("stage") != "direction_audited":
            return False
        from pipeline_state import (
            literature_probe_receipt_binding,
            literature_probe_required,
        )

        if not literature_probe_required(ckpt):
            return False
        current_binding, binding_errors = literature_probe_receipt_binding(ckpt)
        if binding_errors or current_binding is None:
            return False
        expected_binding = receipt_binding or current_binding
        if current_binding != expected_binding:
            return False
        if _literature_probe_payload_errors(
            payload,
            checkpoint=ckpt,
            receipt_binding=expected_binding,
            require_origin_checkpoint=True,
        ):
            return False
        workflow_run_id = str(ckpt.get("workflow_run_id") or "")
        checkpoint_revision = int(ckpt.get("checkpoint_revision") or 0)
        return bool(write_pipeline_checkpoint(
            int(next_v),
            int(ckpt.get("source_v") if source_v is None else source_v),
            "direction_audited",
            literature_probe=payload,
            expected_checkpoint_revision=checkpoint_revision,
            expected_checkpoint_stage="direction_audited",
            expected_workflow_run_id=workflow_run_id,
        ))
    except Exception:
        return False


def _write_literature_probe_cache(
    next_v: int | str,
    payload: dict,
    *,
    checkpoint: dict | None = None,
    receipt_binding: dict | None = None,
) -> dict:
    errors = _literature_probe_payload_errors(
        payload,
        checkpoint=checkpoint,
        receipt_binding=receipt_binding,
        require_origin_checkpoint=True,
    )
    if errors:
        raise ValueError("invalid literature cache payload: " + ",".join(errors[:8]))
    path = _literature_probe_cache_path(next_v)
    producer = payload["producer_receipt"]
    envelope = {
        "schema": _LITERATURE_PROBE_CACHE_SCHEMA,
        "checkpoint_identity": producer["checkpoint_binding"][
            "checkpoint_identity"
        ],
        "payload": deepcopy(payload),
        "payload_digest": payload["canonical_payload_digest"],
        "producer_receipt_digest": producer["receipt_digest"],
    }
    envelope["cache_digest"] = _literature_digest(envelope)
    _write_regular_single_link_json(path, envelope)
    return deepcopy(payload)


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
        max_attempts=3,
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
                "modules, and assets are not Worker targets."
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
        from agent_master import MasterInfrastructureError
        from strict_authority_workflow import StrictAuthorityError

        if isinstance(exc, StrictAuthorityError):
            return await _abandon_strict_master_authority(
                next_v,
                source_v,
                error=exc,
                ui=ui,
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
                    "pipeline.master_plan_compiled",
                    "info",
                    f"Master plan v{next_v}: compiled {len(_compile_meta.get('compiled_tasks', []))} oversized worker prompt(s)",
                    {"next_v": next_v, "source_v": source_v, "phase": phase, "compiler": _compile_meta},
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
                from agent_master import MasterInfrastructureError
                from strict_authority_workflow import StrictAuthorityError

                if isinstance(exc, StrictAuthorityError):
                    return await _abandon_strict_master_authority(
                        next_v,
                        source_v,
                        error=exc,
                        ui=ui,
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
    """DeepEvolve plan→search→reflect→write loop with Ratchet governance.

    Triggered by the orchestrator when stagnation ≥ 2 gens or direction-audit
    flags repetition. Uses web search (Exa, connected MCP) to find concrete,
    codable strategy improvements for the current bot's biggest H2H weakness.
    The output is a hypothesis pool entry (research_governance.add_candidate),
    NOT a direct code edit — run_master may surface it to workers as a hypothesis.
    """
    import asyncio as _asyncio
    _t0 = time.time()
    source_v = args.get("source_v")
    next_v = args.get("next_v")
    if next_v is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Missing next_v"})}]}
    h2h_weakness = args.get("h2h_weakness", "") or ""
    stagnation_info = args.get("stagnation_info", "") or ""

    probe_checkpoint = _matching_checkpoint(next_v, source_v)
    from pipeline_state import (
        literature_probe_receipt_binding,
        literature_probe_required,
    )

    if not isinstance(probe_checkpoint, dict):
        return _json_tool_result({
            "error": "LITERATURE_PROBE_CHECKPOINT_REQUIRED",
            "next_v": next_v,
            "source_v": source_v,
            "directive": (
                "Literature research may only run from the active mandatory "
                "direction_audited checkpoint."
            ),
        })
    if probe_checkpoint.get("stage") != "direction_audited":
        route = route_policy(probe_checkpoint)
        return _json_tool_result({
            "error": "LITERATURE_PROBE_WRONG_STAGE",
            "next_v": next_v,
            "source_v": source_v,
            "checkpoint_stage": probe_checkpoint.get("stage"),
            "expected_stage": "direction_audited",
            "next_tool": route.get("next_tool"),
            "allowed_tools": route.get("allowed_tools"),
            "directive": (
                "Do not run literature research outside direction_audited or "
                "overwrite a later checkpoint."
            ),
        })
    if not literature_probe_required(probe_checkpoint):
        return _json_tool_result({
            "error": "LITERATURE_PROBE_NOT_REQUIRED",
            "next_v": next_v,
            "source_v": source_v,
            "next_tool": "run_master",
            "directive": (
                "The canonical stagnation/direction evidence does not require "
                "literature research for this generation."
            ),
        })
    receipt_binding, binding_errors = literature_probe_receipt_binding(
        probe_checkpoint
    )
    if binding_errors or receipt_binding is None:
        return _json_tool_result({
            "error": "LITERATURE_PROBE_REQUIREMENT_CONTEXT_INVALID",
            "next_v": next_v,
            "source_v": source_v,
            "validation_errors": binding_errors,
            "directive": (
                "Repair or restart the scheduler-owned Master context and "
                "Direction Audit; do not research caller-reconstructed text."
            ),
        })
    # Research queries use scheduler/auditor-owned evidence, not an outer-model
    # paraphrase.  The binding above has already verified this exact context.
    master_context = (
        (probe_checkpoint.get("audit_context") or {}).get("master_context")
    )
    canonical_stagnation = str(master_context.get("stagnation_info") or "")
    direction_audit = probe_checkpoint.get("direction_audit") or {}
    canonical_weakness = str(
        direction_audit.get("suggested_direction")
        or master_context.get("match_analysis")
        or master_context.get("performance_verification")
        or ""
    )
    mismatched_fields = []
    if stagnation_info and stagnation_info != canonical_stagnation:
        mismatched_fields.append("stagnation_info")
    if h2h_weakness and canonical_weakness and h2h_weakness != canonical_weakness:
        mismatched_fields.append("h2h_weakness")
    stagnation_info = canonical_stagnation
    if canonical_weakness:
        h2h_weakness = canonical_weakness
    if mismatched_fields:
        log_system_event(
            "pipeline.literature_probe_caller_context_ignored",
            "warn",
            f"Ignored caller-reconstructed literature context for v{next_v}",
            {
                "next_v": next_v,
                "source_v": source_v,
                "mismatched_fields": mismatched_fields,
                "master_context_digest": master_context.get("context_digest"),
            },
        )

    checkpoint_probe = _read_literature_probe_checkpoint(
        next_v,
        source_v=source_v,
        h2h_weakness=h2h_weakness,
        stagnation_info=stagnation_info,
        receipt_binding=receipt_binding,
    )
    if checkpoint_probe:
        try:
            event_type = "pipeline.literature_probe_checkpoint_cached"
            log_system_event(
                event_type,
                "info",
                f"literature_probe v{next_v}: using checkpoint result",
                {"next_v": next_v, "source_v": checkpoint_probe.get("source_v"),
                 "reason": checkpoint_probe.get("reason"),
                 "candidate_id": checkpoint_probe.get("candidate_id"),
                 "context_mismatch_reused": checkpoint_probe.get("context_mismatch_reused", False)},
            )
        except Exception:
            pass
        return _json_tool_result(checkpoint_probe)
    if probe_checkpoint.get("literature_probe") is not None:
        return _json_tool_result({
            "error": "LITERATURE_PROBE_RECEIPT_INVALID",
            "next_v": next_v,
            "source_v": source_v,
            "next_tool": "abandon_generation",
            "directive": (
                "The checkpoint contains a literature outcome that is not an "
                "exact schema-v2 terminal producer receipt. Do not overwrite or "
                "inject it; use governed abandon/reprepare."
            ),
        })

    cached_probe = _read_literature_probe_cache(
        next_v,
        source_v=source_v,
        h2h_weakness=h2h_weakness,
        stagnation_info=stagnation_info,
        receipt_binding=receipt_binding,
        checkpoint=probe_checkpoint,
    )
    if cached_probe:
        try:
            log_system_event(
                "pipeline.literature_probe_cached",
                "info",
                f"literature_probe v{next_v}: using cached result",
                {"next_v": next_v, "source_v": cached_probe.get("source_v"),
                 "reason": cached_probe.get("reason"),
                 "candidate_id": cached_probe.get("candidate_id")},
            )
        except Exception:
            pass
        if _persist_literature_probe_result(
            next_v,
            source_v,
            cached_probe,
            receipt_binding=receipt_binding,
        ):
            returned = deepcopy(cached_probe)
            returned["cached"] = True
            returned["cache_source"] = "terminal_receipt"
            return _json_tool_result(returned)
        return _json_tool_result(_literature_probe_stale_result(next_v, source_v))

    # ── A6 governance gate: cooldown / blacklist / kill-switch ──
    try:
        from research_governance import should_trigger_web_retrieval
        if not should_trigger_web_retrieval(next_v):
            try:
                log_system_event("research_governance.skipped", "info",
                                 f"run_literature_probe skipped for v{next_v} (cooldown/disabled)",
                                 {"next_v": next_v})
            except Exception:
                pass
            payload = {
                "reason": "governed_skip",
                "next_v": next_v,
                "source_v": source_v,
                "weakness": h2h_weakness,
                "stagnation_info": stagnation_info,
            }
            payload = _build_literature_probe_payload(
                payload,
                checkpoint=probe_checkpoint,
                receipt_binding=receipt_binding,
            )
            try:
                _write_literature_probe_cache(
                    next_v,
                    payload,
                    checkpoint=probe_checkpoint,
                    receipt_binding=receipt_binding,
                )
            except Exception:
                pass
            if not _persist_literature_probe_result(
                next_v,
                source_v,
                payload,
                receipt_binding=receipt_binding,
            ):
                return _json_tool_result(
                    _literature_probe_stale_result(next_v, source_v)
                )
            return _json_tool_result(payload)
    except Exception as e:
        return {"content": [{"type": "text", "text": json.dumps({"error": f"governance gate failed: {e}"})}]}

    ui = _get_ui()
    rendered_prompt = None
    output = None
    try:
        from llm_query import run_claude_query
        from evolution_infra import get_logs_dir
        from research_governance import add_candidate
    except Exception as e:
        return {"content": [{"type": "text", "text": json.dumps({"error": f"import failed: {e}"})}]}

    log_dir = get_logs_dir(next_v)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    probe_log = log_dir / "literature_probe_io.txt"

    # ── Single research agent run (plan/search/reflect/write in one query, with web tools) ──
    # The agent has web search (Exa MCP, connected) + WebSearch. Domain whitelist is in the prompt.
    try:
        log_system_event("pipeline.literature_probe_start", "info",
                         f"literature_probe v{next_v}: research query starting",
                         {"next_v": next_v, "source_v": source_v,
                          "timeout_s": LITERATURE_PROBE_TIMEOUT,
                          "log_file": str(probe_log)})
    except Exception:
        pass
    try:
        ui.clear_io()
        literature_role = f"LITERATURE_PROBE (v{next_v})"
        rendered_prompt = _issue_literature_rendered_prompt(
            next_v=int(next_v),
            source_v=int(source_v),
            weakness=str(h2h_weakness or ""),
            stagnation_info=str(stagnation_info or ""),
        )
        output, _, _ = await _asyncio.wait_for(
            run_claude_query(
                rendered_prompt, [], ui,
                literature_role, probe_log,
                tools=["WebSearch"],  # built-in; Exa MCP auto-available (not in _BLOCKED_MCP_TOOLS)
            ),
            timeout=LITERATURE_PROBE_TIMEOUT,
        )
    except LLMAvailabilityBlocked:
        # A provider stop cannot satisfy the scheduler-owned literature receipt.
        # Leave the checkpoint/revision untouched for exact resume.
        raise
    except _asyncio.TimeoutError:
        elapsed = round(time.time() - _t0, 1)
        try:
            log_system_event("pipeline.literature_probe_timeout", "warn",
                             f"literature_probe v{next_v}: timed out after {LITERATURE_PROBE_TIMEOUT}s; continuing without web hypothesis",
                             {"next_v": next_v, "source_v": source_v,
                              "timeout_s": LITERATURE_PROBE_TIMEOUT,
                              "elapsed_sec": elapsed,
                              "log_file": str(probe_log)})
        except Exception:
            pass
        payload = {
            "reason": "literature_probe_timeout",
            "next_v": next_v,
            "source_v": source_v,
            "weakness": h2h_weakness,
            "stagnation_info": stagnation_info,
            "elapsed_sec": elapsed,
            "timeout_s": LITERATURE_PROBE_TIMEOUT,
        }
        payload = _build_literature_probe_payload(
            payload,
            checkpoint=probe_checkpoint,
            receipt_binding=receipt_binding,
            rendered_prompt=rendered_prompt,
        )
        try:
            _write_literature_probe_cache(
                next_v,
                payload,
                checkpoint=probe_checkpoint,
                receipt_binding=receipt_binding,
            )
        except Exception:
            pass
        if not _persist_literature_probe_result(
            next_v,
            source_v,
            payload,
            receipt_binding=receipt_binding,
        ):
            return _json_tool_result(_literature_probe_stale_result(next_v, source_v))
        return _json_tool_result(payload)
    except Exception as e:
        try:
            log_system_event("pipeline.literature_probe_failed", "warn",
                             f"literature_probe v{next_v}: research query failed: {str(e)[:180]}",
                             {"next_v": next_v, "source_v": source_v,
                              "elapsed_sec": round(time.time() - _t0, 1),
                              "exception_type": type(e).__name__,
                              "error": str(e)[:1000],
                              "log_file": str(probe_log)})
        except Exception:
            pass
        if rendered_prompt is None:
            return _json_tool_result({
                "error": "LITERATURE_PROBE_PRODUCER_RECEIPT_UNAVAILABLE",
                "next_v": next_v,
                "source_v": source_v,
                "detail": f"{type(e).__name__}: {str(e)[:500]}",
                "directive": (
                    "The system could not issue the typed LLM dispatch receipt. "
                    "No terminal attempt or cache was persisted."
                ),
            })
        payload = {
            "reason": "literature_probe_failed",
            "next_v": next_v,
            "source_v": source_v,
            "weakness": h2h_weakness,
            "stagnation_info": stagnation_info,
            "elapsed_sec": round(time.time() - _t0, 1),
            "error": str(e)[:1000],
        }
        payload = _build_literature_probe_payload(
            payload,
            checkpoint=probe_checkpoint,
            receipt_binding=receipt_binding,
            rendered_prompt=rendered_prompt,
        )
        try:
            _write_literature_probe_cache(
                next_v,
                payload,
                checkpoint=probe_checkpoint,
                receipt_binding=receipt_binding,
            )
        except Exception:
            pass
        if not _persist_literature_probe_result(
            next_v,
            source_v,
            payload,
            receipt_binding=receipt_binding,
        ):
            return _json_tool_result(_literature_probe_stale_result(next_v, source_v))
        return _json_tool_result(payload)

    # ── Parse the WRITE-step proposal ──
    try:
        from llm_query import parse_json_output_with_mode
        data, _fm = parse_json_output_with_mode(output)
    except Exception:
        data = None

    proposal = _normalize_literature_proposal(data)
    candidate_id = None
    if proposal is not None:
        # A6 translation_gate + cap + blacklist enforced inside add_candidate
        checkpoint_binding = _literature_checkpoint_binding(
            probe_checkpoint,
            receipt_binding,
        )
        submitted_candidate_id = _expected_literature_candidate_id(
            proposal,
            checkpoint_identity=checkpoint_binding["checkpoint_identity"],
            terminal_output_sha256=hashlib.sha256(
                output.encode("utf-8")
            ).hexdigest(),
        )
        governance_result = add_candidate(
            _literature_candidate_submission(
                proposal,
                int(next_v),
                submitted_candidate_id,
            )
        )
        candidate_id = (
            governance_result
            if governance_result == submitted_candidate_id
            else None
        )

    # ── Persist the proposal + return text for master_prompt injection ──
    _payload = _build_literature_probe_payload({
        "next_v": next_v,
        "source_v": source_v,
        "weakness": h2h_weakness,
        "stagnation_info": stagnation_info,
        "proposal": proposal,
        "candidate_id": candidate_id,
        "elapsed_sec": round(time.time() - _t0, 1),
        "reason": "completed",
    }, checkpoint=probe_checkpoint, receipt_binding=receipt_binding,
        rendered_prompt=rendered_prompt, terminal_output=output)
    try:
        _write_literature_probe_cache(
            next_v,
            _payload,
            checkpoint=probe_checkpoint,
            receipt_binding=receipt_binding,
        )
    except Exception:
        pass
    if not _persist_literature_probe_result(
        next_v,
        source_v,
        _payload,
        receipt_binding=receipt_binding,
    ):
        return _json_tool_result(_literature_probe_stale_result(next_v, source_v))

    try:
        log_system_event("pipeline.literature_probe", "info",
                         f"literature_probe v{next_v}: candidate_id={candidate_id} gated_out={_payload['gated_out']}",
                         {"next_v": next_v, "candidate_id": candidate_id,
                          "target_fn": (proposal or {}).get("target_fn", "")})
    except Exception:
        pass

    # Text returned to the orchestrator is the exact bound receipt.
    return _json_tool_result(_payload)


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


def _full_reset_next_dir(next_dir, source_dir):
    """Restore an invalid-policy candidate exactly from its authoritative source."""
    from evolution_infra import copy_bot_tree_for_candidate

    next_dir = Path(next_dir)
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        raise FileNotFoundError(f"source bot directory missing: {source_dir}")
    if next_dir.exists():
        shutil.rmtree(next_dir)
    copy_bot_tree_for_candidate(source_dir, next_dir)


def _checkpoint_architecture_policy_identity_errors(ckpt):
    if not isinstance(ckpt, dict):
        return []
    quality = (ckpt.get("gate_results") or {}).get("quality") or {}
    transition = quality.get("national_architecture_transition") or {}
    if not isinstance(transition, dict):
        return []
    return [str(item) for item in transition.get("policy_identity_errors") or [] if str(item)]


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
    ledger_digest = _checkpoint_runtime_contract_ledger_digest(ckpt)
    _full_reset_next_dir(next_dir, source_dir)
    existing_audit = ckpt.get("audit_context") or {}
    audit_context = {
        **(existing_audit if isinstance(existing_audit, dict) else {}),
        "architecture_policy_identity_replan": {
            "source_stage": ckpt.get("stage"),
            "identity_errors": errors,
            "candidate_reset_to_source": True,
            "runtime_contract_ledger_reset": True,
            "previous_runtime_contract_ledger_digest": ledger_digest,
            "directive": (
                "The persisted architecture policy no longer matches the source contract. "
                "Build a fresh system-owned policy and Master plan before editing bot code."
            ),
        },
    }
    written = write_pipeline_checkpoint(
        next_v,
        source_v,
        "direction_audited",
        master_plan={},
        direction_audit=ckpt.get("direction_audit"),
        audit_context=audit_context,
        worker_failure_count=ckpt.get("worker_failure_count", 0),
        clear_reviewer_feedback=True,
        touch_stage_timestamp=True,
        reset_runtime_contract_ledger=True,
        expected_runtime_contract_ledger_digest=ledger_digest,
        runtime_contract_ledger_reset_reason="architecture_policy_identity_replan",
    )
    if not written:
        raise RuntimeError("checkpoint rejected architecture policy identity replan")
    log_system_event(
        "pipeline.architecture_policy_identity_replan",
        "error",
        f"Reset v{next_v} to source v{source_v}; stale architecture policy requires re-planning",
        {
            "next_v": next_v,
            "source_v": source_v,
            "source_stage": ckpt.get("stage"),
            "identity_errors": errors,
        },
    )
    return _json_tool_result({
        "error": "ARCHITECTURE_POLICY_IDENTITY_REPLAN",
        "next_v": next_v,
        "source_v": source_v,
        "identity_errors": errors,
        "candidate_reset_to_source": True,
        "next_tool": "run_master",
        "directive": (
            "The stale architecture policy cannot be repaired by a bot worker. "
            "The candidate was reset to its source and the checkpoint moved to "
            "direction_audited. Call run_master to build a fresh policy-bound plan."
        ),
    })


def _checkpoint_plan_with_tasks(ckpt, tasks, replace_existing_tasks=False):
    """Return a checkpoint master_plan that can resume the given worker tasks."""
    existing_plan = ckpt.get("master_plan") if ckpt else None
    if isinstance(existing_plan, dict):
        if existing_plan.get("tasks") and not replace_existing_tasks:
            return existing_plan
        plan = {**existing_plan, "tasks": tasks}
    else:
        plan = {"tasks": tasks}
    try:
        from runtime_architecture_policy import attach_runtime_contract_ledger

        return attach_runtime_contract_ledger(plan)
    except Exception:
        # Keep the original ledger intact. Quality validation will fail closed
        # with its precise integrity error rather than silently replacing it.
        return plan


def _task_declared_scope_files(task, next_v):
    files = set()
    if not isinstance(task, dict):
        return files
    for key in ("target_files", "files_allowed"):
        for target in task.get(key, []) or []:
            rel = _target_rel(target, next_v)
            if rel:
                files.add(rel)
    return files


def _task_write_scope_errors(tasks, next_v):
    """Keep completion requirements from silently becoming write authority."""
    errors = []
    for index, task in enumerate(tasks or []):
        if not isinstance(task, dict):
            errors.append(f"task[{index}]_not_object")
            continue
        writable = _task_declared_scope_files(task, next_v)
        must_change = set()
        for target in task.get("must_change_files", []) or []:
            rel = _target_rel(target, next_v)
            if not rel:
                errors.append(f"task[{index}]_must_change_path_invalid:{target}")
            else:
                must_change.add(rel)
        unauthorized = sorted(must_change - writable)
        if unauthorized:
            errors.append(
                f"task[{index}]_must_change_outside_writable_scope:{unauthorized}"
            )
    return errors


def _plan_repair_scope_files(plan, next_v):
    files = set()
    if not isinstance(plan, dict):
        return files
    raw_scope = plan.get("repair_scope_files", []) or []
    if not isinstance(raw_scope, list):
        raw_scope = []
    for item in raw_scope:
        rel = _target_rel(item, next_v)
        if rel:
            files.add(rel)
    raw_tasks = plan.get("tasks", []) or []
    if not isinstance(raw_tasks, list):
        raw_tasks = []
    for task in raw_tasks:
        files.update(_task_declared_scope_files(task, next_v))
    return files


def _plan_with_accumulated_repair_scope(ckpt, plan, tasks, next_v):
    """Preserve final declared-scope coverage across in-place repair rounds.

    Rework execution may refresh ``tasks`` to only the newest blocker, but the
    repair edits are cumulative. Store only files already authorized by a
    Master/repair task or the immutable repair ledger; observed diffs are
    evidence, never authority. In particular, a crossover's Parent-A→child
    preparation diff must not auto-authorize a later Worker edit.
    """
    if not isinstance(plan, dict):
        return plan
    existing_plan = ckpt.get("master_plan") if isinstance(ckpt, dict) else {}
    scope = set()
    scope.update(_plan_repair_scope_files(existing_plan, next_v))
    scope.update(_plan_repair_scope_files(plan, next_v))
    for task in tasks or []:
        scope.update(_task_declared_scope_files(task, next_v))
    if not scope:
        return plan
    return {**plan, "repair_scope_files": sorted(scope)}


def _task_matches_quality_blocker(task, blocker):
    if str(task.get("repair_blocker") or "") == blocker:
        return True
    if blocker == "size" and str(task.get("repair_blocker") or "") == "file_size":
        return True
    text = " ".join([
        str(task.get("worker_id", "")),
        str(task.get("role", "")),
        str(task.get("repair_blocker", "")),
        " ".join(str(x) for x in task.get("target_files", []) or []),
        str(task.get("worker_prompt", task.get("instruction", ""))),
    ]).lower()
    if blocker == "size":
        return (
            "file_size" in text
            or "line_count" in text
            or "line count" in text
            or "loc limit" in text
            or "oversized" in text
            or "wc -l" in text
            or re.search(r"\bsize\b", text) is not None
            or re.search(r"\d+L/\d+L", text) is not None
        )
    if blocker == "position_semantics":
        return any(marker in text for marker in ("position_semantics", "dealer", "small blind", "big blind", "sb", "bb"))
    return False


def _task_quality_recheck_blockers(task):
    """Return cheap static quality blockers this task is trying to repair.

    Generic ``quality_gate`` tasks are only skippable when their evidence maps to
    a checker we can rerun cheaply. Compile, smoke, decision, and national
    acceptance repairs still run because this callback is intentionally not a
    replacement for the full quality gate.
    """
    if not isinstance(task, dict):
        return set()
    contract = task.get("repair_contract") if isinstance(task.get("repair_contract"), dict) else {}
    blocker = _normalize_repair_blocker(contract.get("blocker") or task.get("repair_blocker"))
    text = " ".join([
        str(task.get("worker_id", "")),
        str(task.get("role", "")),
        str(task.get("repair_blocker", "")),
        str(contract.get("blocker", "")),
        str(contract.get("evidence", "")),
        " ".join(str(x) for x in task.get("target_files", []) or []),
        str(task.get("worker_prompt", task.get("instruction", ""))),
    ]).lower()

    blockers = set()
    if blocker == "file_size" or _task_matches_quality_blocker(task, "size"):
        blockers.add("file_size")
    if blocker == "position_semantics" or _task_matches_quality_blocker(task, "position_semantics"):
        blockers.add("position_semantics")
    if blocker == "national_native_contract" or _is_national_native_contract_failure_text(text):
        blockers.add("national_native_contract")
    if blocker == "runtime_architecture" or "architecture_focus" in text or "architecture_regression" in text:
        blockers.add("runtime_architecture")
    if (
        "protected_contract" in text
        or "tcp action text" in text
        or "output must be json response int" in text
    ):
        blockers.add("protected_contract")
    if "reachability" in text:
        blockers.add("reachability")
    return blockers


def _normalize_repair_blocker(value):
    text = str(value or "").strip().lower()
    if text in {"size", "file_size", "line_count", "loc"}:
        return "file_size"
    if text in {"position", "position_semantics"}:
        return "position_semantics"
    if text in {"national_native", "national_native_contract", "native_tcp_contract"}:
        return "national_native_contract"
    if text in {"official_smoke", "official_platform", "official_platform_compliance"}:
        return "official_smoke"
    if text in {
        "runtime_architecture",
        "architecture_focus",
        "architecture_regression",
        "national_capability_contract",
    }:
        return "runtime_architecture"
    if text in {"quality", "quality_gate", "protected_contract", "compile", "smoke_test"}:
        return "quality_gate"
    return text


def _task_target_filenames(tasks):
    files = set()
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        for target in task.get("target_files", []) or []:
            name = Path(str(target)).name
            if name:
                files.add(name)
    return files


def _quality_contract_signature(contract):
    if not isinstance(contract, dict):
        return ("", "")
    blocker = _normalize_repair_blocker(contract.get("blocker"))
    filename = Path(str(contract.get("file", ""))).name
    return (blocker, filename) if blocker and filename else ("", "")


def _quality_contract_signatures(ckpt, reviewer_feedback=""):
    return {
        signature
        for signature in (
            _quality_contract_signature(contract)
            for contract in _quality_repair_contracts(ckpt, reviewer_feedback)
        )
        if all(signature)
    }


def _task_quality_contract_signatures(tasks):
    signatures = set()
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        files = _task_must_change_filenames(task)
        contract = task.get("repair_contract") if isinstance(task.get("repair_contract"), dict) else {}
        blocker = _normalize_repair_blocker(
            contract.get("blocker")
            or task.get("repair_blocker")
        )
        contract_file = Path(str(contract.get("file", ""))).name
        if contract_file:
            files.add(contract_file)
        if blocker:
            for filename in files:
                signatures.add((blocker, filename))
            continue
        if _task_matches_quality_blocker(task, "size"):
            for filename in files:
                signatures.add(("file_size", filename))
        if _task_matches_quality_blocker(task, "position_semantics"):
            for filename in files:
                signatures.add(("position_semantics", filename))
        text = " ".join([
            str(task.get("worker_id", "")),
            str(task.get("role", "")),
            str(task.get("task_kind", "")),
            str(task.get("worker_prompt", task.get("instruction", ""))),
        ]).lower()
        if "quality_gate" in text or "protected_contract" in text:
            for filename in files:
                signatures.add(("quality_gate", filename))
    return signatures


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _quality_task_contract_refresh_reason(task, current_contract):
    """Return why a saved quality repair task should be regenerated."""
    if not isinstance(task, dict) or not isinstance(current_contract, dict):
        return ""
    signature = _quality_contract_signature(current_contract)
    blocker, filename = signature
    if blocker == "runtime_architecture":
        expected_focus = str(current_contract.get("focus_id") or "")
        if str(task.get("architecture_focus_id") or "") != expected_focus:
            return f"{blocker}:{filename}:architecture_focus_changed"
        expected_layer = str(current_contract.get("skill_layer") or "")
        if str(task.get("skill_layer") or "") != expected_layer:
            return f"{blocker}:{filename}:skill_layer_changed"
        if task.get("runtime_contract") != current_contract.get("runtime_contract"):
            return f"{blocker}:{filename}:runtime_contract_changed"
        expected_checks = [str(item) for item in current_contract.get("required_checks") or []]
        actual_checks = [str(item) for item in task.get("checks_required") or []]
        if actual_checks != expected_checks:
            return f"{blocker}:{filename}:required_checks_changed"
        expected_targets = {
            Path(str(item)).name for item in current_contract.get("files") or [filename]
        }
        actual_targets = {
            Path(str(item)).name for item in task.get("target_files") or []
        }
        if actual_targets != expected_targets:
            return f"{blocker}:{filename}:target_files_changed"
        return ""
    if blocker != "file_size":
        return ""

    saved = task.get("repair_contract") if isinstance(task.get("repair_contract"), dict) else {}
    saved_current = _int_or_none(saved.get("current_lines"))
    saved_limit = _int_or_none(saved.get("line_limit"))
    current_lines = _int_or_none(current_contract.get("current_lines"))
    line_limit = _int_or_none(current_contract.get("line_limit"))

    if line_limit is not None and saved_limit != line_limit:
        return f"{blocker}:{filename}:line_limit_changed"
    if current_lines is not None and saved_current != current_lines:
        return f"{blocker}:{filename}:current_lines_changed"

    prompt = str(task.get("worker_prompt", task.get("instruction", "")))
    if (
        current_lines is not None
        and line_limit is not None
        and current_lines - line_limit >= 200
        and "Large-overage requirement" not in prompt
    ):
        return f"{blocker}:{filename}:large_overage_prompt_outdated"
    return ""


def _is_file_size_repair_task(task):
    if not isinstance(task, dict):
        return False
    contract = task.get("repair_contract") if isinstance(task.get("repair_contract"), dict) else {}
    blocker = _normalize_repair_blocker(
        contract.get("blocker")
        or task.get("repair_blocker")
    )
    if blocker == "file_size":
        return True
    return _task_matches_quality_blocker(task, "size")


def _order_quality_repair_tasks(tasks):
    """Run semantic/protocol repairs before final file-size cleanup.

    Multiple quality blockers can target the same file. If a file-size cleanup
    runs first, a later semantic repair can add a line or two and re-break the
    size gate. Keep ordering stable except for moving file-size repairs to the
    end of the quality-rework batch.
    """
    indexed = list(enumerate(tasks or []))
    ordered = [
        task for _idx, task in sorted(
            indexed,
            key=lambda item: (1 if _is_file_size_repair_task(item[1]) else 0, item[0]),
        )
    ]
    return ordered


def _stale_quality_task_reason(tasks, ckpt, reviewer_feedback=""):
    """Return a refresh reason when saved quality tasks no longer match gate blockers."""
    if (
        not isinstance(ckpt, dict)
        or ckpt.get("stage") not in {"quality_failed", "repair_planned", "rework_running"}
    ):
        return ""
    current_contracts = {
        signature: contract
        for contract in _quality_repair_contracts(ckpt, reviewer_feedback)
        for signature in [_quality_contract_signature(contract)]
        if all(signature)
    }
    current = set(current_contracts)
    if not current:
        return ""
    task_signatures = _task_quality_contract_signatures(tasks)
    missing = sorted(current - task_signatures)
    extra = sorted(task_signatures - current)
    if extra and reviewer_feedback:
        return "stale current quality repair contract(s): extra stale task(s): " + ", ".join(
            f"{blocker}:{filename}" for blocker, filename in extra
        )
    if not missing:
        stale = []
        for task in tasks or []:
            for signature in sorted(_task_quality_contract_signatures([task]) & current):
                reason = _quality_task_contract_refresh_reason(task, current_contracts[signature])
                if reason:
                    stale.append(reason)
        if not stale:
            return ""
        return "stale current quality repair contract(s): " + ", ".join(sorted(set(stale)))
    return "missing current quality repair contract(s): " + ", ".join(
        f"{blocker}:{filename}" for blocker, filename in missing
    )


def _task_must_change_filenames(task):
    files = set()
    if not isinstance(task, dict):
        return files
    for key in ("must_change_files", "target_files"):
        for target in task.get(key, []) or []:
            name = Path(str(target)).name
            if name:
                files.add(name)
        if files:
            break
    return files


def _quality_failure_target_files(ckpt, reviewer_feedback=""):
    if reviewer_feedback:
        contracts = _quality_repair_contracts(ckpt, reviewer_feedback)
        if contracts:
            return {contract["file"] for contract in contracts if contract.get("file")}
    failures = [
        item for item in _quality_failure_items(ckpt)
        if not _is_declared_scope_failure_text(item)
    ]
    files = _extract_quality_failure_files(failures)
    if not files and reviewer_feedback and not _is_declared_scope_failure_text(reviewer_feedback):
        files = _extract_quality_failure_files([reviewer_feedback])
    return set(files)


def _quality_rework_skipper(
    next_dir,
    source_dir,
    next_v,
    source_v,
    *,
    expected_architecture_policy=None,
    master_plan=None,
):
    """Return a per-task skip callback for cheap quality-repair rechecks.

    Full quality validation remains owned by run_quality_gates. This callback
    only avoids wasting LLM calls for blockers that are already cleared by an
    earlier repair worker in the same rework batch.
    """
    def remaining_blockers():
        blockers = {}
        checked = set()
        try:
            _total, oversized = check_code_size(next_dir, source_dir=source_dir)
            checked.add("file_size")
            if oversized:
                blockers["file_size"] = {Path(name).name for name, _lines, _limit in oversized}
        except Exception:
            pass
        try:
            from tool_gates import detect_position_semantics_errors
            position_errors = detect_position_semantics_errors(next_dir)
            checked.add("position_semantics")
            if position_errors:
                files = _extract_quality_failure_files(position_errors)
                blockers["position_semantics"] = set(files)
        except Exception:
            pass
        try:
            from national_native import check_native_contract
            native_errors = check_native_contract(
                next_dir,
                require_current_stream_decoder=True,
                require_current_decision_runtime=True,
            )
            checked.add("national_native_contract")
            if native_errors:
                files = _extract_quality_failure_files(native_errors)
                blockers["national_native_contract"] = set(files or ["national_bot.py"])
        except Exception:
            pass
        try:
            from code_verification import detect_new_function_reachability_warnings
            changed = _py_files_changed_between(source_dir, next_dir)
            reachability = detect_new_function_reachability_warnings(
                source_dir,
                next_dir,
                changed_files=changed,
            )
            checked.add("reachability")
            if reachability:
                files = _extract_quality_failure_files(reachability)
                blockers["reachability"] = set(files)
        except Exception:
            pass
        try:
            from runtime_architecture_policy import (
                evaluate_architecture_transition,
                validate_runtime_contract_implementation,
            )

            transition = evaluate_architecture_transition(
                source_dir,
                next_dir,
                expected_policy=expected_architecture_policy,
            )
            contract_errors = validate_runtime_contract_implementation(
                master_plan if isinstance(master_plan, dict) else {},
                transition.get("candidate_capabilities") or {},
            )
            transition["runtime_contract_implementation_errors"] = contract_errors
            if contract_errors:
                transition["ok"] = False
            checked.add("runtime_architecture")
            if not transition.get("ok"):
                files = set(_architecture_transition_repair_files(transition, next_dir))
                blockers["runtime_architecture"] = files or {"policy.py"}
        except Exception:
            pass
        return blockers, checked

    def skipper(task):
        blockers, checked = remaining_blockers()
        task_blockers = _task_quality_recheck_blockers(task)
        if not task_blockers:
            return ""
        unchecked = task_blockers - checked
        if unchecked:
            return ""
        if not blockers:
            return "all cheap quality rework blockers already cleared by current code"
        task_files = _task_must_change_filenames(task)
        active_task_blockers = set(task_blockers) & set(blockers)
        if not active_task_blockers:
            return (
                "quality blocker(s) already cleared by current code: "
                + ", ".join(sorted(task_blockers))
            )
        if task_files:
            still_relevant = False
            for blocker in active_task_blockers:
                remaining_files = blockers.get(blocker) or set()
                if not remaining_files or task_files & remaining_files:
                    still_relevant = True
                    break
            if not still_relevant:
                return (
                    "quality blocker file(s) already cleared by current code: "
                    + ", ".join(sorted(task_files))
                )
        return ""

    return skipper


def _checkpoint_master_plan(ckpt):
    if not isinstance(ckpt, dict):
        return {}
    plan = ckpt.get("master_plan")
    return plan if isinstance(plan, dict) else {}


def _canonical_tasks_digest(tasks):
    return hashlib.sha256(
        json.dumps(
            tasks,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _worker_execution_task_digest(
    tasks,
    reviewer_feedback,
    worker_template,
):
    """Identity of every frozen input supplied to one outer Worker batch."""
    return hashlib.sha256(json.dumps({
        "tasks": tasks,
        "reviewer_feedback": reviewer_feedback,
        "worker_template": worker_template,
    }, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _worker_backend_contract():
    return {
        key: os.environ.get(key, "")
        for key in (
            "ANTHROPIC_MODEL",
            "CLAUDE_MODEL",
            "POK_LLM_MODEL",
            "ANTHROPIC_BASE_URL",
        )
    }


def _expected_worker_backend_contract(checkpoint, envelope=None):
    """Return the backend identity selected by the frozen execution policy."""
    policy = (
        (envelope or {}).get("execution_policy")
        if isinstance(envelope, dict)
        else None
    ) or {}
    if policy.get("executor") == "system_policy_bootstrap_v1":
        from system_strict_bootstrap import system_worker_backend_contract

        master_receipt = (
            ((checkpoint or {}).get("audit_context") or {}).get(
                "system_strict_bootstrap"
            )
            or {}
        )
        return system_worker_backend_contract(master_receipt)
    return _worker_backend_contract()


def _frozen_rework_task_authority_errors(ckpt, tasks):
    """Validate persisted repair authority without regenerating prompt prose."""
    errors = _task_write_scope_errors(tasks, ckpt.get("next_v"))
    if not isinstance(tasks, list) or not tasks:
        return [*errors, "checkpoint_frozen_rework_tasks_missing_or_empty"]
    for index, task in enumerate(tasks):
        if not _repair_contract_signature(task, ckpt.get("next_v")):
            worker_id = task.get("worker_id") if isinstance(task, dict) else None
            errors.append(
                f"task[{index}]_repair_contract_signature_invalid:"
                f"{worker_id or 'unknown'}"
            )
    return errors


def _checkpoint_master_task_authority_errors(ckpt, authoritative_tasks):
    """Bind initial worker execution to the accepted checkpoint plan/ledger."""
    if not isinstance(authoritative_tasks, list) or not authoritative_tasks:
        return ["checkpoint_master_plan_tasks_missing_or_empty"]
    scope_errors = _task_write_scope_errors(
        authoritative_tasks,
        ckpt.get("next_v") if isinstance(ckpt, dict) else None,
    )
    if scope_errors:
        return scope_errors
    plan = _checkpoint_master_plan(ckpt)
    plan_ledger = plan.get("runtime_contract_ledger")
    checkpoint_ledger = ckpt.get("runtime_contract_ledger") if isinstance(ckpt, dict) else None
    has_runtime_contract = any(
        isinstance(task, dict) and isinstance(task.get("runtime_contract"), dict)
        for task in authoritative_tasks
    )
    ledger_required = has_runtime_contract or isinstance(plan.get("architecture_policy"), dict)
    if not ledger_required and plan_ledger is None and checkpoint_ledger is None:
        return []

    from runtime_architecture_policy import (
        build_runtime_contract_ledger,
        runtime_contract_ledger_digest,
        validate_runtime_contract_ledger,
    )

    errors = []
    if plan_ledger is None:
        errors.append("master_plan_runtime_contract_ledger_missing")
    else:
        errors.extend(
            f"master_plan:{error}"
            for error in validate_runtime_contract_ledger(plan_ledger)
        )
    if checkpoint_ledger is None:
        errors.append("checkpoint_runtime_contract_ledger_missing")
    else:
        errors.extend(
            f"checkpoint:{error}"
            for error in validate_runtime_contract_ledger(checkpoint_ledger)
        )
    if errors:
        return errors

    plan_digest = runtime_contract_ledger_digest(plan_ledger)
    checkpoint_digest = runtime_contract_ledger_digest(checkpoint_ledger)
    if plan_digest != checkpoint_digest:
        errors.append("checkpoint_master_plan_runtime_contract_ledger_mismatch")
    try:
        rebuilt = build_runtime_contract_ledger({"tasks": authoritative_tasks})
        rebuilt_digest = runtime_contract_ledger_digest(rebuilt)
    except Exception as exc:
        errors.append(
            "master_tasks_runtime_contract_ledger_rebuild_failed:"
            f"{type(exc).__name__}:{str(exc)[:240]}"
        )
    else:
        if rebuilt_digest != plan_digest:
            errors.append("master_tasks_runtime_contract_ledger_mismatch")
    return errors


def _checkpoint_work_item(ckpt):
    plan = _checkpoint_master_plan(ckpt)
    work_item = plan.get("work_item")
    return work_item if isinstance(work_item, dict) else {}


def _is_precommit_rework_checkpoint(ckpt):
    if not isinstance(ckpt, dict):
        return False
    if ckpt.get("stage") == "precommit_failed":
        return True
    gate_results = ckpt.get("gate_results") or {}
    precommit_gate = (
        gate_results.get("precommit_eval")
        if isinstance(gate_results, dict)
        else None
    )
    if isinstance(precommit_gate, dict) and precommit_gate.get("passed") is False:
        # Older checkpoints recorded the precommit receipt while leaving the
        # stage at critic_checked.  Preserve that compatibility only for a
        # measured regression; infrastructure-only failures stay on the
        # precommit owner and Critic advice can never enter this branch.
        from failure_classification import classify_precommit_gate

        if classify_precommit_gate(precommit_gate) in {
            "regression",
            "failed_unknown",
        }:
            return True
    work_item = _checkpoint_work_item(ckpt)
    route = work_item.get("route") if isinstance(work_item.get("route"), dict) else {}
    return (
        work_item.get("kind") == "precommit_repair"
        or work_item.get("source_stage") == "precommit_failed"
        or route.get("intent") == "precommit_rework"
    )


def _is_official_rework_checkpoint(ckpt):
    if not isinstance(ckpt, dict):
        return False
    if ckpt.get("stage") == "official_failed":
        return True
    work_item = _checkpoint_work_item(ckpt)
    route = work_item.get("route") if isinstance(work_item.get("route"), dict) else {}
    return (
        work_item.get("kind") == "official_repair"
        or work_item.get("source_stage") == "official_failed"
        or route.get("intent") == "official_rework"
    )


def _has_legacy_critic_repair_contract(ckpt, tasks=()):
    """Detect retired Critic-owned candidate mutation authority.

    A schema-valid Critic receipt is advisory evidence only.  Historical
    checkpoints/tasks may still carry ``critic_repair`` markers; recognizing
    those markers is solely a fail-closed migration guard and never permission
    to synthesize or execute a Worker task.
    """

    work_item = _checkpoint_work_item(ckpt)
    route = work_item.get("route") if isinstance(work_item.get("route"), dict) else {}
    markers = {
        str(work_item.get("kind") or "").lower(),
        str(work_item.get("repair_blocker") or "").lower(),
        str(route.get("intent") or "").lower(),
    }
    for task in tasks or ():
        if not isinstance(task, dict):
            continue
        markers.update({
            str(task.get("task_kind") or "").lower(),
            str(task.get("repair_blocker") or "").lower(),
            str((task.get("repair_contract") or {}).get("blocker") or "").lower()
            if isinstance(task.get("repair_contract"), dict)
            else "",
        })
    return any(
        "critic_repair" in marker
        or "critic_rework" in marker
        or marker == "critic_rejection"
        for marker in markers
    )


def _critic_advisory_rework_refusal(ckpt, tasks, next_v, source_v):
    """Return a fail-closed payload when Critic advice reaches Workers."""

    legacy_critic_repair = _has_legacy_critic_repair_contract(ckpt, tasks)
    critic_without_precommit_regression = (
        isinstance(ckpt, dict)
        and ckpt.get("stage") == "critic_checked"
        and not _is_precommit_rework_checkpoint(ckpt)
    )
    if not legacy_critic_repair and not critic_without_precommit_regression:
        return None
    return {
        "error": (
            "LEGACY_CRITIC_REPAIR_FORBIDDEN"
            if legacy_critic_repair
            else "CRITIC_ADVISORY_REWORK_FORBIDDEN"
        ),
        "next_v": next_v,
        "source_v": source_v,
        "stage": ckpt.get("stage") if isinstance(ckpt, dict) else None,
        "next_tool": (
            "abandon_generation"
            if legacy_critic_repair
            else "run_precommit_eval"
        ),
        "failure_class": (
            "contract_migration"
            if legacy_critic_repair
            else "route_violation"
        ),
        "safe_to_auto_execute": not legacy_critic_repair,
        "directive": (
            "This checkpoint carries retired Critic-owned Worker repair authority. "
            "Do not mutate the candidate; run controlled abandon/re-prepare recovery."
            if legacy_critic_repair
            else "Critic is advisory. Call run_precommit_eval for the unchanged candidate; "
            "only a measured native precommit regression can authorize Worker rework."
        ),
    }


def _is_review_rework_checkpoint(ckpt):
    """Whether the checkpoint represents a Lead Code Reviewer rejection.

    A candidate can have an old rejected critic gate in ``gate_results`` and
    later fail review after an in-place repair. The latest review rejection must
    own the next repair contract; otherwise stale critic/quality tasks can be
    reused against the wrong blocker.
    """
    if not isinstance(ckpt, dict):
        return False
    if ckpt.get("stage") not in {"repair_planned", "rework_running"}:
        return False
    if _is_precommit_rework_checkpoint(ckpt):
        return False
    if _is_official_rework_checkpoint(ckpt):
        return False
    review = (ckpt.get("gate_results") or {}).get("review") or {}
    if not isinstance(review, dict) or not review:
        return False
    if review.get("approved") is False:
        return True
    status = str(review.get("status") or "").lower()
    if status in {"rejected", "failed", "blocked"}:
        return True
    return False


def _precommit_failure_items(ckpt):
    if not isinstance(ckpt, dict):
        return []
    precommit = (ckpt.get("gate_results") or {}).get("precommit_eval") or {}
    items = []

    def add(value):
        if isinstance(value, dict):
            reason = value.get("reason")
            details = value.get("details")
            if reason or details:
                items.append(": ".join(str(x) for x in (reason, details) if x))
            evidence = value.get("evidence")
            if isinstance(evidence, (list, tuple)):
                for item in evidence[:5]:
                    add(item)
            elif evidence:
                add(evidence)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
        elif value is not None:
            text = str(value).strip()
            if text:
                items.append(text)

    add(precommit.get("directive"))
    add(ckpt.get("reviewer_feedback"))
    add(precommit.get("blockers"))
    add(precommit.get("failures"))

    for matchup in (precommit.get("matchups") or [])[:6]:
        if not isinstance(matchup, dict):
            continue
        opponent = matchup.get("opponent") or matchup.get("bot_b") or matchup.get("label") or "unknown"
        wins = matchup.get("wins", matchup.get("wins_a"))
        losses = matchup.get("losses", matchup.get("wins_b"))
        draws = matchup.get("draws", 0)
        reason = matchup.get("reason")
        net = matchup.get("net_chips")
        if isinstance(net, list):
            net = sum(x for x in net if isinstance(x, (int, float)))
        parts = [f"vs {opponent}"]
        if reason:
            parts.append(f"reason={reason}")
        if wins is not None and losses is not None:
            parts.append(f"result={wins}W-{losses}L-{draws}D")
        if net is not None:
            parts.append(f"net_chips={net}")
        items.append("; ".join(parts))

    deduped = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _precommit_changed_python_files(ckpt):
    """Return candidate .py files that actually differ from the source parent."""
    if not isinstance(ckpt, dict):
        return []
    source_v = ckpt.get("source_v")
    next_v = ckpt.get("next_v")
    if source_v is None or next_v is None:
        return []
    if _is_fresh_empty_pool_bootstrap(ckpt):
        # Fresh v143 has no source-side diff.  Its prepared/Worker receipts own
        # the exact policy delta; precommit must not infer one from stale v142.
        return []
    try:
        source_dir = get_bot_dir(source_v)
        next_dir = get_bot_dir(next_v)
        changed = _py_files_changed_between(source_dir, next_dir)
    except Exception:
        return []

    preferred_order = {
        "policy.py": 0,
    }
    normalized = []
    seen = set()
    for item in changed:
        rel = _target_rel(item, next_v)
        if not rel or "backup" in rel:
            continue
        if rel in _ACTIVE_CANDIDATE_WRITABLE_FILES and rel not in seen:
            seen.add(rel)
            normalized.append(rel)
    return sorted(normalized, key=lambda rel: (preferred_order.get(rel, 100), rel))


_PRECOMMIT_STRATEGY_REPAIR_FILES = [
    "policy.py",
]

_PRECOMMIT_PROTOCOL_REPAIR_FILES = frozenset()

_PRECOMMIT_PROTOCOL_EVIDENCE_MARKERS = (
    "official_smoke",
    "official smoke",
    "official-platform",
    "official platform",
    "illegal action",
    "illegal wire",
    "invalid action",
    "malformed action",
    "protocol violation",
    "wire output",
    "action serialization",
    "action format",
    "bet keyword",
    "extra spaces",
    "leading/trailing",
)


def _precommit_protocol_compliance_failure(failures, feedback=""):
    """Whether a precommit failure contains exact illegal/protocol evidence.

    National/official harnesses are compliance oracles in this pipeline. A plain
    W-L regression is a strategy repair and should not ask workers to tune the
    TCP entrypoint. Protocol files are only repair targets when the failure text
    names an illegal wire/action-format problem.
    """

    parts = [str(item) for item in failures or [] if item is not None]
    if feedback:
        parts.append(str(feedback))
    text = "\n".join(parts).lower()
    return any(marker in text for marker in _PRECOMMIT_PROTOCOL_EVIDENCE_MARKERS)


def _precommit_filter_repair_targets(files, *, allow_protocol_files=False):
    """Return only candidate-owned policy targets.

    ``allow_protocol_files`` is retained for caller compatibility; system
    runtime files are never made writable by failure prose.
    """
    allowed = []
    for item in files or []:
        rel = Path(str(item)).name
        if rel not in _ACTIVE_CANDIDATE_WRITABLE_FILES:
            continue
        allowed.append(rel)
    return allowed


def _limit_precommit_repair_targets(files):
    try:
        limit = int(os.environ.get("POK_PRECOMMIT_REPAIR_MAX_TARGETS", "3"))
    except ValueError:
        limit = 3
    limit = max(1, limit)
    targets = []
    seen = set()
    for item in files or []:
        rel = Path(str(item)).name
        if rel in _ACTIVE_CANDIDATE_WRITABLE_FILES and rel not in seen:
            seen.add(rel)
            targets.append(rel)
        if len(targets) >= limit:
            break
    return targets


def _precommit_repair_target_files(ckpt, feedback):
    failures = _precommit_failure_items(ckpt)
    if _precommit_protocol_compliance_failure(failures, feedback):
        # Protocol/runtime bytes are system-owned.  Do not reinterpret a
        # compliance failure as permission for an LLM to mutate them.
        return []
    evidence_files = _extract_quality_failure_files(failures)
    if not evidence_files and feedback:
        evidence_files = _extract_quality_failure_files([feedback])

    changed_files = _precommit_changed_python_files(ckpt)
    changed_repair_files = _precommit_filter_repair_targets(
        changed_files,
        allow_protocol_files=False,
    )
    evidence_repair_files = _precommit_filter_repair_targets(
        evidence_files,
        allow_protocol_files=False,
    )
    if changed_files and evidence_files:
        evidence_set = set(evidence_repair_files)
        intersected = [name for name in changed_repair_files if name in evidence_set]
        if intersected:
            return _limit_precommit_repair_targets(intersected)
    if changed_repair_files:
        return _limit_precommit_repair_targets(changed_repair_files)
    if evidence_repair_files:
        return _limit_precommit_repair_targets(evidence_repair_files)

    try:
        next_v = ckpt.get("next_v") if isinstance(ckpt, dict) else None
        bot_dir = get_bot_dir(next_v) if next_v is not None else None
        if bot_dir:
            existing = [
                name for name in _PRECOMMIT_STRATEGY_REPAIR_FILES
                if (bot_dir / name).exists()
            ]
            if existing:
                return _limit_precommit_repair_targets(existing[:1])
    except Exception:
        pass
    return ["policy.py"]


def _official_failure_items(ckpt, feedback=""):
    items = []

    def add(value):
        if isinstance(value, dict):
            for key, val in value.items():
                add(f"{key}: {val}")
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
        elif value is not None:
            text = str(value).strip()
            if text:
                items.append(text)

    if isinstance(ckpt, dict):
        official = (ckpt.get("gate_results") or {}).get("official_full") or {}
        if isinstance(official, dict):
            add(official.get("issues"))
            add(official.get("official_evidence_summary"))
            add(official.get("verdict"))
            status = official.get("status") if isinstance(official.get("status"), dict) else {}
            add(status.get("official_llm_repair_guidance"))
            add(status.get("official_llm_prompt_feedback"))
            add(status.get("official_llm_analysis_summary"))
    add(feedback)
    deduped = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _official_deterministic_failure_items(ckpt):
    """Return only machine-owned official verdict evidence used for repair scope.

    Reviewer feedback and the official LLM analysis are useful context for a
    worker, but they are not authority for making the system-owned TCP entrypoint
    writable.  In particular, an advisory sentence containing ``wire`` or
    ``protocol`` must never redirect an otherwise strategic repair.
    """
    items = []

    def add(value):
        if isinstance(value, dict):
            for key, val in value.items():
                add(f"{key}: {val}")
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
        elif value is not None:
            text = str(value).strip()
            if text:
                items.append(text)

    if isinstance(ckpt, dict):
        official = (ckpt.get("gate_results") or {}).get("official_full") or {}
        if isinstance(official, dict):
            add(official.get("issues"))
            add(official.get("official_evidence_summary"))
            add(official.get("verdict"))
    return list(dict.fromkeys(items))


def _official_failure_is_protocol(items):
    text = "\n".join(str(item) for item in items or []).lower()
    return any(marker in text for marker in (
        "protocol",
        "illegal",
        "invalid action",
        "unknown action",
        "wire",
        "raise format",
        "sticky",
        "connectionrefused",
        "brokenpipe",
    ))


def _official_repair_target_files(ckpt, feedback):
    deterministic_items = _official_deterministic_failure_items(ckpt)
    evidence_files = _extract_quality_failure_files(deterministic_items)
    if _official_failure_is_protocol(deterministic_items):
        return []

    changed_files = _precommit_changed_python_files(ckpt)
    strategy_candidates = [
        rel for rel in _precommit_filter_repair_targets(changed_files, allow_protocol_files=False)
        if rel in _PRECOMMIT_STRATEGY_REPAIR_FILES
    ]
    evidence_strategy = [
        rel for rel in _precommit_filter_repair_targets(evidence_files, allow_protocol_files=False)
        if rel in _PRECOMMIT_STRATEGY_REPAIR_FILES
    ]
    if strategy_candidates and evidence_strategy:
        evidence_set = set(evidence_strategy)
        intersected = [name for name in strategy_candidates if name in evidence_set]
        if intersected:
            return _limit_precommit_repair_targets(intersected)
    if strategy_candidates:
        return _limit_precommit_repair_targets(strategy_candidates)
    if evidence_strategy:
        return _limit_precommit_repair_targets(evidence_strategy)
    try:
        next_v = ckpt.get("next_v") if isinstance(ckpt, dict) else None
        bot_dir = get_bot_dir(next_v) if next_v is not None else None
        if bot_dir:
            existing = [
                name for name in _PRECOMMIT_STRATEGY_REPAIR_FILES
                if (bot_dir / name).exists()
            ]
            if existing:
                return _limit_precommit_repair_targets(existing[:2])
    except Exception:
        pass
    return ["policy.py"]


def _official_repair_tasks(ckpt, feedback):
    items = _official_failure_items(ckpt, feedback)
    targets = _official_repair_target_files(ckpt, feedback)
    if not targets:
        return []
    evidence = "\n".join(str(item) for item in items[:30]) or str(feedback or "official full certification failed")
    next_v = ckpt.get("next_v") if isinstance(ckpt, dict) else "?"
    source_v = ckpt.get("source_v") if isinstance(ckpt, dict) else "?"
    method = (
        "- This is an official EXE full-certification repair, not a strength-rating tweak.\n"
        "- Use only the checkpoint-injected deterministic round issues below; the raw official evidence path is system-owned.\n"
        "- Fix only the bot-side reason the official 70-hand full gate could not complete.\n"
        "- Do not loosen local validators, suppress official evidence, or mark certification passed manually.\n"
        "- Keep the five-file strict artifact intact and the system-owned TCP entrypoint byte-identical."
    )
    method += (
        "\n- Candidate scope is policy.py only. Repair only a policy exception, "
        "invalid typed intent, or bounded-deadline behavior proven by the evidence."
        "\n- A wire/parser/reducer/entrypoint failure is system-owned and must remain "
        "fail-closed; never edit national_bot.py or precompute.py."
    )
    role = "Algorithmic Logic Architect"
    prompt = (
        f"Repair official EXE full-certification blocker for bots/national_v{next_v} from source v{source_v}.\n\n"
        f"Official evidence:\n{evidence[:5000]}\n\n"
        f"Required method:\n{method}\n\n"
        "Verification expectation:\n"
        "- Run `python -m py_compile` on the exact edited file; imports and dynamic checks remain system-owned.\n"
        "- Confirm only policy.py changed; system artifacts must remain byte-identical.\n"
        "- End with the concrete official failure class you addressed."
    )
    return [{
        "worker_id": "auto_official_full_repair",
        "role": role,
        "target_files": targets,
        "must_change_files": targets,
        "worker_prompt": prompt,
        "task_kind": "official_repair",
        "repair_blocker": "official_full",
        "repair_contract": {
            "blocker": "official_full",
            "files": targets,
            "evidence": evidence[:2000],
            "source_stage": str(ckpt.get("stage") or "")
            if isinstance(ckpt, dict) else "",
        },
    }]


def _review_feedback_items(ckpt, feedback=""):
    items = []

    def add(value):
        if isinstance(value, dict):
            for key, val in value.items():
                add(f"{key}: {val}")
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
        elif value is not None:
            text = str(value).strip()
            if text:
                items.append(text)

    add(feedback)
    if isinstance(ckpt, dict):
        review = (ckpt.get("gate_results") or {}).get("review") or {}
        if isinstance(review, dict):
            for key in (
                "feedback",
                "reasoning",
                "directive",
                "blockers",
                "failures",
                "issues",
                "code_quality_issues",
            ):
                add(review.get(key))

    deduped = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _review_primary_feedback_text(feedback):
    """Trim reviewer feedback down to the blocking issue, excluding side notes."""
    text = str(feedback or "").strip()
    if not text:
        return ""
    text = re.split(r"(?i)\n\s*NOTE:\s+This is\b", text, maxsplit=1)[0].strip()
    text = re.split(r"(?i)\bNote on\s+[A-Za-z0-9_./-]+\.py\s*:", text, maxsplit=1)[0].strip()
    text = re.split(r"(?i)\bAlso notes?\b", text, maxsplit=1)[0].strip()
    text = re.split(r"(?i)\bOther checks\s*:", text, maxsplit=1)[0].strip()
    return text


def _review_repair_target_files(ckpt, feedback):
    primary = _review_primary_feedback_text(feedback)
    evidence_files = _extract_quality_failure_files([primary]) if primary else []
    allow_protocol_files = _precommit_protocol_compliance_failure([primary], feedback)
    evidence_repair_files = _precommit_filter_repair_targets(
        evidence_files,
        allow_protocol_files=allow_protocol_files,
    )
    if evidence_repair_files:
        return _limit_precommit_repair_targets(evidence_repair_files)

    changed_files = _precommit_changed_python_files(ckpt)
    changed_repair_files = _precommit_filter_repair_targets(
        changed_files,
        allow_protocol_files=allow_protocol_files,
    )
    if changed_repair_files:
        return _limit_precommit_repair_targets(changed_repair_files)
    return ["policy.py"]


def _review_repair_task_refresh_reason(tasks, ckpt, feedback=""):
    if not _is_review_rework_checkpoint(ckpt):
        return ""
    if not tasks:
        return "missing review repair task(s)"
    expected = set(_review_repair_target_files(ckpt, feedback))
    task_files = set(_task_target_filenames(tasks))
    task_kinds = {
        str(task.get("task_kind") or "").lower()
        for task in tasks or []
        if isinstance(task, dict)
    }
    task_text = " ".join(
        str(task.get("worker_id", "")) + " " + str(task.get("worker_prompt", ""))[:500]
        for task in tasks or []
        if isinstance(task, dict)
    ).lower()
    if not any("review_repair" in kind for kind in task_kinds) and "code reviewer" not in task_text:
        return "checkpoint task is not a review repair"
    if expected and task_files != expected:
        return "review repair targets are stale"
    if "quality_repair" in task_text or any("quality_repair" in kind for kind in task_kinds):
        return "review repair task still uses quality repair contract"
    return ""


def _checkpoint_rework_feedback(ckpt):
    if not isinstance(ckpt, dict):
        return ""
    if ckpt.get("reviewer_feedback"):
        return str(ckpt.get("reviewer_feedback") or "")
    stage = ckpt.get("stage")
    gates = ckpt.get("gate_results") or {}
    if _is_precommit_rework_checkpoint(ckpt):
        failed = _precommit_failure_items(ckpt)
        if failed:
            return "Precommit failed:\n- " + "\n- ".join(str(item) for item in failed[:20])
    if _is_official_rework_checkpoint(ckpt):
        failed = _official_failure_items(ckpt)
        if failed:
            return "Official EXE full certification failed:\n- " + "\n- ".join(str(item) for item in failed[:20])
    if _is_review_rework_checkpoint(ckpt):
        failed = _review_feedback_items(ckpt)
        if failed:
            return "Reviewer rejected:\n- " + "\n- ".join(str(item) for item in failed[:20])
    if stage in {"quality_failed", "repair_planned", "rework_running"}:
        failed = _quality_failure_items(ckpt)
        if failed:
            return "Quality gates failed:\n- " + "\n- ".join(str(item) for item in failed[:20])
    if stage == "precommit_failed":
        precommit = gates.get("precommit_eval") or {}
        blockers = precommit.get("blockers") or precommit.get("failures") or []
        if blockers:
            return "Precommit failed: " + json.dumps(blockers[:10], ensure_ascii=False)
    return ""


def _checkpoint_repair_baseline_fingerprint(ckpt) -> str:
    """Return the content identity that authorized the current repair route."""
    if not isinstance(ckpt, dict):
        return ""
    top_level = str(ckpt.get("repair_baseline_artifact_hash") or "")
    plan = ckpt.get("master_plan") if isinstance(ckpt.get("master_plan"), dict) else {}
    work_item = plan.get("work_item") if isinstance(plan.get("work_item"), dict) else {}
    bound = str(work_item.get("repair_baseline_artifact_hash") or "")
    stage = str(ckpt.get("stage") or "")
    if stage in {"quality_failed", "precommit_failed", "official_failed"} and top_level:
        return top_level
    if bound:
        return bound
    if top_level:
        return top_level

    gates = ckpt.get("gate_results") if isinstance(ckpt.get("gate_results"), dict) else {}
    if _is_official_rework_checkpoint(ckpt) or ckpt.get("stage") == "official_failed":
        official = gates.get("official_full") if isinstance(gates.get("official_full"), dict) else {}
        identities = [
            official.get("certification_identity"),
            (official.get("status") or {}).get("certification_identity")
            if isinstance(official.get("status"), dict)
            else None,
        ]
        for identity in identities:
            if isinstance(identity, dict) and identity.get("candidate_hash"):
                return str(identity["candidate_hash"])

    if _is_precommit_rework_checkpoint(ckpt) or ckpt.get("stage") == "precommit_failed":
        precommit = gates.get("precommit_eval") if isinstance(gates.get("precommit_eval"), dict) else {}
        if precommit.get("code_fingerprint"):
            return str(precommit["code_fingerprint"])

    quality = gates.get("quality") if isinstance(gates.get("quality"), dict) else {}
    if quality.get("code_fingerprint"):
        return str(quality["code_fingerprint"])

    # Official evidence is created only after the content-bound precommit gate;
    # retain that safe fallback for older official payload projections.
    precommit = gates.get("precommit_eval") if isinstance(gates.get("precommit_eval"), dict) else {}
    return str(precommit.get("code_fingerprint") or "")


def _quality_failure_items(ckpt):
    if not isinstance(ckpt, dict):
        return []
    quality = (ckpt.get("gate_results") or {}).get("quality") or {}
    items = []

    def add(value):
        if isinstance(value, dict):
            for key, val in value.items():
                if str(key).endswith(".py"):
                    items.append(f"{key}: {val}")
                else:
                    items.append(f"{key}={val}")
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
        elif value is not None:
            text = str(value).strip()
            if text:
                items.append(text)

    add(quality.get("failed_gates"))
    add(quality.get("failures"))
    for key in (
        "compile_errors",
        "import_errors",
        "protected_contract_errors",
        "national_native_contract_errors",
        "smoke_errors",
        "national_protocol_errors",
        "national_acceptance_errors",
        "official_smoke_errors",
        "declared_scope_errors",
        "critical_failures",
        "position_semantics_errors",
        "reachability_warnings",
    ):
        add(quality.get(key))
    oversized = quality.get("oversized_files")
    if isinstance(oversized, dict):
        for filename, lines in oversized.items():
            add(f"file_size({filename}:{lines}L)")

    transition = quality.get("national_architecture_transition") or {}
    if isinstance(transition, dict) and not transition.get("ok", True):
        candidate_checks = (
            (transition.get("candidate_capabilities") or {}).get("checks_by_id") or {}
        )
        for error in transition.get("policy_identity_errors") or []:
            add(f"runtime_architecture_policy_identity: {error}")
        for regression in transition.get("regressions") or []:
            check_id = str(regression.get("check_id") or "unknown")
            guidance = regression.get("guidance") or (
                (candidate_checks.get(check_id) or {}).get("guidance")
                or "Restore the source capability."
            )
            add(f"runtime_architecture_regression:{check_id}: {guidance}")
        for failure in transition.get("runtime_floor_failures") or []:
            check_id = str(failure.get("check_id") or "unknown")
            check = candidate_checks.get(check_id) or {}
            add(
                f"runtime_architecture_floor:{check_id}: "
                f"{failure.get('guidance') or check.get('guidance') or 'Complete the mandatory runtime floor.'}"
            )
        for check_id in transition.get("unresolved_focus_checks") or []:
            check = candidate_checks.get(str(check_id)) or {}
            add(
                f"runtime_architecture_focus:{check_id}: "
                f"{check.get('guidance') or 'Complete the selected architecture focus.'}"
            )
        if transition.get("error"):
            add(f"runtime_architecture_error: {transition.get('error')}")

    deduped = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _extract_quality_failure_files(failures):
    files = []
    seen = set()
    for failure in failures or []:
        for match in re.finditer(r"([A-Za-z0-9_./-]+\.py)(?::\d+)?", str(failure)):
            rel = Path(match.group(1)).name
            if rel and rel not in seen:
                seen.add(rel)
                files.append(rel)
    return files


def _flatten_text_items(value):
    items = []

    def add(item):
        if isinstance(item, dict):
            for key, val in item.items():
                add(f"{key}: {val}")
        elif isinstance(item, (list, tuple, set)):
            for sub in item:
                add(sub)
        elif item is not None:
            text = str(item).strip()
            if text:
                items.append(text)

    add(value)
    return items


def _is_declared_scope_failure_text(item):
    text = str(item or "").lower()
    return (
        "declared_scope" in text
        or "declared scope" in text
        or "outside master plan target_files/files_allowed" in text
        or "outside declared target_files/files_allowed" in text
    )


def _is_position_semantics_failure_text(item):
    text = str(item or "").lower()
    return (
        "position_semantics" in text
        or "retired position identifier" in text
        or "retired decision_context key" in text
        or "decision_context.hand.position" in text
        or "decision_context.line.position" in text
        or "acts_first_postflop" in text
        or "hero_in_position_postflop" in text
        or "bb acts first postflop" in text
        or "candidate-side seat reconstruction" in text
    )


def _is_national_native_contract_failure_text(item):
    text = str(item or "").lower()
    return (
        "national_native_contract" in text
        or "native national tcp contract" in text
        or "national_bot.py missing" in text
        or (
            "national_bot.py" in text
            and (
                "sanitizer failure" in text
                or "raw action" in text
                or "direct tcp" in text
                or "botzone integer" in text
            )
        )
    )


def _is_official_smoke_protocol_failure_text(item):
    text = str(item or "").lower()
    if any(marker in text for marker in (
        "protocol_",
        "protocol error",
        "illegal_bet_action",
        "protocol_raise_format",
        "protocol_action_format",
        "protocol_action_whitespace",
        "invalid action",
        "unknown action",
    )):
        return True
    return "illegal" in text and "official" in text


def _is_runtime_architecture_failure_text(item):
    text = str(item or "").lower()
    return any(marker in text for marker in (
        "runtime_architecture",
        "architecture_focus:",
        "architecture_regression:",
        "architecture_policy_",
        "national_capability_contract",
    ))


def _declared_scope_violation_files(ckpt, reviewer_feedback=""):
    """Extract undeclared artifact paths for fail-closed integrity handling."""
    if not isinstance(ckpt, dict):
        return set()
    quality = (ckpt.get("gate_results") or {}).get("quality") or {}
    if not isinstance(quality, dict) or quality.get("declared_scope_ok") is True:
        return set()

    next_v = ckpt.get("next_v")
    evidence = []
    evidence.extend(_flatten_text_items(quality.get("declared_scope_errors")))
    evidence.extend(
        item for item in _quality_failure_items(ckpt)
        if _is_declared_scope_failure_text(item)
    )
    # Machine-owned declared_scope_errors/metrics are the primary authority.
    # If an older checkpoint lacks them, consume only feedback lines that
    # themselves describe a scope violation; never append the aggregate quality
    # receipt because it also names legitimate file_size/position targets.
    if not evidence and reviewer_feedback:
        evidence.extend(
            line.strip()
            for line in str(reviewer_feedback).splitlines()
            if _is_declared_scope_failure_text(line)
        )

    files = set()
    for filename in _extract_quality_failure_files(evidence):
        rel = _target_rel(filename, next_v)
        if rel:
            files.add(rel)

    scope_metrics = quality.get("declared_scope") or {}
    if not files and isinstance(scope_metrics, dict):
        changed = {
            rel for rel in (
                _target_rel(item, next_v)
                for item in scope_metrics.get("changed_files", []) or []
            )
            if rel
        }
        allowed = {
            rel for rel in (
                _target_rel(item, next_v)
                for item in scope_metrics.get("allowed_files", []) or []
            )
            if rel
        }
        files.update(changed - allowed)
    return files


def _task_id_suffix(filename):
    return re.sub(r"[^a-z0-9]+", "_", Path(str(filename)).name.lower()).strip("_")


def _line_count_contracts(quality, failures):
    """Return structured file_size blocker contracts from quality gate output."""
    by_file = {}

    def add(filename, current=None, limit=None, evidence=""):
        rel = Path(str(filename)).name
        if not rel:
            return
        existing = by_file.get(rel, {})
        evidences = []
        if existing.get("evidence"):
            evidences.append(str(existing["evidence"]))
        if evidence and evidence not in evidences:
            evidences.append(evidence)
        by_file[rel] = {
            "blocker": "file_size",
            "file": rel,
            "current_lines": current if current is not None else existing.get("current_lines"),
            "line_limit": limit if limit is not None else existing.get("line_limit"),
            "evidence": "; ".join(evidences),
        }

    oversized = quality.get("oversized_files")
    if isinstance(oversized, dict):
        for filename, lines in oversized.items():
            try:
                current = int(lines)
            except (TypeError, ValueError):
                current = None
            add(filename, current=current, evidence=f"oversized_files[{filename}]={lines}")

    text = "\n".join(str(item) for item in failures or [])
    for group in re.finditer(r"file_size\(([^)]*)\)", text):
        body = group.group(1)
        for match in re.finditer(
            r"([A-Za-z0-9_./-]+\.py):(\d+)L(?:/(\d+)L)?",
            body,
        ):
            current = int(match.group(2))
            limit = int(match.group(3)) if match.group(3) else None
            add(match.group(1), current=current, limit=limit, evidence=f"file_size({body})")
    return [by_file[name] for name in sorted(by_file)]


def _position_contracts(quality):
    """Return structured position_semantics contracts grouped by file."""
    source_items = []
    source_items.extend(_flatten_text_items(quality.get("position_semantics_errors")))
    for item in _flatten_text_items(quality.get("failed_gates")):
        if "position_semantics(" in item:
            source_items.append(item)

    by_file = {}
    for item in source_items:
        text = str(item)
        for match in re.finditer(
            r"([A-Za-z0-9_./-]+\.py):(\d+):?\s*([^;\n)]*)",
            text,
        ):
            rel = Path(match.group(1)).name
            if not rel:
                continue
            detail = {
                "line": int(match.group(2)),
                "message": match.group(3).strip() or text.strip(),
                "evidence": text.strip(),
            }
            by_file.setdefault(rel, []).append(detail)

    contracts = []
    for rel, details in by_file.items():
        deduped = []
        seen = set()
        for detail in details:
            key = (detail["line"], detail["message"])
            if key not in seen:
                seen.add(key)
                deduped.append(detail)
        contracts.append({
            "blocker": "position_semantics",
            "file": rel,
            "details": deduped,
            "evidence": "; ".join(d["evidence"] for d in deduped[:4]),
        })
    return sorted(contracts, key=lambda c: c["file"])


def _national_native_contracts(quality, failures):
    """System runtime contract failures are never candidate repair tasks."""
    return []


def _official_smoke_contracts(quality, failures):
    """Official wire failures remain fail-closed system/infrastructure debt."""
    return []


_ARCHITECTURE_FOCUS_LAYERS = {
    NATIONAL_POLICY_FOCUS_ID: "runtime_architecture",
    "incremental_match_model": "opponent_model",
    "deadline_refinement": "runtime_architecture",
    "bounded_runtime_enumeration": "precompute",
    "decision_path_purity": "runtime_architecture",
}

_ARCHITECTURE_CHECK_FILES = {
    "official_safe_wire_send": ["national_bot.py"],
    "clean_diagnostics_channel": ["national_bot.py"],
    "national_policy_module": ["policy.py"],
    "decision_context_v1": ["policy.py"],
    "typed_intent_v1": ["policy.py"],
    "policy_baseline_entrypoint": ["policy.py"],
    "policy_refinement_entrypoint": ["policy.py"],
    "decision_time_budget_visible": ["policy.py"],
    "killable_decision_runtime": ["national_bot.py"],
    "fast_policy_baseline": ["policy.py"],
    "incremental_refinement_protocol": ["policy.py"],
    "budget_scaled_refinement": ["policy.py"],
    "decision_path_no_external_io": ["policy.py"],
    "decision_path_no_full_history_scan": ["policy.py"],
    "decision_path_no_large_runtime_tables": ["policy.py"],
    "precompute_lookup_path": ["policy.py"],
    "persistent_match_memory": ["national_bot.py"],
    "terminal_response_memory": ["national_bot.py"],
    "showdown_range_posterior": ["national_bot.py"],
    "authoritative_hand_context": ["national_bot.py"],
    "incremental_opponent_model": ["policy.py"],
    "terminal_response_adaptation": ["policy.py"],
    "showdown_range_adaptation": ["policy.py"],
    "donk_line_reachability": ["policy.py"],
    "delayed_probe_line_reachability": ["policy.py"],
    "semantic_line_reachability": ["policy.py"],
}

_STATE_LEARNING_ORACLE_REFS = [
    "docs/official-raise-boundary-oracle-2026-07-11.md",
    "docs/official-terminal-settlement-oracle-2026-07-11.md",
]


def _detected_artifact_consumer(artifact):
    """Return a schema consumer bound to an actual detector call-chain node."""
    candidates = []
    for location in artifact.get("consumer_locations") or []:
        for segment in str(location).split("->"):
            match = re.fullmatch(
                r"([A-Za-z_][A-Za-z0-9_]*)\.py:([A-Za-z_][A-Za-z0-9_]*)",
                segment,
            )
            if match:
                candidates.append(f"{match.group(1)}.{match.group(2)}")
    for preferred in POLICY_ENTRYPOINTS:
        for candidate in candidates:
            if candidate.endswith(f".{preferred}"):
                return candidate
    return candidates[0] if candidates else "policy.get_baseline_decision"


def _candidate_consumed_precompute_contracts(
    candidate_capabilities,
    *,
    require_action_influence: bool = False,
):
    """Translate proven candidate artifacts into repair declarations.

    Static evidence owns identity, build phase, bound, and consumer. Dynamic
    evidence supplies measured key shape, bytes, and import latency. This keeps a
    repair attached to the candidate's real artifact instead of inventing a
    generic lookup whenever an unrelated architecture check fails.  When a
    lookup is the selected primary, require the runtime counterfactual proof as
    well: a read-only/discarded foundation table is still useful acceleration,
    but is not a strategy innovation.
    """
    if not isinstance(candidate_capabilities, dict):
        return []
    precompute = candidate_capabilities.get("precompute_evidence") or {}
    dynamic_rows = {
        (str(row.get("owner_file") or ""), str(row.get("name") or "")): row
        for row in (
            (candidate_capabilities.get("dynamic_runtime_probe") or {}).get("artifacts")
            or []
        )
        if isinstance(row, dict)
    }
    contracts = []
    static_artifacts = [
        artifact
        for artifact in precompute.get("consumed_artifacts") or []
        if isinstance(artifact, dict)
    ]
    static_artifacts.sort(key=lambda artifact: (
        not bool(dynamic_rows.get((
            str(artifact.get("location") or "").split(":", 1)[0],
            str(artifact.get("name") or ""),
        ), {}).get("ok")),
        str(artifact.get("location") or ""),
        str(artifact.get("name") or ""),
    ))
    for artifact in static_artifacts:
        owner_file = str(artifact.get("location") or "").split(":", 1)[0]
        name = str(artifact.get("name") or "").strip()
        if owner_file != "precompute.py" or len(name) < 2:
            continue
        dynamic = dynamic_rows.get((owner_file, name)) or {}
        if require_action_influence and not dynamic.get("value_affects_final_wire"):
            continue
        raw_shape = str(dynamic.get("observed_key_shape") or "int")
        key_shape = (
            raw_shape
            if re.fullmatch(PRECOMPUTE_KEY_SHAPE_PATTERN, raw_shape)
            else "int"
        )
        entries = max(1, int(artifact.get("bound_entries") or 1))
        measured_bytes = max(262_144, int(dynamic.get("deep_bytes") or 0))
        measured_ms = max(
            500,
            int(float(dynamic.get("import_elapsed_ms") or 0) + 0.999),
        )
        contracts.append({
            "name": name,
            "owner_file": owner_file,
            "build_phase": str(artifact.get("build_phase") or "module_import"),
            "max_build_ms": min(PRECOMPUTE_MAX_BUILD_MS, measured_ms),
            "max_entries": min(PRECOMPUTE_MAX_ENTRIES, entries),
            "max_bytes": min(PRECOMPUTE_MAX_BYTES, measured_bytes),
            "key_shape": key_shape,
            "consumer": _detected_artifact_consumer(artifact),
            "fallback": "legal_baseline",
        })
        break
    return contracts


def _default_state_learning_contract(
    focus_id,
    skill_layer,
    required_checks,
    candidate_capabilities=None,
):
    if focus_id != NATIONAL_POLICY_FOCUS_ID:
        return None
    required = {str(item) for item in required_checks or []}
    work_primitive = None
    profile_dimensions = []
    line_controls = []
    wants_precompute = "precompute_lookup_path" in required or skill_layer == "precompute"
    if wants_precompute:
        # Compact system facts remain valid acceleration inputs, but table use
        # cannot be selected as the generation's primary innovation until a
        # digest-bound value-variant probe exists.  Use the independently
        # measurable bounded-candidate primary for deterministic repair plans.
        work_primitive = "sample_counted_candidate_batch"
    elif "terminal_response_adaptation" in required:
        profile_dimensions = ["terminal_response"]
    elif "showdown_range_adaptation" in required:
        profile_dimensions = ["showdown_range"]
    elif "incremental_opponent_model" in required or skill_layer in {
        "match_memory",
        "opponent_model",
    }:
        profile_dimensions = ["action_profile"]
    elif "donk_line_reachability" in required:
        line_controls = ["donk"]
    elif "delayed_probe_line_reachability" in required:
        line_controls = ["delayed_probe"]
    elif skill_layer == "line_template":
        line_controls = ["donk"]
    else:
        work_primitive = "sample_counted_candidate_batch"
    return {
        "work_primitive": work_primitive,
        "profile_dimensions": profile_dimensions,
        "line_controls": line_controls,
        "oracle_refs": list(_STATE_LEARNING_ORACLE_REFS),
    }


def _architecture_default_runtime_contract(
    focus_id,
    skill_layer,
    owner_file=None,
    required_checks=(),
    candidate_capabilities=None,
):
    """Return a strict fallback contract for deterministic/crossover repair plans."""
    required_checks = {str(item) for item in required_checks or []}
    contract = {
        "policy_abi": {
            "module": "policy.py",
            "context_schema_version": POLICY_CONTEXT_SCHEMA_VERSION,
            "context_fields": list(POLICY_CONTEXT_TOP_LEVEL_FIELDS),
            "entrypoints": list(POLICY_ENTRYPOINTS),
            "intent_kinds": list(POLICY_INTENT_KINDS),
            "raise_field": "raise_to",
            "pass_mapping": "socket_owner_call_or_check",
        },
        "decision": None,
        "precompute_artifacts": [],
        "match_memory": None,
        "state_learning": _default_state_learning_contract(
            focus_id,
            skill_layer,
            required_checks,
            candidate_capabilities,
        ),
        "reference_pack_id": "",
        "official_feedback_refs": [],
        "forbidden_runtime_work": [
            "reconstructing match state outside decision_context",
            "file, network, or subprocess I/O inside the decision path",
            "unbounded combinatorial construction per decision",
        ],
    }
    state_learning = contract.get("state_learning") or {}
    primary_work = state_learning.get("work_primitive")
    if primary_work:
        from strategy_reference_pack import default_reference_pack_id

        contract["reference_pack_id"] = default_reference_pack_id(primary_work)
    primary_profiles = set(state_learning.get("profile_dimensions") or [])
    if (
        skill_layer in {"match_memory", "opponent_model"}
        or focus_id in {
            "incremental_match_model",
        }
        or primary_profiles
        or required_checks.intersection({
            "persistent_match_memory",
            "terminal_response_memory",
            "showdown_range_posterior",
            "authoritative_hand_context",
            "incremental_opponent_model",
            "terminal_response_adaptation",
            "showdown_range_adaptation",
            "donk_line_reachability",
            "delayed_probe_line_reachability",
            "semantic_line_reachability",
            "decision_path_no_full_history_scan",
        })
    ):
        contract["match_memory"] = {
            "tracker_class": "OpponentTracker",
            "owner_file": "national_bot.py",
            "reset_boundary": "tcp_connection",
            "update_events": [
                "hand_start",
                "street_start",
                "opponent_action",
                "settlement",
                "showdown",
            ],
            "snapshot_field": "opponent",
            "max_recent_hands": 8,
            "prior_rule": "beta_prior_weight_8",
            "confidence_rule": (
                "global_actions_over_actions_plus_24_and_context_samples_over_samples_plus_8"
            ),
            "adaptation_cap": 0.65,
            "consumer": "policy.get_baseline_decision",
        }
    if (
        skill_layer == "precompute"
        or focus_id in {
            "bounded_runtime_enumeration",
        }
        or required_checks.intersection({
            "precompute_lookup_path",
            "decision_path_no_large_runtime_tables",
        })
    ):
        contract["precompute_artifacts"] = _candidate_consumed_precompute_contracts(
            candidate_capabilities,
            require_action_influence=False,
        )
    if (
        skill_layer in {"runtime_architecture", "native_tcp"}
        or focus_id in {
            "deadline_refinement",
            "decision_path_purity",
        }
        or primary_work == "sample_counted_candidate_batch"
        or required_checks.intersection({
            "decision_time_budget_visible",
            "killable_decision_runtime",
            "fast_policy_baseline",
            "incremental_refinement_protocol",
            "budget_scaled_refinement",
            "decision_path_no_external_io",
        })
    ):
        contract["decision"] = {
            "clock": "time.monotonic",
            "hard_deadline_ms": 55_000,
            "baseline_target_ms": 250,
            "refinement_budget_ms": 54_000,
            "baseline_path": "compute a legal deterministic action before optional refinement",
            "fallback_action": "return typed pass when no wager is faced, otherwise fold",
            "refinement_bound": "stop on the monotonic deadline and an explicit finite sample cap",
            "max_samples": 4_096,
        }
    return contract


def _merge_runtime_contract_floor(inherited, floor_contract):
    """Preserve the accepted contract while adding newly proven floor debt."""
    result = deepcopy(floor_contract)
    if not isinstance(inherited, dict):
        return result
    if inherited.get("policy_abi") is not None:
        result["policy_abi"] = deepcopy(inherited["policy_abi"])
    if inherited.get("decision") is not None:
        result["decision"] = deepcopy(inherited["decision"])
    if inherited.get("match_memory") is not None:
        result["match_memory"] = deepcopy(inherited["match_memory"])
    if inherited.get("state_learning") is not None:
        result["state_learning"] = deepcopy(inherited["state_learning"])
    if inherited.get("reference_pack_id"):
        result["reference_pack_id"] = str(inherited["reference_pack_id"])
    state_learning = result.get("state_learning") or {}
    primary_work = state_learning.get("work_primitive") if isinstance(state_learning, dict) else None
    if primary_work and not result.get("reference_pack_id"):
        from strategy_reference_pack import default_reference_pack_id

        result["reference_pack_id"] = default_reference_pack_id(primary_work)
    inherited_artifacts = [
        deepcopy(item)
        for item in inherited.get("precompute_artifacts") or []
        if isinstance(item, dict)
    ]
    if inherited_artifacts:
        by_identity = {
            (str(item.get("owner_file")), str(item.get("name"))): item
            for item in result.get("precompute_artifacts") or []
            if isinstance(item, dict)
        }
        for item in inherited_artifacts:
            by_identity[(str(item.get("owner_file")), str(item.get("name")))] = item
        result["precompute_artifacts"] = list(by_identity.values())
    for key in ("official_feedback_refs", "forbidden_runtime_work"):
        result[key] = list(dict.fromkeys([
            *(result.get(key) or []),
            *(inherited.get(key) or []),
        ]))[:8]
    return result


def _architecture_repair_context(ckpt, focus_id):
    plan = _checkpoint_master_plan(ckpt)
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        if focus_id and str(task.get("architecture_focus_id") or "") != focus_id:
            continue
        contract = task.get("runtime_contract")
        if isinstance(contract, dict):
            return str(task.get("skill_layer") or ""), contract
    return "", None


def _architecture_transition_failure_ids(transition):
    candidate = transition.get("candidate_capabilities") or {}
    failing_ids = []
    for item in candidate.get("required_failures") or []:
        check_id = str(item.get("check_id") or item.get("name") or "")
        if check_id and check_id not in failing_ids:
            failing_ids.append(check_id)
    for item in transition.get("regressions") or []:
        check_id = str(item.get("check_id") or "")
        if check_id and check_id not in failing_ids:
            failing_ids.append(check_id)
    for item in transition.get("runtime_floor_failures") or []:
        check_id = str(item.get("check_id") or "")
        if check_id and check_id not in failing_ids:
            failing_ids.append(check_id)
    for check_id in transition.get("unresolved_focus_checks") or []:
        check_id = str(check_id)
        if check_id and check_id not in failing_ids:
            failing_ids.append(check_id)
    if transition.get("runtime_contract_implementation_errors"):
        failing_ids.append("runtime_contract_implementation")
    return failing_ids


def _architecture_transition_repair_files(transition, candidate_dir=None):
    candidate = transition.get("candidate_capabilities") or {}
    checks_by_id = candidate.get("checks_by_id") or {}
    policy = transition.get("policy") or {}
    focus = transition.get("selected_focus") or policy.get("selected_focus") or {}
    require_existing = bool(candidate_dir and Path(candidate_dir).is_dir())
    files = []

    def add_file(value):
        rel = Path(str(value)).name
        if rel not in _ACTIVE_CANDIDATE_WRITABLE_FILES or rel in files:
            return
        if require_existing and not (Path(candidate_dir) / rel).is_file():
            return
        files.append(rel)

    for check_id in _architecture_transition_failure_ids(transition):
        check = checks_by_id.get(check_id) or {}
        locations = [str(item) for item in (check.get("evidence") or {}).get("locations") or []]
        for rel in _extract_quality_failure_files(locations):
            add_file(rel)
        for rel in _ARCHITECTURE_CHECK_FILES.get(check_id, []):
            add_file(rel)
    if not files:
        for rel in focus.get("suggested_files") or []:
            add_file(rel)
    return files


def _architecture_contracts(quality, ckpt):
    """Build one evidence-scoped repair contract for the transition hard gate.

    Runtime architecture is deliberately repaired as one coherent task. Splitting
    provider, consumer, and decision-path cleanup across generic workers can make
    each edit look plausible while the end-to-end AST capability still fails.
    """
    transition = quality.get("national_architecture_transition") or {}
    if not isinstance(transition, dict) or transition.get("ok", True):
        return []
    if transition.get("runtime_probe_infra"):
        return []
    if transition.get("policy_identity_errors"):
        return []

    candidate = transition.get("candidate_capabilities") or {}
    checks_by_id = candidate.get("checks_by_id") or {}
    policy = transition.get("policy") or {}
    focus = transition.get("selected_focus") or policy.get("selected_focus") or {}
    focus_id = str(focus.get("focus_id") or "")

    failing_ids = _architecture_transition_failure_ids(transition)

    # A policy identity mismatch is repository/checkpoint drift, not bot code
    # debt. Do not waste a worker edit trying to change a digest.
    if not failing_ids:
        return []
    worker_repairable = (
        "runtime_contract_implementation" in failing_ids
        or any(
            "policy.py" in _ARCHITECTURE_CHECK_FILES.get(check_id, ())
            for check_id in failing_ids
        )
    )
    if not worker_repairable:
        # Reducer, socket, tracker, or system-precompute failures cannot be
        # redirected into policy.py merely to obtain a repair task.
        return []

    inherited_layer, inherited_contract = _architecture_repair_context(ckpt, focus_id)
    skill_layer = inherited_layer or _ARCHITECTURE_FOCUS_LAYERS.get(focus_id, "")
    if not skill_layer:
        for check_id in failing_ids:
            candidate_layer = str((checks_by_id.get(check_id) or {}).get("skill_layer") or "")
            if candidate_layer:
                skill_layer = candidate_layer
                break
    skill_layer = skill_layer or "runtime_architecture"

    candidate_dir = get_bot_dir(ckpt.get("next_v")) if ckpt.get("next_v") is not None else None
    target_files = _architecture_transition_repair_files(transition, candidate_dir)

    evidence_lines = []
    for check_id in failing_ids:
        check = checks_by_id.get(check_id) or {}
        evidence = check.get("evidence") or {}
        guidance = check.get("guidance") or "Satisfy this capability with code consumed by the decision path."
        locations = [str(item) for item in evidence.get("locations") or []]
        summary = str(evidence.get("summary") or "no detector summary")
        location_text = f"; locations={locations[:3]}" if locations else ""
        evidence_lines.append(f"{check_id}: {summary}; required={guidance}{location_text}")
    for error in transition.get("runtime_contract_implementation_errors") or []:
        evidence_lines.append(f"runtime_contract_implementation: {error}")

    target_files = ["policy.py"]
    primary = target_files[0]
    precompute_owner = "precompute.py"
    floor_contract = _architecture_default_runtime_contract(
        focus_id,
        skill_layer,
        precompute_owner,
        required_checks=failing_ids,
        candidate_capabilities=candidate,
    )
    runtime_contract = _merge_runtime_contract_floor(inherited_contract, floor_contract)
    validated_runtime_contract = RuntimeContract.model_validate(runtime_contract)
    primary_checks = (
        list(validated_runtime_contract.state_learning.primary_checks())
        if validated_runtime_contract.state_learning is not None
        else []
    )
    task_required_checks = list(dict.fromkeys([
        *failing_ids,
        *primary_checks,
    ]))
    return [{
        "blocker": "runtime_architecture",
        "file": primary,
        "files": target_files,
        "must_change_files": [primary],
        "focus_id": focus_id,
        "required_checks": task_required_checks,
        "preserve_checks": list(policy.get("baseline_passed_checks") or []),
        "skill_layer": skill_layer,
        "evidence": "\n".join(evidence_lines),
        "architecture_policy": policy,
        "runtime_contract": runtime_contract,
    }]


def _split_reviewer_quality_feedback(feedback):
    """Return actionable reviewer issue snippets, excluding positive check text."""
    text = str(feedback or "").strip()
    if not text:
        return []
    if text.lower().startswith("quality gates failed:"):
        return []

    chunks = []
    for part in re.split(r"(?m)(?:^|\n)\s*(?=\d+[\.)]\s+)", text):
        cleaned = re.sub(r"^\s*\d+[\.)]\s+", "", part.strip())
        if cleaned:
            chunks.append(cleaned)
    if not chunks:
        chunks = [text]

    actionable = []
    problem_markers = (
        "block",
        "issue",
        "violation",
        "dead code",
        "unused",
        "unconsumed",
        "must be",
        "must not",
        "rejected",
        "reject",
        "flag",
        "risk",
        "failed",
        "failure",
        "scope",
    )
    positive_markers = (
        "other checks",
        "compile cleanly",
        "compiles",
        "imports succeed",
        "valid raw tcp client",
        "unchanged and remains",
    )
    for chunk in chunks:
        chunk = re.split(r"(?i)\bOther checks\s*:", chunk, maxsplit=1)[0].strip()
        if not chunk:
            continue
        lower = chunk.lower()
        if not re.search(r"[A-Za-z0-9_./-]+\.py", chunk):
            continue
        if any(marker in lower for marker in positive_markers) and not any(
            marker in lower for marker in ("but", "however", "block", "issue", "violation", "dead code", "unused")
        ):
            continue
        if any(marker in lower for marker in problem_markers):
            actionable.append(chunk.strip())
    return actionable


def _primary_feedback_file(item):
    text = str(item or "")
    scope_files = _scope_drift_feedback_files(text)
    if scope_files:
        return scope_files[0]
    patterns = (
        r"(?:in|on|file)\s+([A-Za-z0-9_./-]+\.py)\s*:",
        r"([A-Za-z0-9_./-]+\.py)\s*:",
        r"([A-Za-z0-9_./-]+\.py)\s+(?:edits|changes|changed|computes|defines|returns|stores)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            rel = Path(match.group(1)).name
            if rel:
                return rel
    files = _extract_quality_failure_files([text])
    return files[0] if files else ""


_SCOPE_DRIFT_FEEDBACK_MARKERS = (
    "unauthorized scope",
    "scope drift",
    "role-boundary violation",
    "role boundary violation",
    "prohibited_files",
    "prohibited files",
    "do_not_touch",
    "do not touch",
    "outside declared target_files",
    "outside master plan target_files",
)

_REVERT_FEEDBACK_MARKERS = ("revert", "restore", "rollback", "roll back")


def _has_scope_drift_marker(item):
    text = str(item or "").lower()
    return any(marker in text for marker in _SCOPE_DRIFT_FEEDBACK_MARKERS)


def _scope_drift_feedback_files(item):
    """Return the actual files that a reviewer asks to revert/restore.

    Reviewer feedback can begin with positive context like "policy.py changes
    are compliant" and only later say "However,
    national_bot.py was in do_not_touch; revert it". The first file mention is
    then explicitly not the repair target. Parse scope-drift/revert cues before
    falling back to generic primary-file extraction.
    """

    text = str(item or "")
    lower = text.lower()
    if not any(marker in lower for marker in _SCOPE_DRIFT_FEEDBACK_MARKERS + _REVERT_FEEDBACK_MARKERS):
        return []

    candidates = []

    def add(value):
        rel = Path(str(value)).name
        if rel and rel.endswith(".py") and rel not in candidates:
            candidates.append(rel)

    for pattern in (
        r"\b(?:revert|restore|rollback|roll\s+back)\s+(?:bots/[A-Za-z0-9_./-]+/)?([A-Za-z0-9_./-]+\.py)\b",
        r"\b([A-Za-z0-9_./-]+\.py)\b[^.\n;]{0,220}\b(?:do_not_touch|do\s+not\s+touch|prohibited_files|prohibited\s+files)\b",
        r"\b([A-Za-z0-9_./-]+\.py)\b[^.\n;]{0,220}\b(?:unauthorized\s+scope|scope\s+drift|role-boundary\s+violation|role\s+boundary\s+violation)\b",
    ):
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            add(match.group(1))

    for part in re.split(r"(?i)\b(?:however|but|nevertheless)\b[:,]?\s*", text)[1:]:
        part_lower = part.lower()
        if any(marker in part_lower for marker in _SCOPE_DRIFT_FEEDBACK_MARKERS + _REVERT_FEEDBACK_MARKERS):
            for filename in _extract_quality_failure_files([part]):
                add(filename)

    return candidates


def _feedback_quality_contracts(feedback):
    """Return file-scoped contracts from reviewer feedback.

    Reviewer prose often names helper files while describing a policy-consumer
    problem, for example "ranges.py returns fields never read by policy.py".
    Use the first/primary file in the issue snippet as the repair target instead
    of expanding to every mentioned file.
    """
    by_file = {}
    for item in _split_reviewer_quality_feedback(feedback):
        scope_files = _scope_drift_feedback_files(item)
        targets = scope_files or [_primary_feedback_file(item)]
        for rel in targets:
            if not rel:
                continue
            by_file.setdefault(rel, []).append(item)

    contracts = []
    for rel in sorted(by_file):
        evidence = "\n".join(dict.fromkeys(by_file[rel]))
        contract = {
            "blocker": "quality_gate",
            "file": rel,
            "evidence": evidence,
        }
        lower = evidence.lower()
        if (
            rel == "policy.py"
            and (
                "hyperparameter tuner" in lower
                or "role boundary" in lower
                or "existing numeric" in lower
                or "existing constant" in lower
                or "threshold" in lower
            )
        ):
            contract["role_hint"] = "tuner"
        if _scope_drift_feedback_files(evidence) and _has_scope_drift_marker(evidence):
            contract["role_hint"] = "scope_revert"
        contracts.append(contract)
    return contracts


def _generic_quality_contracts(
    quality,
    failures,
    claimed_files,
    architecture_contracts=None,
):
    """Build file-scoped fallback contracts for non-mechanical quality blockers."""
    evidence_items = []
    for key in (
        "compile_errors",
        "import_errors",
        "protected_contract_errors",
        "national_native_contract_errors",
        "smoke_errors",
        "national_protocol_errors",
        "national_acceptance_errors",
        "critical_failures",
        "reachability_warnings",
    ):
        evidence_items.extend(_flatten_text_items(quality.get(key)))
    evidence_items = [
        item for item in evidence_items
        if not _is_declared_scope_failure_text(item)
        and not _is_national_native_contract_failure_text(item)
        and not _is_official_smoke_protocol_failure_text(item)
        and not _is_runtime_architecture_failure_text(item)
    ]
    if not evidence_items:
        evidence_items = [
            item for item in failures
            if not str(item).startswith("file_size(")
            and not _is_position_semantics_failure_text(item)
            and not _is_declared_scope_failure_text(item)
            and not _is_national_native_contract_failure_text(item)
            and not _is_official_smoke_protocol_failure_text(item)
            and not _is_runtime_architecture_failure_text(item)
        ]
    evidence_files = _extract_quality_failure_files(evidence_items)
    mechanical_files = {c["file"] for c in _line_count_contracts(quality, failures)}
    mechanical_files.update(c["file"] for c in _position_contracts(quality))
    mechanical_files.update(c["file"] for c in _national_native_contracts(quality, failures))
    mechanical_files.update(c["file"] for c in _official_smoke_contracts(quality, failures))
    mechanical_files.update(c["file"] for c in architecture_contracts or [])
    if not evidence_items:
        return []
    generic_files = evidence_files or [f for f in claimed_files if f not in mechanical_files]
    if not generic_files:
        return []

    contracts = []
    for rel in generic_files:
        matching = [item for item in evidence_items if rel in str(item)]
        contracts.append({
            "blocker": "quality_gate",
            "file": rel,
            "evidence": "\n".join(str(item) for item in (matching or evidence_items)[:8]),
        })
    return contracts


def _quality_repair_contracts(ckpt, feedback=""):
    if not isinstance(ckpt, dict):
        return []
    quality = (ckpt.get("gate_results") or {}).get("quality") or {}
    failures = _quality_failure_items(ckpt)
    claimed_files = _extract_quality_failure_files(failures)
    if not claimed_files and feedback:
        claimed_files = _extract_quality_failure_files([feedback])
    violation_files = _declared_scope_violation_files(ckpt, feedback)
    if violation_files:
        claimed_files = [
            filename for filename in claimed_files if filename not in violation_files
        ]
    architecture_contracts = _architecture_contracts(quality, ckpt)
    contracts = []
    contracts.extend(_line_count_contracts(quality, failures))
    contracts.extend(_position_contracts(quality))
    contracts.extend(_national_native_contracts(quality, failures))
    contracts.extend(_official_smoke_contracts(quality, failures))
    contracts.extend(architecture_contracts)
    contracts.extend(_feedback_quality_contracts(feedback))
    contracts.extend(
        _generic_quality_contracts(
            quality,
            failures,
            claimed_files,
            architecture_contracts=architecture_contracts,
        )
    )

    ordered = []
    seen = set()
    for contract in contracts:
        if Path(str(contract.get("file") or "")).name not in _ACTIVE_CANDIDATE_WRITABLE_FILES:
            # System artifacts, extra modules, and undeclared files are not
            # repaired by candidate Workers. Their owning gate remains failed.
            continue
        key = (contract.get("blocker"), contract.get("file"))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(contract)
    return ordered


def _format_position_details(details):
    lines = []
    for detail in details or []:
        line = detail.get("line")
        message = detail.get("message") or detail.get("evidence") or ""
        lines.append(f"- line {line}: {message}" if line else f"- {message}")
    return "\n".join(lines) if lines else "- gate reported a position_semantics violation in this file"


def _quality_contract_task(contract, ckpt, preservation, task_kind):
    next_v = ckpt.get("next_v")
    filename = contract["file"]
    if Path(str(filename)).name not in _ACTIVE_CANDIDATE_WRITABLE_FILES:
        raise ValueError(
            "quality repair cannot make a system or extra artifact writable: "
            f"{filename}"
        )
    suffix = _task_id_suffix(filename)
    blocker = contract.get("blocker")
    if blocker == "file_size":
        current = contract.get("current_lines")
        limit = contract.get("line_limit")
        overage = None
        required = (
            f"Reduce `{filename}` to <= {limit} lines."
            if limit else f"Reduce `{filename}` enough to clear the file_size gate."
        )
        if current is not None and limit is not None:
            try:
                overage = int(current) - int(limit)
            except (TypeError, ValueError):
                overage = None
            required += f" Current gate reading: {current}L/{limit}L."
        large_overage = ""
        if overage is not None and overage >= 200:
            target_removal = overage + 50
            large_overage = (
                "\nLarge-overage requirement:\n"
                f"- This file is {overage} lines over the gate. Do not spend the attempt "
                "on tiny comment trimming alone.\n"
                f"- Before editing, identify a removal/consolidation plan worth at least "
                f"{target_removal} lines so the final file has margin under the limit.\n"
                "- Remove whole dead/debug/self-test blocks, duplicated historical notes, "
                "and unreferenced helper wrappers first. If comments cannot meet the target, "
                "delete or consolidate unreachable helper code verified by local grep/references.\n"
                "- A script-based rewrite is acceptable when it writes only this assigned file; "
                "run `wc -l` early and again before finishing.\n"
            )
        prompt = (
            f"{preservation.format(next_v=next_v)}\n\n"
            f"Repair contract: file_size\n"
            f"- Target file: `{filename}`\n"
            f"- Evidence: {contract.get('evidence') or 'file_size gate failed'}\n"
            f"- Required outcome: {required}\n\n"
            f"{large_overage}"
            "Required method:\n"
            f"- Edit `{filename}`. This file is listed in `must_change_files`; a no-op or editing only other files is failure.\n"
            "- Prefer deleting duplicated/dead comments, stale historical notes, or redundant helper wrappers before touching active decisions.\n"
            "- Do not remove active strategy branches just to save lines.\n"
            f"- Verify with `wc -l bots/national_v{next_v}/{filename}` before finishing.\n"
            "- End your output with the exact line count you observed."
        )
        return {
            "worker_id": f"auto_quality_repair_file_size_{suffix}",
            "role": "Algorithmic Logic Architect",
            "target_files": [filename],
            "must_change_files": [filename],
            "worker_prompt": prompt,
            "task_kind": task_kind,
            "repair_blocker": "file_size",
            "repair_contract": contract,
        }
    if blocker == "position_semantics":
        prompt = (
            f"{preservation.format(next_v=next_v)}\n\n"
            f"Repair contract: position_semantics\n"
            f"- Target file: `{filename}`\n"
            f"- Flagged locations:\n{_format_position_details(contract.get('details'))}\n\n"
            "Authoritative typed position contract:\n"
            "- Read `decision_context.hand.position` or `decision_context.line.position`; values are `small_blind` and `big_blind`.\n"
            "- Read `decision_context.hand.acts_first_postflop`; it is true only for `big_blind`.\n"
            "- Read `decision_context.line.hero_in_position_postflop`; it is true only for `small_blind`.\n"
            "- Read `decision_context.line.can_donk`, `can_delayed_probe`, and `responding_to_check` as system-derived facts.\n\n"
            "Required method:\n"
            f"- Edit `{filename}`. This file is listed in `must_change_files`; a no-op or editing only another file is failure.\n"
            "- Remove every candidate-side seat/action-order derivation and replace it with a direct read of the typed fields above.\n"
            "- Do not introduce alternate top-level context keys or inspect protocol/runtime internals.\n"
            "- If the flagged line is prose/comment/test text, update that text to the authoritative contract above.\n"
            "- Do not change card mapping, action protocol, or unrelated strategy behavior.\n"
            "- Before finishing, verify every position-dependent branch is sourced from `hand` or `line` in `decision_context`."
        )
        return {
            "worker_id": f"auto_quality_repair_position_{suffix}",
            "role": "Algorithmic Logic Architect",
            "target_files": [filename],
            "must_change_files": [filename],
            "worker_prompt": prompt,
            "task_kind": task_kind,
            "repair_blocker": "position_semantics",
            "repair_contract": contract,
        }
    if blocker in {"national_native_contract", "official_smoke"}:
        raise ValueError(
            f"{blocker} is a fail-closed system-runtime blocker, not a Worker repair"
        )
    if blocker == "runtime_architecture":
        targets = ["policy.py"]
        must_change = ["policy.py"]
        focus_id = str(contract.get("focus_id") or "")
        policy = contract.get("architecture_policy") or {}
        focus = policy.get("selected_focus") or {}
        required_checks = [str(item) for item in contract.get("required_checks") or []]
        preserve_checks = [str(item) for item in contract.get("preserve_checks") or []]
        skill_layer = str(contract.get("skill_layer") or "runtime_architecture")
        runtime_contract = contract.get("runtime_contract") or {}
        owner_files = []
        match_memory = runtime_contract.get("match_memory") or {}
        if isinstance(match_memory, dict) and match_memory.get("owner_file"):
            owner_files.append(Path(str(match_memory["owner_file"])).name)
        for artifact in runtime_contract.get("precompute_artifacts") or []:
            if isinstance(artifact, dict) and artifact.get("owner_file"):
                owner_files.append(Path(str(artifact["owner_file"])).name)
        state_learning = runtime_contract.get("state_learning") or {}
        if (
            state_learning.get("profile_dimensions")
            or state_learning.get("line_controls")
        ):
            owner_files.append("national_bot.py")
        read_only_dependencies = list(dict.fromkeys([
            "national_bot.py",
            "precompute.py",
            *(
                owner
                for owner in owner_files
                if owner in {"national_bot.py", "precompute.py"}
            ),
        ]))
        files_allowed = []
        try:
            selected_state = RuntimeContract.model_validate(runtime_contract).state_learning
            primary_innovation = (
                selected_state.primary_innovation() if selected_state is not None else ""
            )
        except Exception:
            primary_innovation = ""
        primary_guidance = {
            "sample_counted_candidate_batch": (
                "- Primary innovation: publish a sanitized legal baseline, then run real "
                "deadline-scaled candidate batches. Candidate-reported `sample_count`, "
                "`confidence`, and `complete` are diagnostic only; hard proof is system-trusted "
                "iterator steps, CPU/elapsed work, true StopIteration exhaustion, and the sanitized "
                "action trajectory. Stop early at low uncertainty. Design for the local 2-second "
                "strength envelope; the official 55-second ceiling is safety headroom, not a target "
                "to spend on every decision.\n"
            ),
            "action_profile": (
                "- Primary innovation: consume the `action_profile` fields from bounded "
                "`decision_context.opponent`, scale by confidence, and prove a typed-intent "
                "counterfactual plus telemetry.\n"
            ),
            "terminal_response": (
                "- Primary innovation: consume terminal-response fold-to-raise/fold-to-jam/"
                "river-overcall posteriors with confidence and prove a sanitized-action "
                "counterfactual plus telemetry.\n"
            ),
            "showdown_range": (
                "- Primary innovation: consume the selection-aware `showdown_range` posterior "
                "with confidence and prove a tight/loose sanitized-action counterfactual plus telemetry.\n"
            ),
            "donk": (
                "- Primary innovation: consume `decision_context.line.can_donk` and prove its "
                "one-predicate positive/control transcript changes a typed intent and telemetry.\n"
            ),
            "delayed_probe": (
                "- Primary innovation: consume `decision_context.line.can_delayed_probe` and prove "
                "its one-predicate positive/control transcript changes a typed intent and telemetry.\n"
            ),
        }.get(primary_innovation, "")
        if skill_layer in {"match_memory", "opponent_model"}:
            role = "Opponent Modeler"
        else:
            role = "Algorithmic Runtime Architect"
        primary_scope_line = f"- Typed primary innovation: `{primary_innovation or 'none'}`. Other policy dimensions are shadow/advisory unless listed in parent preservation checks.\n"
        prompt = (
            f"{preservation.format(next_v=next_v)}\n\n"
            "Repair contract: runtime_architecture\n"
            f"- Architecture focus: `{focus_id or 'parent_capability_regression'}`\n"
            f"- Focus rationale: {focus.get('rationale') or 'Restore evidence-backed runtime behavior.'}\n"
            f"- Required AST checks: {', '.join(required_checks)}\n"
            f"- Parent checks that must not regress: {', '.join(preserve_checks)}\n"
            "- Writable candidate file: `policy.py` only.\n"
            f"- Files that must change: {', '.join(f'`{item}`' for item in must_change)}\n"
            f"- Read-only system dependencies: {', '.join(f'`{item}`' for item in read_only_dependencies) or 'none'}; never edit these files.\n"
            f"{primary_scope_line}"
            f"- Detector evidence:\n{contract.get('evidence') or 'transition hard gate failed'}\n\n"
            "Executable RuntimeContract (implement it; do not merely copy its names):\n"
            f"```json\n{json.dumps(runtime_contract, ensure_ascii=False, indent=2)}\n```\n\n"
            "Required method:\n"
            "- Read every target plus the source-parent counterpart before editing. Preserve the legal fast baseline.\n"
            "- Implement the behavior in policy.get_baseline_decision and/or policy.iter_decisions over the schema-versioned decision_context. A class, cache, label, comment, or telemetry field that neither entrypoint consumes is failure.\n"
            f"{primary_guidance}"
            "- Treat decision_context.hand/betting/history/line/legal/opponent as the only authoritative decision input; never reconstruct another protocol history.\n"
            "- Do not weaken native TCP, official wire, card mapping, or any parent capability to make the selected check pass.\n"
            "- Run `evaluate_national_capabilities` on the candidate and report the required check states before finishing."
        )
        return {
            "worker_id": f"auto_runtime_architecture_{_task_id_suffix(focus_id or filename)}",
            "role": role,
            "target_files": targets,
            "files_allowed": files_allowed,
            "read_only_dependencies": read_only_dependencies,
            "must_change_files": must_change,
            "worker_prompt": prompt,
            "task_kind": task_kind,
            "repair_blocker": "runtime_architecture",
            "repair_contract": contract,
            "skill_layer": skill_layer,
            "architecture_focus_id": focus_id,
            "runtime_contract": runtime_contract,
            "checks_required": required_checks,
        }
    evidence = contract.get('evidence') or 'quality gate failed'
    if contract.get("role_hint") == "tuner":
        role = "Hyperparameter Tuner"
    elif contract.get("role_hint") == "scope_revert":
        role = "Scope Boundary Repair Architect"
    else:
        role = "Algorithmic Logic Architect"
    reachability_guidance = ""
    if "reachability" in str(evidence).lower():
        reachability_guidance = (
            "\nReachability-specific method:\n"
            "- If the flagged symbol is a top-level `_self_test_*` or probe helper, "
            "remove it or move the assertions under `if __name__ == \"__main__\":`.\n"
            "- If the helper is real runtime logic, wire it into the actual strategy "
            "dispatch path that consumes its result.\n"
            "- Do not add a dummy reference, unused import, or unreachable call just "
            "to silence the gate.\n"
        )
    role_guidance = ""
    if role == "Hyperparameter Tuner":
        role_guidance = (
            "\nConstants-only role method:\n"
            "- This repair is assigned to Hyperparameter Tuner because the reviewer "
            "evidence concerns an existing numeric constant/threshold in `policy.py`.\n"
            "- Edit only an existing numeric constant in `policy.py`; do not add imports, functions, classes, loops, "
            "or control flow.\n"
            "- Fix the exact reviewer evidence by reverting or retuning the named "
            "numeric constant as a Tuner-owned change, with adjacent rationale if needed.\n"
            "- Do not touch protocol/card mapping or non-constant strategy code.\n"
        )
    elif role == "Scope Boundary Repair Architect":
        role_guidance = (
            "\nScope-drift repair method:\n"
            "- The reviewer evidence says this file changed outside the approved worker scope.\n"
            "- Apply only the exact rollback described in the injected evidence; the source parent is not readable by the Worker.\n"
            "- Do not add strategy thresholds, protocol refactors, helper subsystems, or action-behavior changes.\n"
            "- Keep the repair limited to restoring the approved scope boundary; other candidate files are intentionally preserved.\n"
        )
    prompt = (
        f"{preservation.format(next_v=next_v)}\n\n"
        f"Repair contract: quality_gate\n"
        f"- Target file: `{filename}`\n"
        f"- Evidence:\n{evidence}\n\n"
        f"{reachability_guidance}"
        f"{role_guidance}"
        "Required method:\n"
        f"- Edit `{filename}`. This file is listed in `must_change_files`; a no-op or editing only another file is failure.\n"
        "- Fix only the listed gate blocker.\n"
        "- Preserve national protocol/card mapping and previously passing behavior.\n"
        "- Run `python -m py_compile` on the exact edited file before finishing; system gates own imports and execution."
    )
    return {
        "worker_id": f"auto_quality_repair_gate_{suffix}",
        "role": role,
        "target_files": [filename],
        "must_change_files": [filename],
        "worker_prompt": prompt,
        "task_kind": task_kind,
        "repair_blocker": "quality_gate",
        "repair_contract": contract,
    }


def _text_line_count(text):
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _docstring_line_ranges(text):
    ranges = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ranges
    node_types = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if not isinstance(node, node_types) or not getattr(node, "body", None):
            continue
        first = node.body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(getattr(first, "value", None), ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        if not isinstance(node, ast.Module) and len(node.body) == 1:
            continue
        end_lineno = getattr(first, "end_lineno", first.lineno)
        ranges.update(range(first.lineno, end_lineno + 1))
    return ranges


def _tokenized_comment_and_string_lines(text):
    comment_lines = set()
    string_lines = set()
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for tok in tokens:
            if tok.type == tokenize.COMMENT:
                line = tok.line or ""
                if not line[:tok.start[1]].strip():
                    comment_lines.add(tok.start[0])
            elif tok.type == tokenize.STRING:
                string_lines.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, IndentationError):
        pass
    return comment_lines, string_lines


def _mechanically_trim_python_text(text):
    """Remove non-behavioral Python text and return ``(new_text, stats)``."""
    lines = text.splitlines(keepends=True)
    before = len(lines)
    if not lines:
        return text, {"before": 0, "after": 0, "removed": 0}

    docstring_lines = _docstring_line_ranges(text)
    comment_lines, string_lines = _tokenized_comment_and_string_lines(text)
    protected_string_lines = string_lines - docstring_lines
    remove_lines = set(docstring_lines)
    remove_lines.update(comment_lines - protected_string_lines)
    for idx, line in enumerate(lines, start=1):
        if idx not in protected_string_lines and not line.strip():
            remove_lines.add(idx)

    trimmed_lines = [
        line for idx, line in enumerate(lines, start=1)
        if idx not in remove_lines
    ]
    new_text = "".join(trimmed_lines)
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    after = _text_line_count(new_text)
    return new_text, {
        "before": before,
        "after": after,
        "removed": before - after,
        "docstring_lines": len(docstring_lines),
        "comment_lines": len(comment_lines),
        "blank_lines": sum(
            1 for idx, line in enumerate(lines, start=1)
            if idx in remove_lines and not line.strip()
        ),
    }


def _mechanical_trim_python_file(path, limit):
    path = Path(path)
    try:
        old_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"changed": False, "error": str(exc), "file": str(path)}
    before = _text_line_count(old_text)
    if limit is not None and before <= int(limit):
        return {"changed": False, "file": str(path), "before": before, "after": before, "limit": limit}

    new_text, stats = _mechanically_trim_python_text(old_text)
    after = _text_line_count(new_text)
    if after >= before:
        return {"changed": False, "file": str(path), "before": before, "after": after, "limit": limit}

    try:
        path.write_text(new_text, encoding="utf-8")
        py_compile.compile(str(path), doraise=True)
    except Exception as exc:
        try:
            path.write_text(old_text, encoding="utf-8")
        except OSError:
            pass
        return {
            "changed": False,
            "rolled_back": True,
            "error": str(exc),
            "file": str(path),
            "before": before,
            "after": before,
            "attempted_after": after,
            "limit": limit,
        }
    return {"changed": True, "file": str(path), "limit": limit, **stats}


def _apply_mechanical_file_size_trims(tasks, next_dir, source_dir, next_v, source_v):
    """Apply behavior-preserving text trims before expensive file_size workers."""
    try:
        _total, oversized = check_code_size(next_dir, source_dir=source_dir)
    except Exception as exc:
        log_system_event(
            "pipeline.file_size_mechanical_trim_check_failed",
            "warn",
            f"Could not compute file_size mechanical trim inputs for v{next_v}: {exc}",
            {"next_v": next_v, "source_v": source_v},
        )
        return []
    oversized_by_name = {Path(name).name: (lines, limit) for name, lines, limit in oversized}
    results = []
    for task in tasks or []:
        if not _is_file_size_repair_task(task):
            continue
        for target in task.get("target_files", []) or []:
            rel = _target_rel(target, next_v)
            if not rel:
                continue
            filename = Path(rel).name
            current = oversized_by_name.get(filename)
            if not current:
                continue
            lines, limit = current
            if int(lines) - int(limit) < 200:
                continue
            path = next_dir / rel
            result = _mechanical_trim_python_file(path, limit)
            result.update({
                "next_v": next_v,
                "source_v": source_v,
                "target": rel,
                "initial_lines": lines,
            })
            results.append(result)
            if result.get("changed"):
                log_system_event(
                    "pipeline.file_size_mechanical_trim_applied",
                    "warn",
                    (
                        f"Applied mechanical file_size trim to v{next_v}/{rel}: "
                        f"{result.get('before')}L -> {result.get('after')}L "
                        f"(limit {limit})"
                    ),
                    result,
                )
            elif result.get("error"):
                log_system_event(
                    "pipeline.file_size_mechanical_trim_failed",
                    "warn",
                    f"Mechanical file_size trim failed for v{next_v}/{rel}: {result.get('error')}",
                    result,
                )
    return results


def _precommit_repair_task(filename, ckpt, feedback):
    if Path(str(filename)).name not in _ACTIVE_CANDIDATE_WRITABLE_FILES:
        raise ValueError(
            f"precommit repair cannot write system/extra artifact {filename!r}"
        )
    next_v = ckpt.get("next_v")
    source_v = ckpt.get("source_v")
    suffix = _task_id_suffix(filename)
    line_note = ""
    try:
        path = get_bot_dir(next_v) / filename
        if path.exists():
            line_count = sum(1 for _ in path.open("r", encoding="utf-8", errors="ignore"))
            if line_count >= 2300:
                line_note = (
                    f"\n- `{filename}` is near the hard size cap ({line_count} lines). "
                    "Prefer deleting or tightening an existing risky branch over adding a new subsystem."
                )
    except Exception:
        line_note = ""

    prompt = (
        "This is one file-scoped precommit regression repair from a failed native "
        f"national TCP final gate for bots/national_v{next_v}.\n\n"
        f"Target file: `{filename}`\n"
        f"Source lineage identity: national_v{source_v} (not readable by this Worker)\n"
        f"Failed candidate: bots/national_v{next_v}/\n\n"
        f"Exact precommit feedback:\n{feedback}\n\n"
        "Non-negotiable national position invariant:\n"
        "- This invariant is protocol correctness, not an EV/matchup lever. Do not change, relax, "
        "or roll it back to chase a precommit result.\n"
        "- Read `decision_context.hand.position`/`acts_first_postflop` and "
        "`decision_context.line.position`/`hero_in_position_postflop` directly.\n"
        "- `big_blind` acts first postflop; `small_blind` is in position postflop.\n"
        "- Never reconstruct seat identity, action order, donk, delayed-probe, or "
        "responding-to-check state inside candidate policy.\n"
        "- Preserve the candidate's national TCP position semantics and the official oracle boundaries.\n\n"
        "Required method:\n"
        f"- Only edit `{filename}`. Other files are intentionally out of scope for this worker.\n"
        "- This is a policy/matchup repair. `policy.py` is the sole writable file; "
        "national_bot.py and precompute.py remain byte-identical system artifacts.\n"
        "- Use the system-injected precommit feedback and current candidate region to identify "
        "which changed behavior could explain the losing complete 70-hand native TCP samples.\n"
        "- Make one bounded EV/matchup correction in this file. Prefer tightening, gating, or partially "
        "rolling back a risky new branch over adding broad new logic.\n"
        "- Do not wholesale replace the candidate with the source parent; the final candidate must remain "
        "a real code change after repair.\n"
        "- Preserve native TCP protocol/card mapping, national action legality, and previously passed quality gates.\n"
        f"- Run `python -m py_compile bots/national_v{next_v}/{filename}` before finishing; "
        "system gates own imports and dynamic execution."
        f"{line_note}"
    )
    return {
        "worker_id": f"auto_precommit_repair_{suffix}",
        "role": "Strategic Regression Repair Architect",
        "target_files": [filename],
        "must_change_files": [filename],
        "worker_prompt": prompt,
        "task_kind": "precommit_repair",
        "repair_blocker": "precommit_regression",
        "repair_contract": {
            "blocker": "precommit_regression",
            "file": filename,
            "evidence": feedback[:2000],
            "protected_invariants": ["national_position_semantics"],
        },
    }


def _precommit_repair_tasks(ckpt, feedback):
    return [
        _precommit_repair_task(filename, ckpt, feedback)
        for filename in _precommit_repair_target_files(ckpt, feedback)
    ]


def _precommit_repair_task_refresh_reason(tasks, ckpt, feedback=""):
    if not _is_precommit_rework_checkpoint(ckpt):
        return ""
    if not tasks:
        return "missing precommit repair task(s)"

    expected = set(_precommit_repair_target_files(ckpt, feedback))
    task_targets = []
    for task in tasks or []:
        if not isinstance(task, dict):
            return "invalid precommit repair task"
        task_kind = str(task.get("task_kind") or "").lower()
        task_text = " ".join([
            str(task.get("worker_id", "")),
            str(task.get("role", "")),
            str(task.get("worker_prompt", task.get("instruction", "")))[:500],
        ]).lower()
        if "precommit_repair" not in task_kind and "precommit" not in task_text:
            return "checkpoint task is not a precommit repair"
        prompt_text = str(task.get("worker_prompt", task.get("instruction", ""))).lower()
        if (
            "national position invariant" not in prompt_text
            or "decision_context.hand.position" not in prompt_text
            or "decision_context.line.position" not in prompt_text
            or "not an ev/matchup lever" not in prompt_text
        ):
            return "precommit repair task is missing national position invariant"
        targets = [
            rel for rel in (
                _target_rel(target, ckpt.get("next_v"))
                for target in task.get("target_files", []) or []
            )
            if rel
        ]
        must_change = [
            rel for rel in (
                _target_rel(target, ckpt.get("next_v"))
                for target in task.get("must_change_files", []) or []
            )
            if rel
        ]
        if len(targets) != 1:
            return "precommit repair task is not file-scoped"
        if must_change and must_change != targets:
            return "precommit repair must_change_files do not match its single target"
        task_targets.extend(targets)

    task_set = set(task_targets)
    if expected and task_set != expected:
        return "precommit repair targets are stale"
    if len(task_targets) != len(task_set):
        return "duplicate precommit repair targets"
    return ""


def _synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback=""):
    """Build bounded repair tasks when a checkpoint has gate feedback but no plan.

    Legacy crossover checkpoints and the defensive hard-position repair route
    may store a synthetic plan with no worker tasks. New crossover generations
    stop at ``prepared`` and pass through direction audit, Master, and Workers
    before quality; deterministic task synthesis remains necessary for older or
    explicit repair checkpoints.
    """
    if not isinstance(ckpt, dict):
        return []
    stage = ckpt.get("stage")
    if stage not in {"quality_failed", "repair_planned", "rework_running", "precommit_failed", "official_failed"}:
        return []
    if _has_legacy_critic_repair_contract(
        ckpt,
        _checkpoint_master_plan(ckpt).get("tasks", []),
    ):
        # Retired Critic-owned repair checkpoints require controlled recovery;
        # silently translating them into a quality repair would mutate the
        # candidate under an authority that no longer exists.
        return []

    feedback = str(reviewer_feedback or _checkpoint_rework_feedback(ckpt) or "").strip()
    if not feedback:
        return []

    master_plan = _checkpoint_master_plan(ckpt)
    is_precommit_rework = _is_precommit_rework_checkpoint(ckpt)
    is_official_rework = _is_official_rework_checkpoint(ckpt)
    is_review_rework = _is_review_rework_checkpoint(ckpt)
    quality_contracts = (
        []
        if is_precommit_rework or is_official_rework or is_review_rework
        else _quality_repair_contracts(ckpt, feedback)
    )
    if is_precommit_rework:
        return _precommit_repair_tasks(ckpt, feedback)
    elif is_official_rework:
        return _official_repair_tasks(ckpt, feedback)
    elif is_review_rework:
        target_files = _review_repair_target_files(ckpt, feedback)
    elif quality_contracts:
        target_files = [contract["file"] for contract in quality_contracts]
    elif reviewer_feedback:
        return []
    else:
        failures = _quality_failure_items(ckpt)
        target_files = _extract_quality_failure_files(failures)
        if not target_files:
            target_files = _extract_quality_failure_files([feedback])
    if not target_files:
        return []

    targets = target_files
    is_crossover = bool(ckpt.get("parent2_v")) or master_plan.get("strategy") == "crossover"
    if is_review_rework:
        preservation = (
            "This is a Lead Code Reviewer hard-gate repair. Preserve the current "
            "candidate in bots/national_v{next_v}; fix the exact code-quality "
            "blocker named by the reviewer. Do not chase secondary notes unless "
            "they are required to resolve the primary blocker."
        )
        method = (
            "- Read all listed target files and the quoted reviewer feedback before editing.\n"
            "- Resolve the primary rejected state coherently. If the feedback offers mutually exclusive paths, choose ONE complete path.\n"
            "- Do not leave defined-but-unwired helpers, misleading comments/docstrings, unused imports, or half-restored systems.\n"
            "- Keep the candidate's already-passing national protocol/card mapping behavior intact.\n"
            "- Run `python -m py_compile` on the exact edited file before finishing; system gates own imports and self-tests."
        )
        worker_id = "auto_review_repair"
        role = "Algorithmic Logic Architect"
        task_kind = "crossover_review_repair" if is_crossover else "review_repair"
    elif is_crossover and stage in {"quality_failed", "repair_planned", "rework_running"}:
        preservation = (
            "This is a crossover quality repair. Preserve the current candidate's "
            "crossover behavior in bots/national_v{next_v}; fix only the blocking "
            "quality-gate issues unless a tiny local cleanup is required."
        )
        method = (
            "- Read the listed target files before editing.\n"
            "- For file_size blockers, remove dead/duplicated code or consolidate helper logic; do not weaken strategy by deleting active decisions blindly.\n"
            "- For position_semantics blockers, remove local seat derivation and read `decision_context.hand.position`/`acts_first_postflop` plus `decision_context.line.position` directly.\n"
            "- Do not change protocol/card mapping behavior outside the named blockers.\n"
            "- Leave stderr telemetry honest if touched."
        )
        worker_id = "auto_quality_repair"
        role = "Algorithmic Logic Architect"
        task_kind = "quality_repair"
    else:
        preservation = (
            "This is a gate repair. Make the smallest structural correction that "
            "clears the listed blockers while preserving the intended strategy."
        )
        method = (
            "- Read the listed target files before editing.\n"
            "- For file_size blockers, remove dead/duplicated code or consolidate helper logic; do not weaken strategy by deleting active decisions blindly.\n"
            "- For position_semantics blockers, remove local seat derivation and read `decision_context.hand.position`/`acts_first_postflop` plus `decision_context.line.position` directly.\n"
            "- Do not change protocol/card mapping behavior outside the named blockers.\n"
            "- Leave stderr telemetry honest if touched."
        )
        worker_id = "auto_quality_repair"
        role = "Algorithmic Logic Architect"
        task_kind = "quality_repair"

    if quality_contracts:
        return _order_quality_repair_tasks([
            _quality_contract_task(contract, ckpt, preservation, task_kind)
            for contract in quality_contracts
        ])

    prompt = (
        f"{preservation.format(next_v=ckpt.get('next_v'))}\n\n"
        f"Exact gate feedback:\n{feedback}\n\n"
        f"Required method:\n{method}"
    )
    repair_blocker = (
        "review_rejection"
        if is_review_rework
        else "quality_gate"
    )
    return [{
        "worker_id": worker_id,
        "role": role,
        "target_files": targets,
        "must_change_files": targets,
        "worker_prompt": prompt,
        "task_kind": task_kind,
        "repair_blocker": repair_blocker,
        "repair_contract": {
            "blocker": repair_blocker,
            "files": targets,
            "evidence": feedback[:2000],
            "source_stage": str(stage or ""),
        },
    }]


def _transport_equivalent_feedback(left, right):
    """Compare an MCP-carried feedback string without granting rewrite power.

    JSON/TCP transports may normalize line endings or omit one surrounding
    newline.  Those representations are equivalent; changing any non-boundary
    content is not.  In particular, whitespace inside paths/evidence remains
    significant so a caller cannot smuggle a second repair directive.
    """

    def normalize(value):
        if not isinstance(value, str):
            return None
        return value.replace("\r\n", "\n").replace("\r", "\n").strip()

    normalized_left = normalize(left)
    normalized_right = normalize(right)
    return (
        normalized_left is not None
        and normalized_right is not None
        and normalized_left == normalized_right
    )


def _repair_contract_signature(task, next_v):
    """Return a content signature for one system-owned repair contract.

    This is an integrity receipt, not caller authority.  It binds the gate
    contract to the writable task scope; execute_workers still compares the
    complete canonical task list before accepting a non-empty caller echo.
    """
    if not isinstance(task, dict):
        return ""
    contract = task.get("repair_contract")
    if not isinstance(contract, dict) or not str(contract.get("blocker") or "").strip():
        return ""

    raw_contract_files = contract.get("files")
    if raw_contract_files is None:
        raw_contract_files = [contract.get("file")]
    if not isinstance(raw_contract_files, (list, tuple)):
        return ""
    contract_files = set()
    for target in raw_contract_files:
        rel = _target_rel(target, next_v)
        if not rel:
            return ""
        contract_files.add(rel)

    writable_files = _task_declared_scope_files(task, next_v)
    # The contract's primary file(s) must be writable.  A system runtime
    # contract may derive additional files_allowed from typed owner_file fields;
    # those are bound by writable_files in the signature payload even when the
    # human-facing repair_contract keeps only its primary targets.
    if not contract_files or not writable_files or not contract_files.issubset(writable_files):
        return ""
    payload = {
        "repair_contract": contract,
        "writable_files": sorted(writable_files),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _authoritative_rework_tasks(ckpt, feedback):
    """Rebuild the only tasks authorized by immutable checkpoint/gate evidence."""
    tasks = _synthesize_rework_tasks_from_checkpoint(ckpt, feedback)
    errors = []
    if not tasks:
        errors.append("system_repair_task_synthesis_empty")
        return [], errors
    for index, task in enumerate(tasks):
        if not _repair_contract_signature(task, ckpt.get("next_v")):
            worker_id = task.get("worker_id") if isinstance(task, dict) else None
            errors.append(
                f"task[{index}]_repair_contract_signature_invalid:{worker_id or 'unknown'}"
            )
    return tasks, errors


def _should_reset_before_rework(ckpt, tasks):
    """Return False for in-place repairs that must preserve the current candidate."""
    if not isinstance(ckpt, dict):
        return True
    if _is_precommit_rework_checkpoint(ckpt):
        return False
    stage = ckpt.get("stage")
    if stage not in {"quality_failed", "repair_planned", "rework_running", "official_failed"}:
        return True
    master_plan = ckpt.get("master_plan") if isinstance(ckpt.get("master_plan"), dict) else {}
    work_item = master_plan.get("work_item") if isinstance(master_plan.get("work_item"), dict) else {}
    work_kind = str(work_item.get("kind") or "")
    task_kinds = {
        str(task.get("task_kind") or "")
        for task in tasks or []
        if isinstance(task, dict)
    }
    is_official_repair = (
        "official_repair" in work_kind
        or any("official_repair" in kind for kind in task_kinds)
        or _is_official_rework_checkpoint(ckpt)
    )
    if is_official_repair:
        return False
    is_review_repair = (
        "review_repair" in work_kind
        or any("review_repair" in kind for kind in task_kinds)
        or _is_review_rework_checkpoint(ckpt)
    )
    if is_review_repair:
        return False
    is_quality_repair = (
        stage == "quality_failed"
        or "quality_repair" in work_kind
        or work_kind == "crossover_gate_rework"
        or any("quality_repair" in kind for kind in task_kinds)
    )
    if is_quality_repair and "precommit" not in work_kind:
        return False
    is_crossover = (
        bool(ckpt.get("parent2_v"))
        or master_plan.get("strategy") == "crossover"
        or work_kind.startswith("crossover_")
    )
    if not is_crossover:
        return True
    return True


def _load_worker_prompt_template(prompts_dir, *, native_tcp=None):
    """Compose the worker harness for the sole national-native profile."""
    prompts_dir = Path(prompts_dir)
    if native_tcp is None:
        from workflow_profiles import get_workflow_profile

        native_tcp = (
            getattr(get_workflow_profile(), "national_execution_mode", "native_tcp")
            == "native_tcp"
        )
    if not native_tcp:
        raise RuntimeError("active Worker execution requires national native TCP")
    common = (prompts_dir / "worker_prompt.md").read_text(encoding="utf-8")
    marker = "{execution_profile_contract}"
    if common.count(marker) != 1:
        raise RuntimeError(
            "worker_prompt.md must contain exactly one execution profile marker"
        )
    profile = (prompts_dir / "worker_profile_national_native.md").read_text(
        encoding="utf-8"
    )
    return common.replace(marker, profile)


def _durable_checkpoint_contract_matches(checkpoint, contract):
    if not isinstance(checkpoint, dict) or not isinstance(contract, dict):
        return False
    checkpoint_workflow_id = str(
        checkpoint.get("workflow_run_id")
        or checkpoint.get("run_id")
        or (
            f"{int(checkpoint.get('next_v'))}#"
            f"{int(checkpoint.get('generation_attempt') or 0)}"
        )
    )
    return (
        checkpoint_workflow_id
        == str(contract.get("workflow_run_id") or "")
        and int(checkpoint.get("checkpoint_revision") or 0)
        == int(contract.get("checkpoint_revision") or 0)
        and str(checkpoint.get("stage") or "")
        == str(contract.get("checkpoint_stage") or "")
    )


def _durable_output_already_projected(checkpoint, projection):
    if not isinstance(checkpoint, dict):
        return False
    contract = projection.get("checkpoint_contract") or {}
    checkpoint_workflow_id = str(
        checkpoint.get("workflow_run_id")
        or checkpoint.get("run_id")
        or ""
    )
    if checkpoint_workflow_id != str(contract.get("workflow_run_id") or ""):
        return False
    receipt = (
        (checkpoint.get("audit_context") or {}).get("durable_worker_output")
        if isinstance(checkpoint.get("audit_context"), dict)
        else None
    )
    expected = projection.get("durable_worker_output") or {}
    return bool(
        isinstance(receipt, dict)
        and receipt.get("artifact_hash") == expected.get("artifact_hash")
        and receipt.get("envelope_digest") == expected.get("envelope_digest")
    )


async def _project_durable_worker_output(worker_workflow, next_dir, state):
    """Project a completed immutable Worker receipt without invoking an LLM."""
    projection = deepcopy(state.get("projection") or {})
    envelope = state.get("envelope") or {}
    next_v = int(envelope.get("next_v"))
    source_v = int(envelope.get("source_v"))
    contract = projection.get("checkpoint_contract") or {}
    checkpoint = _matching_checkpoint(next_v, source_v)
    if _durable_output_already_projected(checkpoint, projection):
        # At the immediate workers_done projection, reconcile a missing or
        # poisoned canonical tree from the immutable artifact.  If downstream
        # gates already advanced the checkpoint, their matching receipt proves
        # this output was published; never rewind candidate bytes that a later
        # authorized stage may have transformed.
        if checkpoint.get("stage") == "workers_done":
            expected_output = str(state.get("output_artifact_hash") or "")
            canonical_exists = Path(next_dir).exists()
            canonical_hash = (
                _complete_artifact_fingerprint(next_dir)
                if canonical_exists
                else ""
            )
            if canonical_exists and canonical_hash != expected_output:
                return _json_tool_result({
                    "error": "DURABLE_WORKER_PROJECTED_ARTIFACT_MISMATCH",
                    "success": False,
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                })
            if not canonical_exists:
                worker_workflow.artifacts.materialize(
                    str(state.get("output_snapshot_hash") or ""),
                    next_dir,
                    expected_destination_digest=None,
                )
            if _complete_artifact_fingerprint(next_dir) != expected_output:
                return _json_tool_result({
                    "error": "DURABLE_WORKER_PROJECTED_ARTIFACT_MISMATCH",
                    "success": False,
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                })
        worker_workflow.projected("workers_done")
        return _json_tool_result({
            "success": True,
            "durable_recovery": (
                "confirmed_existing_worker_projection"
                if checkpoint.get("stage") == "workers_done"
                else "confirmed_downstream_worker_projection"
            ),
            "current_checkpoint_stage": checkpoint.get("stage"),
            "output_artifact_hash": state.get("output_artifact_hash"),
            "next_v": next_v,
            "source_v": source_v,
        })
    if not _durable_checkpoint_contract_matches(checkpoint, contract):
        return _json_tool_result({
            "error": "DURABLE_WORKER_OUTPUT_PROJECTION_CONFLICT",
            "success": False,
            "action": "operator_reconcile",
            "next_v": next_v,
            "source_v": source_v,
            "expected_checkpoint": contract,
            "current_checkpoint": {
                "workflow_run_id": (
                    checkpoint.get("workflow_run_id") if checkpoint else None
                ),
                "checkpoint_revision": (
                    checkpoint.get("checkpoint_revision") if checkpoint else None
                ),
                "stage": checkpoint.get("stage") if checkpoint else None,
            },
            "directive": (
                "The immutable output is safe, but another command advanced the "
                "checkpoint. Do not rewind it or call the LLM; reconcile the actor "
                "history with the current projection."
            ),
        })
    projection_preimage_hash = str(
        envelope.get("projection_preimage_artifact_hash") or ""
    )
    projection_preimage_snapshot = str(
        envelope.get("projection_preimage_snapshot_hash") or ""
    )
    output_hash = str(state.get("output_artifact_hash") or "")
    current_artifact_hash = _complete_artifact_fingerprint(next_dir)
    if (
        Path(next_dir).exists()
        and current_artifact_hash not in {
            projection_preimage_hash,
            output_hash,
        }
    ):
        return _json_tool_result({
            "error": "DURABLE_WORKER_PRE_PROJECTION_ARTIFACT_DRIFT",
            "success": False,
            "action": "operator_reconcile",
            "next_v": next_v,
            "source_v": source_v,
            "expected_output_artifact_hash": output_hash,
            "expected_projection_preimage_artifact_hash": (
                projection_preimage_hash
            ),
            "current_artifact_hash": current_artifact_hash,
            "directive": (
                "The canonical candidate no longer matches either immutable "
                "Worker boundary. Do not overwrite concurrent or operator bytes."
            ),
        })
    materialization_receipt = worker_workflow.artifacts.materialize(
        str(state.get("output_snapshot_hash") or ""),
        next_dir,
        expected_destination_digest=(
            current_artifact_hash if Path(next_dir).exists() else None
        ),
    )
    audit_context = deepcopy(projection.get("audit_context") or {})
    audit_context["durable_worker_output"] = deepcopy(
        projection.get("durable_worker_output") or {}
    )
    projected = write_pipeline_checkpoint(
        next_v,
        source_v,
        "workers_done",
        master_plan=deepcopy(projection.get("master_plan") or {}),
        reviewer_feedback=str(projection.get("reviewer_feedback") or ""),
        worker_failure_count=int(projection.get("worker_failure_count") or 0),
        audit_context=audit_context,
        precommit_rework_count=int(
            projection.get("precommit_rework_count") or 0
        ),
        official_rework_count=int(
            projection.get("official_rework_count") or 0
        ),
        expected_checkpoint_revision=int(contract.get("checkpoint_revision") or 0),
        expected_checkpoint_stage=str(contract.get("checkpoint_stage") or ""),
        expected_workflow_run_id=str(contract.get("workflow_run_id") or ""),
    )
    if not projected:
        current_checkpoint = _matching_checkpoint(next_v, source_v)
        if _durable_output_already_projected(current_checkpoint, projection):
            if (
                current_checkpoint.get("stage") == "workers_done"
                and _complete_artifact_fingerprint(next_dir) != output_hash
            ):
                return _json_tool_result({
                    "error": "DURABLE_WORKER_CONCURRENT_PROJECTION_ARTIFACT_MISMATCH",
                    "success": False,
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "expected_output_artifact_hash": output_hash,
                    "current_artifact_hash": _complete_artifact_fingerprint(next_dir),
                })
            worker_workflow.projected("workers_done")
            return _json_tool_result({
                "success": True,
                "durable_recovery": "confirmed_concurrent_worker_projection",
                "current_checkpoint_stage": current_checkpoint.get("stage"),
                "output_artifact_hash": output_hash,
                "next_v": next_v,
                "source_v": source_v,
            })

        if not materialization_receipt.installed:
            return _json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_PREEXISTED_FAILED_CHECKPOINT_CAS",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "output_artifact_hash": output_hash,
                "materialization_receipt_digest": (
                    materialization_receipt.receipt_digest
                ),
                "directive": (
                    "The output bytes predated this command, so this command has "
                    "no authority to roll them back after losing the checkpoint CAS."
                ),
            })

        # Candidate bytes and checkpoint projection are one semantic effect.
        # If the CAS lost, restore the exact immutable preimage, but only while
        # the canonical tree is still the output written by this command.  A
        # different hash proves a concurrent writer and must never be clobbered.
        post_cas_artifact_hash = _complete_artifact_fingerprint(next_dir)
        if post_cas_artifact_hash != output_hash:
            return _json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_PROJECTION_CONCURRENT_DRIFT",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "expected_output_artifact_hash": output_hash,
                "current_artifact_hash": post_cas_artifact_hash,
                "directive": (
                    "The checkpoint CAS failed and another writer changed the "
                    "candidate. Preserve both histories for operator reconciliation."
                ),
            })
        try:
            worker_workflow.artifacts.materialize(
                projection_preimage_snapshot,
                next_dir,
                expected_destination_digest=output_hash,
            )
        except BaseException as exc:
            return _json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_ROLLBACK_FAILED",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
            })
        restored_hash = _complete_artifact_fingerprint(next_dir)
        if restored_hash != projection_preimage_hash:
            return _json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_ROLLBACK_MISMATCH",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "expected_projection_preimage_artifact_hash": (
                    projection_preimage_hash
                ),
                "restored_artifact_hash": restored_hash,
            })
        return _json_tool_result({
            "error": "DURABLE_WORKER_OUTPUT_PROJECTION_FAILED",
            "success": False,
            "action": "retry_same_tool",
            "next_v": next_v,
            "source_v": source_v,
            "output_artifact_hash": state.get("output_artifact_hash"),
            "canonical_artifact_restored": True,
            "restored_artifact_hash": restored_hash,
            "directive": (
                "The immutable Worker output receipt is safe. Retry execute_workers "
                "to project it; the LLM will not be called again."
            ),
        })
    post_commit_artifact_hash = _complete_artifact_fingerprint(next_dir)
    if post_commit_artifact_hash != output_hash:
        return _json_tool_result({
            "error": "DURABLE_WORKER_POST_COMMIT_ARTIFACT_MISMATCH",
            "success": False,
            "action": "operator_reconcile",
            "next_v": next_v,
            "source_v": source_v,
            "expected_output_artifact_hash": output_hash,
            "current_artifact_hash": post_commit_artifact_hash,
        })
    worker_workflow.projected("workers_done")
    return _json_tool_result({
        "success": True,
        "durable_recovery": "projected_existing_worker_output",
        "output_artifact_hash": state.get("output_artifact_hash"),
        "next_v": next_v,
        "source_v": source_v,
    })


async def _project_durable_worker_failure(worker_workflow, state):
    """Project a semantic failure receipt before another Worker cycle can open."""
    projection = deepcopy(state.get("failure_projection") or {})
    envelope = state.get("envelope") or {}
    next_v = int(envelope.get("next_v"))
    source_v = int(envelope.get("source_v"))
    contract = projection.get("checkpoint_contract") or {}
    checkpoint = _matching_checkpoint(next_v, source_v)
    target_stage = str(projection.get("stage") or "repair_planned")
    receipt = (
        (checkpoint.get("audit_context") or {}).get("durable_worker_failure")
        if isinstance(checkpoint, dict)
        and isinstance(checkpoint.get("audit_context"), dict)
        else None
    )
    expected_receipt = projection.get("durable_worker_failure") or {}
    already_projected = bool(
        isinstance(checkpoint, dict)
        and str(
            checkpoint.get("workflow_run_id")
            or checkpoint.get("run_id")
            or ""
        ) == str(contract.get("workflow_run_id") or "")
        and isinstance(receipt, dict)
        and receipt.get("envelope_digest")
        == expected_receipt.get("envelope_digest")
        and receipt.get("semantic_attempt")
        == expected_receipt.get("semantic_attempt")
    )
    if not already_projected:
        if not _durable_checkpoint_contract_matches(checkpoint, contract):
            return _json_tool_result({
                "error": "DURABLE_WORKER_FAILURE_PROJECTION_CONFLICT",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
            })
        audit_context = deepcopy(projection.get("audit_context") or {})
        audit_context["durable_worker_failure"] = expected_receipt
        checkpoint_kwargs = {}
        if target_stage == "direction_audited" and projection.get(
            "runtime_contract_ledger_digest"
        ):
            checkpoint_kwargs = {
                "reset_runtime_contract_ledger": True,
                "expected_runtime_contract_ledger_digest": projection[
                    "runtime_contract_ledger_digest"
                ],
                "runtime_contract_ledger_reset_reason": (
                    "master_plan_rejected_replan"
                ),
            }
        written = write_pipeline_checkpoint(
            next_v,
            source_v,
            target_stage,
            master_plan=deepcopy(projection.get("master_plan") or {}),
            direction_audit=projection.get("direction_audit"),
            reviewer_feedback=str(projection.get("reviewer_feedback") or ""),
            worker_failure_count=int(projection.get("worker_failure_count") or 0),
            audit_context=audit_context,
            precommit_rework_count=int(
                projection.get("precommit_rework_count") or 0
            ),
            official_rework_count=int(
                projection.get("official_rework_count") or 0
            ),
            touch_stage_timestamp=True,
            expected_checkpoint_revision=int(
                contract.get("checkpoint_revision") or 0
            ),
            expected_checkpoint_stage=str(contract.get("checkpoint_stage") or ""),
            expected_workflow_run_id=str(contract.get("workflow_run_id") or ""),
            **checkpoint_kwargs,
        )
        if not written:
            return _json_tool_result({
                "error": "DURABLE_WORKER_FAILURE_PROJECTION_FAILED",
                "success": False,
                "action": "retry_same_tool",
                "next_v": next_v,
                "source_v": source_v,
            })
    evidence = projection.get("evidence") or {}
    if target_stage == "direction_audited":
        worker_workflow.supersede(
            "initial_worker_semantic_failure_requires_master_replan",
            evidence,
            stage=target_stage,
        )
    else:
        worker_workflow.failure_projected(target_stage)
    return _json_tool_result({
        "success": False,
        "failure_class": "semantic",
        "next_v": next_v,
        "source_v": source_v,
        "next_stage": target_stage,
        "boundary_errors": evidence.get("boundary_errors") or [],
    })


async def _run_durable_worker_effect(
    worker_workflow,
    envelope,
    next_dir,
    worker_template,
):
    """Run exactly one fenced Worker activity from a frozen envelope."""
    from agent_workers import WorkerInfrastructureError
    from llm_availability import LLMAvailabilityBlocked
    from worker_boundary import (
        diff_file_snapshot,
        restore_complete_artifact_snapshot,
        snapshot_python_files,
    )

    next_v = int(envelope["next_v"])
    source_v = int(envelope["source_v"])
    tasks = deepcopy(envelope.get("tasks") or [])
    reviewer_feedback = str(envelope.get("reviewer_feedback") or "")
    policy = deepcopy(envelope.get("execution_policy") or {})
    contract = envelope.get("checkpoint_contract") or {}
    checkpoint = _matching_checkpoint(next_v, source_v)
    if not _durable_checkpoint_contract_matches(checkpoint, contract):
        worker_workflow.abandon("worker_checkpoint_contract_drift_before_claim")
        return _json_tool_result({
            "error": "DURABLE_WORKER_CHECKPOINT_CONTRACT_DRIFT",
            "success": False,
            "action": "abandon_generation",
            "next_v": next_v,
            "source_v": source_v,
        })
    _eb = checkpoint.get("epoch_binding") or {}
    _source_inherited = bool(_eb.get("source_artifact_inherited", True))
    source_hash = (
        _complete_artifact_fingerprint(next_dir)
        if not _source_inherited
        else _complete_artifact_fingerprint(get_bot_dir(source_v))
    )
    if source_hash != str(envelope.get("source_artifact_hash") or ""):
        worker_workflow.abandon("worker_source_artifact_drift_before_claim")
        return _json_tool_result({
            "error": "DURABLE_WORKER_SOURCE_ARTIFACT_DRIFT",
            "success": False,
            "action": "abandon_generation",
            "next_v": next_v,
            "source_v": source_v,
            "expected_source_hash": envelope.get("source_artifact_hash"),
            "current_source_hash": source_hash,
        })

    _worker_uses_llm = policy.get("executor") != "system_policy_bootstrap_v1"
    if _worker_uses_llm:
        try:
            from llm_availability_store import active_llm_pause

            active_pause = active_llm_pause()
        except Exception as exc:
            return _json_tool_result({
                "error": "LLM_AVAILABILITY_STATE_INVALID",
                "success": False,
                "failure_class": "control_plane",
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                "directive": (
                    "The provider pause record is invalid. No Worker effect "
                    "was claimed."
                ),
            })
        if active_pause is not None:
            state = worker_workflow.state()
            return _json_tool_result({
                "error": "LLM_AVAILABILITY_BLOCKED",
                "success": False,
                "failure_class": "availability",
                "action": "wait_for_llm_availability",
                "next_v": next_v,
                "source_v": source_v,
                "worker_status": state.get("status"),
                "attempt": int(state.get("attempt") or 0),
                "max_attempts": int(state.get("max_attempts") or 0),
                "effect_id": state.get("effect_id"),
                "availability": active_pause,
                "directive": (
                    "The provider pause became active before lease claim. No "
                    "Worker attempt was consumed."
                ),
            })

    try:
        lease = worker_workflow.request_or_claim(
            owner=f"pid:{os.getpid()}",
            lease_seconds=3600,
        )
    except Exception as exc:
        return _json_tool_result({
            "error": "DURABLE_WORKER_EFFECT_CLAIM_FAILED",
            "failure_class": "infrastructure",
            "action": "retry_same_tool",
            "message": f"{type(exc).__name__}: {str(exc)[:300]}",
            "next_v": next_v,
            "source_v": source_v,
        })

    workspace = None
    availability_defer_failed = False
    try:
        if _worker_uses_llm:
            try:
                from llm_availability_store import active_llm_pause

                active_pause = active_llm_pause()
            except Exception as exc:
                with worker_workflow.store.command_lock(
                    worker_workflow.run_id,
                    blocking=True,
                ):
                    worker_workflow.availability_deferred(
                        lease,
                        {
                            "schema_version": 1,
                            "active": True,
                            "category": "availability_control_invalid",
                            "summary": (
                                "provider pause state could not be read after claim"
                            ),
                            "evidence_digest": hashlib.sha256(
                                (
                                    f"{type(exc).__name__}:"
                                    f"{str(exc)[:300]}"
                                ).encode("utf-8")
                            ).hexdigest(),
                            "persistence_error": (
                                f"{type(exc).__name__}: {str(exc)[:300]}"
                            ),
                        },
                    )
                return _json_tool_result({
                    "error": "LLM_AVAILABILITY_STATE_INVALID",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "effect_id": lease.effect_id,
                    "claimed_attempt": lease.attempt,
                    "restored_attempt": int(
                        worker_workflow.state().get("attempt") or 0
                    ),
                    "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                })
            if active_pause is not None:
                with worker_workflow.store.command_lock(
                    worker_workflow.run_id,
                    blocking=True,
                ):
                    deferred_state = worker_workflow.availability_deferred(
                        lease,
                        active_pause,
                    )
                return _json_tool_result({
                    "error": "LLM_AVAILABILITY_BLOCKED",
                    "success": False,
                    "failure_class": "availability",
                    "action": "wait_for_llm_availability",
                    "next_v": next_v,
                    "source_v": source_v,
                    "effect_id": lease.effect_id,
                    "lease_epoch": lease.lease_epoch,
                    "claimed_attempt": lease.attempt,
                    "restored_attempt": int(
                        deferred_state.get("attempt") or 0
                    ),
                    "max_attempts": lease.max_attempts,
                    "availability": active_pause,
                    "directive": (
                        "The provider pause became active at the claim boundary. "
                        "The lease was deferred without consuming an attempt."
                    ),
                })
        workspace = worker_workflow.artifacts.workspace_for(
            lease,
            str(envelope.get("prepared_snapshot_hash") or ""),
        )
        task_skipper = None
        if policy.get("quality_skipper"):
            task_skipper = _quality_rework_skipper(
                workspace,
                get_bot_dir(source_v),
                next_v,
                source_v,
                expected_architecture_policy=policy.get(
                    "expected_architecture_policy"
                ),
                master_plan=deepcopy(envelope.get("projection_plan") or {}),
            )
        baseline = snapshot_python_files(workspace)
        ui = _get_ui()
        system_worker_receipt = None
        try:
            if policy.get("executor") == "system_policy_bootstrap_v1":
                from system_strict_bootstrap import (
                    apply_blueprint,
                    bind_worker_effect_receipt,
                )

                worker_snapshots, audit_focus_areas, system_worker_receipt = (
                    apply_blueprint(
                        workspace,
                        checkpoint=checkpoint,
                        envelope=envelope,
                    )
                )
                system_worker_receipt = bind_worker_effect_receipt(
                    system_worker_receipt,
                    effect_id=lease.effect_id,
                    lease_epoch=lease.lease_epoch,
                )
                success = True
                ui.log_history(
                    "Applied the content-bound strict-v1 consumer blueprint "
                    "without invoking an LLM Worker.",
                    "info",
                )
            else:
                success, worker_snapshots, audit_focus_areas = await _execute_workers(
                    tasks,
                    worker_template,
                    workspace,
                    next_v,
                    [],
                    ui,
                    reviewer_feedback=reviewer_feedback,
                    source_v=source_v,
                    force_sequential=bool(policy.get("force_sequential")),
                    task_skipper=task_skipper,
                    worker_effect_identity={
                        "workflow_run_id": str(
                            checkpoint.get("workflow_run_id") or ""
                        ),
                        "envelope_digest": str(
                            envelope.get("envelope_digest") or ""
                        ),
                        "effect_id": str(lease.effect_id),
                        "lease_epoch": int(lease.lease_epoch),
                    },
                )
        except BaseException as exc:
            rollback_error = ""
            try:
                restore_complete_artifact_snapshot(workspace, baseline)
            except BaseException as rollback_exc:
                rollback_error = (
                    f"{type(rollback_exc).__name__}: {str(rollback_exc)[:300]}"
                )
            if isinstance(exc, LLMAvailabilityBlocked):
                pause_state = exc.pause_state()
                # Fence and release the Worker lease *before* publishing the
                # cross-process pause.  If the process dies immediately after
                # the pause file is fsynced, replay already sees EffectDeferred
                # and the claim's attempt increment has been rolled back.
                try:
                    with worker_workflow.store.command_lock(
                        worker_workflow.run_id,
                        blocking=True,
                    ):
                        deferred_state = (
                            worker_workflow.availability_deferred(
                                lease,
                                pause_state,
                            )
                        )
                except Exception as defer_exc:
                    availability_defer_failed = True
                    return _json_tool_result({
                        "error": "WORKER_AVAILABILITY_DEFER_FAILED",
                        "success": False,
                        "failure_class": "control_plane",
                        "action": "operator_reconcile",
                        "next_v": next_v,
                        "source_v": source_v,
                        "message": (
                            f"{type(defer_exc).__name__}: "
                            f"{str(defer_exc)[:300]}"
                        ),
                        "persistence_error": "",
                        "rollback_error": rollback_error,
                        "directive": (
                            "The LLM availability pause could not be fenced into "
                            "the durable Worker journal. Do not classify or retry "
                            "it as a Worker infrastructure failure."
                        ),
                    })
                persistence_error = ""
                try:
                    from llm_availability_store import persist_llm_pause

                    pause_state = persist_llm_pause(pause_state)
                except Exception as pause_exc:
                    persistence_error = (
                        f"{type(pause_exc).__name__}: {str(pause_exc)[:300]}"
                    )
                    return _json_tool_result({
                        "error": "LLM_AVAILABILITY_PAUSE_WAS_NOT_PERSISTED",
                        "success": False,
                        "failure_class": "control_plane",
                        "action": "operator_reconcile",
                        "next_v": next_v,
                        "source_v": source_v,
                        "effect_id": lease.effect_id,
                        "lease_epoch": lease.lease_epoch,
                        "claimed_attempt": lease.attempt,
                        "restored_attempt": int(
                            deferred_state.get("attempt") or 0
                        ),
                        "max_attempts": lease.max_attempts,
                        "availability": exc.pause_state(),
                        "persistence_error": persistence_error,
                        "rollback_error": rollback_error,
                        "directive": (
                            "The Worker lease is safely deferred and attempt-neutral, "
                            "but the global pause was not published. Reconcile the "
                            "pause record before resuming this exact effect."
                        ),
                    })
                return _json_tool_result({
                    "error": "LLM_AVAILABILITY_BLOCKED",
                    "success": False,
                    "failure_class": "availability",
                    "action": "wait_for_llm_availability",
                    "next_v": next_v,
                    "source_v": source_v,
                    "effect_id": lease.effect_id,
                    "lease_epoch": lease.lease_epoch,
                    "claimed_attempt": lease.attempt,
                    "restored_attempt": int(
                        deferred_state.get("attempt") or 0
                    ),
                    "max_attempts": lease.max_attempts,
                    "availability": pause_state,
                    "persistence_error": persistence_error,
                    "rollback_error": rollback_error,
                    "directive": (
                        "The provider is unavailable. The Worker lease was "
                        "released without consuming an attempt; resume only "
                        "through the content-bound LLM availability control."
                    ),
                })

            from system_strict_bootstrap import (
                SystemStrictBootstrapError,
            )

            if isinstance(exc, SystemStrictBootstrapError):
                try:
                    with worker_workflow.store.command_lock(worker_workflow.run_id):
                        worker_workflow.execution_failed(
                            lease,
                            list(exc.errors),
                            retryable=False,
                        )
                    worker_workflow.abandon(
                        "system_strict_bootstrap_execution_failed"
                    )
                except Exception:
                    pass
                return _json_tool_result({
                    "error": "SYSTEM_STRICT_BOOTSTRAP_EXECUTION_FAILED",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "abandon_generation",
                    "next_v": next_v,
                    "source_v": source_v,
                    "validation_errors": list(exc.errors),
                    "rollback_error": rollback_error,
                    "directive": (
                        "The checked-in blueprint failed its exact workspace or output "
                        "identity. Abandon; never retry it as an LLM Worker."
                    ),
                })
            if isinstance(exc, WorkerInfrastructureError) and not rollback_error:
                with worker_workflow.store.command_lock(worker_workflow.run_id):
                    failed_state = worker_workflow.infrastructure_failed(
                        lease,
                        exc.issues,
                    )
                exhausted = failed_state.get("status") == "exhausted"
                if exhausted:
                    worker_workflow.abandon("worker_infrastructure_exhausted")
                return _json_tool_result({
                    **(
                        {"error": "WORKER_INFRASTRUCTURE_EXHAUSTED"}
                        if exhausted
                        else {}
                    ),
                    "success": False,
                    "failure_class": "infrastructure",
                    "action": (
                        "abandon_generation" if exhausted else "retry_same_tool"
                    ),
                    "attempt": lease.attempt,
                    "max_attempts": lease.max_attempts,
                    "attempt_key": envelope.get("envelope_digest"),
                    "effect_id": lease.effect_id,
                    "lease_epoch": lease.lease_epoch,
                    "next_v": next_v,
                    "source_v": source_v,
                })
            issues = [
                f"{type(exc).__name__}: {str(exc)[:500]}",
                *( [f"rollback: {rollback_error}"] if rollback_error else [] ),
            ]
            with worker_workflow.store.command_lock(worker_workflow.run_id):
                failed_state = worker_workflow.execution_failed(
                    lease,
                    issues,
                    retryable=not bool(rollback_error),
                )
            if rollback_error or failed_state.get("status") == "exhausted":
                worker_workflow.abandon("worker_harness_failure")
            return _json_tool_result({
                "error": (
                    "WORKER_BATCH_EXCEPTION_ROLLBACK_FAILED"
                    if rollback_error
                    else "DURABLE_WORKER_HARNESS_FAILED"
                ),
                "success": False,
                "failure_class": "infrastructure",
                "action": (
                    "abandon_generation"
                    if rollback_error or failed_state.get("status") == "exhausted"
                    else "retry_same_tool"
                ),
                "next_v": next_v,
                "source_v": source_v,
                "message": "; ".join(issues),
            })

        boundary_errors = []
        policy_identity_refresh_receipt = None
        if success:
            changed = diff_file_snapshot(workspace, baseline)
            if not changed:
                success = False
                boundary_errors.append({"type": "worker_zero_artifact_changes"})
        if success:
            boundary_errors = _validate_worker_boundaries(
                tasks,
                source_v,
                next_v,
                worker_snapshots=worker_snapshots,
                candidate_dir=workspace,
                source_artifact_inherited=_source_inherited,
            )
            success = not boundary_errors
        if success:
            # The model-facing boundary has now proved that only policy.py was
            # candidate-written (the deterministic v143 bootstrap has already
            # proved its exact three-file blueprint separately).  Only after
            # that proof may the host rebuild the two digest-bound identities.
            try:
                from bot_artifact import canonical_digest
                from bot_namespace import (
                    SYSTEM_DERIVED_IDENTITY_FILES,
                    refresh_policy_identity_documents,
                    strict_lineage_parent_versions,
                )

                pre_refresh_changed = sorted(changed)
                expected_pre_refresh = (
                    {"policy.py", *SYSTEM_DERIVED_IDENTITY_FILES}
                    if policy.get("executor") == "system_policy_bootstrap_v1"
                    else {"policy.py"}
                )
                if set(pre_refresh_changed) != expected_pre_refresh:
                    raise RuntimeError(
                        "candidate change set before identity refresh mismatch: "
                        f"expected={sorted(expected_pre_refresh)}:"
                        f"actual={pre_refresh_changed}"
                    )
                lineage_parents = strict_lineage_parent_versions(
                    next_v,
                    source_v,
                    checkpoint.get("parent2_v"),
                )
                identity = refresh_policy_identity_documents(
                    workspace,
                    next_v,
                    parent_versions=lineage_parents,
                )
                final_changed = diff_file_snapshot(workspace, baseline)
                expected_final = {"policy.py", *SYSTEM_DERIVED_IDENTITY_FILES}
                if set(final_changed) != expected_final:
                    raise RuntimeError(
                        "final strict artifact delta mismatch: "
                        f"expected={sorted(expected_final)}:actual={final_changed}"
                    )
                receipt_subject = {
                    "schema_version": 1,
                    "kind": "strict-policy-identity-refresh-v1",
                    "version": next_v,
                    "parent_versions": list(lineage_parents),
                    "candidate_changed_files": ["policy.py"],
                    "system_derived_files": sorted(SYSTEM_DERIVED_IDENTITY_FILES),
                    "final_changed_files": final_changed,
                    "runtime_manifest_digest": identity[
                        "runtime_manifest_digest"
                    ],
                    "epoch_receipt_digest": identity["epoch_receipt_digest"],
                    "envelope_digest": envelope.get("envelope_digest"),
                    "effect_id": lease.effect_id,
                    "lease_epoch": lease.lease_epoch,
                }
                policy_identity_refresh_receipt = {
                    **receipt_subject,
                    "receipt_digest": canonical_digest(receipt_subject),
                }
            except Exception as exc:
                rollback_error = ""
                try:
                    restore_complete_artifact_snapshot(workspace, baseline)
                except Exception as rollback_exc:
                    rollback_error = (
                        f"{type(rollback_exc).__name__}: "
                        f"{str(rollback_exc)[:300]}"
                    )
                issue = (
                    "system policy identity refresh failed: "
                    f"{type(exc).__name__}: {str(exc)[:500]}"
                )
                with worker_workflow.store.command_lock(worker_workflow.run_id):
                    failed_state = worker_workflow.execution_failed(
                        lease,
                        [issue, *([f"rollback: {rollback_error}"] if rollback_error else [])],
                        retryable=not bool(rollback_error),
                    )
                if rollback_error or failed_state.get("status") == "exhausted":
                    worker_workflow.abandon("system_policy_identity_refresh_failed")
                return _json_tool_result({
                    "error": "SYSTEM_POLICY_IDENTITY_REFRESH_FAILED",
                    "success": False,
                    "failure_class": "infrastructure",
                    "action": (
                        "abandon_generation"
                        if rollback_error or failed_state.get("status") == "exhausted"
                        else "retry_same_tool"
                    ),
                    "next_v": next_v,
                    "source_v": source_v,
                    "message": issue,
                    "rollback_error": rollback_error,
                })
        if success:
            try:
                _clear_compiled_task_context(workspace)
            except Exception as exc:
                success = False
                boundary_errors.append({
                    "type": "transient_control_artifact_cleanup_failed",
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                })

        if not success:
            try:
                restore_complete_artifact_snapshot(workspace, baseline)
            except Exception as exc:
                worker_workflow.execution_failed(
                    lease,
                    [f"semantic rollback failed: {type(exc).__name__}: {exc}"],
                    retryable=False,
                )
                worker_workflow.abandon("worker_semantic_rollback_failed")
                return _json_tool_result({
                    "error": "WORKER_BATCH_ROLLBACK_FAILED",
                    "success": False,
                    "action": "abandon_generation",
                    "next_v": next_v,
                    "source_v": source_v,
                })
            evidence = {
                "boundary_errors": boundary_errors,
                "audit_focus_areas": audit_focus_areas,
                "worker_reported_success": False,
            }
            target_stage = (
                "repair_planned" if reviewer_feedback else "direction_audited"
            )
            next_failure_count = int(envelope.get("worker_failure_count") or 0) + 1
            audit_context = deepcopy(envelope.get("audit_context") or {})
            failure_plan = (
                deepcopy(envelope.get("projection_plan") or {})
                if reviewer_feedback
                else {}
            )
            if not reviewer_feedback:
                audit_context["worker_execution_failed_replan"] = {
                    "failed_tasks": [
                        {
                            "worker_id": task.get("worker_id"),
                            "role": task.get("role"),
                            "target_files": task.get("target_files", []),
                        }
                        for task in tasks[:5]
                    ],
                    "worker_failure_count": next_failure_count,
                }
            failure_projection = {
                "schema_version": 1,
                "stage": target_stage,
                "checkpoint_contract": deepcopy(contract),
                "master_plan": failure_plan,
                "direction_audit": checkpoint.get("direction_audit"),
                "reviewer_feedback": reviewer_feedback,
                "worker_failure_count": next_failure_count,
                "audit_context": audit_context,
                "precommit_rework_count": int(
                    envelope.get("precommit_rework_count") or 0
                ),
                "official_rework_count": int(
                    envelope.get("official_rework_count") or 0
                ),
                "runtime_contract_ledger_digest": (
                    _checkpoint_runtime_contract_ledger_digest(checkpoint)
                    if target_stage == "direction_audited"
                    and checkpoint.get("runtime_contract_ledger") is not None
                    else ""
                ),
                "evidence": evidence,
                "durable_worker_failure": {
                    "envelope_digest": envelope.get("envelope_digest"),
                    "semantic_attempt": int(
                        worker_workflow.state().get("semantic_attempt") or 0
                    ) + 1,
                },
            }
            with worker_workflow.store.command_lock(worker_workflow.run_id):
                semantic_state = worker_workflow.semantic_failed(
                    lease,
                    evidence,
                    projection=failure_projection,
                )
                return await _project_durable_worker_failure(
                    worker_workflow,
                    semantic_state,
                )

        try:
            artifact_hash = _complete_artifact_fingerprint(workspace)
            snapshot_hash = worker_workflow.artifacts.capture(workspace)
            if not artifact_hash or artifact_hash != snapshot_hash:
                raise RuntimeError("Worker output snapshot mismatch")
        except Exception as exc:
            worker_workflow.execution_failed(
                lease,
                [f"output capture failed: {type(exc).__name__}: {exc}"],
                retryable=True,
            )
            return _json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_CAPTURE_FAILED",
                "success": False,
                "failure_class": "infrastructure",
                "action": "retry_same_tool",
                "next_v": next_v,
                "source_v": source_v,
            })
        audit_context = deepcopy(envelope.get("audit_context") or {})
        if audit_focus_areas:
            audit_context["worker_cot_focus_areas"] = audit_focus_areas
        if system_worker_receipt is not None:
            audit_context["system_strict_bootstrap_worker"] = (
                system_worker_receipt
            )
        if policy_identity_refresh_receipt is not None:
            policy_identity_refresh_receipt = {
                **policy_identity_refresh_receipt,
                "output_artifact_hash": artifact_hash,
            }
            from bot_artifact import canonical_digest

            policy_identity_refresh_receipt["receipt_digest"] = canonical_digest({
                key: value
                for key, value in policy_identity_refresh_receipt.items()
                if key != "receipt_digest"
            })
            audit_context["strict_policy_identity_refresh"] = (
                policy_identity_refresh_receipt
            )
        projection = {
            "schema_version": 1,
            "checkpoint_contract": deepcopy(contract),
            "master_plan": deepcopy(envelope.get("projection_plan") or {}),
            "reviewer_feedback": reviewer_feedback,
            "worker_failure_count": int(envelope.get("worker_failure_count") or 0),
            "audit_context": audit_context,
            "precommit_rework_count": int(
                envelope.get("precommit_rework_count") or 0
            ),
            "official_rework_count": int(
                envelope.get("official_rework_count") or 0
            ),
            "durable_worker_output": {
                "artifact_hash": artifact_hash,
                "snapshot_hash": snapshot_hash,
                "envelope_digest": envelope.get("envelope_digest"),
                "effect_id": lease.effect_id,
                "lease_epoch": lease.lease_epoch,
            },
        }
        try:
            with worker_workflow.store.command_lock(worker_workflow.run_id):
                output_state = worker_workflow.output_ready(
                    lease,
                    artifact_hash=artifact_hash,
                    snapshot_hash=snapshot_hash,
                    projection=projection,
                )
                return await _project_durable_worker_output(
                    worker_workflow,
                    next_dir,
                    output_state,
                )
        except Exception as exc:
            try:
                worker_workflow.execution_failed(
                    lease,
                    [f"output receipt failed: {type(exc).__name__}: {exc}"],
                    retryable=True,
                )
            except Exception:
                pass
            return _json_tool_result({
                "error": "DURABLE_WORKER_OUTPUT_RECEIPT_FAILED",
                "success": False,
                "action": "retry_same_tool",
                "next_v": next_v,
                "source_v": source_v,
            })
    finally:
        # Lease-outcome invariant: every path after claim must durably complete,
        # fail, exhaust, or abandon the effect. This guard covers injected
        # failures in workspace creation, validators, receipt construction, and
        # future hooks without relying on each branch remembering cleanup.
        try:
            effect = worker_workflow.store.effect(lease.effect_id)
            if (
                not availability_defer_failed
                and effect.get("status") == "running"
                and int(effect.get("lease_epoch") or 0) == int(lease.lease_epoch)
            ):
                worker_workflow.execution_failed(
                    lease,
                    ["Worker activity exited without a durable outcome"],
                    retryable=True,
                )
        except Exception:
            pass
        if workspace is not None:
            try:
                worker_workflow.artifacts.discard_workspace(workspace)
            except Exception:
                pass


def _worker_availability_resume_receipt_errors(deferred, pause_audit):
    """Validate the global resume receipt against the deferred Worker effect.

    The Worker journal is the authority for *which* provider failure suspended
    this effect.  Absence of an active global pause is therefore necessary but
    not sufficient to resume: the inactive audit record must prove that the
    same evidence was reconciled through the allowed manual/cooldown path.
    """
    errors = []
    if not isinstance(deferred, dict) or not deferred:
        return ["worker_deferred_availability_missing"]

    digest = str(deferred.get("evidence_digest") or "")
    category = str(deferred.get("category") or "")
    manual = bool(deferred.get("requires_manual_resume"))
    if len(digest) != 64 or any(
        ch not in "0123456789abcdef" for ch in digest.lower()
    ):
        errors.append("worker_deferred_evidence_digest_invalid")
    if not category:
        errors.append("worker_deferred_category_missing")
    if not isinstance(pause_audit, dict) or not pause_audit:
        errors.append("global_pause_resume_receipt_missing")
        return errors

    if pause_audit.get("active") is not False:
        errors.append("global_pause_resume_receipt_not_inactive")
    if str(pause_audit.get("source") or "") != "llm_availability":
        errors.append("global_pause_resume_receipt_source_invalid")
    for key in ("category", "evidence_digest", "retry_policy", "http_status"):
        if pause_audit.get(key) != deferred.get(key):
            errors.append(f"global_pause_resume_receipt_{key}_mismatch")
    if bool(pause_audit.get("requires_manual_resume")) != manual:
        errors.append("global_pause_resume_receipt_manual_policy_mismatch")
    if not str(pause_audit.get("resumed_at") or ""):
        errors.append("global_pause_resume_receipt_timestamp_missing")

    resume_source = str(pause_audit.get("resume_source") or "")
    resume_digest = str(pause_audit.get("resume_evidence_digest") or "")
    if manual:
        if resume_source != "operator_evidence_digest":
            errors.append("manual_pause_operator_receipt_missing")
        if resume_digest != digest:
            errors.append("manual_pause_resume_evidence_digest_mismatch")
    else:
        if resume_source != "bounded_cooldown_elapsed":
            errors.append("transient_pause_cooldown_receipt_missing")
        if resume_digest:
            errors.append("transient_pause_unexpected_operator_digest")
        if not str(pause_audit.get("auto_resume_at") or ""):
            errors.append("transient_pause_auto_resume_deadline_missing")
    return errors


@dataclass(frozen=True)
class _DeferredWorkerActivity:
    workflow: object
    envelope: dict
    next_dir: Path
    worker_template: str


async def _execute_workers_command(args, *, actor_lock_owned=False):
    _t0 = time.time()
    tasks = args.get("tasks", [])
    if not isinstance(tasks, list):
        return _json_tool_result({
            "error": "WORKER_TASKS_NOT_LIST",
            "directive": "Pass tasks=[] to load the checkpoint-owned Master plan.",
        })
    tasks_provided = bool(tasks)
    next_v = args.get("next_v")
    source_v = args.get("source_v")
    if next_v is None or source_v is None:
        next_v, source_v = _resolve_version_args(args)
    if next_v is None or source_v is None:
        return _json_tool_result({"error": "Missing next_v/source_v and no active checkpoint"})
    reviewer_feedback = args.get("reviewer_feedback", "")

    _set_pipeline_status(f"Executing workers for v{next_v}")

    next_dir = get_bot_dir(next_v)
    prompts_dir = PROJECT_ROOT / "web" / "core" / "prompts"
    worker_template = _load_worker_prompt_template(prompts_dir)

    ckpt = _matching_checkpoint(next_v, source_v)
    if not ckpt:
        return _state_blocked(
            "execute_workers requires a matching checkpoint from prepare_next_gen.",
            next_v,
            source_v,
        )
    checkpoint_tasks = _checkpoint_master_plan(ckpt).get("tasks", [])
    if not isinstance(checkpoint_tasks, list):
        checkpoint_tasks = []
    critic_refusal = _critic_advisory_rework_refusal(
        ckpt,
        [*checkpoint_tasks, *tasks],
        next_v,
        source_v,
    )
    if critic_refusal:
        return _json_tool_result(critic_refusal)
    _system_bootstrap_executor = False
    from system_strict_bootstrap import is_declared_native_bootstrap

    _declared_system_bootstrap = is_declared_native_bootstrap(ckpt)
    _system_initial_worker_stage = bool(
        ckpt.get("stage") == "master_planned" and not reviewer_feedback
    )
    if _declared_system_bootstrap and not _system_initial_worker_stage:
        return _json_tool_result({
            "error": "SYSTEM_STRICT_BOOTSTRAP_REWORK_FORBIDDEN",
            "success": False,
            "action": "abandon_generation",
            "failure_class": "control_plane",
            "next_v": next_v,
            "source_v": source_v,
            "stage": ckpt.get("stage"),
            "directive": (
                "A content-bound first-migration blueprint may run only once from "
                "master_planned. If quality, Review, Critic, or precommit rejects "
                "it, abandon and change the checked-in blueprint/control contract "
                "in a fresh generation; never fall back to an LLM repair Worker."
            ),
        })

    if _declared_system_bootstrap:
        from system_strict_bootstrap import validate_master_receipt

        _system_worker_errors = validate_master_receipt(
            ckpt,
            candidate_dir=next_dir,
            require_prepared_content=True,
        )
        if _system_worker_errors:
            return _json_tool_result({
                "error": "SYSTEM_STRICT_BOOTSTRAP_WORKER_AUTHORITY_INVALID",
                "success": False,
                "action": "abandon_generation",
                "failure_class": "control_plane",
                "next_v": next_v,
                "source_v": source_v,
                "validation_errors": _system_worker_errors,
                "directive": (
                    "The fresh-bootstrap system receipt or prepared artifact drifted. "
                    "Abandon this generation; never fall back to an LLM Worker."
                ),
            })
        _system_bootstrap_executor = True
    if (
        not str(ckpt.get("workflow_run_id") or "").strip()
        or int(ckpt.get("checkpoint_revision") or 0) < 1
    ):
        return _json_tool_result({
            "error": "STALE_WORKFLOW_ID_UNSUPPORTED",
            "failure_class": "state_migration",
            "action": "abandon_generation",
            "next_v": next_v,
            "source_v": source_v,
            "directive": (
                "This active checkpoint predates the immutable generation actor "
                "identity. Abandon it while the runtime is stopped and prepare a "
                "new generation; do not migrate a half-executed workflow."
            ),
        })
    _worker_infra, _worker_infra_error = _owned_infrastructure_failure(
        ckpt,
        "execute_workers",
    )
    if _worker_infra_error:
        infra_route = route_policy(ckpt)
        return _state_blocked(
            _worker_infra_error + f"; next tool is {infra_route.get('next_tool')}",
            next_v,
            source_v,
            checkpoint=ckpt,
        )
    from worker_workflow import (
        WorkerWorkflow,
        next_worker_command,
        validate_worker_envelope,
    )

    worker_workflow = WorkerWorkflow.for_checkpoint(ckpt)
    if _worker_infra is not None:
        return _json_tool_result({
            "error": "STALE_WORKER_INFRASTRUCTURE_STATE_UNSUPPORTED",
            "failure_class": "state_migration",
            "action": "abandon_generation",
            "next_v": next_v,
            "source_v": source_v,
            "directive": (
                "This generation was created by the retired Worker overlay state "
                "machine. Abandon it from the stopped runtime and start from a new "
                "baseline; do not translate two authorities into one history."
            ),
        })
    durable_worker_state = worker_workflow.state()
    durable_worker_status = str(durable_worker_state.get("status") or "idle")
    if durable_worker_status == "completed":
        previous_envelope = durable_worker_state.get("envelope") or {}
        previous_contract = previous_envelope.get("checkpoint_contract") or {}
        current_revision = int(ckpt.get("checkpoint_revision") or 0)
        previous_revision = int(previous_contract.get("checkpoint_revision") or 0)
        worker_entry_stages = {
            "master_planned",
            "quality_failed",
            "quality_passed",
            "reviewed",
            "critic_checked",
            "precommit_failed",
            "official_failed",
            "repair_planned",
            "rework_running",
        }
        if (
            ckpt.get("stage") in worker_entry_stages
            and current_revision > previous_revision
            and route_policy(ckpt).get("next_tool") == "execute_workers"
        ):
            work_receipt = hashlib.sha256(
                json.dumps(
                    {
                        "workflow_run_id": ckpt.get("workflow_run_id"),
                        "checkpoint_revision": current_revision,
                        "stage": ckpt.get("stage"),
                        "master_plan": ckpt.get("master_plan") or {},
                        "reviewer_feedback": ckpt.get("reviewer_feedback") or "",
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            durable_worker_state = worker_workflow.open_cycle(
                f"checkpoint_work_receipt:{work_receipt}"
            )
            durable_worker_status = "idle"
    durable_worker_envelope = (
        durable_worker_state.get("envelope")
        if isinstance(durable_worker_state.get("envelope"), dict)
        else {}
    )
    worker_command = next_worker_command(durable_worker_state)
    command_name = str(worker_command.get("command") or "recover")
    if command_name == "reconcile_abandon":
        return _json_tool_result({
            "error": "WORKER_WORKFLOW_ABANDONED",
            "success": False,
            "failure_class": "infrastructure",
            "action": "abandon_generation",
            "worker_abandon_reason": str(
                worker_command.get("reason") or "worker_abandoned"
            ),
            "next_v": next_v,
            "source_v": source_v,
            "stage": ckpt.get("stage"),
            "directive": (
                "The durable Worker journal is terminal while the outer "
                "checkpoint is still active. Reconcile by centrally abandoning "
                "this generation; never reopen or recreate the exhausted effect."
            ),
        })
    durable_worker_resume = command_name != "prepare"
    if durable_worker_resume and durable_worker_envelope:
        envelope_errors = validate_worker_envelope(durable_worker_envelope)
        if envelope_errors:
            worker_workflow.abandon("durable_worker_envelope_invalid")
            return _json_tool_result({
                "error": "DURABLE_WORKER_ENVELOPE_INVALID",
                "validation_errors": envelope_errors,
                "next_v": next_v,
                "source_v": source_v,
                "action": "abandon_generation",
            })
        if (
            int(durable_worker_envelope.get("next_v")) != int(next_v)
            or int(durable_worker_envelope.get("source_v")) != int(source_v)
        ):
            worker_workflow.abandon("durable_worker_identity_mismatch")
            return _json_tool_result({
                "error": "DURABLE_WORKER_IDENTITY_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "action": "abandon_generation",
            })
        current_template_hash = hashlib.sha256(
            worker_template.encode("utf-8")
        ).hexdigest()
        if (
            durable_worker_envelope.get("worker_template_hash")
            != current_template_hash
            or durable_worker_envelope.get("backend_contract")
            != _expected_worker_backend_contract(
                ckpt,
                durable_worker_envelope,
            )
        ):
            worker_workflow.abandon("durable_worker_definition_drift")
            return _json_tool_result({
                "error": "DURABLE_WORKER_DEFINITION_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "action": "abandon_generation",
            })
    _worker_uses_llm = bool(
        (durable_worker_envelope.get("execution_policy") or {}).get(
            "executor"
        )
        != "system_policy_bootstrap_v1"
    )
    if (
        _worker_uses_llm
        and command_name in {
            "request_or_claim_worker",
            "claim_worker",
            "wait_for_llm_availability",
        }
    ):
        try:
            from llm_availability_store import active_llm_pause, load_llm_pause

            _active_pause = active_llm_pause()
            _pause_audit = load_llm_pause()
        except Exception as exc:
            return _json_tool_result({
                "error": "LLM_AVAILABILITY_STATE_INVALID",
                "success": False,
                "failure_class": "control_plane",
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "worker_status": durable_worker_status,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                "directive": (
                    "The durable provider pause record could not be validated. "
                    "Do not claim or fail the Worker effect until that control "
                    "record is reconciled."
                ),
            })
        if _active_pause is not None:
            return _json_tool_result({
                "error": "LLM_AVAILABILITY_BLOCKED",
                "success": False,
                "failure_class": "availability",
                "action": "wait_for_llm_availability",
                "next_v": next_v,
                "source_v": source_v,
                "worker_status": durable_worker_status,
                "attempt": int(durable_worker_state.get("attempt") or 0),
                "max_attempts": int(
                    durable_worker_state.get("max_attempts") or 0
                ),
                "effect_id": durable_worker_state.get("effect_id"),
                "availability": _active_pause,
                "directive": (
                    "The provider pause is still active. No Worker effect was "
                    "claimed and no attempt was consumed."
                ),
            })
        if command_name == "wait_for_llm_availability":
            _deferred_availability = (
                durable_worker_state.get("availability") or {}
            )
            if _deferred_availability.get("persistence_error"):
                return _json_tool_result({
                    "error": "LLM_AVAILABILITY_PAUSE_WAS_NOT_PERSISTED",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "worker_status": durable_worker_status,
                    "attempt": int(
                        durable_worker_state.get("attempt") or 0
                    ),
                    "effect_id": durable_worker_state.get("effect_id"),
                    "availability": _deferred_availability,
                    "directive": (
                        "The Worker lease was safely deferred, but the global "
                        "pause write failed. Preserve the attempt-neutral effect "
                        "and reconcile the pause record before resuming."
                    ),
                })
            _resume_receipt_errors = _worker_availability_resume_receipt_errors(
                _deferred_availability,
                _pause_audit,
            )
            if _resume_receipt_errors:
                return _json_tool_result({
                    "error": "WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "worker_status": durable_worker_status,
                    "attempt": int(
                        durable_worker_state.get("attempt") or 0
                    ),
                    "effect_id": durable_worker_state.get("effect_id"),
                    "receipt_errors": _resume_receipt_errors,
                    "availability": _deferred_availability,
                    "directive": (
                        "The global pause is not active, but no matching durable "
                        "resume receipt authorizes this deferred Worker effect. "
                        "Preserve the attempt-neutral journal and reconcile the "
                        "exact evidence digest before resuming."
                    ),
                })
            try:
                if actor_lock_owned:
                    durable_worker_state = (
                        worker_workflow.resume_availability_deferred()
                    )
                else:
                    with worker_workflow.store.command_lock(
                        worker_workflow.run_id
                    ):
                        durable_worker_state = (
                            worker_workflow.resume_availability_deferred()
                        )
            except Exception as exc:
                return _json_tool_result({
                    "error": "WORKER_AVAILABILITY_RESUME_FAILED",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "directive": (
                        "The provider pause cleared, but its fenced Worker effect "
                        "could not transition back to requested. Do not recreate "
                        "or fail the effect."
                    ),
                })
            durable_worker_status = str(
                durable_worker_state.get("status") or "requested"
            )
            worker_command = next_worker_command(durable_worker_state)
            command_name = str(
                worker_command.get("command") or "recover"
            )
            if command_name != "claim_worker":
                return _json_tool_result({
                    "error": "WORKER_AVAILABILITY_RESUME_INVARIANT_FAILED",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "worker_status": durable_worker_status,
                    "next_command": command_name,
                })
    if command_name == "project_output":
        if actor_lock_owned:
            return await _project_durable_worker_output(
                worker_workflow,
                next_dir,
                durable_worker_state,
            )
        with worker_workflow.store.command_lock(worker_workflow.run_id):
            return await _project_durable_worker_output(
                worker_workflow,
                next_dir,
                durable_worker_state,
            )
    if command_name == "project_failure":
        if actor_lock_owned:
            return await _project_durable_worker_failure(
                worker_workflow,
                durable_worker_state,
            )
        with worker_workflow.store.command_lock(worker_workflow.run_id):
            return await _project_durable_worker_failure(
                worker_workflow,
                durable_worker_state,
            )
    if command_name in {"request_or_claim_worker", "claim_worker"}:
        if actor_lock_owned:
            return _DeferredWorkerActivity(
                workflow=worker_workflow,
                envelope=durable_worker_envelope,
                next_dir=next_dir,
                worker_template=worker_template,
            )
        return await _run_durable_worker_effect(
            worker_workflow,
            durable_worker_envelope,
            next_dir,
            worker_template,
        )
    if command_name == "abandon":
        worker_workflow.abandon("worker_infrastructure_exhausted")
        return _json_tool_result({
            "error": "WORKER_INFRASTRUCTURE_EXHAUSTED",
            "failure_class": "infrastructure",
            "action": "abandon_generation",
            "next_v": next_v,
            "source_v": source_v,
        })
    if command_name == "none":
        return _json_tool_result({
            "error": "WORKER_CYCLE_HAS_NO_PENDING_COMMAND",
            "next_v": next_v,
            "source_v": source_v,
            "stage": ckpt.get("stage"),
            "projected_stage": durable_worker_state.get("projected_stage"),
            "next_tool": route_policy(ckpt).get("next_tool"),
        })
    if _checkpoint_architecture_policy_identity_errors(ckpt):
        if _is_fresh_empty_pool_bootstrap(ckpt):
            return _json_tool_result({
                "error": "FIRST_STRICT_ARCHITECTURE_POLICY_IDENTITY_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "action": "abandon_generation",
                "directive": (
                    "The fresh first-strict architecture identity drifted. "
                    "Abandon and rematerialize the system blueprint; never "
                    "recover it from numeric high-water source bytes."
                ),
            })
        try:
            recovery = _recover_architecture_policy_identity(
                ckpt,
                next_dir,
                get_bot_dir(source_v),
            )
        except Exception as exc:
            log_system_event(
                "pipeline.architecture_policy_identity_replan_failed",
                "error",
                f"Could not reset stale-policy candidate v{next_v}: {type(exc).__name__}: {exc}",
                {"next_v": next_v, "source_v": source_v, "stage": ckpt.get("stage")},
            )
            return _json_tool_result({
                "error": "ARCHITECTURE_POLICY_IDENTITY_RECOVERY_FAILED",
                "next_v": next_v,
                "source_v": source_v,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                "directive": "Do not run bot workers; repair checkpoint/source synchronization first.",
            })
        if recovery is not None:
            return recovery
    if ckpt.get("stage") == "master_planned":
        from prepared_baseline_contract import validate_prepared_artifact_contract

        prepared_artifact_contract = (
            (ckpt.get("audit_context") or {}).get("prepared_artifact_contract")
        )
        prepared_artifact_errors = validate_prepared_artifact_contract(
            prepared_artifact_contract,
            prepared_dir=next_dir,
            source_v=source_v,
            next_v=next_v,
            verify_live_content=True,
        )
        if prepared_artifact_errors:
            return _json_tool_result({
                "error": "PREPARED_ARTIFACT_DRIFT_BEFORE_WORKERS",
                "next_v": next_v,
                "source_v": source_v,
                "validation_errors": prepared_artifact_errors,
                "next_tool": "abandon_generation",
                "directive": (
                    "The candidate changed after Master accepted the frozen prepared "
                    "baseline but before Workers. Abandon and restart; do not grant "
                    "the drift a repair scope."
                ),
            })
    rework_stages = {"quality_failed", "precommit_failed", "official_failed", "repair_planned", "rework_running"}
    checkpoint_work_item = (
        durable_worker_envelope.get("work_item")
        if durable_worker_resume
        and isinstance(durable_worker_envelope.get("work_item"), dict)
        else _checkpoint_master_plan(ckpt).get("work_item")
        if isinstance(_checkpoint_master_plan(ckpt).get("work_item"), dict)
        else {}
    )
    checkpoint_has_frozen_preparation = bool(
        isinstance(checkpoint_work_item, dict)
        and checkpoint_work_item.get("repair_baseline_artifact_hash")
        and checkpoint_work_item.get("prepared_snapshot_hash")
    )
    frozen_rework_resume = bool(
        durable_worker_resume
        and durable_worker_envelope.get("kind") != "initial_worker"
        or (
            ckpt.get("stage") in {"repair_planned", "rework_running"}
            and checkpoint_work_item.get("repair_baseline_artifact_hash")
        )
    )
    prepared_repair_resume_dir = None
    prepared_repair_resume_hash = ""
    if (
        durable_worker_status == "idle"
        and ckpt.get("stage") in {"repair_planned", "rework_running"}
        and isinstance(checkpoint_work_item, dict)
    ):
        prepared_repair_resume_hash = str(
            checkpoint_work_item.get("prepared_snapshot_hash") or ""
        )
        if (
            checkpoint_work_item.get("repair_baseline_artifact_hash")
            and not prepared_repair_resume_hash
        ):
            return _json_tool_result({
                "error": "DURABLE_REPAIR_PREPARATION_RECEIPT_MISSING",
                "failure_class": "state_migration",
                "action": "abandon_generation",
                "next_v": next_v,
                "source_v": source_v,
                "directive": (
                    "A repair work item claims a prepared baseline but does not "
                    "bind its immutable snapshot. Do not reconstruct or rerun "
                    "one-time preparation from mutable candidate bytes."
                ),
            })
        if prepared_repair_resume_hash:
            try:
                prepared_repair_resume_dir = worker_workflow.artifacts.path_for(
                    prepared_repair_resume_hash
                )
            except Exception:
                prepared_repair_resume_dir = None
    if ckpt.get("stage") in rework_stages:
        expected_repair_baseline = _checkpoint_repair_baseline_fingerprint(ckpt)
        # Once repair preparation has been captured and projected into the
        # checkpoint, that immutable artifact is the recovery authority.  The
        # canonical candidate intentionally still contains the pre-preparation
        # bytes, so comparing it here would turn a crash between checkpoint
        # publication and WorkerPrepared into a false drift/abandon.
        current_repair_baseline = _complete_artifact_fingerprint(
            prepared_repair_resume_dir
            if prepared_repair_resume_dir is not None
            else next_dir
        )
        if not expected_repair_baseline:
            return _json_tool_result({
                "error": "REPAIR_BASELINE_RECEIPT_MISSING",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "next_tool": "abandon_generation",
                "directive": (
                    "The failed gate/repair plan does not bind the exact complete "
                    "candidate artifact. Abandon; do not infer repair authority "
                    "from file paths or the live diff."
                ),
            })
        if (
            not current_repair_baseline
            or current_repair_baseline != expected_repair_baseline
        ):
            abandon_result = {}
            if frozen_rework_resume:
                abandon_result = await _force_abandon_frozen_worker_generation(
                    next_v,
                    source_v,
                    "frozen_rework_baseline_drift",
                    actor_lock_owned=actor_lock_owned,
                )
            return _json_tool_result({
                "error": "REPAIR_BASELINE_ARTIFACT_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "expected_artifact_hash": expected_repair_baseline,
                "current_artifact_hash": current_repair_baseline,
                "next_tool": "abandon_generation",
                **abandon_result,
                "directive": (
                    "The candidate changed after the gate evidence or repair plan "
                    "was frozen. Abandon; the drift cannot piggyback on a declared "
                    "repair file."
                ),
            })
        canonical_feedback = (
            str(durable_worker_envelope.get("reviewer_feedback") or "")
            if durable_worker_resume
            else _checkpoint_rework_feedback(ckpt)
        )
        if not canonical_feedback:
            return _json_tool_result({
                "error": "REWORK_FEEDBACK_AUTHORITY_MISSING",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "next_tool": "abandon_generation",
                "directive": (
                    "The checkpoint/gate receipt contains no canonical repair "
                    "feedback. Caller feedback cannot create repair authority."
                ),
            })
        if reviewer_feedback and not _transport_equivalent_feedback(
            reviewer_feedback,
            canonical_feedback,
        ):
            log_system_event(
                "pipeline.worker_rework_feedback_mismatch",
                "error",
                f"Rejected caller-rewritten rework feedback for v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": ckpt.get("stage"),
                    "canonical_feedback_digest": hashlib.sha256(
                        canonical_feedback.encode("utf-8")
                    ).hexdigest(),
                    "supplied_feedback_digest": hashlib.sha256(
                        str(reviewer_feedback).encode("utf-8")
                    ).hexdigest(),
                },
            )
            return _json_tool_result({
                "error": "REWORK_FEEDBACK_AUTHORITY_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "next_tool": "abandon_generation",
                "directive": (
                    "Pass empty reviewer_feedback to load the checkpoint receipt, "
                    "or echo that receipt exactly. Caller-authored feedback cannot "
                    "add files, blockers, or repair instructions."
                ),
            })
        reviewer_feedback = canonical_feedback

        if frozen_rework_resume:
            authoritative_rework_tasks = deepcopy(
                durable_worker_envelope.get("tasks")
                if durable_worker_resume
                else _checkpoint_master_plan(ckpt).get("tasks") or []
            )
            authority_errors = _frozen_rework_task_authority_errors(
                ckpt,
                authoritative_rework_tasks,
            )
        else:
            authoritative_rework_tasks, authority_errors = (
                _authoritative_rework_tasks(
                    ckpt,
                    canonical_feedback,
                )
            )
        if authority_errors:
            abandon_result = {}
            if frozen_rework_resume:
                abandon_result = await _force_abandon_frozen_worker_generation(
                    next_v,
                    source_v,
                    "frozen_rework_task_authority_invalid",
                    actor_lock_owned=actor_lock_owned,
                )
            return _json_tool_result({
                "error": "REWORK_TASK_AUTHORITY_INVALID",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "validation_errors": authority_errors,
                "next_tool": "abandon_generation",
                **abandon_result,
                "directive": (
                    "The system could not derive signed, file-scoped repair tasks "
                    "from the checkpoint/gate receipt. Do not execute caller tasks."
                ),
            })
        if tasks_provided and _canonical_tasks_digest(tasks) != _canonical_tasks_digest(
            authoritative_rework_tasks
        ):
            unsigned_workers = [
                str(task.get("worker_id") or f"task_{index}")
                for index, task in enumerate(tasks)
                if not _repair_contract_signature(task, next_v)
            ]
            log_system_event(
                "pipeline.worker_rework_task_authority_mismatch",
                "error",
                f"Rejected caller-rewritten rework tasks for v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": ckpt.get("stage"),
                    "expected_digest": _canonical_tasks_digest(authoritative_rework_tasks),
                    "supplied_digest": _canonical_tasks_digest(tasks),
                    "unsigned_worker_ids": unsigned_workers,
                },
            )
            return _json_tool_result({
                "error": "REWORK_TASK_AUTHORITY_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "checkpoint_stage": ckpt.get("stage"),
                "expected_digest": _canonical_tasks_digest(authoritative_rework_tasks),
                "supplied_digest": _canonical_tasks_digest(tasks),
                "unsigned_worker_ids": unsigned_workers,
                "next_tool": "abandon_generation",
                "directive": (
                    "Pass tasks=[] to load system-synthesized repair tasks, or echo "
                    "the exact canonical list. Extra, shortened, or unsigned tasks "
                    "cannot expand repair authority."
                ),
            })
        tasks = deepcopy(authoritative_rework_tasks)
    declared_scope_violations = _declared_scope_violation_files(
        ckpt,
        reviewer_feedback,
    )
    if declared_scope_violations:
        log_system_event(
            "pipeline.declared_scope_integrity_violation",
            "error",
            f"Refusing repair workers for v{next_v}: undeclared artifact edits",
            {
                "next_v": next_v,
                "source_v": source_v,
                "stage": ckpt.get("stage"),
                "violation_files": sorted(declared_scope_violations),
            },
        )
        return _json_tool_result({
            "error": "DECLARED_SCOPE_INTEGRITY_VIOLATION",
            "next_v": next_v,
            "source_v": source_v,
            "violation_files": sorted(declared_scope_violations),
            "next_tool": "abandon_generation",
            "directive": (
                "A failed diff cannot authorize itself through a repair ledger. "
                "Abandon this candidate and restart from a frozen prepared/source "
                "baseline with explicit Master task scope."
            ),
        })
    if not ckpt.get("master_plan") and ckpt.get("stage") not in rework_stages:
        return _json_tool_result({
            "error": "execute_workers requires a master plan. Call run_master first to produce a task plan.",
            "next_v": next_v,
            "source_v": source_v,
        })

    # Initial execution is owned by the accepted Master checkpoint.  The outer
    # orchestrator may echo that list (the MCP schema currently requires a tasks
    # argument) or pass [], but it cannot shorten/rewrite prompts, targets,
    # checks, or runtime contracts.  Rework stages use their separate,
    # deterministic synthesis/replacement routes below.
    if ckpt.get("stage") == "master_planned":
        if reviewer_feedback:
            log_system_event(
                "pipeline.worker_initial_feedback_rejected",
                "error",
                f"Rejected caller feedback on initial worker plan for v{next_v}",
                {"next_v": next_v, "source_v": source_v},
            )
            return _json_tool_result({
                "error": "WORKER_INITIAL_FEEDBACK_FORBIDDEN",
                "next_v": next_v,
                "source_v": source_v,
                "directive": (
                    "Initial master_planned execution must use the checkpoint task "
                    "verbatim with empty reviewer_feedback. Feedback is accepted only "
                    "on an explicit review/quality/precommit rework route."
                ),
            })
        _authoritative_tasks = _checkpoint_master_plan(ckpt).get("tasks")
        _authority_errors = _checkpoint_master_task_authority_errors(
            ckpt,
            _authoritative_tasks,
        )
        if _authority_errors:
            log_system_event(
                "pipeline.worker_task_authority_invalid",
                "error",
                f"Checkpoint worker authority invalid for v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "errors": _authority_errors,
                },
            )
            return _json_tool_result({
                "error": "WORKER_TASK_AUTHORITY_INVALID",
                "next_v": next_v,
                "source_v": source_v,
                "validation_errors": _authority_errors,
                "directive": (
                    "Do not execute workers. The accepted Master task/ledger "
                    "authority must be repaired or the generation abandoned."
                ),
            })
        if tasks_provided and tasks != _authoritative_tasks:
            _expected_digest = _canonical_tasks_digest(_authoritative_tasks)
            _supplied_digest = _canonical_tasks_digest(tasks)
            log_system_event(
                "pipeline.worker_task_plan_mismatch",
                "error",
                f"Rejected caller-rewritten worker tasks for v{next_v}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "expected_digest": _expected_digest,
                    "supplied_digest": _supplied_digest,
                    "expected_worker_ids": [
                        task.get("worker_id") for task in _authoritative_tasks
                        if isinstance(task, dict)
                    ],
                    "supplied_worker_ids": [
                        task.get("worker_id") for task in tasks if isinstance(task, dict)
                    ],
                },
            )
            return _json_tool_result({
                "error": "WORKER_TASK_PLAN_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "expected_digest": _expected_digest,
                "supplied_digest": _supplied_digest,
                "directive": (
                    "Pass tasks=[] to load the checkpoint-owned plan, or pass the "
                    "exact tasks returned by run_master. Do not paraphrase them."
                ),
            })
        if durable_worker_resume:
            durable_tasks = durable_worker_envelope.get("tasks") or []
            if _canonical_tasks_digest(durable_tasks) != _canonical_tasks_digest(
                _authoritative_tasks
            ):
                abandon_result = await _force_abandon_frozen_worker_generation(
                    next_v,
                    source_v,
                    "durable_initial_worker_task_drift",
                    actor_lock_owned=actor_lock_owned,
                )
                worker_workflow.abandon("durable_initial_worker_task_drift")
                return _json_tool_result({
                    "error": "DURABLE_INITIAL_WORKER_TASK_DRIFT",
                    "next_v": next_v,
                    "source_v": source_v,
                    **abandon_result,
                })
            _authoritative_tasks = durable_tasks
        tasks = deepcopy(_authoritative_tasks)

    review_rework_checkpoint = _is_review_rework_checkpoint(ckpt)
    official_rework_checkpoint = _is_official_rework_checkpoint(ckpt)
    replace_checkpoint_tasks = ckpt.get("stage") in rework_stages

    if official_rework_checkpoint and not frozen_rework_resume:
        checkpoint_tasks = _checkpoint_master_plan(ckpt).get("tasks", [])
        supplied_tasks = tasks
        tasks = _official_repair_tasks(ckpt, reviewer_feedback)
        replace_checkpoint_tasks = True
        log_system_event(
            "pipeline.official_repair_tasks_forced",
            "warn",
            f"Replaced prior/supplied tasks with deterministic official repair for v{next_v}",
            {
                "next_v": next_v,
                "source_v": source_v,
                "stage": ckpt.get("stage"),
                "old_target_files": sorted(_task_target_filenames(checkpoint_tasks)),
                "supplied_target_files": sorted(_task_target_filenames(supplied_tasks)),
                "new_target_files": sorted(_task_target_filenames(tasks)),
                "worker_id": tasks[0].get("worker_id") if tasks else None,
            },
        )

    # If tasks are not provided, load them from the authoritative checkpoint.
    # Provider sessions are always fresh and never carry task authority in
    # remote conversation history.
    if not tasks:
        plan = _checkpoint_master_plan(ckpt)
        checkpoint_tasks = plan.get("tasks", [])
        precommit_stale_reason = (
            _precommit_repair_task_refresh_reason(checkpoint_tasks, ckpt, reviewer_feedback)
            if checkpoint_tasks and _is_precommit_rework_checkpoint(ckpt) else ""
        )
        review_stale_reason = (
            _review_repair_task_refresh_reason(checkpoint_tasks, ckpt, reviewer_feedback)
            if checkpoint_tasks and review_rework_checkpoint else ""
        )
        quality_stale_reason = (
            _stale_quality_task_reason(checkpoint_tasks, ckpt, reviewer_feedback)
            if (
                checkpoint_tasks
                and not _is_precommit_rework_checkpoint(ckpt)
                and not _is_official_rework_checkpoint(ckpt)
                and not review_rework_checkpoint
            ) else ""
        )
        if ckpt.get("stage") in rework_stages and (
            not checkpoint_tasks
            or quality_stale_reason
            or precommit_stale_reason
            or review_stale_reason
        ):
            tasks = _synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback)
            if tasks:
                replace_checkpoint_tasks = bool(checkpoint_tasks)
                event_type = (
                    "pipeline.workers_tasks_refreshed"
                    if checkpoint_tasks else "pipeline.workers_tasks_synthesized"
                )
                if checkpoint_tasks and _is_precommit_rework_checkpoint(ckpt):
                    event_message = (
                        f"Refreshed precommit repair task(s) for v{next_v}: {precommit_stale_reason}"
                    )
                elif checkpoint_tasks and review_stale_reason:
                    event_message = (
                        f"Refreshed review repair task(s) for v{next_v}: {review_stale_reason}"
                    )
                elif quality_stale_reason:
                    event_message = (
                        f"Refreshed quality repair task(s) for v{next_v}: {quality_stale_reason}"
                    )
                else:
                    event_message = (
                        f"Synthesized {len(tasks)} rework task(s) for v{next_v} from checkpoint gate feedback"
                    )
                log_system_event(
                    event_type,
                    "warn",
                    event_message,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": ckpt.get("stage"),
                        "parent2_v": ckpt.get("parent2_v"),
                        "old_target_files": sorted(_task_target_filenames(checkpoint_tasks)),
                        "new_target_files": sorted(_task_target_filenames(tasks)),
                        "refresh_reason": (
                            precommit_stale_reason
                            or review_stale_reason
                            or quality_stale_reason
                        ),
                        "num_tasks": len(tasks),
                        "task_kind": tasks[0].get("task_kind") if tasks else None,
                    },
                )
        elif checkpoint_tasks:
            tasks = checkpoint_tasks
            log_system_event("pipeline.workers_tasks_from_checkpoint", "info",
                             f"Tasks loaded from checkpoint for v{next_v} (LLM omitted tasks arg)",
                             {"next_v": next_v, "num_tasks": len(tasks)})
        else:
            return _json_tool_result({
                "error": "No tasks provided and checkpoint has no task plan. Call run_master first.",
                "next_v": next_v,
                "source_v": source_v,
                })
        if not tasks:
            return _json_tool_result({
                "error": "No tasks provided and checkpoint has no task plan. Call run_master first.",
                "next_v": next_v,
                "source_v": source_v,
                "stage": ckpt.get("stage"),
            })

    if (
        not frozen_rework_resume
        and tasks
        and ckpt.get("stage") in {"quality_failed", "repair_planned", "rework_running"}
        and not _is_precommit_rework_checkpoint(ckpt)
        and not _is_official_rework_checkpoint(ckpt)
        and not review_rework_checkpoint
    ):
        failure_files = _quality_failure_target_files(ckpt, reviewer_feedback)
        task_files = _task_target_filenames(tasks)
        missing_files = sorted(failure_files - task_files)
        quality_stale_reason = _stale_quality_task_reason(tasks, ckpt, reviewer_feedback)
        if missing_files or quality_stale_reason:
            refreshed_tasks = _synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback)
            if refreshed_tasks:
                tasks = refreshed_tasks
                replace_checkpoint_tasks = True
                refresh_reason = (
                    f"old task targets missed {missing_files}" if missing_files else quality_stale_reason
                )
                log_system_event(
                    "pipeline.workers_tasks_refreshed",
                    "warn",
                    f"Refreshed quality repair task(s) for v{next_v}; {refresh_reason}",
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "missing_files": missing_files,
                        "refresh_reason": quality_stale_reason,
                        "old_target_files": sorted(task_files),
                        "new_target_files": sorted(_task_target_filenames(refreshed_tasks)),
                        "num_tasks": len(refreshed_tasks),
                    },
                )

    if (
        not frozen_rework_resume
        and tasks
        and _is_precommit_rework_checkpoint(ckpt)
    ):
        precommit_stale_reason = _precommit_repair_task_refresh_reason(tasks, ckpt, reviewer_feedback)
    else:
        precommit_stale_reason = ""
    if tasks and _is_precommit_rework_checkpoint(ckpt) and precommit_stale_reason:
        refreshed_tasks = _synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback)
        if refreshed_tasks:
            old_files = sorted(_task_target_filenames(tasks))
            tasks = refreshed_tasks
            replace_checkpoint_tasks = True
            log_system_event(
                "pipeline.workers_tasks_refreshed",
                "warn",
                f"Refreshed precommit repair task(s) for v{next_v}; {precommit_stale_reason}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "old_target_files": old_files,
                    "new_target_files": sorted(_task_target_filenames(refreshed_tasks)),
                    "num_tasks": len(refreshed_tasks),
                    "task_kind": refreshed_tasks[0].get("task_kind") if refreshed_tasks else None,
                    "refresh_reason": precommit_stale_reason,
                },
            )

    if not frozen_rework_resume and tasks and review_rework_checkpoint:
        review_stale_reason = _review_repair_task_refresh_reason(tasks, ckpt, reviewer_feedback)
    else:
        review_stale_reason = ""
    if tasks and review_rework_checkpoint and review_stale_reason:
        refreshed_tasks = _synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback)
        if refreshed_tasks:
            old_files = sorted(_task_target_filenames(tasks))
            tasks = refreshed_tasks
            replace_checkpoint_tasks = True
            log_system_event(
                "pipeline.workers_tasks_refreshed",
                "warn",
                f"Refreshed review repair task(s) for v{next_v}; {review_stale_reason}",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "old_target_files": old_files,
                    "new_target_files": sorted(_task_target_filenames(refreshed_tasks)),
                    "num_tasks": len(refreshed_tasks),
                    "task_kind": refreshed_tasks[0].get("task_kind") if refreshed_tasks else None,
                    "refresh_reason": review_stale_reason,
                },
            )

    if (
        not frozen_rework_resume
        and tasks
        and ckpt.get("stage") in rework_stages
        and not _is_precommit_rework_checkpoint(ckpt)
        and not _is_official_rework_checkpoint(ckpt)
        and not review_rework_checkpoint
    ):
        ordered_tasks = _order_quality_repair_tasks(tasks)
        old_order = [str(task.get("worker_id", idx + 1)) for idx, task in enumerate(tasks)]
        new_order = [str(task.get("worker_id", idx + 1)) for idx, task in enumerate(ordered_tasks)]
        if new_order != old_order:
            tasks = ordered_tasks
            replace_checkpoint_tasks = True
            log_system_event(
                "pipeline.quality_repair_tasks_reordered",
                "info",
                f"Reordered quality repair tasks for v{next_v}; file_size cleanup will run last",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "old_order": old_order,
                    "new_order": new_order,
                },
            )

    critic_refusal = _critic_advisory_rework_refusal(
        ckpt,
        tasks,
        next_v,
        source_v,
    )
    if critic_refusal:
        return _json_tool_result(critic_refusal)

    task_write_scope_errors = _task_write_scope_errors(tasks, next_v)
    if task_write_scope_errors:
        return _json_tool_result({
            "error": "WORKER_TASK_WRITE_SCOPE_INVALID",
            "next_v": next_v,
            "source_v": source_v,
            "validation_errors": task_write_scope_errors,
            "next_tool": "abandon_generation",
            "directive": (
                "must_change_files is a completion requirement, not write "
                "authority. Every required file must already be in "
                "target_files/files_allowed."
            ),
        })

    # B6 (2026-06-30): redundant-call guard. execute_workers is NOT idempotent —
    # a redundant call (no reviewer_feedback) when workers already ran resets code
    # from source + re-runs every Worker-LLM (the single most expensive pipeline
    # step), wasting cost and mutating already-gated code. Only allow a re-run when
    # there is reviewer_feedback (a legitimate retry-after-reviewer-reject). A pure
    # redundant call must be refused so the orchestrator proceeds to the next gate.
    _b6_stage = ckpt.get("stage")
    if (not reviewer_feedback
            and _b6_stage in ("workers_done", "quality_failed", "quality_passed", "reviewed", "critic_checked", "precommit_failed", "verified")):
        if _b6_stage == "precommit_failed":
            return _json_tool_result({
                "error": (
                    "Precommit failed, but execute_workers was called without reviewer_feedback. "
                    "Pass the exact precommit_eval directive/blockers as reviewer_feedback."
                ),
                "next_v": next_v,
                "source_v": source_v,
                "stage": _b6_stage,
                "intent": {
                    "kind": "rework",
                    "next_tool": "execute_workers",
                    "failure_class": "regression",
                    "authority": "tool:execute_workers",
                    "safe_to_auto_execute": False,
                },
            })
        try:
            log_system_event(
                "pipeline.workers_redundant_call_blocked", "warn",
                f"execute_workers called again for v{next_v} at stage={_b6_stage} with no "
                f"reviewer_feedback — refusing re-run (would reset code + waste Worker-LLM "
                f"cost). Proceed to the next gate instead.",
                {"next_v": next_v, "source_v": source_v, "stage": _b6_stage},
            )
        except Exception:
            pass
        return _json_tool_result({
            "info": (f"Workers already ran for v{next_v} (stage={_b6_stage}). The code is in place. "
                     f"Do NOT call execute_workers again — proceed to the next pipeline gate "
                     f"(run_quality_gates / run_review / run_critic / run_precommit_eval / commit_bot)."),
            "next_v": next_v,
            "source_v": source_v,
            "stage": _b6_stage,
            "redundant_call_blocked": True,
        })

    # Circuit breaker: limit total worker failures per generation
    # Backward compat: old checkpoints used worker_invocation_count instead of worker_failure_count
    failure_count = ckpt.get("worker_failure_count", ckpt.get("worker_invocation_count", 0))
    MAX_WORKER_FAILURES = 6
    if failure_count >= MAX_WORKER_FAILURES:
        try:
            log_system_event('pipeline.circuit_breaker', 'error',
                f'Circuit breaker: {failure_count} worker failures',
                {'next_v': next_v, 'source_v': source_v, 'failure_count': failure_count})
        except Exception:
            pass
        return _json_tool_result({
            "error": f"CIRCUIT BREAKER: {failure_count} worker failures already recorded this generation (max {MAX_WORKER_FAILURES}). Abandon this generation and start a new one.",
            "failure_count": failure_count,
            "next_v": next_v,
            "source_v": source_v,
        })

    # When retrying after workers already ran, actually reset code from source first.
    # Previous claim that code was reset was FALSE — now we actually do it.
    force_sequential_rework = False
    task_skipper = None
    quality_skipper_config = None
    rework_plan_metadata = None
    precommit_rework_count_for_write = None
    official_rework_count_for_write = None
    mechanical_trim_results = []
    rework_preparation_dir = None
    prepared_candidate_dir = next_dir
    durable_preparation_resume = False

    def rollback_rework_preparation():
        if rework_preparation_dir is None:
            return ""
        try:
            worker_workflow.artifacts.discard_workspace(
                rework_preparation_dir
            )
            return ""
        except Exception as rollback_exc:
            return f"{type(rollback_exc).__name__}: {str(rollback_exc)[:300]}"
    existing_prepared_work = (
        (_checkpoint_master_plan(ckpt).get("work_item") or {})
        if isinstance(_checkpoint_master_plan(ckpt).get("work_item"), dict)
        else {}
    )
    existing_prepared_snapshot = str(
        existing_prepared_work.get("prepared_snapshot_hash") or ""
    )
    if (
        durable_worker_status == "idle"
        and ckpt.get("stage") in {"repair_planned", "rework_running"}
        and existing_prepared_snapshot
    ):
        try:
            prepared_candidate_dir = worker_workflow.artifacts.path_for(
                existing_prepared_snapshot
            )
            expected_prepared_hash = str(
                existing_prepared_work.get("repair_baseline_artifact_hash") or ""
            )
            if (
                not expected_prepared_hash
                or _complete_artifact_fingerprint(prepared_candidate_dir)
                != expected_prepared_hash
            ):
                raise RuntimeError("prepared repair snapshot hash mismatch")
            durable_preparation_resume = True
            rework_plan_metadata = deepcopy(existing_prepared_work)
            frozen_worker_input = rework_plan_metadata.get(
                "frozen_worker_input"
            )
            frozen_worker_input_digest = str(
                rework_plan_metadata.get("frozen_worker_input_digest") or ""
            )
            projection_preimage_artifact_hash = str(
                rework_plan_metadata.get(
                    "projection_preimage_artifact_hash"
                )
                or ""
            )
            projection_preimage_snapshot_hash = str(
                rework_plan_metadata.get(
                    "projection_preimage_snapshot_hash"
                )
                or ""
            )
            if not isinstance(frozen_worker_input, dict):
                raise RuntimeError("frozen Worker preparation input missing")
            actual_frozen_input_digest = hashlib.sha256(
                json.dumps(
                    frozen_worker_input,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            if actual_frozen_input_digest != frozen_worker_input_digest:
                raise RuntimeError("frozen Worker preparation input digest mismatch")
            if (
                frozen_worker_input.get("schema_version") != 4
                or frozen_worker_input.get("tasks") != tasks
                or str(frozen_worker_input.get("reviewer_feedback") or "")
                != reviewer_feedback
                or frozen_worker_input.get("worker_template_hash")
                != hashlib.sha256(worker_template.encode("utf-8")).hexdigest()
                or frozen_worker_input.get("backend_contract")
                != _worker_backend_contract()
                or "worker_execution_context" in frozen_worker_input
                or not projection_preimage_artifact_hash
                or not projection_preimage_snapshot_hash
                or frozen_worker_input.get(
                    "projection_preimage_artifact_hash"
                )
                != projection_preimage_artifact_hash
                or frozen_worker_input.get(
                    "projection_preimage_snapshot_hash"
                )
                != projection_preimage_snapshot_hash
            ):
                raise RuntimeError("frozen Worker preparation input contract drift")
            projection_preimage_dir = worker_workflow.artifacts.path_for(
                projection_preimage_snapshot_hash
            )
            if (
                _complete_artifact_fingerprint(projection_preimage_dir)
                != projection_preimage_artifact_hash
            ):
                raise RuntimeError("frozen Worker projection preimage mismatch")
            if (
                _complete_artifact_fingerprint(next_dir)
                != projection_preimage_artifact_hash
            ):
                raise RuntimeError("canonical Worker projection preimage drift")
            precommit_rework_count_for_write = int(
                ckpt.get("precommit_rework_count") or 0
            )
            official_rework_count_for_write = int(
                ckpt.get("official_rework_count") or 0
            )
            task_kinds = {
                str(task.get("task_kind") or "")
                for task in tasks
                if isinstance(task, dict)
            }
            if (
                "quality_repair" in str(
                    existing_prepared_work.get("kind") or ""
                )
                or any("quality_repair" in kind for kind in task_kinds)
            ) and not _is_precommit_rework_checkpoint(
                ckpt
            ) and not _is_official_rework_checkpoint(ckpt):
                force_sequential_rework = True
                quality_skipper_config = {
                    "source_dir": get_bot_dir(source_v),
                    "expected_architecture_policy": (
                        _checkpoint_master_plan(ckpt).get(
                            "architecture_policy"
                        )
                    ),
                    "master_plan": _checkpoint_master_plan(ckpt),
                }
        except Exception as exc:
            return _json_tool_result({
                "error": "DURABLE_REPAIR_PREPARATION_UNAVAILABLE",
                "next_v": next_v,
                "source_v": source_v,
                "action": "abandon_generation",
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
            })
    if (
        frozen_rework_resume
        and reviewer_feedback
        and ckpt.get("stage") in {"repair_planned", "rework_running"}
    ):
        frozen_plan = _checkpoint_master_plan(ckpt)
        frozen_work_item = (
            frozen_plan.get("work_item")
            if isinstance(frozen_plan.get("work_item"), dict)
            else {}
        )
        frozen_rework_kind = str(frozen_work_item.get("kind") or "")
        frozen_task_kinds = {
            str(task.get("task_kind") or "")
            for task in tasks or []
            if isinstance(task, dict)
        }
        is_frozen_quality_rework = (
            "quality_repair" in frozen_rework_kind
            or any("quality_repair" in kind for kind in frozen_task_kinds)
        )
        if (
            is_frozen_quality_rework
            and not _is_precommit_rework_checkpoint(ckpt)
            and not _is_official_rework_checkpoint(ckpt)
        ):
            force_sequential_rework = True
            quality_skipper_config = {
                "source_dir": get_bot_dir(source_v),
                "expected_architecture_policy": (
                    frozen_plan.get("architecture_policy")
                    if isinstance(frozen_plan.get("architecture_policy"), dict)
                    else None
                ),
                "master_plan": frozen_plan,
            }
        if ckpt.get("stage") == "repair_planned":
            rework_plan_metadata = frozen_work_item
    if (
        not frozen_rework_resume
        and not durable_preparation_resume
        and reviewer_feedback
        and ckpt.get("stage") in (
        "workers_done", "quality_failed", "quality_passed", "reviewed", "critic_checked",
        "precommit_failed", "official_failed", "repair_planned", "rework_running"
        )
    ):
        rework_kind = "quality_repair" if ckpt.get("stage") == "quality_failed" else "gate_rework"
        if ckpt.get("stage") == "official_failed":
            rework_kind = "official_repair"
        elif ckpt.get("stage") == "precommit_failed":
            rework_kind = "precommit_repair"
        elif ckpt.get("parent2_v") is not None:
            rework_kind = f"crossover_{rework_kind}"
        existing_work_item = (
            (ckpt.get("master_plan") or {}).get("work_item")
            if isinstance(ckpt.get("master_plan"), dict) else None
        )
        if (
            ckpt.get("stage") in {"repair_planned", "rework_running"}
            and isinstance(existing_work_item, dict)
            and existing_work_item.get("kind")
        ):
            rework_kind = str(existing_work_item.get("kind"))
        task_kinds = {
            str(task.get("task_kind") or "")
            for task in tasks or []
            if isinstance(task, dict)
        }
        if review_rework_checkpoint or any("review_repair" in kind for kind in task_kinds):
            rework_kind = (
                "crossover_review_repair"
                if ckpt.get("parent2_v") is not None or rework_kind.startswith("crossover_")
                else "review_repair"
            )
        elif _is_official_rework_checkpoint(ckpt) or any("official_repair" in kind for kind in task_kinds):
            rework_kind = "official_repair"
        is_precommit_rework = rework_kind == "precommit_repair" or _is_precommit_rework_checkpoint(ckpt)
        is_official_rework = rework_kind == "official_repair" or _is_official_rework_checkpoint(ckpt)
        if is_precommit_rework:
            prior_rework_count = int(ckpt.get("precommit_rework_count") or 0)
            precommit_rework_count_for_write = prior_rework_count + 1
            if precommit_rework_count_for_write > MAX_PRECOMMIT_REWORK_ROUNDS:
                message = (
                    f"PRECOMMIT_REWORK_CIRCUIT_BREAKER: v{next_v} already used "
                    f"{prior_rework_count} precommit repair round(s) (max {MAX_PRECOMMIT_REWORK_ROUNDS}). "
                    "Abandon this generation and start a fresh direction."
                )
                log_system_event(
                    "pipeline.precommit_rework_circuit_breaker",
                    "error",
                    message,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": ckpt.get("stage"),
                        "precommit_rework_count": prior_rework_count,
                        "max_rework_rounds": MAX_PRECOMMIT_REWORK_ROUNDS,
                        "task_targets": sorted(_task_target_filenames(tasks)),
                    },
                )
                return _json_tool_result({
                    "error": "PRECOMMIT_REWORK_CIRCUIT_BREAKER",
                    "message": message,
                    "next_v": next_v,
                    "source_v": source_v,
                    "precommit_rework_count": prior_rework_count,
                    "max_rework_rounds": MAX_PRECOMMIT_REWORK_ROUNDS,
                    "directive": "Abandon this generation; repeated precommit repair did not converge.",
                })
        if is_official_rework:
            prior_official_rework_count = int(ckpt.get("official_rework_count") or 0)
            official_rework_count_for_write = prior_official_rework_count + 1
            if official_rework_count_for_write > MAX_OFFICIAL_REWORK_ROUNDS:
                message = (
                    f"OFFICIAL_REWORK_CIRCUIT_BREAKER: v{next_v} already used "
                    f"{prior_official_rework_count} official repair round(s) "
                    f"(max {MAX_OFFICIAL_REWORK_ROUNDS}). Abandon this generation; "
                    "repeated formal certification repair did not converge."
                )
                log_system_event(
                    "pipeline.official_rework_circuit_breaker",
                    "error",
                    message,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": ckpt.get("stage"),
                        "official_rework_count": prior_official_rework_count,
                        "max_rework_rounds": MAX_OFFICIAL_REWORK_ROUNDS,
                        "task_targets": sorted(_task_target_filenames(tasks)),
                    },
                )
                abandon_result = await _force_abandon_official_rework_generation(
                    next_v,
                    source_v,
                    actor_lock_owned=actor_lock_owned,
                )
                return _json_tool_result({
                    "error": "OFFICIAL_REWORK_CIRCUIT_BREAKER",
                    "message": message,
                    "next_v": next_v,
                    "source_v": source_v,
                    "official_rework_count": prior_official_rework_count,
                    "max_rework_rounds": MAX_OFFICIAL_REWORK_ROUNDS,
                    "abandoned": bool(abandon_result.get("abandoned")),
                    "abandon_result": abandon_result,
                    "directive": (
                        "This generation was abandoned by the tool layer after "
                        "repeated official repair failed to converge. Start a fresh direction."
                    ),
                })
        source_dir_r = get_bot_dir(source_v)
        try:
            preparation_base = worker_workflow.artifacts.capture(next_dir)
            projection_preimage_artifact_hash = (
                _complete_artifact_fingerprint(next_dir)
            )
            projection_preimage_snapshot_hash = preparation_base
            if projection_preimage_artifact_hash != preparation_base:
                raise RuntimeError(
                    "canonical repair preimage snapshot mismatch"
                )
            preparation_digest = hashlib.sha256(
                json.dumps(
                    {
                        "stage": ckpt.get("stage"),
                        "tasks": tasks,
                        "reviewer_feedback": reviewer_feedback,
                        "source_hash": _complete_artifact_fingerprint(source_dir_r),
                        "precommit_rework_count": precommit_rework_count_for_write,
                        "official_rework_count": official_rework_count_for_write,
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            rework_preparation_dir = worker_workflow.artifacts.preparation_workspace(
                run_id=worker_workflow.run_id,
                cycle=int(durable_worker_state.get("cycle") or 0),
                input_digest=preparation_base,
                preparation_digest=preparation_digest,
            )
            prepared_candidate_dir = rework_preparation_dir
        except Exception as exc:
            return _json_tool_result({
                "error": "REWORK_PREPARATION_SNAPSHOT_FAILED",
                "next_v": next_v,
                "source_v": source_v,
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                "next_tool": "abandon_generation",
                "directive": (
                    "Could not freeze the complete candidate before one-time "
                    "repair preparation. No reset or hygiene mutation was run."
                ),
            })
        reset_before_rework = _should_reset_before_rework(ckpt, tasks)
        if reset_before_rework and source_dir_r.exists() and prepared_candidate_dir.exists():
            _log.info(f"Resetting v{next_v} code from source v{source_v} before worker retry (incremental, preserves NEW files)")
            # Incremental reset: overwrite source files (undo worker edits) but
            # PRESERVE worker-created NEW files absent from source. This avoids
            # wiping NEW files on redundant orchestrator re-calls of execute_workers
            # (which would otherwise cause zero-changes wasted retries).
            try:
                preserved = _incremental_reset_next_dir(
                    prepared_candidate_dir,
                    source_dir_r,
                )
            except Exception as exc:
                rollback_error = rollback_rework_preparation()
                return _json_tool_result({
                    "error": (
                        "REWORK_PREPARATION_ROLLBACK_FAILED"
                        if rollback_error else "REWORK_SOURCE_RESET_FAILED"
                    ),
                    "next_v": next_v,
                    "source_v": source_v,
                    "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "rollback_error": rollback_error,
                    "next_tool": "abandon_generation" if rollback_error else "execute_workers",
                })
            if preserved:
                _log.info("Preserved %d worker-created NEW file(s) across reset: %s",
                          len(preserved), preserved)
        elif not reset_before_rework:
            if rework_kind == "precommit_repair" or _is_precommit_rework_checkpoint(ckpt):
                log_system_event(
                    "pipeline.precommit_repair_in_place",
                    "warn",
                    f"Repairing v{next_v} in place after precommit failure; preserving candidate code",
                    {"next_v": next_v, "source_v": source_v, "parent2_v": ckpt.get("parent2_v")},
                )
            elif "review_repair" in rework_kind:
                event_type = (
                    "pipeline.crossover_review_repair_in_place"
                    if rework_kind.startswith("crossover_") or ckpt.get("parent2_v") is not None
                    else "pipeline.review_repair_in_place"
                )
                event_message = (
                    f"Repairing crossover v{next_v} in place after reviewer rejection; preserving fused candidate code"
                    if event_type == "pipeline.crossover_review_repair_in_place"
                    else f"Repairing v{next_v} in place after reviewer rejection; preserving generated candidate code"
                )
                log_system_event(
                    event_type,
                    "warn",
                    event_message,
                    {"next_v": next_v, "source_v": source_v, "parent2_v": ckpt.get("parent2_v")},
                )
            else:
                in_place_kind = (
                    "crossover_quality_repair"
                    if rework_kind.startswith("crossover_") or ckpt.get("parent2_v") is not None
                    else "quality_repair"
                )
                event_type = (
                    "pipeline.crossover_quality_repair_in_place"
                    if in_place_kind == "crossover_quality_repair"
                    else "pipeline.quality_repair_in_place"
                )
                event_message = (
                    f"Repairing crossover v{next_v} in place after quality failure; preserving fused candidate code"
                    if in_place_kind == "crossover_quality_repair"
                    else f"Repairing v{next_v} in place after quality failure; preserving generated candidate code"
                )
                log_system_event(
                    event_type,
                    "warn",
                    event_message,
                    {"next_v": next_v, "source_v": source_v, "parent2_v": ckpt.get("parent2_v")},
                )

        try:
            from candidate_hygiene import sanitize_candidate_dir
            from workflow_profiles import get_workflow_profile
            execution_mode = getattr(
                get_workflow_profile(), "national_execution_mode", "native_tcp"
            )
            if execution_mode != "native_tcp":
                raise RuntimeError(
                    "active candidate hygiene requires the official native_tcp "
                    f"execution mode, got {execution_mode!r}"
                )
            sanitize_candidate_dir(
                prepared_candidate_dir,
                require_native_tcp=True,
            )
        except Exception as exc:
            rollback_error = rollback_rework_preparation()
            log_system_event(
                "pipeline.candidate_hygiene_failed",
                "error",
                f"Candidate hygiene failed for v{next_v}: {exc}",
                {"next_v": next_v, "source_v": source_v, "stage": ckpt.get("stage")},
            )
            return _json_tool_result({
                "error": (
                    "REWORK_PREPARATION_ROLLBACK_FAILED"
                    if rollback_error else "CANDIDATE_HYGIENE_FAILED"
                ),
                "message": f"Candidate hygiene failed: {exc}",
                "rollback_error": rollback_error,
                "next_v": next_v,
                "source_v": source_v,
                "next_tool": "abandon_generation" if rollback_error else "execute_workers",
            })

        # Write intermediate checkpoint so pipeline state reflects the in-progress retry.
        # Without this, a crash between code reset and worker execution would leave
        # the checkpoint at a stale stage (e.g. "reviewed" or "critic_checked")
        # while the actual code has been wiped back to source.
        retry_plan = _checkpoint_plan_with_tasks(
            ckpt, tasks, replace_existing_tasks=replace_checkpoint_tasks
        )
        rework_plan_metadata = {
            "kind": rework_kind,
            "source_stage": ckpt.get("stage"),
            "reset_performed": reset_before_rework,
            "route": route_policy(ckpt),
        }
        retry_plan = {
            **retry_plan,
            "work_item": rework_plan_metadata,
        }
        for task in tasks:
            if isinstance(task, dict):
                task.setdefault("task_kind", rework_kind)
        retry_plan = _plan_with_accumulated_repair_scope(ckpt, retry_plan, tasks, next_v)
        task_kinds = {
            str(task.get("task_kind") or "")
            for task in tasks or []
            if isinstance(task, dict)
        }
        is_quality_rework = (
            ckpt.get("stage") == "quality_failed"
            or "quality_repair" in rework_kind
            or any("quality_repair" in kind for kind in task_kinds)
        )
        if (
            is_quality_rework
            and not _is_precommit_rework_checkpoint(ckpt)
            and not _is_official_rework_checkpoint(ckpt)
            and ckpt.get("stage") in {"quality_failed", "repair_planned", "rework_running"}
        ):
            force_sequential_rework = True
            quality_skipper_config = {
                "source_dir": source_dir_r,
                "expected_architecture_policy": (
                    (_checkpoint_master_plan(ckpt).get("architecture_policy"))
                    if isinstance(_checkpoint_master_plan(ckpt).get("architecture_policy"), dict)
                    else None
                ),
                "master_plan": retry_plan,
            }
            try:
                mechanical_trim_results = _apply_mechanical_file_size_trims(
                    tasks,
                    prepared_candidate_dir,
                    source_dir_r,
                    next_v,
                    source_v,
                )
            except Exception as exc:
                rollback_error = rollback_rework_preparation()
                return _json_tool_result({
                    "error": (
                        "REWORK_PREPARATION_ROLLBACK_FAILED"
                        if rollback_error else "REWORK_MECHANICAL_TRIM_FAILED"
                    ),
                    "next_v": next_v,
                    "source_v": source_v,
                    "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "rollback_error": rollback_error,
                    "next_tool": "abandon_generation" if rollback_error else "execute_workers",
                })

        if reset_before_rework:
            reviewer_feedback += (
                f"\n\nNOTE: This is a retry. The code in bots/national_v{next_v}/ has been ACTUALLY RESET "
                f"by the system to the exact national_v{source_v} preimage. The source path remains "
                f"unreadable to this Worker. Any modifications described in the feedback above no "
                f"longer exist in the candidate — re-implement them from the injected contract."
            )
        elif rework_kind == "precommit_repair" or _is_precommit_rework_checkpoint(ckpt):
            reviewer_feedback += (
                f"\n\nNOTE: This is an in-place precommit regression repair. The current code in "
                f"bots/national_v{next_v}/ is the candidate that failed precommit; preserve it except "
                f"for targeted EV/matchup regression fixes."
            )
        elif rework_kind == "official_repair" or _is_official_rework_checkpoint(ckpt):
            reviewer_feedback += (
                f"\n\nNOTE: This is an in-place official EXE full-certification repair. The current code in "
                f"bots/national_v{next_v}/ passed local gates but failed the real Windows national platform. "
                "Preserve the candidate except for the exact compliance/state-machine/obvious-decision blocker "
                "shown in the official evidence; do not use EXE win/loss as strength tuning evidence."
            )
        elif "review_repair" in rework_kind:
            reviewer_feedback += (
                f"\n\nNOTE: This is an in-place Lead Code Reviewer repair. The current code in "
                f"bots/national_v{next_v}/ is the candidate that failed the reviewer hard gate; "
                "preserve it except for the exact code-quality blocker described above."
            )
        else:
            if rework_kind.startswith("crossover_") or ckpt.get("parent2_v") is not None:
                reviewer_feedback += (
                    f"\n\nNOTE: This is an in-place crossover quality repair. The current code in "
                    f"bots/national_v{next_v}/ is the generated crossover candidate and must be preserved "
                    f"except for the exact quality-gate blockers above."
                )
            else:
                reviewer_feedback += (
                    f"\n\nNOTE: This is an in-place quality repair. The current code in "
                    f"bots/national_v{next_v}/ is the generated candidate and must be preserved "
                    f"except for the exact quality-gate blockers above."
                )
        changed_trims = [item for item in mechanical_trim_results if item.get("changed")]
        if changed_trims:
            trim_summary = "; ".join(
                f"{Path(item.get('target', item.get('file', ''))).name}: "
                f"{item.get('before')}L->{item.get('after')}L"
                for item in changed_trims
            )
            reviewer_feedback += (
                "\n\nNOTE: Before LLM workers, the pipeline mechanically removed "
                "non-behavioral Python text (comments/docstrings/blank lines) from "
                f"large file_size targets: {trim_summary}. Continue only if a blocker remains."
            )

        repair_baseline_artifact_hash = _complete_artifact_fingerprint(
            prepared_candidate_dir
        )
        if not repair_baseline_artifact_hash:
            rollback_error = rollback_rework_preparation()
            return _json_tool_result({
                "error": (
                    "REWORK_PREPARATION_ROLLBACK_FAILED"
                    if rollback_error else "REPAIR_BASELINE_ARTIFACT_UNAVAILABLE"
                ),
                "next_v": next_v,
                "source_v": source_v,
                "next_tool": "abandon_generation",
                "rollback_error": rollback_error,
                "directive": (
                    "Could not freeze the complete post-reset/post-hygiene repair "
                    "baseline. Do not execute Workers without a content receipt."
                ),
            })
        prepared_repair_snapshot_hash = worker_workflow.artifacts.capture(
            prepared_candidate_dir
        )
        if prepared_repair_snapshot_hash != repair_baseline_artifact_hash:
            rollback_error = rollback_rework_preparation()
            return _json_tool_result({
                "error": "REPAIR_PREPARATION_SNAPSHOT_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "rollback_error": rollback_error,
            })
        frozen_preparation_input = {
            "schema_version": 4,
            "tasks": deepcopy(tasks),
            "reviewer_feedback": reviewer_feedback,
            "worker_template_hash": hashlib.sha256(
                worker_template.encode("utf-8")
            ).hexdigest(),
            "backend_contract": _worker_backend_contract(),
            "projection_preimage_artifact_hash": (
                projection_preimage_artifact_hash
            ),
            "projection_preimage_snapshot_hash": (
                projection_preimage_snapshot_hash
            ),
        }
        frozen_preparation_input_digest = hashlib.sha256(
            json.dumps(
                frozen_preparation_input,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        rework_plan_metadata = {
            **rework_plan_metadata,
            "projection_preimage_artifact_hash": (
                projection_preimage_artifact_hash
            ),
            "projection_preimage_snapshot_hash": (
                projection_preimage_snapshot_hash
            ),
            "repair_baseline_artifact_hash": repair_baseline_artifact_hash,
            "prepared_snapshot_hash": prepared_repair_snapshot_hash,
            "frozen_worker_input": frozen_preparation_input,
            "frozen_worker_input_digest": frozen_preparation_input_digest,
        }
        retry_plan = {
            **retry_plan,
            "work_item": rework_plan_metadata,
        }
        retry_plan = _plan_with_accumulated_repair_scope(
            ckpt,
            retry_plan,
            tasks,
            next_v,
        )
        repair_checkpoint_written = write_pipeline_checkpoint(
            next_v,
            source_v,
            "repair_planned",
            master_plan=retry_plan,
            reviewer_feedback=reviewer_feedback,
            worker_failure_count=ckpt.get("worker_failure_count", 0),
            precommit_rework_count=precommit_rework_count_for_write,
            official_rework_count=official_rework_count_for_write,
            repair_baseline_artifact_hash=repair_baseline_artifact_hash,
            expected_checkpoint_revision=int(
                ckpt.get("checkpoint_revision") or 0
            ),
            expected_checkpoint_stage=str(ckpt.get("stage") or ""),
            expected_workflow_run_id=str(ckpt.get("workflow_run_id") or ""),
        )
        if not repair_checkpoint_written:
            rollback_error = rollback_rework_preparation()
            return _json_tool_result({
                "error": (
                    "REWORK_PREPARATION_ROLLBACK_FAILED"
                    if rollback_error else "REPAIR_BASELINE_CHECKPOINT_FAILED"
                ),
                "next_v": next_v,
                "source_v": source_v,
                "expected_artifact_hash": repair_baseline_artifact_hash,
                "candidate_restored": not rollback_error,
                "rollback_error": rollback_error,
                "directive": (
                    "The system prepared a repair baseline but could not persist its "
                    "content receipt. Do not execute Workers or claim repair authority."
                ),
            })

    if reviewer_feedback and rework_plan_metadata:
        expected_rework_hash = str(
            rework_plan_metadata.get("repair_baseline_artifact_hash") or ""
        )
        current_rework_hash = _complete_artifact_fingerprint(
            prepared_candidate_dir
        )
        if (
            not expected_rework_hash
            or not current_rework_hash
            or current_rework_hash != expected_rework_hash
        ):
            return _json_tool_result({
                "error": "REPAIR_BASELINE_ARTIFACT_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "expected_artifact_hash": expected_rework_hash,
                "current_artifact_hash": current_rework_hash,
                "next_tool": "abandon_generation",
                "directive": (
                    "The candidate changed after the repair baseline receipt was "
                    "written and before Workers. Abandon this generation."
                ),
            })
        running_plan = (
            _checkpoint_plan_with_tasks(
                ckpt, tasks, replace_existing_tasks=replace_checkpoint_tasks
            )
            if ckpt else {"tasks": tasks}
        )
        running_plan = {**running_plan, "work_item": rework_plan_metadata}
        running_plan = _plan_with_accumulated_repair_scope(ckpt, running_plan, tasks, next_v)
        rework_projection_ckpt = _matching_checkpoint(next_v, source_v)
        if not rework_projection_ckpt:
            return _json_tool_result({
                "error": "REWORK_PROJECTION_CHECKPOINT_MISSING",
                "next_v": next_v,
                "source_v": source_v,
            })
        rework_checkpoint_written = write_pipeline_checkpoint(
            next_v,
            source_v,
            "rework_running",
            master_plan=running_plan,
            reviewer_feedback=reviewer_feedback,
            worker_failure_count=ckpt.get("worker_failure_count", 0) if ckpt else 0,
            precommit_rework_count=precommit_rework_count_for_write,
            official_rework_count=official_rework_count_for_write,
            repair_baseline_artifact_hash=expected_rework_hash,
            expected_checkpoint_revision=int(
                rework_projection_ckpt.get("checkpoint_revision") or 0
            ),
            expected_checkpoint_stage=str(
                rework_projection_ckpt.get("stage") or ""
            ),
            expected_workflow_run_id=str(
                rework_projection_ckpt.get("workflow_run_id") or ""
            ),
        )
        if not rework_checkpoint_written:
            return _json_tool_result({
                "error": "REWORK_RUNNING_CHECKPOINT_FAILED",
                "next_v": next_v,
                "source_v": source_v,
                "expected_artifact_hash": expected_rework_hash,
                "directive": (
                    "The repair baseline was frozen but the rework-running "
                    "transition could not be persisted. Do not execute Workers."
                ),
            })

        # Recheck immediately before the Worker batch.  This closes the gap in
        # which a self-modifying test or external process edits an otherwise
        # declared repair file after checkpoint publication.
        current_rework_hash = _complete_artifact_fingerprint(
            prepared_candidate_dir
        )
        if current_rework_hash != expected_rework_hash:
            return _json_tool_result({
                "error": "REPAIR_BASELINE_ARTIFACT_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "expected_artifact_hash": expected_rework_hash,
                "current_artifact_hash": current_rework_hash,
                "next_tool": "abandon_generation",
            })

    if frozen_rework_resume and ckpt.get("stage") in rework_stages:
        expected_retry_hash = _checkpoint_repair_baseline_fingerprint(ckpt)
        current_retry_hash = _complete_artifact_fingerprint(
            prepared_candidate_dir
        )
        if (
            not expected_retry_hash
            or not current_retry_hash
            or current_retry_hash != expected_retry_hash
        ):
            abandon_result = await _force_abandon_frozen_worker_generation(
                next_v,
                source_v,
                "frozen_rework_pre_worker_drift",
                actor_lock_owned=actor_lock_owned,
            )
            return _json_tool_result({
                "error": "REPAIR_BASELINE_ARTIFACT_DRIFT",
                "next_v": next_v,
                "source_v": source_v,
                "expected_artifact_hash": expected_retry_hash,
                "current_artifact_hash": current_retry_hash,
                "next_tool": "abandon_generation",
                **abandon_result,
                "directive": (
                    "The infrastructure retry candidate no longer matches its "
                    "frozen repair baseline. Abandon without consuming the lease."
                ),
            })

    task_digest = _worker_execution_task_digest(
        tasks,
        reviewer_feedback,
        worker_template,
    )
    if durable_worker_resume:
        durable_input_digest = _worker_execution_task_digest(
            durable_worker_envelope.get("tasks") or [],
            str(durable_worker_envelope.get("reviewer_feedback") or ""),
            worker_template,
        )
        if task_digest != durable_input_digest:
            abandon_result = await _force_abandon_frozen_worker_generation(
                next_v,
                source_v,
                "durable_worker_frozen_input_drift",
                actor_lock_owned=actor_lock_owned,
            )
            worker_workflow.abandon("durable_worker_frozen_input_drift")
            return _json_tool_result({
                "error": "DURABLE_WORKER_FROZEN_INPUT_DRIFT",
                "success": False,
                "next_v": next_v,
                "source_v": source_v,
                **abandon_result,
            })

    if durable_worker_status == "idle":
        from worker_workflow import build_worker_envelope

        projection_ckpt = _matching_checkpoint(next_v, source_v)
        if not projection_ckpt:
            return _json_tool_result({
                "error": "DURABLE_WORKER_CHECKPOINT_MISSING_BEFORE_PREPARE",
                "next_v": next_v,
                "source_v": source_v,
            })
        prepared_artifact_hash = _complete_artifact_fingerprint(
            prepared_candidate_dir
        )
        prepared_snapshot_hash = worker_workflow.artifacts.capture(
            prepared_candidate_dir
        )
        if prepared_artifact_hash != prepared_snapshot_hash:
            return _json_tool_result({
                "error": "DURABLE_WORKER_PREPARED_SNAPSHOT_MISMATCH",
                "next_v": next_v,
                "source_v": source_v,
                "prepared_artifact_hash": prepared_artifact_hash,
                "prepared_snapshot_hash": prepared_snapshot_hash,
                "next_tool": "abandon_generation",
            })
        active_work_item = rework_plan_metadata or (
            (_checkpoint_master_plan(ckpt).get("work_item") or {})
            if isinstance(_checkpoint_master_plan(ckpt).get("work_item"), dict)
            else {}
        )
        worker_kind = str(active_work_item.get("kind") or "initial_worker")
        projection_plan = _checkpoint_plan_with_tasks(
            projection_ckpt,
            tasks,
            replace_existing_tasks=replace_checkpoint_tasks,
        )
        if active_work_item:
            projection_plan = {
                **projection_plan,
                "work_item": active_work_item,
            }
        if reviewer_feedback:
            projection_plan = _plan_with_accumulated_repair_scope(
                projection_ckpt,
                projection_plan,
                tasks,
                next_v,
            )
        projection_preimage_artifact_hash = str(
            active_work_item.get("projection_preimage_artifact_hash")
            or prepared_artifact_hash
        )
        projection_preimage_snapshot_hash = (
            str(
                active_work_item.get("projection_preimage_snapshot_hash")
                or ""
            )
            or prepared_snapshot_hash
        )
        try:
            worker_workflow.artifacts.path_for(
                projection_preimage_snapshot_hash
            )
        except Exception as exc:
            return _json_tool_result({
                "error": "DURABLE_WORKER_PROJECTION_PREIMAGE_UNAVAILABLE",
                "success": False,
                "action": "operator_reconcile",
                "next_v": next_v,
                "source_v": source_v,
                "projection_preimage_artifact_hash": (
                    projection_preimage_artifact_hash
                ),
                "message": f"{type(exc).__name__}: {str(exc)[:300]}",
            })
        checkpoint_contract = {
            "workflow_run_id": str(
                projection_ckpt.get("workflow_run_id")
                or projection_ckpt.get("run_id")
                or worker_workflow.run_id
                or ""
            ),
            "checkpoint_revision": int(
                projection_ckpt.get("checkpoint_revision") or 0
            ),
            "checkpoint_stage": str(projection_ckpt.get("stage") or ""),
        }
        execution_policy = {
            "force_sequential": bool(force_sequential_rework),
            "quality_skipper": quality_skipper_config is not None,
            "expected_architecture_policy": (
                deepcopy(
                    quality_skipper_config.get(
                        "expected_architecture_policy"
                    )
                )
                if isinstance(quality_skipper_config, dict)
                else None
            ),
            **(
                {"executor": "system_policy_bootstrap_v1"}
                if _system_bootstrap_executor
                else {}
            ),
        }
        envelope = build_worker_envelope(
            checkpoint=projection_ckpt,
            kind=worker_kind,
            source_stage=str(projection_ckpt.get("stage") or ""),
            prepared_artifact_hash=prepared_artifact_hash,
            prepared_snapshot_hash=prepared_snapshot_hash,
            source_artifact_hash=(
                prepared_artifact_hash
                if _system_bootstrap_executor
                else _complete_artifact_fingerprint(
                    get_bot_dir(source_v)
                )
            ),
            tasks=tasks,
            reviewer_feedback=reviewer_feedback,
            worker_template_hash=hashlib.sha256(
                worker_template.encode("utf-8")
            ).hexdigest(),
            work_item=active_work_item,
            backend_contract=_expected_worker_backend_contract(
                projection_ckpt,
                {"execution_policy": execution_policy},
            ),
            precommit_rework_count=(
                int(precommit_rework_count_for_write)
                if precommit_rework_count_for_write is not None
                else int(projection_ckpt.get("precommit_rework_count") or 0)
            ),
            official_rework_count=(
                int(official_rework_count_for_write)
                if official_rework_count_for_write is not None
                else int(projection_ckpt.get("official_rework_count") or 0)
            ),
            projection_plan=projection_plan,
            audit_context=deepcopy(projection_ckpt.get("audit_context") or {}),
            execution_policy=execution_policy,
            checkpoint_contract=checkpoint_contract,
            worker_failure_count=int(
                projection_ckpt.get("worker_failure_count") or 0
            ),
            projection_preimage_artifact_hash=(
                projection_preimage_artifact_hash
            ),
            projection_preimage_snapshot_hash=(
                projection_preimage_snapshot_hash
            ),
        )
        durable_worker_state = worker_workflow.prepare(
            envelope,
            max_attempts=1 if _system_bootstrap_executor else 3,
        )
        durable_worker_envelope = durable_worker_state["envelope"]
        durable_worker_status = durable_worker_state["status"]
        if rework_preparation_dir is not None:
            worker_workflow.artifacts.discard_workspace(
                rework_preparation_dir
            )
        if not _system_bootstrap_executor:
            try:
                from llm_availability_store import active_llm_pause

                _active_pause = active_llm_pause()
            except Exception as exc:
                return _json_tool_result({
                    "error": "LLM_AVAILABILITY_STATE_INVALID",
                    "success": False,
                    "failure_class": "control_plane",
                    "action": "operator_reconcile",
                    "next_v": next_v,
                    "source_v": source_v,
                    "worker_status": durable_worker_status,
                    "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "directive": (
                        "Worker preparation is durable, but the provider pause "
                        "record is invalid. No effect was claimed."
                    ),
                })
            if _active_pause is not None:
                return _json_tool_result({
                    "error": "LLM_AVAILABILITY_BLOCKED",
                    "success": False,
                    "failure_class": "availability",
                    "action": "wait_for_llm_availability",
                    "next_v": next_v,
                    "source_v": source_v,
                    "worker_status": durable_worker_status,
                    "attempt": int(
                        durable_worker_state.get("attempt") or 0
                    ),
                    "max_attempts": int(
                        durable_worker_state.get("max_attempts") or 0
                    ),
                    "effect_id": durable_worker_state.get("effect_id"),
                    "availability": _active_pause,
                    "directive": (
                        "Worker input was frozen, but the provider pause is "
                        "active. No effect was claimed and no attempt was consumed."
                    ),
                })
        if actor_lock_owned:
            return _DeferredWorkerActivity(
                workflow=worker_workflow,
                envelope=durable_worker_envelope,
                next_dir=next_dir,
                worker_template=worker_template,
            )
        return await _run_durable_worker_effect(
            worker_workflow,
            durable_worker_envelope,
            next_dir,
            worker_template,
        )

    return _json_tool_result({
        "error": "DURABLE_WORKER_COMMAND_DISPATCH_INVARIANT",
        "workflow_status": durable_worker_status,
        "next_v": next_v,
        "source_v": source_v,
    })


@tool("execute_workers", "Execute worker tasks to modify bot code. Each task has worker_id, role, target_files, worker_prompt.", {"tasks": list, "next_v": int, "source_v": int, "reviewer_feedback": str})
async def execute_workers(args):
    """Serialize deterministic preparation, then run the leased LLM outside it.

    Only idle/completed histories can perform one-time preparation or open a
    new cycle.  They enter the generation actor before replaying again.  The
    resulting Worker activity is returned as an internal dispatch token so the
    expensive model call never holds the actor lock and a central abandon can
    fence it immediately.
    """
    next_v = args.get("next_v") or args.get("version")
    source_v = args.get("source_v")
    if next_v is None or source_v is None:
        next_v, source_v = _resolve_version_args(args)
    checkpoint = (
        _matching_checkpoint(next_v, source_v)
        if next_v is not None and source_v is not None
        else None
    )
    if not isinstance(checkpoint, dict):
        return await _execute_workers_command(args)

    try:
        from worker_workflow import WorkerWorkflow
        from workflow_kernel import WorkflowBusy

        workflow = WorkerWorkflow.for_checkpoint(checkpoint)
        try:
            with workflow.store.command_lock(workflow.run_id):
                result = await _execute_workers_command(
                    args,
                    actor_lock_owned=True,
                )
        except WorkflowBusy:
            return _json_tool_result({
                "error": "WORKER_COMMAND_BUSY",
                "failure_class": "infrastructure",
                "action": "retry_same_tool",
                "next_v": next_v,
                "source_v": source_v,
                "directive": (
                    "Another process is publishing the deterministic Worker "
                    "preparation for this generation. Retry without editing the "
                    "candidate or rebuilding the prompt."
                ),
            })
        if isinstance(result, _DeferredWorkerActivity):
            return await _run_durable_worker_effect(
                result.workflow,
                result.envelope,
                result.next_dir,
                result.worker_template,
            )
        return result
    except WorkflowBusy:
        return _json_tool_result({
            "error": "WORKER_COMMAND_BUSY",
            "failure_class": "infrastructure",
            "action": "retry_same_tool",
            "next_v": next_v,
            "source_v": source_v,
        })
