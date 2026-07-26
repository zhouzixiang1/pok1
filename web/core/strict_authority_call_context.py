"""Generation/call-context builders and gate prompt rendering for strict_authority_workflow.

Extracted as a cohesive business cluster; strict_authority_workflow.py retains
thin delegate shells so external ``from strict_authority_workflow import <name>``
and ``monkeypatch.setattr(strict_authority_workflow, "<name>", ...)`` keep resolving.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

import strict_authority_workflow as _sa  # noqa: E402  (circular by design)


def generation_binding(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Return immutable generation identity; stage/revision stay call-local."""

    if not isinstance(checkpoint, dict):
        raise _sa.StrictAuthorityError("strict_authority_checkpoint_missing")
    audit = checkpoint.get("audit_context") or {}
    protocol = audit.get("protocol_bootstrap") or {}
    prepared = audit.get("prepared_artifact_contract") or {}
    workflow_run_id = str(checkpoint.get("workflow_run_id") or "").strip()
    source_v = checkpoint.get("source_v")
    next_v = checkpoint.get("next_v")
    if not workflow_run_id:
        raise _sa.StrictAuthorityError("strict_authority_workflow_run_id_missing")
    if not _sa._plain_int(source_v) or not _sa._plain_int(next_v):
        raise _sa.StrictAuthorityError("strict_authority_version_identity_invalid")
    subject = {
        "schema_version": 1,
        "workflow_run_id": workflow_run_id,
        "source_v": int(source_v),
        "next_v": int(next_v),
        "protocol_bootstrap_receipt_digest": protocol.get("receipt_digest"),
        "prepared_artifact_contract_digest": prepared.get("contract_digest"),
        "prepared_artifact_hash": prepared.get("prepared_artifact_hash"),
    }
    for field in (
        "protocol_bootstrap_receipt_digest",
        "prepared_artifact_contract_digest",
        "prepared_artifact_hash",
    ):
        if not _sa._valid_digest(subject[field]):
            raise _sa.StrictAuthorityError(f"strict_authority_{field}_invalid")
    return subject


def proposal_call_context(
    *,
    context_digest: str,
    source_code_digest: str,
    direction: str,
    allowed_primaries: Iterable[str] | None = None,
    evidence_mode: str | None = None,
) -> dict[str, Any]:
    context = {
        "phase": "proposal",
        "direction": str(direction),
        "planning_context_digest": str(context_digest),
        "source_code_digest": str(source_code_digest),
    }
    if allowed_primaries is not None:
        if isinstance(allowed_primaries, (str, bytes)):
            raise _sa.StrictAuthorityError(
                "strict_authority_proposal_allowed_primaries_invalid"
            )
        values = tuple(sorted({
            str(value).strip()
            for value in allowed_primaries
            if str(value).strip()
        }))
        if not values or any(
            re.fullmatch(r"[a-z][a-z0-9_]{1,79}", value) is None
            for value in values
        ):
            raise _sa.StrictAuthorityError(
                "strict_authority_proposal_allowed_primaries_invalid"
            )
        context["allowed_primaries"] = list(values)
    # Omit this key for the historical fresh-v143 contract so already
    # published receipts retain their exact context digest. A singleton
    # successor uses the same prepared-child source graph but a different
    # strategy/measurement projection, so its mode must be explicit.
    if evidence_mode is not None:
        if evidence_mode != "singleton_parent_no_strength":
            raise _sa.StrictAuthorityError(
                "strict_authority_proposal_evidence_mode_invalid"
            )
        context["evidence_mode"] = evidence_mode
    return context


def _architecture_proposal_primaries(
    architecture_policy: dict[str, Any] | None,
) -> tuple[str, ...] | None:
    """Reconstruct the frozen Scout-primary set from plan policy bytes."""

    if not isinstance(architecture_policy, dict):
        return None
    try:
        from output_schema import MASTER_PROPOSAL_FALSIFIER_PRIMARY

        checks = list(architecture_policy.get("plan_required_floor_checks") or ())
        focus = architecture_policy.get("selected_focus")
        if isinstance(focus, dict):
            checks.extend(focus.get("required_checks") or ())
        check_set = {str(check).strip() for check in checks if str(check).strip()}
        values = tuple(
            primary
            for test_name, primary in MASTER_PROPOSAL_FALSIFIER_PRIMARY.items()
            if test_name in check_set
        )
    except Exception as exc:
        raise _sa.StrictAuthorityError(
            "strict_authority_proposal_allowed_primaries_unavailable:"
            f"{type(exc).__name__}"
        ) from exc
    return values or None


def ballot_call_context(
    *,
    context_digest: str,
    source_code_digest: str,
    critic_id: str,
    proposal_ids: Iterable[str],
    critic_criteria: dict[str, Any],
) -> dict[str, Any]:
    return {
        "phase": "ballot",
        "critic_id": str(critic_id),
        "planning_context_digest": str(context_digest),
        "source_code_digest": str(source_code_digest),
        "proposal_ids": sorted(map(str, proposal_ids)),
        "critic_criteria_digest": _sa.content_digest(_sa._json_value(critic_criteria)),
    }


def final_master_call_context(
    proposal_packet: dict[str, Any],
    architecture_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "phase": "master_final",
        "planning_context_digest": proposal_packet.get("context_digest"),
        "source_code_digest": proposal_packet.get("source_code_digest"),
        "proposal_packet_digest": hashlib.sha256(
            json.dumps(
                proposal_packet,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        # These are system inputs available before dispatch.  Keeping their full
        # canonical values lets crash recovery rerun the exact post-provider
        # projection instead of trusting a caller-supplied final plan.
        "proposal_packet": _sa._json_value(proposal_packet),
        "architecture_policy": _sa._json_value(architecture_policy or {}),
    }


def expected_master_contexts(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    packet = (plan or {}).get("proposal_ensemble") or {}
    if not isinstance(packet, dict):
        raise _sa.StrictAuthorityError("strict_authority_proposal_packet_missing")
    context_digest = str(packet.get("context_digest") or "")
    source_code_digest = str(packet.get("source_code_digest") or "")
    proposals = packet.get("ordered_proposals") or []
    proposal_ids = [
        str(item.get("proposal_id") or "")
        for item in proposals
        if isinstance(item, dict)
    ]
    if len(proposal_ids) != 3 or len(set(proposal_ids)) != 3:
        raise _sa.StrictAuthorityError(
            "strict_authority_proposal_packet_id_set_invalid"
        )
    criteria = packet.get("critic_criteria") or {}
    result = {
        f"proposal:{direction}": _sa.proposal_call_context(
            context_digest=context_digest,
            source_code_digest=source_code_digest,
            direction=direction,
            allowed_primaries=_sa._architecture_proposal_primaries(
                (plan or {}).get("architecture_policy")
            ),
            evidence_mode=(
                "singleton_parent_no_strength"
                if packet.get("evidence_mode")
                == "singleton_parent_no_strength"
                else None
            ),
        )
        for direction in ("mechanism", "counterfactual", "compute_memory")
    }
    result.update({
        f"ballot:{critic_id}": _sa.ballot_call_context(
            context_digest=context_digest,
            source_code_digest=source_code_digest,
            critic_id=critic_id,
            proposal_ids=proposal_ids,
            critic_criteria=criteria,
        )
        for critic_id in ("falsification", "scope")
    })
    result["master:final"] = _sa.final_master_call_context(
        packet,
        (plan or {}).get("architecture_policy") or {},
    )
    return result


def expected_master_role_results(plan: dict[str, Any]) -> dict[str, Any]:
    """Derive the five proposal/ballot payloads retained in a final plan.

    ``master:final`` is deliberately not reverse-projected here.  The plan
    compiler externalizes oversized Worker prompts, so deleting compiler fields
    would be a lossy and forgeable inverse.  ``validate_master_final_projection``
    instead replays the compiler from the full accepted role payload stored in
    the authority journal and compares the resulting checkpoint plan exactly.
    """

    if not isinstance(plan, dict):
        raise _sa.StrictAuthorityError("strict_authority_master_plan_missing")
    packet = plan.get("proposal_ensemble")
    if not isinstance(packet, dict):
        raise _sa.StrictAuthorityError("strict_authority_proposal_packet_missing")

    proposals = packet.get("ordered_proposals")
    if not isinstance(proposals, list) or len(proposals) != 3:
        raise _sa.StrictAuthorityError(
            "strict_authority_master_role_proposals_invalid"
        )
    proposal_results: dict[str, Any] = {}
    for proposal in proposals:
        if not isinstance(proposal, dict):
            raise _sa.StrictAuthorityError(
                "strict_authority_master_role_proposals_invalid"
            )
        direction = str(proposal.get("direction") or "")
        slot = f"proposal:{direction}"
        if slot not in _sa.MASTER_SLOTS[:3] or slot in proposal_results:
            raise _sa.StrictAuthorityError(
                "strict_authority_master_role_proposal_set_invalid"
            )
        proposal_results[slot] = _sa._json_value(proposal)
    if set(proposal_results) != set(_sa.MASTER_SLOTS[:3]):
        raise _sa.StrictAuthorityError(
            "strict_authority_master_role_proposal_set_invalid"
        )

    reviews = packet.get("critic_reviews")
    if not isinstance(reviews, list) or len(reviews) != 2:
        raise _sa.StrictAuthorityError(
            "strict_authority_master_role_ballots_invalid"
        )
    ballot_results: dict[str, Any] = {}
    for review in reviews:
        if not isinstance(review, dict):
            raise _sa.StrictAuthorityError(
                "strict_authority_master_role_ballots_invalid"
            )
        critic_id = str(review.get("critic_id") or "")
        slot = f"ballot:{critic_id}"
        if slot not in _sa.MASTER_SLOTS[3:5] or slot in ballot_results:
            raise _sa.StrictAuthorityError(
                "strict_authority_master_role_ballot_set_invalid"
            )
        if "invocation_evidence" not in review:
            raise _sa.StrictAuthorityError(
                "strict_authority_master_role_ballot_evidence_missing"
            )
        ballot_results[slot] = _sa._json_value({
            key: value
            for key, value in review.items()
            if key not in {"critic_id", "invocation_evidence"}
        })
    if set(ballot_results) != set(_sa.MASTER_SLOTS[3:5]):
        raise _sa.StrictAuthorityError(
            "strict_authority_master_role_ballot_set_invalid"
        )

    return {
        **proposal_results,
        **ballot_results,
    }


def expected_master_invocation_evidence(
    plan: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Extract the five packet evidence receipts by their authority slots."""

    packet = (plan or {}).get("proposal_ensemble")
    if not isinstance(packet, dict):
        raise _sa.StrictAuthorityError("strict_authority_proposal_packet_missing")
    proposals = packet.get("ordered_proposals")
    proposal_invocations = packet.get("proposal_invocations")
    reviews = packet.get("critic_reviews")
    if (
        not isinstance(proposals, list)
        or len(proposals) != 3
        or not isinstance(proposal_invocations, dict)
        or not isinstance(reviews, list)
        or len(reviews) != 2
    ):
        raise _sa.StrictAuthorityError(
            "strict_authority_master_invocation_evidence_invalid"
        )
    result: dict[str, dict[str, Any]] = {}
    for proposal in proposals:
        if not isinstance(proposal, dict):
            raise _sa.StrictAuthorityError(
                "strict_authority_master_invocation_evidence_invalid"
            )
        slot = f"proposal:{proposal.get('direction')}"
        evidence = proposal_invocations.get(proposal.get("proposal_id"))
        if slot not in _sa.MASTER_SLOTS[:3] or not isinstance(evidence, dict):
            raise _sa.StrictAuthorityError(
                "strict_authority_master_invocation_evidence_invalid"
            )
        result[slot] = _sa._json_value(evidence)
    for review in reviews:
        if not isinstance(review, dict):
            raise _sa.StrictAuthorityError(
                "strict_authority_master_invocation_evidence_invalid"
            )
        slot = f"ballot:{review.get('critic_id')}"
        evidence = review.get("invocation_evidence")
        if slot not in _sa.MASTER_SLOTS[3:5] or not isinstance(evidence, dict):
            raise _sa.StrictAuthorityError(
                "strict_authority_master_invocation_evidence_invalid"
            )
        result[slot] = _sa._json_value(evidence)
    if set(result) != set(_sa.MASTER_SLOTS[:5]):
        raise _sa.StrictAuthorityError(
            "strict_authority_master_invocation_evidence_invalid"
        )
    return result


_GATE_RENDER_INVOCATION_SENTINEL = "0" * 32
_STRICT_CRITIC_NO_STRENGTH_CONTRACT = (
    "# SYSTEM-SUPPLIED FIRST-STRICT NO-STRENGTH CONTRACT\n"
    "The national_tcp_policy_v1 pool is empty. No rating, H2H, replay, Arena, "
    "official-EXE result, retired bot, or historical experience is admissible "
    "for this one-time v143 Critic call. Evaluate only the prepared policy, the "
    "content-bound Master plan, the national ABI, and the completed quality and "
    "Reviewer receipts."
)


def _normalized_reviewer_focus_areas(
    checkpoint: dict[str, Any],
) -> list[str]:
    gates = checkpoint.get("gate_results") or {}
    audit = checkpoint.get("audit_context") or {}
    raw = audit.get("worker_cot_focus_areas") or (
        (gates.get("workers") or {}).get("audit_focus_areas") or []
    )
    if not isinstance(raw, list) or any(
        not isinstance(item, str) for item in raw
    ):
        raise _sa.StrictAuthorityError(
            "strict_authority_review_focus_areas_invalid"
        )
    # Preserve the exact established prompt semantics: order, duplicates,
    # whitespace, and empty strings all remain provider-visible.  JSON
    # normalization below supplies stable serialization without rewriting the
    # evidence itself.
    return list(raw)


def _gate_renderer_components(gate_name: str):
    if gate_name in {"review", "review:retry"}:
        from tool_gates import _render_reviewer_provider_prompt

        return "LEAD CODE REVIEWER", _render_reviewer_provider_prompt
    if gate_name == "critic":
        from agent_review import _render_critic_provider_prompt

        return "STRATEGY CRITIC", _render_critic_provider_prompt
    raise _sa.StrictAuthorityError("strict_authority_gate_name_invalid")


def _render_registered_gate_prompt(
    gate_name: str,
    renderer_inputs: dict[str, Any],
):
    role, producer = _sa._gate_renderer_components(gate_name)
    from llm_query import render_llm_prompt

    registered_inputs = dict(renderer_inputs)
    if gate_name in {"review", "review:retry"}:
        # The authority slot is system-owned renderer input.  Keeping it out of
        # the checkpoint-derived semantic input object preserves the reviewed
        # legacy migration shape, while the renderer receipt and prompt still
        # bind the exact first/retry purpose.
        registered_inputs["authority_slot"] = gate_name
    return render_llm_prompt(
        role,
        producer=producer,
        renderer_inputs=registered_inputs,
    )


def _gate_renderer_semantic_contract(
    gate_name: str,
    semantic_inputs: dict[str, Any],
) -> dict[str, Any]:
    """Seal actual producer/template semantics with a normalized invocation."""

    role, _producer = _sa._gate_renderer_components(gate_name)
    normalized_inputs = _sa._json_value(semantic_inputs)
    rendered = _sa._render_registered_gate_prompt(
        gate_name,
        {
            **deepcopy(normalized_inputs),
            "invocation_id": _GATE_RENDER_INVOCATION_SENTINEL,
        },
    )
    receipt = rendered.dispatch_receipt
    renderer = receipt.renderer
    evidence = receipt.evidence
    static_identity = {
        "producer_file": renderer.producer_file,
        "producer_name": renderer.producer_name,
        "producer_file_sha256": renderer.producer_file_sha256,
        "producer_function_sha256": renderer.producer_function_sha256,
        "template_digests": _sa._json_value(renderer.template_digests),
    }
    subject = {
        "schema_version": 1,
        "role": role,
        "invocation_normalization": "fixed-32-byte-sentinel-v1",
        "semantic_inputs": normalized_inputs,
        "semantic_inputs_digest": _sa.content_digest(normalized_inputs),
        "renderer_static_identity": static_identity,
        "renderer_static_identity_digest": _sa.content_digest(static_identity),
        "sentinel_rendered_prompt_sha256": renderer.rendered_prompt_sha256,
        "sentinel_rendered_prompt_chars": renderer.rendered_prompt_chars,
        "sentinel_evidence_kind": evidence.provenance_kind,
        "sentinel_evidence_provenance_sha256": evidence.provenance_sha256,
        "sentinel_renderer_receipt_digest": renderer.receipt_digest,
        "sentinel_evidence_receipt_digest": evidence.receipt_digest,
        "sentinel_dispatch_receipt_digest": receipt.receipt_digest,
    }
    return {**subject, "contract_digest": _sa.content_digest(subject)}


def _gate_semantic_inputs(
    checkpoint: dict[str, Any],
    *,
    gate_name: str,
    candidate_dir: Path,
    candidate_artifact_hash: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    source_v = checkpoint.get("source_v")
    next_v = checkpoint.get("next_v")
    master_plan = checkpoint.get("master_plan")
    if (
        not _sa._plain_int(source_v)
        or not _sa._plain_int(next_v)
        or not isinstance(master_plan, dict)
    ):
        raise _sa.StrictAuthorityError(
            "strict_authority_gate_checkpoint_semantics_invalid"
        )
    normalized_plan = _sa._json_value(master_plan)
    if gate_name in {"review", "review:retry"}:
        from tool_gates import _review_semantic_contract

        try:
            review_semantics = _review_semantic_contract(
                normalized_plan,
                _sa._json_value((checkpoint.get("gate_results") or {}).get("quality") or {}),
            )
        except (TypeError, ValueError) as exc:
            raise _sa.StrictAuthorityError(
                "strict_authority_review_semantic_contract_invalid"
            ) from exc
        return ({
            "master_plan": normalized_plan,
            "source_v": int(source_v),
            "next_v": int(next_v),
            "strict_bootstrap": True,
            "focus_areas": _sa._normalized_reviewer_focus_areas(checkpoint),
            "review_semantic_contract": review_semantics,
        }, None)

    from agent_review import _critic_code_evidence
    from tool_helpers import _previous_critic_result

    code_evidence = _critic_code_evidence(
        int(next_v),
        int(source_v),
        protocol_bootstrap_prepared_only=True,
        target_dir=candidate_dir,
    )
    if code_evidence.get("target_artifact_hash") != candidate_artifact_hash:
        raise _sa.StrictAuthorityError(
            "strict_authority_critic_code_artifact_mismatch"
        )
    h2h_contract = _STRICT_CRITIC_NO_STRENGTH_CONTRACT
    snapshot_dir = None
    previous = _previous_critic_result(checkpoint)
    if previous is not None and not isinstance(previous, dict):
        raise _sa.StrictAuthorityError(
            "strict_authority_critic_previous_result_invalid"
        )
    semantic_inputs = {
        "source_v": int(source_v),
        "next_v": int(next_v),
        "master_plan": json.dumps(
            normalized_plan,
            indent=2,
            ensure_ascii=False,
        ),
        # _critic_code_evidence enforces the existing 400k policy/diff bound;
        # the snapshot producer above retains the existing 12k JSON bound.
        "code_evidence": _sa._json_value(code_evidence),
        "h2h_snapshot_contract": str(h2h_contract),
        "previous_critic": _sa._json_value(previous),
    }
    evidence_scope = {
        "schema_version": 1,
        "allowed_evidence_snapshot_dir": (
            str(snapshot_dir) if snapshot_dir is not None else None
        ),
        "h2h_snapshot_contract_digest": hashlib.sha256(
            str(h2h_contract).encode("utf-8")
        ).hexdigest(),
    }
    evidence_scope["scope_digest"] = _sa.content_digest(evidence_scope)
    return semantic_inputs, evidence_scope


def gate_call_context(
    checkpoint: dict[str, Any],
    *,
    gate_name: str,
    candidate_dir: str | Path,
) -> dict[str, Any]:
    if gate_name not in _sa.GATE_SLOTS:
        raise _sa.StrictAuthorityError("strict_authority_gate_name_invalid")
    from bot_artifact import hash_path

    candidate = Path(candidate_dir)
    candidate_artifact_hash = hash_path(candidate)
    gates = checkpoint.get("gate_results") or {}
    audit = checkpoint.get("audit_context") or {}
    master_receipt = audit.get("system_strict_bootstrap") or {}
    subject = {
        "phase": gate_name,
        "candidate_artifact_hash": candidate_artifact_hash,
        "quality_gate_digest": _sa.content_digest(gates.get("quality") or {}),
        "master_receipt_digest": master_receipt.get("receipt_digest"),
        "master_plan_digest": master_receipt.get("plan_digest"),
    }
    if gate_name == "critic":
        subject["review_receipt_digest"] = (
            ((gates.get("review") or {}).get("system_verifier_receipt") or {}).get(
                "receipt_digest"
            )
        )
    semantic_inputs, evidence_scope = _sa._gate_semantic_inputs(
        checkpoint,
        gate_name=gate_name,
        candidate_dir=candidate,
        candidate_artifact_hash=candidate_artifact_hash,
    )
    subject["renderer_semantics"] = _sa._gate_renderer_semantic_contract(
        gate_name,
        semantic_inputs,
    )
    if gate_name == "critic":
        subject["provider_evidence_scope"] = evidence_scope
    return subject


def render_gate_provider_prompt(call: dict[str, Any]):
    """Render a gate only from its durable descriptor-owned semantics."""

    slot = str((call or {}).get("slot") or "")
    if slot not in _sa.GATE_SLOTS:
        raise _sa.StrictAuthorityError(
            "strict_authority_gate_render_call_invalid"
        )
    context = (call or {}).get("context_binding")
    if (
        not isinstance(context, dict)
        or _sa.content_digest(context) != call.get("context_binding_digest")
    ):
        raise _sa.StrictAuthorityError(
            "strict_authority_gate_render_context_invalid"
        )
    contract = context.get("renderer_semantics")
    if not isinstance(contract, dict):
        raise _sa.StrictAuthorityError(
            "strict_authority_gate_renderer_semantics_missing"
        )
    semantic_inputs = contract.get("semantic_inputs")
    if not isinstance(semantic_inputs, dict) or (
        _sa._gate_renderer_semantic_contract(slot, semantic_inputs) != contract
    ):
        raise _sa.StrictAuthorityError(
            f"strict_authority_gate_renderer_semantics_drift:{slot}"
        )

    invocation_id = str(call.get("invocation_id") or "")
    if len(invocation_id) != 32:
        raise _sa.StrictAuthorityError(
            "strict_authority_gate_render_invocation_invalid"
        )
    actual_inputs = {
        **deepcopy(semantic_inputs),
        "invocation_id": invocation_id,
    }
    rendered = _sa._render_registered_gate_prompt(
        slot,
        actual_inputs,
    )
    try:
        replay_inputs = json.loads(rendered.renderer_inputs_json)
        renderer = rendered.dispatch_receipt.renderer
        actual_static = {
            "producer_file": renderer.producer_file,
            "producer_name": renderer.producer_name,
            "producer_file_sha256": renderer.producer_file_sha256,
            "producer_function_sha256": renderer.producer_function_sha256,
            "template_digests": _sa._json_value(renderer.template_digests),
        }
    except _sa.StrictAuthorityError:
        raise
    except Exception as exc:
        raise _sa.StrictAuthorityError(
            "strict_authority_gate_render_receipt_invalid"
        ) from exc
    if (
        _sa._json_value(replay_inputs) != _sa._json_value({
            **actual_inputs,
            **(
                {"authority_slot": slot}
                if slot in {"review", "review:retry"}
                else {}
            ),
        })
        or actual_static != contract.get("renderer_static_identity")
    ):
        raise _sa.StrictAuthorityError(
            f"strict_authority_gate_render_receipt_drift:{slot}"
        )
    return rendered


def gate_provider_evidence_snapshot_dir(
    call: dict[str, Any],
) -> Path | None:
    """Return the Critic read scope frozen beside its renderer semantics."""

    if str((call or {}).get("slot") or "") != "critic":
        raise _sa.StrictAuthorityError(
            "strict_authority_gate_evidence_scope_call_invalid"
        )
    context = (call or {}).get("context_binding")
    if (
        not isinstance(context, dict)
        or _sa.content_digest(context) != call.get("context_binding_digest")
    ):
        raise _sa.StrictAuthorityError(
            "strict_authority_gate_evidence_scope_context_invalid"
        )
    scope = context.get("provider_evidence_scope")
    expected_fields = {
        "schema_version",
        "allowed_evidence_snapshot_dir",
        "h2h_snapshot_contract_digest",
        "scope_digest",
    }
    scope_subject = {
        key: value for key, value in (scope or {}).items()
        if key != "scope_digest"
    }
    semantic_inputs = (
        (context.get("renderer_semantics") or {}).get("semantic_inputs") or {}
    )
    h2h_contract = semantic_inputs.get("h2h_snapshot_contract")
    if (
        not isinstance(scope, dict)
        or set(scope) != expected_fields
        or scope.get("schema_version") != 1
        or scope.get("scope_digest") != _sa.content_digest(scope_subject)
        or not isinstance(h2h_contract, str)
        or scope.get("h2h_snapshot_contract_digest")
        != hashlib.sha256(h2h_contract.encode("utf-8")).hexdigest()
    ):
        raise _sa.StrictAuthorityError(
            "strict_authority_gate_evidence_scope_invalid"
        )
    value = scope.get("allowed_evidence_snapshot_dir")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise _sa.StrictAuthorityError(
            "strict_authority_gate_evidence_scope_path_invalid"
        )
    path = Path(value)
    if not path.is_absolute():
        raise _sa.StrictAuthorityError(
            "strict_authority_gate_evidence_scope_path_invalid"
        )
    return path
