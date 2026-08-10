"""Governed literature probe subsystem.

Extracted from tool_planning.py as a single business responsibility:
deep-research a specific H2H weakness via web search (Exa), synthesize ONE
codable strategy proposal, governed by research_governance
(cooldown/blacklist/translation gate). Distinct dispatch route from
direction_audit and run_master.

All public symbols are re-exported by tool_planning.py for backward
compatibility (thin delegate shells preserve every
``from tool_planning import <name>`` site and every monkeypatch on
``tool_planning.<name>``).

Cross-reference policy
----------------------
* Calls to another MOVED function -> bare global (intra-companion).
* Calls to STAYING ``tool_planning`` module-level helpers / constants that
  tests monkeypatch on ``tool_planning.<name>`` -> routed through ``_tp.``
  so the monkeypatch is honored. This covers at minimum:
  ``_get_ui``, ``log_system_event``, ``write_pipeline_checkpoint``,
  ``_matching_checkpoint``, ``_json_tool_result``, ``LITERATURE_PROBE_TIMEOUT``.
* Things provided directly by ``evolution_infra`` and NOT monkeypatched on
  ``tool_planning`` are imported directly:
  ``read_pipeline_checkpoint``, ``get_logs_dir``, ``RESULTS_DIR``.
"""

import hashlib
import json
import math
import os
import re
import stat
import time
from copy import deepcopy
from pathlib import Path

# Lazy reference back to tool_planning so monkeypatches on
# ``tool_planning.<name>`` are respected at call time. Imported lazily inside
# helpers to avoid an import cycle at module load (tool_planning imports this
# companion at top level).
import tool_planning as _tp  # noqa: E402

from evolution_infra import (  # noqa: E402
    RESULTS_DIR,
    get_logs_dir,
    read_pipeline_checkpoint,
)
from llm_availability import LLMAvailabilityBlocked  # noqa: E402
from pipeline_state import route_policy  # noqa: E402


# ──────────────────────────────────────────────
# Module-level schema / field constants (literature-probe specific)
# ──────────────────────────────────────────────

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


# ──────────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────────

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


# ──────────────────────────────────────────────
# Cache path / fingerprints / digests
# ──────────────────────────────────────────────

def _literature_probe_cache_path(next_v: int | str) -> Path:
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
    """Digest the semantic checkpoint preimage across the receipt CAS write.

    Strips transient bookkeeping that changes between Master retries
    (audit_attempt bumps, audit_context.master_analysis evidence) so a normal
    Master-retry does not invalidate a valid probe receipt. The genuine
    research-requirement content is independently bound by the four
    master_context_digest / direction_audit_digest / requirement_context[_digest]
    fields checked separately in _literature_probe_payload_errors.
    """

    projection = deepcopy(checkpoint)
    projection.pop("literature_probe", None)
    for field in ("timestamp", "last_update_ts", "last_stage_change_ts"):
        projection.pop(field, None)
    # Strip Master-retry transient bookkeeping (audit C/D literature-probe
    # diagnosis 2026-08-10): a normal Master attempt that gets 2/3 scouts
    # bumps audit_attempt and writes audit_context.master_analysis, which
    # changed this digest and falsely invalidated a valid probe receipt,
    # causing every generation to abandon at the Master stage.
    projection.pop("audit_attempt", None)
    _audit_ctx = projection.get("audit_context")
    if isinstance(_audit_ctx, dict):
        _audit_ctx = {
            k: v for k, v in _audit_ctx.items()
            if k != "master_analysis"
        }
        projection["audit_context"] = _audit_ctx
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

    # The literature_probe LLM role contract pins producer_file to
    # ``web/core/tool_planning.py`` (llm_query._producer_binding uses
    # inspect.getsourcefile(producer) to verify). Route through the
    # tool_planning-side wrapper — its source file is tool_planning.py and it
    # delegates back to this module's _render_literature_provider_prompt for
    # the real rendering, so the contract is satisfied without behaviour
    # change.
    return render_llm_prompt(
        f"LITERATURE_PROBE (v{int(next_v)})",
        producer=_tp._render_literature_provider_prompt,
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


# ──────────────────────────────────────────────
# Bounded single-link JSON helpers (cache I/O)
# ──────────────────────────────────────────────

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


# ──────────────────────────────────────────────
# Cache + checkpoint read/write
# ──────────────────────────────────────────────

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
        # write_pipeline_checkpoint is monkeypatched on tool_planning in tests;
        # route through _tp so the patch is honored.
        return bool(_tp.write_pipeline_checkpoint(
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
# Tool entrypoint
# ──────────────────────────────────────────────

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

    # _matching_checkpoint, _json_tool_result and _get_ui live on tool_planning
    # as re-exports from tool_helpers; route through _tp so any monkeypatch on
    # tool_planning.<name> is honored (tests do this for _get_ui).
    probe_checkpoint = _tp._matching_checkpoint(next_v, source_v)
    from pipeline_state import (
        literature_probe_receipt_binding,
        literature_probe_required,
    )

    if not isinstance(probe_checkpoint, dict):
        return _tp._json_tool_result({
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
        return _tp._json_tool_result({
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
        return _tp._json_tool_result({
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
        return _tp._json_tool_result({
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
        _tp.log_system_event(
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
            _tp.log_system_event(
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
        return _tp._json_tool_result(checkpoint_probe)
    if probe_checkpoint.get("literature_probe") is not None:
        return _tp._json_tool_result({
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
            _tp.log_system_event(
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
            return _tp._json_tool_result(returned)
        return _tp._json_tool_result(_literature_probe_stale_result(next_v, source_v))

    # ── A6 governance gate: cooldown / blacklist / kill-switch ──
    try:
        from research_governance import should_trigger_web_retrieval
        if not should_trigger_web_retrieval(next_v):
            try:
                _tp.log_system_event("research_governance.skipped", "info",
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
                return _tp._json_tool_result(
                    _literature_probe_stale_result(next_v, source_v)
                )
            return _tp._json_tool_result(payload)
    except Exception as e:
        return {"content": [{"type": "text", "text": json.dumps({"error": f"governance gate failed: {e}"})}]}

    ui = _tp._get_ui()
    rendered_prompt = None
    output = None
    try:
        from llm_query import run_claude_query
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
    # LITERATURE_PROBE_TIMEOUT lives on tool_planning and is monkeypatched in tests.
    timeout_s = _tp.LITERATURE_PROBE_TIMEOUT
    try:
        _tp.log_system_event("pipeline.literature_probe_start", "info",
                         f"literature_probe v{next_v}: research query starting",
                         {"next_v": next_v, "source_v": source_v,
                          "timeout_s": timeout_s,
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
            timeout=timeout_s,
        )
    except LLMAvailabilityBlocked:
        # A provider stop cannot satisfy the scheduler-owned literature receipt.
        # Leave the checkpoint/revision untouched for exact resume.
        raise
    except _asyncio.TimeoutError:
        elapsed = round(time.time() - _t0, 1)
        try:
            _tp.log_system_event("pipeline.literature_probe_timeout", "warn",
                             f"literature_probe v{next_v}: timed out after {timeout_s}s; continuing without web hypothesis",
                             {"next_v": next_v, "source_v": source_v,
                              "timeout_s": timeout_s,
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
            "timeout_s": timeout_s,
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
            return _tp._json_tool_result(_literature_probe_stale_result(next_v, source_v))
        return _tp._json_tool_result(payload)
    except Exception as e:
        try:
            _tp.log_system_event("pipeline.literature_probe_failed", "warn",
                             f"literature_probe v{next_v}: research query failed: {str(e)[:180]}",
                             {"next_v": next_v, "source_v": source_v,
                              "elapsed_sec": round(time.time() - _t0, 1),
                              "exception_type": type(e).__name__,
                              "error": str(e)[:1000],
                              "log_file": str(probe_log)})
        except Exception:
            pass
        if rendered_prompt is None:
            return _tp._json_tool_result({
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
            return _tp._json_tool_result(_literature_probe_stale_result(next_v, source_v))
        return _tp._json_tool_result(payload)

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
        return _tp._json_tool_result(_literature_probe_stale_result(next_v, source_v))

    try:
        _tp.log_system_event("pipeline.literature_probe", "info",
                         f"literature_probe v{next_v}: candidate_id={candidate_id} gated_out={_payload['gated_out']}",
                         {"next_v": next_v, "candidate_id": candidate_id,
                          "target_fn": (proposal or {}).get("target_fn", "")})
    except Exception:
        pass

    # Text returned to the orchestrator is the exact bound receipt.
    return _tp._json_tool_result(_payload)
