"""Fenced provider and schema receipts for the first strict generation.

The first strict generation is a one-time trust bootstrap.  A checkpoint flag
or an ``*_io.txt`` file cannot prove that its Master/Reviewer/Critic was really
executed.  This module therefore records each provider dispatch as a
``WorkflowStore`` effect and records deterministic schema acceptance as a
separate domain event.

The authority stream deliberately uses a run id distinct from the Worker
stream while sharing ``RESULTS_DIR/workflow/events.sqlite3``::

    {workflow_run_id}:strict-authority-v1

Only :func:`llm_query.run_claude_query` completes provider effects.  Role code
may append an acceptance event only after the completed provider effect has
been re-read and its prompt/output binding has been verified.

This is durable execution provenance, not a cryptographic same-UID boundary.
Python code running as the operator can import public functions and rewrite the
SQLite database.  The parent/sub-agent Bash guards remove those routes from LLM
tools; filesystem ownership and operator process isolation remain the security
boundary against arbitrary local code.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
import re
import stat
from pathlib import Path
import time
import uuid
import threading
from typing import Any, Iterable

from claude_agent_sdk import ResultMessage
from workflow_kernel import WorkflowConflict, WorkflowStore, content_digest


DEFINITION_VERSION = 1
RUN_SUFFIX = "strict-authority-v1"
EFFECT_KIND = "first-strict-llm-provider-call-v1"
ACCEPTED_EVENT = "StrictRoleAccepted"
REJECTED_EVENT = "StrictRoleRejected"
INVOCATION_EVIDENCE_BOUND_EVENT = "StrictInvocationEvidenceBound"
RECEIPT_KIND = "first-strict-llm-authority-receipt-v1"
INVOCATION_EVIDENCE_BINDING_KIND = (
    "first-strict-invocation-evidence-binding-v1"
)
MAX_SCHEMA_ATTEMPTS_PER_SLOT = 2

MASTER_SLOTS = (
    "proposal:mechanism",
    "proposal:counterfactual",
    "proposal:compute_memory",
    "ballot:falsification",
    "ballot:scope",
    "master:final",
)
GATE_SLOTS = ("review", "critic")
ALL_SLOTS = MASTER_SLOTS + GATE_SLOTS
INVOCATION_EVIDENCE_SLOTS = MASTER_SLOTS[:5] + GATE_SLOTS

SLOT_CONTRACTS = {
    "proposal:mechanism": (
        "MASTER PROPOSAL mechanism",
        "master_proposal_scout:mechanism",
    ),
    "proposal:counterfactual": (
        "MASTER PROPOSAL counterfactual",
        "master_proposal_scout:counterfactual",
    ),
    "proposal:compute_memory": (
        "MASTER PROPOSAL compute_memory",
        "master_proposal_scout:compute_memory",
    ),
    "ballot:falsification": (
        "MASTER PROPOSAL CRITIC falsification",
        "master_proposal_critic:falsification",
    ),
    "ballot:scope": (
        "MASTER PROPOSAL CRITIC scope",
        "master_proposal_critic:scope",
    ),
    "master:final": ("MASTER", "system_strict_bootstrap_master:final"),
    "review": (
        "LEAD CODE REVIEWER",
        "system_strict_bootstrap_gate:review",
    ),
    "critic": (
        "STRATEGY CRITIC",
        "system_strict_bootstrap_gate:critic",
    ),
}
SLOT_STAGES = {
    **{slot: "direction_audited" for slot in MASTER_SLOTS},
    "review": "quality_passed",
    "critic": "reviewed",
}
SLOT_PARSE_CONTRACTS = {
    **{slot: "master-proposal-v2" for slot in MASTER_SLOTS[:3]},
    **{slot: "master-proposal-ballot-v1" for slot in MASTER_SLOTS[3:5]},
    "master:final": "master-plan-schema-v1",
    "review": "reviewer-output-schema-v1",
    "critic": "critic-output-schema-v1",
}
SLOT_TOOLS = {
    **{slot: ["Read"] for slot in MASTER_SLOTS[:3]},
    **{slot: [] for slot in MASTER_SLOTS[3:5]},
    "master:final": ["Read"],
    "review": ["Read"],
    "critic": ["Read"],
}

_HEX = frozenset("0123456789abcdef")
_OBSERVED_PROVIDER_RESULTS: dict[int, tuple[str, str]] = {}
_OBSERVED_PROVIDER_RESULTS_LOCK = threading.Lock()


class StrictAuthorityError(RuntimeError):
    """A strict call or its durable authority chain is invalid."""

    def __init__(self, errors: str | Iterable[str]):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = tuple(dict.fromkeys(str(item) for item in errors if item))
        super().__init__("; ".join(self.errors) or "strict authority invalid")


def _observe_provider_result(
    result: Any,
    *,
    invocation_id: str,
    effect_id: str,
) -> None:
    """Register one SDK ResultMessage observed by llm_query._process_stream."""

    if not isinstance(result, ResultMessage):
        raise StrictAuthorityError("strict_authority_non_sdk_result")
    with _OBSERVED_PROVIDER_RESULTS_LOCK:
        _OBSERVED_PROVIDER_RESULTS[id(result)] = (
            str(invocation_id),
            str(effect_id),
        )


def _provider_results_were_observed(
    results: Iterable[Any],
    *,
    invocation_id: str,
    effect_id: str,
) -> bool:
    result_ids = [id(item) for item in results]
    with _OBSERVED_PROVIDER_RESULTS_LOCK:
        return bool(result_ids) and all(
            isinstance(item, ResultMessage)
            and _OBSERVED_PROVIDER_RESULTS.get(item_id)
            == (str(invocation_id), str(effect_id))
            for item, item_id in zip(results, result_ids)
        )


def _consume_observed_provider_results(results: Iterable[Any]) -> None:
    with _OBSERVED_PROVIDER_RESULTS_LOCK:
        for item in results:
            _OBSERVED_PROVIDER_RESULTS.pop(id(item), None)


def _store() -> WorkflowStore:
    from evolution_infra import RESULTS_DIR

    return WorkflowStore(Path(RESULTS_DIR) / "workflow" / "events.sqlite3")


def _plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in _HEX for char in value)
    )


def _json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def authority_run_id(workflow_run_id: str) -> str:
    workflow_run_id = str(workflow_run_id or "").strip()
    if not workflow_run_id:
        raise StrictAuthorityError("strict_authority_workflow_run_id_missing")
    return f"{workflow_run_id}:{RUN_SUFFIX}"


def abandon_authority(
    checkpoint: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    """Fence the strict child journal when its generation is abandoned."""

    run_id = authority_run_id(str((checkpoint or {}).get("workflow_run_id") or ""))
    store = _store()
    payload = {
        "reason": str(reason)[:1000],
        "workflow_run_id": str(checkpoint.get("workflow_run_id") or ""),
    }
    instance = store.instance(run_id)
    if not instance:
        # new_call is intentionally side-effect free, so an abandon can race a
        # descriptor that has not reached dispatch yet.  Publish an abandoned
        # tombstone first: even a crash before the terminal event is appended
        # then prevents ensure_instance/request_effect from resurrecting this
        # child journal as running.  This uses the SQLite instance transaction,
        # not a nested actor flock; callers already own the Worker actor lock.
        store.ensure_instance(
            run_id,
            definition_version=DEFINITION_VERSION,
            status="abandoned",
        )
        instance = store.instance(run_id)

    terminal_events = [
        event
        for event in store.events(run_id)
        if event.event_type == "StrictAuthorityAbandoned"
    ]
    if len(terminal_events) > 1:
        raise WorkflowConflict(
            f"multiple strict authority abandon events: {run_id}"
        )
    if instance.get("status") != "abandoned" or not terminal_events:
        try:
            store.terminal_transition(
                run_id,
                event_type="StrictAuthorityAbandoned",
                payload=payload,
                causation_id=(
                    f"strict-authority-abandoned:{run_id}:"
                    f"{content_digest(payload)}"
                ),
                expected_version=int(instance["stream_version"]),
                status="abandoned",
            )
        except WorkflowConflict:
            current = store.instance(run_id)
            if current.get("status") != "abandoned":
                raise
    current = store.instance(run_id)
    return {
        "run_id": run_id,
        "present": True,
        "abandoned": current.get("status") == "abandoned",
        "fence_epoch": int(current.get("fence_epoch") or 0),
        "stream_version": int(current.get("stream_version") or 0),
    }


def generation_binding(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Return immutable generation identity; stage/revision stay call-local."""

    if not isinstance(checkpoint, dict):
        raise StrictAuthorityError("strict_authority_checkpoint_missing")
    audit = checkpoint.get("audit_context") or {}
    protocol = audit.get("protocol_bootstrap") or {}
    prepared = audit.get("prepared_artifact_contract") or {}
    workflow_run_id = str(checkpoint.get("workflow_run_id") or "").strip()
    source_v = checkpoint.get("source_v")
    next_v = checkpoint.get("next_v")
    if not workflow_run_id:
        raise StrictAuthorityError("strict_authority_workflow_run_id_missing")
    if not _plain_int(source_v) or not _plain_int(next_v):
        raise StrictAuthorityError("strict_authority_version_identity_invalid")
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
        if not _valid_digest(subject[field]):
            raise StrictAuthorityError(f"strict_authority_{field}_invalid")
    return subject


def proposal_call_context(
    *,
    context_digest: str,
    source_code_digest: str,
    direction: str,
) -> dict[str, Any]:
    return {
        "phase": "proposal",
        "direction": str(direction),
        "planning_context_digest": str(context_digest),
        "source_code_digest": str(source_code_digest),
    }


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
        "critic_criteria_digest": content_digest(_json_value(critic_criteria)),
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
        "proposal_packet": _json_value(proposal_packet),
        "architecture_policy": _json_value(architecture_policy or {}),
    }


def expected_master_contexts(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    packet = (plan or {}).get("proposal_ensemble") or {}
    if not isinstance(packet, dict):
        raise StrictAuthorityError("strict_authority_proposal_packet_missing")
    context_digest = str(packet.get("context_digest") or "")
    source_code_digest = str(packet.get("source_code_digest") or "")
    proposals = packet.get("ordered_proposals") or []
    proposal_ids = [
        str(item.get("proposal_id") or "")
        for item in proposals
        if isinstance(item, dict)
    ]
    if len(proposal_ids) != 3 or len(set(proposal_ids)) != 3:
        raise StrictAuthorityError(
            "strict_authority_proposal_packet_id_set_invalid"
        )
    criteria = packet.get("critic_criteria") or {}
    result = {
        f"proposal:{direction}": proposal_call_context(
            context_digest=context_digest,
            source_code_digest=source_code_digest,
            direction=direction,
        )
        for direction in ("mechanism", "counterfactual", "compute_memory")
    }
    result.update({
        f"ballot:{critic_id}": ballot_call_context(
            context_digest=context_digest,
            source_code_digest=source_code_digest,
            critic_id=critic_id,
            proposal_ids=proposal_ids,
            critic_criteria=criteria,
        )
        for critic_id in ("falsification", "scope")
    })
    result["master:final"] = final_master_call_context(
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
        raise StrictAuthorityError("strict_authority_master_plan_missing")
    packet = plan.get("proposal_ensemble")
    if not isinstance(packet, dict):
        raise StrictAuthorityError("strict_authority_proposal_packet_missing")

    proposals = packet.get("ordered_proposals")
    if not isinstance(proposals, list) or len(proposals) != 3:
        raise StrictAuthorityError(
            "strict_authority_master_role_proposals_invalid"
        )
    proposal_results: dict[str, Any] = {}
    for proposal in proposals:
        if not isinstance(proposal, dict):
            raise StrictAuthorityError(
                "strict_authority_master_role_proposals_invalid"
            )
        direction = str(proposal.get("direction") or "")
        slot = f"proposal:{direction}"
        if slot not in MASTER_SLOTS[:3] or slot in proposal_results:
            raise StrictAuthorityError(
                "strict_authority_master_role_proposal_set_invalid"
            )
        proposal_results[slot] = _json_value(proposal)
    if set(proposal_results) != set(MASTER_SLOTS[:3]):
        raise StrictAuthorityError(
            "strict_authority_master_role_proposal_set_invalid"
        )

    reviews = packet.get("critic_reviews")
    if not isinstance(reviews, list) or len(reviews) != 2:
        raise StrictAuthorityError(
            "strict_authority_master_role_ballots_invalid"
        )
    ballot_results: dict[str, Any] = {}
    for review in reviews:
        if not isinstance(review, dict):
            raise StrictAuthorityError(
                "strict_authority_master_role_ballots_invalid"
            )
        critic_id = str(review.get("critic_id") or "")
        slot = f"ballot:{critic_id}"
        if slot not in MASTER_SLOTS[3:5] or slot in ballot_results:
            raise StrictAuthorityError(
                "strict_authority_master_role_ballot_set_invalid"
            )
        if "invocation_evidence" not in review:
            raise StrictAuthorityError(
                "strict_authority_master_role_ballot_evidence_missing"
            )
        ballot_results[slot] = _json_value({
            key: value
            for key, value in review.items()
            if key not in {"critic_id", "invocation_evidence"}
        })
    if set(ballot_results) != set(MASTER_SLOTS[3:5]):
        raise StrictAuthorityError(
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
        raise StrictAuthorityError("strict_authority_proposal_packet_missing")
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
        raise StrictAuthorityError(
            "strict_authority_master_invocation_evidence_invalid"
        )
    result: dict[str, dict[str, Any]] = {}
    for proposal in proposals:
        if not isinstance(proposal, dict):
            raise StrictAuthorityError(
                "strict_authority_master_invocation_evidence_invalid"
            )
        slot = f"proposal:{proposal.get('direction')}"
        evidence = proposal_invocations.get(proposal.get("proposal_id"))
        if slot not in MASTER_SLOTS[:3] or not isinstance(evidence, dict):
            raise StrictAuthorityError(
                "strict_authority_master_invocation_evidence_invalid"
            )
        result[slot] = _json_value(evidence)
    for review in reviews:
        if not isinstance(review, dict):
            raise StrictAuthorityError(
                "strict_authority_master_invocation_evidence_invalid"
            )
        slot = f"ballot:{review.get('critic_id')}"
        evidence = review.get("invocation_evidence")
        if slot not in MASTER_SLOTS[3:5] or not isinstance(evidence, dict):
            raise StrictAuthorityError(
                "strict_authority_master_invocation_evidence_invalid"
            )
        result[slot] = _json_value(evidence)
    if set(result) != set(MASTER_SLOTS[:5]):
        raise StrictAuthorityError(
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
        raise StrictAuthorityError(
            "strict_authority_review_focus_areas_invalid"
        )
    # Preserve the exact established prompt semantics: order, duplicates,
    # whitespace, and empty strings all remain provider-visible.  JSON
    # normalization below supplies stable serialization without rewriting the
    # evidence itself.
    return list(raw)


def _gate_renderer_components(gate_name: str):
    if gate_name == "review":
        from tool_gates import _render_reviewer_provider_prompt

        return "LEAD CODE REVIEWER", _render_reviewer_provider_prompt
    if gate_name == "critic":
        from agent_review import _render_critic_provider_prompt

        return "STRATEGY CRITIC", _render_critic_provider_prompt
    raise StrictAuthorityError("strict_authority_gate_name_invalid")


def _render_registered_gate_prompt(
    gate_name: str,
    renderer_inputs: dict[str, Any],
):
    role, producer = _gate_renderer_components(gate_name)
    from llm_query import render_llm_prompt

    return render_llm_prompt(
        role,
        producer=producer,
        renderer_inputs=renderer_inputs,
    )


def _gate_renderer_semantic_contract(
    gate_name: str,
    semantic_inputs: dict[str, Any],
) -> dict[str, Any]:
    """Seal actual producer/template semantics with a normalized invocation."""

    role, _producer = _gate_renderer_components(gate_name)
    normalized_inputs = _json_value(semantic_inputs)
    rendered = _render_registered_gate_prompt(
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
        "template_digests": _json_value(renderer.template_digests),
    }
    subject = {
        "schema_version": 1,
        "role": role,
        "invocation_normalization": "fixed-32-byte-sentinel-v1",
        "semantic_inputs": normalized_inputs,
        "semantic_inputs_digest": content_digest(normalized_inputs),
        "renderer_static_identity": static_identity,
        "renderer_static_identity_digest": content_digest(static_identity),
        "sentinel_rendered_prompt_sha256": renderer.rendered_prompt_sha256,
        "sentinel_rendered_prompt_chars": renderer.rendered_prompt_chars,
        "sentinel_evidence_kind": evidence.provenance_kind,
        "sentinel_evidence_provenance_sha256": evidence.provenance_sha256,
        "sentinel_renderer_receipt_digest": renderer.receipt_digest,
        "sentinel_evidence_receipt_digest": evidence.receipt_digest,
        "sentinel_dispatch_receipt_digest": receipt.receipt_digest,
    }
    return {**subject, "contract_digest": content_digest(subject)}


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
        not _plain_int(source_v)
        or not _plain_int(next_v)
        or not isinstance(master_plan, dict)
    ):
        raise StrictAuthorityError(
            "strict_authority_gate_checkpoint_semantics_invalid"
        )
    normalized_plan = _json_value(master_plan)
    if gate_name == "review":
        return ({
            "master_plan": normalized_plan,
            "source_v": int(source_v),
            "next_v": int(next_v),
            "strict_bootstrap": True,
            "focus_areas": _normalized_reviewer_focus_areas(checkpoint),
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
        raise StrictAuthorityError(
            "strict_authority_critic_code_artifact_mismatch"
        )
    h2h_contract = _STRICT_CRITIC_NO_STRENGTH_CONTRACT
    snapshot_dir = None
    previous = _previous_critic_result(checkpoint)
    if previous is not None and not isinstance(previous, dict):
        raise StrictAuthorityError(
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
        "code_evidence": _json_value(code_evidence),
        "h2h_snapshot_contract": str(h2h_contract),
        "previous_critic": _json_value(previous),
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
    evidence_scope["scope_digest"] = content_digest(evidence_scope)
    return semantic_inputs, evidence_scope


def gate_call_context(
    checkpoint: dict[str, Any],
    *,
    gate_name: str,
    candidate_dir: str | Path,
) -> dict[str, Any]:
    if gate_name not in GATE_SLOTS:
        raise StrictAuthorityError("strict_authority_gate_name_invalid")
    from bot_artifact import hash_path

    candidate = Path(candidate_dir)
    candidate_artifact_hash = hash_path(candidate)
    gates = checkpoint.get("gate_results") or {}
    audit = checkpoint.get("audit_context") or {}
    master_receipt = audit.get("system_strict_bootstrap") or {}
    subject = {
        "phase": gate_name,
        "candidate_artifact_hash": candidate_artifact_hash,
        "quality_gate_digest": content_digest(gates.get("quality") or {}),
        "master_receipt_digest": master_receipt.get("receipt_digest"),
        "master_plan_digest": master_receipt.get("plan_digest"),
    }
    if gate_name == "critic":
        subject["review_receipt_digest"] = (
            ((gates.get("review") or {}).get("system_verifier_receipt") or {}).get(
                "receipt_digest"
            )
        )
    semantic_inputs, evidence_scope = _gate_semantic_inputs(
        checkpoint,
        gate_name=gate_name,
        candidate_dir=candidate,
        candidate_artifact_hash=candidate_artifact_hash,
    )
    subject["renderer_semantics"] = _gate_renderer_semantic_contract(
        gate_name,
        semantic_inputs,
    )
    if gate_name == "critic":
        subject["provider_evidence_scope"] = evidence_scope
    return subject


def render_gate_provider_prompt(call: dict[str, Any]):
    """Render a gate only from its durable descriptor-owned semantics."""

    slot = str((call or {}).get("slot") or "")
    if slot not in GATE_SLOTS:
        raise StrictAuthorityError(
            "strict_authority_gate_render_call_invalid"
        )
    context = (call or {}).get("context_binding")
    if (
        not isinstance(context, dict)
        or content_digest(context) != call.get("context_binding_digest")
    ):
        raise StrictAuthorityError(
            "strict_authority_gate_render_context_invalid"
        )
    contract = context.get("renderer_semantics")
    if not isinstance(contract, dict):
        raise StrictAuthorityError(
            "strict_authority_gate_renderer_semantics_missing"
        )
    semantic_inputs = contract.get("semantic_inputs")
    if not isinstance(semantic_inputs, dict) or (
        _gate_renderer_semantic_contract(slot, semantic_inputs) != contract
    ):
        raise StrictAuthorityError(
            f"strict_authority_gate_renderer_semantics_drift:{slot}"
        )

    invocation_id = str(call.get("invocation_id") or "")
    if len(invocation_id) != 32:
        raise StrictAuthorityError(
            "strict_authority_gate_render_invocation_invalid"
        )
    actual_inputs = {
        **deepcopy(semantic_inputs),
        "invocation_id": invocation_id,
    }
    rendered = _render_registered_gate_prompt(
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
            "template_digests": _json_value(renderer.template_digests),
        }
    except StrictAuthorityError:
        raise
    except Exception as exc:
        raise StrictAuthorityError(
            "strict_authority_gate_render_receipt_invalid"
        ) from exc
    if (
        _json_value(replay_inputs) != _json_value(actual_inputs)
        or actual_static != contract.get("renderer_static_identity")
    ):
        raise StrictAuthorityError(
            f"strict_authority_gate_render_receipt_drift:{slot}"
        )
    return rendered


def gate_provider_evidence_snapshot_dir(
    call: dict[str, Any],
) -> Path | None:
    """Return the Critic read scope frozen beside its renderer semantics."""

    if str((call or {}).get("slot") or "") != "critic":
        raise StrictAuthorityError(
            "strict_authority_gate_evidence_scope_call_invalid"
        )
    context = (call or {}).get("context_binding")
    if (
        not isinstance(context, dict)
        or content_digest(context) != call.get("context_binding_digest")
    ):
        raise StrictAuthorityError(
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
        or scope.get("scope_digest") != content_digest(scope_subject)
        or not isinstance(h2h_contract, str)
        or scope.get("h2h_snapshot_contract_digest")
        != hashlib.sha256(h2h_contract.encode("utf-8")).hexdigest()
    ):
        raise StrictAuthorityError(
            "strict_authority_gate_evidence_scope_invalid"
        )
    value = scope.get("allowed_evidence_snapshot_dir")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise StrictAuthorityError(
            "strict_authority_gate_evidence_scope_path_invalid"
        )
    path = Path(value)
    if not path.is_absolute():
        raise StrictAuthorityError(
            "strict_authority_gate_evidence_scope_path_invalid"
        )
    return path


def _recover_accepted_call(descriptor: dict[str, Any]) -> dict[str, Any] | None:
    """Recover one accepted slot after a crash before checkpoint projection."""

    store = _store()
    if not store.instance(descriptor["run_id"]):
        return None
    events = [
        event
        for event in store.events(descriptor["run_id"])
        if event.event_type == ACCEPTED_EVENT
        and event.payload.get("slot") == descriptor["slot"]
    ]
    if not events:
        return None
    if len(events) != 1:
        raise StrictAuthorityError(
            f"strict_authority_{descriptor['slot']}_accepted_count:{len(events)}"
        )
    event = events[0]
    payload = event.payload
    receipt_subject = {
        key: value for key, value in payload.items() if key != "receipt_digest"
    }
    if payload.get("receipt_digest") != content_digest(receipt_subject):
        raise StrictAuthorityError("strict_authority_recovery_receipt_invalid")
    if payload.get("role_result_digest") != content_digest(
        _json_value(payload.get("role_result"))
    ):
        raise StrictAuthorityError("strict_authority_recovery_role_result_invalid")
    expected = {
        "run_id": descriptor["run_id"],
        "slot": descriptor["slot"],
        "role": descriptor["role"],
        "purpose": descriptor["purpose"],
        "generation_binding_digest": descriptor["generation_binding_digest"],
        "checkpoint_stage": descriptor["checkpoint_stage"],
        "checkpoint_revision": descriptor["checkpoint_revision"],
        "context_binding_digest": descriptor["context_binding_digest"],
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise StrictAuthorityError(
                f"strict_authority_recovery_{field}_mismatch:{descriptor['slot']}"
            )
    effect = store.effect(str(payload.get("effect_id") or ""))
    provider = effect.get("result_payload") or {}
    input_payload = effect.get("input_payload") or {}
    if effect.get("status") != "completed" or not isinstance(
        provider.get("raw_output"), str
    ):
        raise StrictAuthorityError("strict_authority_recovery_provider_missing")
    provider_subject = {
        key: value for key, value in provider.items() if key != "result_digest"
    }
    if provider.get("result_digest") != content_digest(provider_subject):
        raise StrictAuthorityError("strict_authority_recovery_provider_digest_invalid")
    for field in (
        "slot",
        "role",
        "purpose",
        "invocation_id",
        "generation_binding_digest",
        "context_binding_digest",
        "checkpoint_stage",
        "checkpoint_revision",
        "prompt_digest",
        "actual_role",
        "model",
        "tools",
    ):
        if input_payload.get(field) != payload.get(field) or provider.get(
            field
        ) != payload.get(field):
            raise StrictAuthorityError(
                f"strict_authority_recovery_effect_{field}_mismatch"
            )
    if hashlib.sha256(provider["raw_output"].encode("utf-8")).hexdigest() != provider.get(
        "raw_output_digest"
    ):
        raise StrictAuthorityError("strict_authority_recovery_raw_output_invalid")
    if (
        provider.get("role_projection_valid") is not True
        or provider.get("projected_role_result_digest")
        != payload.get("role_result_digest")
        or _json_value(provider.get("projected_role_result"))
        != _json_value(payload.get("role_result"))
    ):
        raise StrictAuthorityError(
            "strict_authority_recovery_role_projection_invalid"
        )
    receipt_ref = {
        "schema_version": 1,
        "kind": RECEIPT_KIND,
        "run_id": descriptor["run_id"],
        "slot": descriptor["slot"],
        "effect_id": payload["effect_id"],
        "invocation_id": payload["invocation_id"],
        "event_seq": int(event.seq),
        "event_payload_digest": event.payload_digest,
        "receipt_digest": payload["receipt_digest"],
        "role_result_digest": payload["role_result_digest"],
    }
    return {
        **descriptor,
        "invocation_id": payload["invocation_id"],
        "effect_id": payload["effect_id"],
        "lease_epoch": int(payload["lease_epoch"]),
        "prompt_digest": payload["prompt_digest"],
        "raw_output_digest": payload["raw_output_digest"],
        "provider_result_digest": payload["provider_result_digest"],
        "provider_event_id": payload["provider_event_id"],
        "provider_session_id": payload["provider_session_id"],
        "projected_role_result": deepcopy(provider["projected_role_result"]),
        "projected_role_result_digest": provider[
            "projected_role_result_digest"
        ],
        "provider_completed": True,
        "accepted_receipt": receipt_ref,
        "accepted_role_result_digest": payload["role_result_digest"],
        "accepted_role_result": deepcopy(payload.get("role_result")),
        "replay_provider": True,
        "replay_raw_output": provider["raw_output"],
        "replay_cost_usd": provider.get("provider_cost_usd"),
        "replay_usage": provider.get("provider_usage"),
        "replay_input_payload": input_payload,
        "actual_role": provider.get("actual_role"),
        "model": provider.get("model"),
        "tools": deepcopy(provider.get("tools")),
    }


def _recover_completed_unaccepted_call(
    descriptor: dict[str, Any],
) -> dict[str, Any] | None:
    """Replay one valid completed provider effect after a pre-accept crash.

    Schema-invalid projections carry a durable ``StrictRoleRejected`` event and
    are intentionally skipped so the caller's normal schema-retry path can issue
    a fresh invocation instead of replaying a poison result forever.
    """

    store = _store()
    if not store.instance(descriptor["run_id"]):
        return None
    events = store.events(descriptor["run_id"])
    accepted_ids = {
        str(event.payload.get("effect_id") or "")
        for event in events
        if event.event_type == ACCEPTED_EVENT
    }
    rejected_ids = {
        str(event.payload.get("effect_id") or "")
        for event in events
        if event.event_type == REJECTED_EVENT
    }
    observed_ids = [
        str(event.payload.get("effect_id") or "")
        for event in events
        if event.event_type == "StrictProviderResultObserved"
        and event.payload.get("slot") == descriptor["slot"]
    ]
    matches: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    stable_fields = (
        "slot",
        "role",
        "purpose",
        "generation_binding",
        "generation_binding_digest",
        "context_binding",
        "context_binding_digest",
        "checkpoint_stage",
        "checkpoint_revision",
    )
    for effect_id in dict.fromkeys(observed_ids):
        if not effect_id or effect_id in accepted_ids or effect_id in rejected_ids:
            continue
        effect = store.effect(effect_id)
        input_payload = effect.get("input_payload") or {}
        provider = effect.get("result_payload") or {}
        if (
            effect.get("status") != "completed"
            or effect.get("kind") != EFFECT_KIND
            or effect.get("run_id") != descriptor["run_id"]
            or provider.get("role_projection_valid") is not True
            or not isinstance(provider.get("raw_output"), str)
            or any(
                input_payload.get(field) != descriptor.get(field)
                for field in stable_fields
            )
        ):
            continue
        provider_subject = {
            key: value for key, value in provider.items() if key != "result_digest"
        }
        projected = provider.get("projected_role_result")
        if (
            provider.get("result_digest") != content_digest(provider_subject)
            or provider.get("projected_role_result_digest")
            != content_digest(_json_value(projected))
            or provider.get("raw_output_digest")
            != hashlib.sha256(provider["raw_output"].encode("utf-8")).hexdigest()
        ):
            raise StrictAuthorityError(
                "strict_authority_completed_recovery_payload_invalid"
            )
        matches.append((effect, input_payload, provider))
    if not matches:
        return None
    if len(matches) != 1:
        raise StrictAuthorityError(
            f"strict_authority_completed_unaccepted_count:{len(matches)}"
        )
    effect, input_payload, provider = matches[0]
    return {
        **descriptor,
        "invocation_id": input_payload["invocation_id"],
        "effect_id": effect["effect_id"],
        "lease_epoch": int(effect.get("lease_epoch") or 0),
        "prompt_digest": input_payload["prompt_digest"],
        "raw_output_digest": provider["raw_output_digest"],
        "provider_result_digest": provider["result_digest"],
        "provider_event_id": provider["provider_event_id"],
        "provider_session_id": provider["provider_session_id"],
        "provider_completed": True,
        "projected_role_result": deepcopy(provider["projected_role_result"]),
        "projected_role_result_digest": provider[
            "projected_role_result_digest"
        ],
        "replay_provider": True,
        "replay_raw_output": provider["raw_output"],
        "replay_cost_usd": provider.get("provider_cost_usd"),
        "replay_usage": provider.get("provider_usage"),
        "replay_input_payload": input_payload,
        "actual_role": provider.get("actual_role"),
        "model": provider.get("model"),
        "tools": deepcopy(provider.get("tools")),
    }


_STABLE_CALL_FIELDS = (
    "slot",
    "role",
    "purpose",
    "generation_binding",
    "generation_binding_digest",
    "context_binding",
    "context_binding_digest",
    "checkpoint_stage",
    "checkpoint_revision",
)


def _input_matches_descriptor(
    input_payload: dict[str, Any], descriptor: dict[str, Any]
) -> bool:
    return all(
        input_payload.get(field) == descriptor.get(field)
        for field in _STABLE_CALL_FIELDS
    )


def _provider_owner_pid(owner: Any) -> int | None:
    match = re.match(r"^llm_query:(\d+):", str(owner or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _provider_owner_is_alive(owner: Any) -> bool | None:
    """Return local owner liveness, or ``None`` for an unrecognised owner."""

    pid = _provider_owner_pid(owner)
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _reap_or_block_running_calls(descriptor: dict[str, Any]) -> None:
    """Fence dead/expired provider owners before issuing another slot call.

    A process crash can leave a strict effect in ``running`` until its lease
    timestamp.  Starting a second provider for the same immutable slot while
    the old owner is still alive permits two concurrent terminal results.  A
    dead or expired owner is therefore failed under its exact lease epoch;
    every live or unverifiable owner blocks the new dispatch fail-closed.
    """

    store = _store()
    if not store.instance(descriptor["run_id"]):
        return
    requested_ids = [
        str(event.payload.get("effect_id") or "")
        for event in store.events(descriptor["run_id"])
        if event.event_type == "EffectRequested"
    ]
    now = time.time()
    for effect_id in dict.fromkeys(requested_ids):
        if not effect_id:
            continue
        effect = store.effect(effect_id)
        if (
            effect.get("kind") != EFFECT_KIND
            or effect.get("run_id") != descriptor["run_id"]
            or effect.get("status") != "running"
            or not _input_matches_descriptor(
                effect.get("input_payload") or {}, descriptor
            )
        ):
            continue
        owner = effect.get("lease_owner")
        lease_until = effect.get("lease_until")
        expired = bool(
            lease_until is not None and float(lease_until) <= now
        )
        alive = _provider_owner_is_alive(owner)
        if not expired and alive is not False:
            raise StrictAuthorityError(
                f"strict_authority_provider_call_active:{descriptor['slot']}"
            )
        try:
            store.fail_effect(
                effect_id,
                lease_epoch=int(effect.get("lease_epoch") or 0),
                error=(
                    "strict provider lease expired"
                    if expired
                    else f"strict provider owner exited: {owner}"
                ),
                retryable=False,
                causation_id=(
                    f"strict-provider-owner-reaped:{effect_id}:"
                    f"{int(effect.get('lease_epoch') or 0)}"
                ),
            )
        except Exception:
            # Completion and reaping race through the same SQLite fence.  If
            # completion won, recovery below will verify/replay it.  Any other
            # state is not safe to ignore.
            refreshed = store.effect(effect_id)
            if refreshed.get("status") != "completed":
                raise StrictAuthorityError(
                    f"strict_authority_provider_reap_failed:{descriptor['slot']}"
                )


def _schema_rejections(descriptor: dict[str, Any]) -> list[dict[str, Any]]:
    """Return verified deterministic projection rejections for this slot."""

    store = _store()
    if not store.instance(descriptor["run_id"]):
        return []
    all_events = store.events(descriptor["run_id"])
    rows: list[dict[str, Any]] = []
    for event in all_events:
        if (
            event.event_type != REJECTED_EVENT
            or event.payload.get("slot") != descriptor["slot"]
        ):
            continue
        effect_id = str(event.payload.get("effect_id") or "")
        effect = store.effect(effect_id)
        provider = effect.get("result_payload") or {}
        input_payload = effect.get("input_payload") or {}
        provider_subject = {
            key: value
            for key, value in provider.items()
            if key != "result_digest"
        }
        errors = list(event.payload.get("projection_errors") or ())
        rejection_kind = str(
            event.payload.get("rejection_kind") or "schema_projection"
        )
        common_valid = bool(
            effect.get("status") == "completed"
            and effect.get("kind") == EFFECT_KIND
            and effect.get("run_id") == descriptor["run_id"]
            and _input_matches_descriptor(input_payload, descriptor)
            and event.payload.get("parse_contract")
            == SLOT_PARSE_CONTRACTS[descriptor["slot"]]
            and event.payload.get("raw_output_digest")
            == provider.get("raw_output_digest")
            and provider.get("result_digest") == content_digest(provider_subject)
        )
        if rejection_kind == "schema_projection":
            valid = bool(
                common_valid
                and provider.get("role_projection_valid") is False
                and list(provider.get("role_projection_errors") or ()) == errors
            )
        elif rejection_kind == "proposal_identity_collision":
            projected = provider.get("projected_role_result")
            proposal_id = (
                str(projected.get("proposal_id") or "")
                if isinstance(projected, dict)
                else ""
            )
            conflicting_slots = sorted({
                str(accepted.payload.get("slot") or "")
                for accepted in all_events
                if accepted.event_type == ACCEPTED_EVENT
                and accepted.payload.get("slot") in MASTER_SLOTS[:3]
                and accepted.payload.get("slot") != descriptor["slot"]
                and isinstance(accepted.payload.get("role_result"), dict)
                and str(
                    accepted.payload["role_result"].get("proposal_id") or ""
                ) == proposal_id
            })
            valid = bool(
                common_valid
                and provider.get("role_projection_valid") is True
                and proposal_id
                and event.payload.get("proposal_id") == proposal_id
                and event.payload.get("projected_role_result_digest")
                == provider.get("projected_role_result_digest")
                and event.payload.get("conflicting_slots") == conflicting_slots
                and conflicting_slots
                and errors == [
                    "strict_authority_proposal_identity_collision"
                ]
            )
        else:
            valid = False
        if not valid:
            raise StrictAuthorityError(
                f"strict_authority_schema_rejection_invalid:{descriptor['slot']}"
            )
        rows.append({
            "effect_id": effect_id,
            "invocation_id": str(event.payload.get("invocation_id") or ""),
            "event_seq": int(event.seq),
            "event_payload_digest": event.payload_digest,
            "rejection_kind": rejection_kind,
            "projection_errors": errors,
        })
    return rows


def schema_retry_prompt(call: dict[str, Any]) -> str:
    """Render the system-owned one-shot repair suffix after a durable rejection."""

    if not isinstance(call, dict) or not call.get("schema_retry_required"):
        return ""
    prior = call.get("prior_schema_rejection") or {}
    errors = ", ".join(map(str, prior.get("projection_errors") or ()))
    parse_contract = SLOT_PARSE_CONTRACTS.get(str(call.get("slot") or ""))
    if prior.get("rejection_kind") == "proposal_identity_collision":
        return (
            "\n\n# SYSTEM-OWNED DETERMINISTIC ENSEMBLE DISTINCTNESS REPAIR\n"
            "The preceding schema-valid provider result for this exact "
            "immutable role/context duplicated an already accepted proposal "
            "identity. This is the single permitted ensemble repair. Keep the "
            "assigned scout lens, source, evidence, writable scope, and gates "
            "unchanged. Produce a genuinely different poker mechanism and "
            "causal claim, demonstrated by substantive differences in its "
            "structural_change/counterfactual and reachable_chain or falsifier. "
            "Changing only direction, risks, formatting, thresholds, or wording "
            "is invalid. proposal_id is derived by the system: do not emit, "
            "invent, or manipulate it. Do not combine with or copy another "
            "proposal. Return only one complete object accepted by "
            f"parse_contract={parse_contract}."
        )
    return (
        "\n\n# SYSTEM-OWNED STRICT SCHEMA REPAIR\n"
        "The preceding provider result for this exact immutable role/context "
        "failed deterministic projection. This is the single permitted "
        "schema-only repair attempt. Keep the assigned lens and evidence "
        "unchanged; do not synthesize evidence, relax the schema, or copy a "
        "different proposal. Return only one complete object accepted by "
        f"parse_contract={parse_contract}."
        + (f" Prior deterministic errors: {errors}." if errors else "")
    )


def _authority_phase(slot: str) -> tuple[str, tuple[str, ...]]:
    if slot in MASTER_SLOTS:
        return "master", MASTER_SLOTS
    if slot in GATE_SLOTS:
        return slot, (slot,)
    raise StrictAuthorityError(f"strict_authority_slot_invalid:{slot}")


def _frozen_phase_checkpoint_revision(
    checkpoint: dict[str, Any],
    *,
    slot: str,
    binding: dict[str, Any],
    current_revision: int,
    expected_context_binding: dict[str, Any] | None = None,
) -> int:
    """Return the first durable revision for one authority phase.

    Checkpoint metadata can advance after a partial Master packet or a neutral
    infrastructure overlay.  The provider effects themselves remain one
    append-only authority phase: accepted slots must replay, missing slots must
    consume only their remaining schema budget, and later ballots/final output
    must use the same revision.  Deriving the anchor from verified effect input
    keeps recovery fail-closed without weakening receipt equality.
    """

    phase_name, phase_slots = _authority_phase(slot)
    run_id = authority_run_id(binding["workflow_run_id"])
    store = _store()
    try:
        instance = store.instance(run_id)
        if not instance:
            return int(current_revision)
        if instance.get("status") == "abandoned":
            raise StrictAuthorityError(
                f"strict_authority_phase_journal_abandoned:{phase_name}"
            )
        events = store.events(run_id)
    except StrictAuthorityError:
        raise
    except Exception as exc:
        raise StrictAuthorityError(
            f"strict_authority_phase_revision_unavailable:{phase_name}:"
            f"{type(exc).__name__}"
        ) from exc

    revisions: set[int] = set()
    context_digests_by_slot: dict[str, set[str]] = {}
    expected_binding_digest = content_digest(binding)
    for event in events:
        if event.event_type != "EffectRequested":
            continue
        if event.payload.get("kind") != EFFECT_KIND:
            continue
        effect_id = str(event.payload.get("effect_id") or "")
        effect = store.effect(effect_id)
        input_payload = effect.get("input_payload") or {}
        if (
            not effect_id
            or effect.get("effect_id") != effect_id
            or effect.get("run_id") != run_id
            or effect.get("kind") != EFFECT_KIND
            or not isinstance(input_payload, dict)
            or effect.get("input_digest") != content_digest(input_payload)
            or event.payload.get("input_digest") != effect.get("input_digest")
            or event.payload.get("max_attempts") != 1
        ):
            raise StrictAuthorityError(
                f"strict_authority_phase_effect_invalid:{phase_name}"
            )
        effect_slot = str(input_payload.get("slot") or "")
        if effect_slot not in ALL_SLOTS:
            raise StrictAuthorityError(
                f"strict_authority_phase_effect_slot_invalid:{phase_name}"
            )
        if effect_slot not in phase_slots:
            continue
        expected_role, expected_purpose = SLOT_CONTRACTS[effect_slot]
        context_binding = input_payload.get("context_binding")
        revision = input_payload.get("checkpoint_revision")
        if (
            input_payload.get("schema_version") != 1
            or input_payload.get("role") != expected_role
            or input_payload.get("purpose") != expected_purpose
            or input_payload.get("generation_binding") != binding
            or input_payload.get("generation_binding_digest")
            != expected_binding_digest
            or input_payload.get("checkpoint_stage")
            != SLOT_STAGES[effect_slot]
            or not isinstance(context_binding, dict)
            or not context_binding
            or input_payload.get("context_binding_digest")
            != content_digest(context_binding)
            or not _plain_int(revision)
            or int(revision) < 0
        ):
            raise StrictAuthorityError(
                f"strict_authority_phase_effect_binding_invalid:{phase_name}"
            )
        revisions.add(int(revision))
        context_digests_by_slot.setdefault(effect_slot, set()).add(
            str(input_payload["context_binding_digest"])
        )

    if not revisions:
        return int(current_revision)
    if len(revisions) != 1:
        raise StrictAuthorityError(
            f"strict_authority_phase_checkpoint_revision_drift:{phase_name}"
        )
    for effect_slot, context_digests in context_digests_by_slot.items():
        if len(context_digests) != 1:
            raise StrictAuthorityError(
                "strict_authority_phase_slot_context_drift:"
                f"{phase_name}:{effect_slot}"
            )
    existing_context_digests = context_digests_by_slot.get(slot)
    if (
        existing_context_digests
        and expected_context_binding is not None
        and existing_context_digests
        != {content_digest(_json_value(expected_context_binding))}
    ):
        raise StrictAuthorityError(
            "strict_authority_phase_slot_context_drift:"
            f"{phase_name}:{slot}"
        )
    frozen_revision = next(iter(revisions))
    if int(current_revision) < frozen_revision:
        raise StrictAuthorityError(
            f"strict_authority_phase_checkpoint_revision_regressed:{phase_name}"
        )
    return frozen_revision


def new_call(
    checkpoint: dict[str, Any],
    *,
    slot: str,
    role: str | None = None,
    purpose: str | None = None,
    context_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a call descriptor; no provider effect exists until dispatch."""

    if slot not in SLOT_CONTRACTS:
        raise StrictAuthorityError(f"strict_authority_slot_invalid:{slot}")
    expected_role, expected_purpose = SLOT_CONTRACTS[slot]
    role = str(role or expected_role)
    purpose = str(purpose or expected_purpose)
    # Retry suffixes are presentation only.  The durable role is the stable
    # contract role so schema retries cannot create a ninth accepted slot.
    if not (role == expected_role or role.startswith(expected_role + " ")):
        raise StrictAuthorityError(f"strict_authority_role_mismatch:{slot}")
    if purpose != expected_purpose:
        raise StrictAuthorityError(f"strict_authority_purpose_mismatch:{slot}")
    binding = generation_binding(checkpoint)
    current_revision = checkpoint.get("checkpoint_revision")
    if not _plain_int(current_revision) or int(current_revision) < 0:
        raise StrictAuthorityError("strict_authority_checkpoint_revision_invalid")
    stage = str(checkpoint.get("stage") or "").strip()
    if stage != SLOT_STAGES[slot]:
        raise StrictAuthorityError(
            f"strict_authority_checkpoint_stage_invalid:{slot}:{stage}"
        )
    if not isinstance(context_binding, dict) or not context_binding:
        raise StrictAuthorityError(
            f"strict_authority_context_binding_missing:{slot}"
        )
    normalized_context = _json_value(context_binding)
    revision = _frozen_phase_checkpoint_revision(
        checkpoint,
        slot=slot,
        binding=binding,
        current_revision=int(current_revision),
        expected_context_binding=normalized_context,
    )
    descriptor = {
        "schema_version": 1,
        "run_id": authority_run_id(binding["workflow_run_id"]),
        "slot": slot,
        "role": expected_role,
        "purpose": expected_purpose,
        "invocation_id": uuid.uuid4().hex,
        "generation_binding": binding,
        "generation_binding_digest": content_digest(binding),
        "checkpoint_stage": stage,
        "checkpoint_revision": int(revision),
        "context_binding": normalized_context,
        "context_binding_digest": content_digest(normalized_context),
    }
    _reap_or_block_running_calls(descriptor)
    recovered = _recover_accepted_call(descriptor)
    if recovered is not None:
        return recovered
    recovered = _recover_completed_unaccepted_call(descriptor)
    if recovered is not None:
        return recovered
    rejections = _schema_rejections(descriptor)
    if len(rejections) >= MAX_SCHEMA_ATTEMPTS_PER_SLOT:
        raise StrictAuthorityError(
            f"strict_authority_schema_retry_exhausted:{slot}"
        )
    if rejections:
        descriptor.update({
            "schema_retry_required": True,
            "schema_attempt": len(rejections) + 1,
            "prior_schema_rejection": deepcopy(rejections[-1]),
        })
    else:
        descriptor["schema_attempt"] = 1
    return descriptor


def dispatch_call(
    call: dict[str, Any],
    *,
    full_prompt: str,
    tools: Any,
    owner: str,
    actual_role: str | None = None,
    model: str = "sonnet",
    lease_seconds: float = 3600.0,
) -> dict[str, Any]:
    """Request and claim the provider effect immediately before SDK dispatch."""

    if not isinstance(call, dict) or call.get("schema_version") != 1:
        raise StrictAuthorityError("strict_authority_call_descriptor_invalid")
    run_id = str(call.get("run_id") or "")
    if not run_id:
        raise StrictAuthorityError("strict_authority_dispatch_run_id_missing")
    slot = str(call.get("slot") or "")
    if slot not in SLOT_CONTRACTS:
        raise StrictAuthorityError("strict_authority_call_slot_invalid")
    if call.get("replay_provider"):
        incoming_role = str(actual_role or call.get("role") or "")
        stable_role = str(call.get("role") or "")
        if not (
            incoming_role == stable_role
            or incoming_role.startswith(stable_role + " ")
        ):
            raise StrictAuthorityError("strict_authority_replay_role_mismatch")
        if str(model) != "sonnet" or _json_value(tools) != SLOT_TOOLS[slot]:
            raise StrictAuthorityError("strict_authority_replay_runtime_mismatch")
        store = _store()
        try:
            with store.command_lock(run_id, blocking=True):
                instance = store.instance(run_id)
                if instance.get("status") != "running":
                    raise StrictAuthorityError(
                        "strict_authority_dispatch_journal_abandoned"
                        if instance.get("status") == "abandoned"
                        else "strict_authority_dispatch_journal_not_running"
                    )
                # An accepted schema-retry/Try-N result is the authority.
                # Restarting begins at Try-1, so its newly rendered attempt
                # prompt may differ.  The original prompt remains bound inside
                # the completed effect; replay never replaces that digest.
                call["replay_request_prompt_digest"] = hashlib.sha256(
                    str(full_prompt).encode("utf-8")
                ).hexdigest()
                call["dispatched"] = True
                call["provider_completed"] = True
                return call
        except StrictAuthorityError:
            raise
        except Exception as exc:
            raise StrictAuthorityError(
                "strict_authority_dispatch_journal_unavailable:"
                f"{type(exc).__name__}"
            ) from exc
    if call.get("dispatched"):
        raise StrictAuthorityError("strict_authority_call_already_dispatched")
    invocation_id = str(call.get("invocation_id") or "")
    if len(invocation_id) != 32:
        raise StrictAuthorityError("strict_authority_invocation_id_invalid")
    actual_role = str(actual_role or call.get("role") or "")
    stable_role = str(call.get("role") or "")
    if not (actual_role == stable_role or actual_role.startswith(stable_role + " ")):
        raise StrictAuthorityError("strict_authority_actual_role_mismatch")
    if str(model) != "sonnet":
        raise StrictAuthorityError("strict_authority_model_mismatch")
    normalized_tools = _json_value(tools)
    if normalized_tools != SLOT_TOOLS[slot]:
        raise StrictAuthorityError(f"strict_authority_tools_mismatch:{slot}")
    try:
        bounded_lease_seconds = float(lease_seconds)
    except (TypeError, ValueError):
        raise StrictAuthorityError("strict_authority_lease_seconds_invalid")
    if not 1.0 <= bounded_lease_seconds <= 7200.0:
        raise StrictAuthorityError("strict_authority_lease_seconds_invalid")
    prompt_digest = hashlib.sha256(str(full_prompt).encode("utf-8")).hexdigest()
    input_payload = {
        "schema_version": 1,
        "slot": slot,
        "role": call.get("role"),
        "purpose": call.get("purpose"),
        "invocation_id": invocation_id,
        "generation_binding": deepcopy(call.get("generation_binding")),
        "generation_binding_digest": call.get("generation_binding_digest"),
        "checkpoint_stage": call.get("checkpoint_stage"),
        "checkpoint_revision": call.get("checkpoint_revision"),
        "context_binding": deepcopy(call.get("context_binding")),
        "context_binding_digest": call.get("context_binding_digest"),
        "prompt_digest": prompt_digest,
        "tools": normalized_tools,
        "actual_role": actual_role,
        "model": str(model),
    }
    effect_id = "strict-llm-" + content_digest({
        "run_id": call.get("run_id"),
        "invocation_id": invocation_id,
        "slot": slot,
        "prompt_digest": prompt_digest,
    })
    store = _store()
    try:
        with store.command_lock(run_id, blocking=True):
            store.ensure_instance(
                run_id,
                definition_version=DEFINITION_VERSION,
            )
            instance = store.instance(run_id)
            if instance.get("status") != "running":
                raise StrictAuthorityError(
                    "strict_authority_dispatch_journal_abandoned"
                    if instance.get("status") == "abandoned"
                    else "strict_authority_dispatch_journal_not_running"
                )
            store.request_effect(
                run_id=run_id,
                effect_id=effect_id,
                kind=EFFECT_KIND,
                input_payload=input_payload,
                causation_id=f"strict-call-request:{invocation_id}",
                max_attempts=1,
            )
            lease = store.claim_effect(
                effect_id,
                owner=str(owner),
                lease_seconds=bounded_lease_seconds,
            )
    except StrictAuthorityError:
        raise
    except Exception as exc:
        raise StrictAuthorityError(
            "strict_authority_dispatch_journal_unavailable:"
            f"{type(exc).__name__}"
        ) from exc
    call.update({
        "effect_id": effect_id,
        "lease_epoch": int(lease.lease_epoch),
        "prompt_digest": prompt_digest,
        "actual_role": actual_role,
        "model": str(model),
        "tools": normalized_tools,
        "dispatched": True,
    })
    return call


def _provider_metadata(result: Any, *, attempt: int) -> dict[str, Any]:
    from orchestrator_cost_policy import sdk_result_event_id

    return {
        "provider_event_id": sdk_result_event_id(
            result,
            source="strict_authority",
            attempt=int(attempt),
        ),
        "provider_session_id": str(getattr(result, "session_id", "") or ""),
        "provider_subtype": str(getattr(result, "subtype", "") or ""),
        "provider_is_error": bool(getattr(result, "is_error", False)),
        "provider_num_turns": getattr(result, "num_turns", None),
        "provider_stop_reason": getattr(result, "stop_reason", None),
        "provider_cost_usd": getattr(result, "total_cost_usd", None),
        "provider_usage": _json_value(getattr(result, "usage", None)),
        "provider_result_output": getattr(result, "result", None),
        "provider_structured_output": _json_value(
            getattr(result, "structured_output", None)
        ),
    }


def _provider_output_binding(
    terminal_result: Any,
    *,
    raw_output: str,
) -> dict[str, Any]:
    """Bind persisted raw text to the terminal SDK result payload.

    A successful ``ResultMessage`` is not enough: its terminal result (or its
    structured result on structured-output transports) must be the exact bytes
    that the deterministic role parser consumes.  Streaming ``TextBlock``
    aggregation is observability only: tool-loop prose and proxy reassembly can
    differ from the terminal provider result and therefore cannot be authority.
    """

    raw_output = str(raw_output)
    result_output = getattr(terminal_result, "result", None)
    structured_output = getattr(terminal_result, "structured_output", None)
    if isinstance(result_output, str):
        if raw_output != result_output:
            raise StrictAuthorityError(
                "strict_authority_provider_raw_result_mismatch"
            )
        mode = "terminal_result_text"
    elif structured_output is not None:
        from llm_query import parse_json_output_with_mode

        parsed, _mode = parse_json_output_with_mode(raw_output)
        if _json_value(parsed) != _json_value(structured_output):
            raise StrictAuthorityError(
                "strict_authority_provider_raw_structured_mismatch"
            )
        mode = "terminal_structured_output"
    else:
        raise StrictAuthorityError(
            "strict_authority_provider_terminal_output_missing"
        )
    subject = {
        "mode": mode,
        "raw_output_digest": hashlib.sha256(raw_output.encode("utf-8")).hexdigest(),
        "terminal_result_output_digest": (
            hashlib.sha256(result_output.encode("utf-8")).hexdigest()
            if isinstance(result_output, str)
            else None
        ),
        "terminal_structured_output_digest": (
            content_digest(_json_value(structured_output))
            if structured_output is not None
            else None
        ),
    }
    return {**subject, "output_binding_digest": content_digest(subject)}


def canonical_provider_output(provider_results: Iterable[Any]) -> str:
    """Return the exact terminal provider output consumed by strict parsers."""

    results = list(provider_results)
    if not results or not isinstance(results[-1], ResultMessage):
        raise StrictAuthorityError("strict_authority_provider_result_missing")
    terminal = results[-1]
    if bool(getattr(terminal, "is_error", False)) or str(
        getattr(terminal, "subtype", "") or ""
    ) != "success":
        raise StrictAuthorityError("strict_authority_provider_result_not_success")
    result_output = getattr(terminal, "result", None)
    if isinstance(result_output, str):
        return result_output
    structured_output = getattr(terminal, "structured_output", None)
    if structured_output is not None:
        return json.dumps(
            _json_value(structured_output),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    raise StrictAuthorityError(
        "strict_authority_provider_terminal_output_missing"
    )


def _project_role_result(call: dict[str, Any], raw_output: str) -> Any:
    """Run the checked-in deterministic parser/projection for one role slot."""

    slot = str(call.get("slot") or "")
    context = call.get("context_binding") or {}
    binding = call.get("generation_binding") or {}
    projection_detail_errors: list[str] = []
    if slot.startswith("proposal:"):
        from agent_master import (
            _master_proposal_projection_hints,
            _source_symbol_graph,
            _validated_master_proposal,
        )
        from evolution_infra import get_bot_dir

        direction = slot.split(":", 1)[1]
        candidate_dir = get_bot_dir(int(binding.get("next_v")))
        source_graph, source_digest = _source_symbol_graph(candidate_dir)
        if source_digest != context.get("source_code_digest"):
            raise StrictAuthorityError(
                "strict_authority_projection_source_digest_mismatch"
            )
        projected = _validated_master_proposal(
            raw_output,
            direction,
            source_graph=source_graph,
            snapshot_dir=(
                candidate_dir / ".protocol_bootstrap_no_strength_evidence"
            ),
            national_policy_only=True,
            execution_mode="fixed_blueprint_capability_audit",
            evidence_mode="fresh_strict_control_no_strength",
        )
        if not isinstance(projected, dict):
            hints = _master_proposal_projection_hints(
                raw_output,
                source_graph=source_graph,
                snapshot_dir=(
                    candidate_dir / ".protocol_bootstrap_no_strength_evidence"
                ),
                national_policy_only=True,
                evidence_mode="fresh_strict_control_no_strength",
            ) or ["proposal_contract_invalid"]
            projection_detail_errors = [
                "strict_authority_proposal_projection:" + hint
                for hint in hints
            ]
    elif slot.startswith("ballot:"):
        from agent_master import _validated_proposal_critique

        proposal_ids = context.get("proposal_ids")
        projected = (
            _validated_proposal_critique(raw_output, set(map(str, proposal_ids)))
            if isinstance(proposal_ids, list)
            else None
        )
    elif slot == "master:final":
        from agent_master import _project_strict_final_master_result

        projected, errors = _project_strict_final_master_result(
            raw_output,
            proposal_packet=context.get("proposal_packet"),
            architecture_policy=context.get("architecture_policy"),
        )
        if errors:
            raise StrictAuthorityError(
                ["strict_authority_projection_master:" + item for item in errors]
            )
    elif slot in {"review", "critic"}:
        from llm_query import parse_json_output_with_mode
        from output_schema import validate_agent_output

        parsed, _mode = parse_json_output_with_mode(raw_output)
        if not isinstance(parsed, dict):
            projected = None
        else:
            if slot == "critic" and "feedback" in parsed and not isinstance(
                parsed["feedback"], str
            ):
                parsed["feedback"] = (
                    str(parsed["feedback"])
                    if parsed["feedback"] is not None
                    else ""
                )
            projected, errors = validate_agent_output(
                "reviewer" if slot == "review" else "critic",
                parsed,
            )
            if errors:
                projected = None
    else:
        projected = None
    if not isinstance(projected, dict):
        raise StrictAuthorityError([
            f"strict_authority_role_projection_rejected:{slot}",
            *projection_detail_errors,
        ])
    return _json_value(projected)


def complete_provider_call(
    call: dict[str, Any],
    *,
    raw_output: str,
    provider_results: list[Any],
) -> dict[str, Any]:
    """Complete one fenced effect from real SDK ``ResultMessage`` objects."""

    if not call.get("dispatched"):
        raise StrictAuthorityError("strict_authority_provider_not_dispatched")
    if not provider_results:
        raise StrictAuthorityError("strict_authority_provider_result_missing")
    if not _provider_results_were_observed(
        provider_results,
        invocation_id=str(call.get("invocation_id") or ""),
        effect_id=str(call.get("effect_id") or ""),
    ):
        raise StrictAuthorityError("strict_authority_provider_result_not_observed")
    metadata = [
        _provider_metadata(result, attempt=index)
        for index, result in enumerate(provider_results)
    ]
    terminal = metadata[-1]
    if terminal["provider_is_error"] or terminal["provider_subtype"] != "success":
        raise StrictAuthorityError("strict_authority_provider_result_not_success")
    if not terminal["provider_event_id"] or not terminal["provider_session_id"]:
        raise StrictAuthorityError("strict_authority_provider_identity_missing")
    raw_output_digest = hashlib.sha256(str(raw_output).encode("utf-8")).hexdigest()
    output_binding = _provider_output_binding(
        provider_results[-1],
        raw_output=str(raw_output),
    )
    projection_errors: tuple[str, ...] = ()
    projected_role_result = None
    try:
        projected_role_result = _project_role_result(call, str(raw_output))
    except StrictAuthorityError as exc:
        projection_errors = exc.errors
    projected_role_result_digest = (
        content_digest(_json_value(projected_role_result))
        if projected_role_result is not None
        else None
    )
    result_material = {
        "schema_version": 1,
        "effect_id": call["effect_id"],
        "invocation_id": call["invocation_id"],
        "slot": call["slot"],
        "role": call["role"],
        "purpose": call["purpose"],
        "generation_binding_digest": call["generation_binding_digest"],
        "context_binding_digest": call["context_binding_digest"],
        "checkpoint_stage": call["checkpoint_stage"],
        "checkpoint_revision": int(call["checkpoint_revision"]),
        "actual_role": call.get("actual_role"),
        "model": call.get("model"),
        "tools": deepcopy(call.get("tools")),
        "prompt_digest": call["prompt_digest"],
        "raw_output_digest": raw_output_digest,
        "raw_output": str(raw_output),
        "parse_contract": SLOT_PARSE_CONTRACTS[call["slot"]],
        "role_projection_valid": not projection_errors,
        "role_projection_errors": list(projection_errors),
        "projected_role_result": _json_value(projected_role_result),
        "projected_role_result_digest": projected_role_result_digest,
        **output_binding,
        **terminal,
        "provider_result_count": len(metadata),
        "provider_event_ids": [row["provider_event_id"] for row in metadata],
    }
    result_payload = {
        **result_material,
        "result_digest": content_digest(result_material),
    }
    followup_events = [{
        "event_type": "StrictProviderResultObserved",
        "payload": {
            "effect_id": call["effect_id"],
            "slot": call["slot"],
            "invocation_id": call["invocation_id"],
            "provider_event_id": terminal["provider_event_id"],
            "result_digest": result_payload["result_digest"],
        },
        "causation_id": f"strict-provider-observed:{call['invocation_id']}",
    }]
    if projection_errors:
        followup_events.append({
            "event_type": REJECTED_EVENT,
            "payload": {
                "effect_id": call["effect_id"],
                "slot": call["slot"],
                "invocation_id": call["invocation_id"],
                "rejection_kind": "schema_projection",
                "parse_contract": SLOT_PARSE_CONTRACTS[call["slot"]],
                "raw_output_digest": raw_output_digest,
                "projection_errors": list(projection_errors),
            },
            "causation_id": f"strict-role-rejected:{call['invocation_id']}",
        })
    completion = _store().complete_effect(
        call["effect_id"],
        lease_epoch=int(call["lease_epoch"]),
        completion_id=f"strict-provider-complete:{call['invocation_id']}",
        result_payload=result_payload,
        causation_id=f"strict-provider-result:{call['invocation_id']}",
        followup_events=followup_events,
        require_live_lease=True,
    )
    if completion.get("accepted") is not True:
        raise StrictAuthorityError("strict_authority_provider_completion_fenced")
    _consume_observed_provider_results(provider_results)
    call.update({
        "provider_completed": True,
        "raw_output_digest": raw_output_digest,
        "provider_result_digest": result_payload["result_digest"],
        "provider_event_id": terminal["provider_event_id"],
        "provider_session_id": terminal["provider_session_id"],
        "projected_role_result": deepcopy(projected_role_result),
        "projected_role_result_digest": projected_role_result_digest,
        "role_projection_valid": not projection_errors,
    })
    return result_payload


def reject_duplicate_proposal(call: dict[str, Any]) -> dict[str, Any]:
    """Durably reject a valid scout result that duplicates an accepted slot.

    Proposal identity intentionally excludes the scout direction.  Therefore a
    schema-valid response can still violate the three-distinct-proposal set.
    The ensemble caller must record that deterministic cross-slot rejection
    before requesting its one repair; otherwise restart would replay the
    completed-but-unaccepted duplicate forever.
    """

    slot = str((call or {}).get("slot") or "")
    if slot not in MASTER_SLOTS[:3]:
        raise StrictAuthorityError(
            "strict_authority_duplicate_rejection_slot_invalid"
        )
    store = _store()
    effect = store.effect(str(call.get("effect_id") or ""))
    provider = effect.get("result_payload") or {}
    input_payload = effect.get("input_payload") or {}
    provider_subject = {
        key: value for key, value in provider.items() if key != "result_digest"
    }
    projected = provider.get("projected_role_result")
    proposal_id = (
        str(projected.get("proposal_id") or "")
        if isinstance(projected, dict)
        else ""
    )
    if (
        effect.get("status") != "completed"
        or effect.get("kind") != EFFECT_KIND
        or effect.get("run_id") != call.get("run_id")
        or provider.get("role_projection_valid") is not True
        or not proposal_id
        or provider.get("result_digest") != content_digest(provider_subject)
        or provider.get("projected_role_result_digest")
        != content_digest(_json_value(projected))
        or any(
            input_payload.get(field) != call.get(field)
            or provider.get(field) != call.get(field)
            for field in (
                "slot",
                "role",
                "purpose",
                "invocation_id",
                "generation_binding_digest",
                "context_binding_digest",
                "checkpoint_stage",
                "checkpoint_revision",
                "prompt_digest",
                "actual_role",
                "model",
                "tools",
            )
        )
    ):
        raise StrictAuthorityError(
            "strict_authority_duplicate_rejection_provider_invalid"
        )
    events = store.events(str(call.get("run_id") or ""))
    accepted_collisions = sorted({
        str(event.payload.get("slot") or "")
        for event in events
        if event.event_type == ACCEPTED_EVENT
        and event.payload.get("slot") in MASTER_SLOTS[:3]
        and event.payload.get("slot") != slot
        and isinstance(event.payload.get("role_result"), dict)
        and str(event.payload["role_result"].get("proposal_id") or "")
        == proposal_id
    })
    if not accepted_collisions:
        raise StrictAuthorityError(
            "strict_authority_duplicate_rejection_collision_missing"
        )
    if any(
        event.event_type == ACCEPTED_EVENT
        and event.payload.get("effect_id") == call.get("effect_id")
        for event in events
    ):
        raise StrictAuthorityError(
            "strict_authority_duplicate_rejection_already_accepted"
        )
    payload = {
        "effect_id": call["effect_id"],
        "slot": slot,
        "invocation_id": call["invocation_id"],
        "rejection_kind": "proposal_identity_collision",
        "parse_contract": SLOT_PARSE_CONTRACTS[slot],
        "raw_output_digest": provider.get("raw_output_digest"),
        "projection_errors": [
            "strict_authority_proposal_identity_collision"
        ],
        "projected_role_result_digest": provider.get(
            "projected_role_result_digest"
        ),
        "proposal_id": proposal_id,
        "conflicting_slots": accepted_collisions,
    }
    event = store.append_event(
        call["run_id"],
        REJECTED_EVENT,
        payload,
        causation_id=f"strict-role-duplicate-rejected:{call['invocation_id']}",
    )
    return {
        "schema_version": 1,
        "run_id": call["run_id"],
        "slot": slot,
        "effect_id": call["effect_id"],
        "event_seq": int(event.seq),
        "event_payload_digest": event.payload_digest,
        "proposal_id": proposal_id,
        "conflicting_slots": accepted_collisions,
    }


def fail_provider_call(call: dict[str, Any], error: BaseException | str) -> None:
    """Close a claimed call that cannot produce an admissible provider result."""

    if not isinstance(call, dict) or not call.get("dispatched") or call.get(
        "provider_completed"
    ):
        return
    try:
        _store().fail_effect(
            str(call["effect_id"]),
            lease_epoch=int(call["lease_epoch"]),
            error=f"{type(error).__name__}: {str(error)[:1500]}",
            retryable=False,
            causation_id=f"strict-provider-failed:{call['invocation_id']}",
        )
    except Exception:
        # Preserve the original LLM exception.  A running/expired effect is not
        # accepted and therefore remains fail-closed at every later validator.
        pass


def accept_role_result(
    call: dict[str, Any],
    *,
    role_result: Any,
    parse_contract: str,
) -> dict[str, Any]:
    """Append the post-parse accepted-role event and return its stable ref."""

    slot = str(call.get("slot") or "")
    if parse_contract != SLOT_PARSE_CONTRACTS.get(slot):
        raise StrictAuthorityError(
            f"strict_authority_parse_contract_invalid:{slot}:{parse_contract}"
        )
    role_result_digest = content_digest(_json_value(role_result))
    if call.get("accepted_receipt"):
        if role_result_digest != call.get("accepted_role_result_digest"):
            raise StrictAuthorityError(
                "strict_authority_replay_role_result_mismatch"
            )
        return deepcopy(call["accepted_receipt"])
    store = _store()
    effect = store.effect(str(call.get("effect_id") or ""))
    if effect.get("status") != "completed":
        raise StrictAuthorityError("strict_authority_provider_effect_not_completed")
    provider = effect.get("result_payload") or {}
    input_payload = effect.get("input_payload") or {}
    checks = {
        "invocation_id": call.get("invocation_id"),
        "slot": call.get("slot"),
        "role": call.get("role"),
        "purpose": call.get("purpose"),
        "generation_binding_digest": call.get("generation_binding_digest"),
        "context_binding_digest": call.get("context_binding_digest"),
        "checkpoint_stage": call.get("checkpoint_stage"),
        "checkpoint_revision": call.get("checkpoint_revision"),
        "actual_role": call.get("actual_role"),
        "model": call.get("model"),
        "tools": call.get("tools"),
        "prompt_digest": call.get("prompt_digest"),
    }
    errors = []
    for field, expected in checks.items():
        if input_payload.get(field) != expected or provider.get(field) != expected:
            errors.append(f"strict_authority_accept_{field}_mismatch")
    if provider.get("raw_output_digest") != call.get("raw_output_digest"):
        errors.append("strict_authority_accept_raw_output_mismatch")
    if provider.get("result_digest") != call.get("provider_result_digest"):
        errors.append("strict_authority_accept_provider_result_mismatch")
    if provider.get("provider_is_error") is not False or provider.get(
        "provider_subtype"
    ) != "success":
        errors.append("strict_authority_accept_provider_not_success")
    if provider.get("parse_contract") != parse_contract:
        errors.append("strict_authority_accept_projection_contract_mismatch")
    if provider.get("role_projection_valid") is not True:
        errors.append("strict_authority_accept_role_projection_invalid")
    if provider.get("projected_role_result_digest") != role_result_digest:
        errors.append("strict_authority_accept_role_projection_mismatch")
    if _json_value(provider.get("projected_role_result")) != _json_value(
        role_result
    ):
        errors.append("strict_authority_accept_projected_role_result_mismatch")
    if errors:
        raise StrictAuthorityError(errors)
    subject = {
        "schema_version": 1,
        "kind": RECEIPT_KIND,
        "run_id": call["run_id"],
        "effect_id": call["effect_id"],
        "lease_epoch": int(call["lease_epoch"]),
        "slot": call["slot"],
        "role": call["role"],
        "purpose": call["purpose"],
        "invocation_id": call["invocation_id"],
        "generation_binding_digest": call["generation_binding_digest"],
        "checkpoint_stage": call["checkpoint_stage"],
        "checkpoint_revision": int(call["checkpoint_revision"]),
        "actual_role": provider["actual_role"],
        "model": provider["model"],
        "tools": deepcopy(provider["tools"]),
        "context_binding_digest": call["context_binding_digest"],
        "provider_event_id": provider["provider_event_id"],
        "provider_session_id": provider["provider_session_id"],
        "provider_result_digest": provider["result_digest"],
        "prompt_digest": provider["prompt_digest"],
        "raw_output_digest": provider["raw_output_digest"],
        "parse_contract": str(parse_contract),
        "schema_valid": True,
        "role_result_digest": role_result_digest,
        "role_result": _json_value(role_result),
    }
    subject["receipt_digest"] = content_digest(subject)
    event = store.append_event(
        call["run_id"],
        ACCEPTED_EVENT,
        subject,
        causation_id=f"strict-role-accepted:{call['invocation_id']}",
    )
    ref = {
        "schema_version": 1,
        "kind": RECEIPT_KIND,
        "run_id": call["run_id"],
        "slot": call["slot"],
        "effect_id": call["effect_id"],
        "invocation_id": call["invocation_id"],
        "event_seq": int(event.seq),
        "event_payload_digest": event.payload_digest,
        "receipt_digest": subject["receipt_digest"],
        "role_result_digest": role_result_digest,
    }
    call["accepted_receipt"] = deepcopy(ref)
    return ref


def _invocation_evidence_log_name(slot: str, actual_role: str) -> str:
    """Return the sole log filename admitted for an evidence-bearing role."""

    if slot in MASTER_SLOTS[:3]:
        direction = slot.split(":", 1)[1]
        base_role = f"MASTER PROPOSAL {direction}"
        if actual_role == base_role:
            suffix = ""
        elif actual_role == base_role + " SCHEMA RETRY":
            suffix = "_schema_retry"
        elif actual_role == base_role + " DISTINCTNESS RETRY":
            suffix = "_distinctness_retry"
        else:
            raise StrictAuthorityError(
                f"strict_authority_invocation_evidence_role_invalid:{slot}"
            )
        return f"master_proposal_{direction}{suffix}_io.txt"
    if slot in MASTER_SLOTS[3:5]:
        critic_id = slot.split(":", 1)[1]
        base_role = f"MASTER PROPOSAL CRITIC {critic_id}"
        if actual_role == base_role:
            suffix = ""
        elif actual_role == base_role + " SCHEMA RETRY":
            suffix = "_schema_retry"
        else:
            raise StrictAuthorityError(
                f"strict_authority_invocation_evidence_role_invalid:{slot}"
            )
        return f"master_proposal_critic_{critic_id}{suffix}_io.txt"
    if slot == "review" and actual_role == "LEAD CODE REVIEWER":
        return "reviewer_io.txt"
    if slot == "critic" and actual_role == "STRATEGY CRITIC":
        return "critic_io.txt"
    raise StrictAuthorityError(
        f"strict_authority_invocation_evidence_slot_invalid:{slot}"
    )


def _strict_generation_logs_root(subject: dict[str, Any]) -> Path:
    """Derive the only log root admitted by one generation binding."""

    binding = (subject or {}).get("generation_binding")
    next_v = binding.get("next_v") if isinstance(binding, dict) else None
    if not _plain_int(next_v) or int(next_v) < 1:
        raise StrictAuthorityError(
            "strict_authority_invocation_log_generation_binding_invalid"
        )
    from evolution_infra import RESULTS_DIR

    return Path(os.path.abspath(os.fspath(RESULTS_DIR))) / f"v{next_v}" / "logs"


def strict_invocation_log_path(
    call: dict[str, Any],
    *,
    logs_dir: str | Path,
    basename: str,
) -> Path:
    """Allocate the immutable role log owned by one strict provider effect.

    A version can be prepared repeatedly after canonical abandonment.  The
    human-facing role name is therefore not a unique log identity: reusing
    ``v<N>/logs/<role>_io.txt`` would append later provider calls and evidence
    trailers to an earlier accepted call.  The strict invocation id is durable,
    globally unique, and later bound into the provider effect, so isolate every
    call beneath that id while preserving the conventional basename used by
    observability and evidence validation.
    """

    invocation_id = str((call or {}).get("invocation_id") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", invocation_id):
        raise StrictAuthorityError(
            "strict_authority_invocation_log_invocation_id_invalid"
        )
    basename = str(basename or "")
    if (
        Path(basename).name != basename
        or not re.fullmatch(r"[a-z0-9_]+_io\.txt", basename)
    ):
        raise StrictAuthorityError(
            "strict_authority_invocation_log_basename_invalid"
        )

    try:
        root = Path(os.path.abspath(os.fspath(logs_dir)))
        if root != _strict_generation_logs_root(call):
            raise StrictAuthorityError(
                "strict_authority_invocation_log_generation_root_mismatch"
            )
        chain = [root]
        while chain[-1] != chain[-1].parent:
            chain.append(chain[-1].parent)
        for component in reversed(chain):
            if not os.path.lexists(component):
                continue
            metadata = os.lstat(component)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
                metadata.st_mode
            ):
                raise StrictAuthorityError(
                    "strict_authority_invocation_log_parent_invalid"
                )
        root.mkdir(parents=True, exist_ok=True)
        for component in reversed(chain):
            metadata = os.lstat(component)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
                metadata.st_mode
            ):
                raise StrictAuthorityError(
                    "strict_authority_invocation_log_parent_invalid"
                )
        strict_root = root / "strict_invocations"
        strict_root.mkdir(mode=0o700, exist_ok=True)
        if strict_root.is_symlink() or not strict_root.is_dir():
            raise StrictAuthorityError(
                "strict_authority_invocation_log_root_invalid"
            )
        invocation_dir = strict_root / invocation_id
        invocation_dir.mkdir(mode=0o700, exist_ok=True)
        if invocation_dir.is_symlink() or not invocation_dir.is_dir():
            raise StrictAuthorityError(
                "strict_authority_invocation_log_invocation_dir_invalid"
            )
        path = invocation_dir / basename
        if os.path.lexists(path):
            metadata = os.lstat(path)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise StrictAuthorityError(
                    "strict_authority_invocation_log_path_invalid"
                )
        return path
    except StrictAuthorityError:
        raise
    except OSError as exc:
        raise StrictAuthorityError(
            "strict_authority_invocation_log_filesystem_invalid:"
            f"{type(exc).__name__}"
        ) from exc


def _invocation_evidence_authority(
    call: dict[str, Any],
) -> tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Re-open the exact accepted role and completed provider effect."""

    slot = str((call or {}).get("slot") or "")
    if slot not in INVOCATION_EVIDENCE_SLOTS:
        raise StrictAuthorityError(
            f"strict_authority_invocation_evidence_slot_invalid:{slot}"
        )
    run_id = str(call.get("run_id") or "")
    effect_id = str(call.get("effect_id") or "")
    store = _store()
    accepted = [
        event
        for event in store.events(run_id)
        if event.event_type == ACCEPTED_EVENT
        and event.payload.get("effect_id") == effect_id
        and event.payload.get("slot") == slot
    ]
    if len(accepted) != 1:
        raise StrictAuthorityError(
            f"strict_authority_invocation_evidence_accepted_count:{slot}:"
            f"{len(accepted)}"
        )
    accepted_event = accepted[0]
    accepted_payload = accepted_event.payload
    accepted_subject = {
        key: value
        for key, value in accepted_payload.items()
        if key != "receipt_digest"
    }
    if (
        accepted_event.payload_digest != content_digest(accepted_payload)
        or accepted_payload.get("receipt_digest")
        != content_digest(accepted_subject)
        or accepted_payload.get("role_result_digest")
        != content_digest(_json_value(accepted_payload.get("role_result")))
    ):
        raise StrictAuthorityError(
            "strict_authority_invocation_evidence_accepted_invalid"
        )
    effect = store.effect(effect_id)
    provider = effect.get("result_payload") or {}
    input_payload = effect.get("input_payload") or {}
    provider_subject = {
        key: value for key, value in provider.items() if key != "result_digest"
    }
    stable_fields = (
        "slot",
        "role",
        "purpose",
        "invocation_id",
        "generation_binding_digest",
        "context_binding_digest",
        "checkpoint_stage",
        "checkpoint_revision",
        "prompt_digest",
        "actual_role",
        "model",
        "tools",
    )
    if (
        effect.get("status") != "completed"
        or effect.get("kind") != EFFECT_KIND
        or effect.get("run_id") != run_id
        or provider.get("result_digest") != content_digest(provider_subject)
        or any(
            input_payload.get(field) != accepted_payload.get(field)
            or provider.get(field) != accepted_payload.get(field)
            or call.get(field) != accepted_payload.get(field)
            for field in stable_fields
        )
        or not isinstance(input_payload.get("generation_binding"), dict)
        or content_digest(_json_value(input_payload.get("generation_binding")))
        != accepted_payload.get("generation_binding_digest")
        or (
            "generation_binding" in call
            and input_payload.get("generation_binding")
            != call.get("generation_binding")
        )
        or provider.get("raw_output_digest")
        != accepted_payload.get("raw_output_digest")
        or provider.get("result_digest")
        != accepted_payload.get("provider_result_digest")
        or provider.get("projected_role_result_digest")
        != accepted_payload.get("role_result_digest")
    ):
        raise StrictAuthorityError(
            "strict_authority_invocation_evidence_effect_invalid"
        )
    return accepted_event, accepted_payload, provider, input_payload


def _validate_bound_invocation_evidence(
    evidence: Any,
    *,
    call: dict[str, Any],
    accepted_payload: dict[str, Any],
    provider: dict[str, Any],
    generation_binding: dict[str, Any],
) -> None:
    from system_strict_bootstrap import (
        llm_result_digest,
        validate_llm_invocation_evidence,
    )

    slot = str(call.get("slot") or "")
    actual_role = str(accepted_payload.get("actual_role") or "")
    errors = validate_llm_invocation_evidence(
        evidence,
        expected_purpose=str(accepted_payload.get("purpose") or ""),
        expected_role=actual_role,
        expected_log_name=_invocation_evidence_log_name(slot, actual_role),
    )
    if isinstance(evidence, dict):
        evidence_path = Path(str(evidence.get("io_log_path") or ""))
        expected_log_path = (
            _strict_generation_logs_root({
                "generation_binding": generation_binding,
            })
            / "strict_invocations"
            / str(accepted_payload.get("invocation_id") or "")
            / _invocation_evidence_log_name(slot, actual_role)
        )
        expected = {
            "invocation_id": accepted_payload.get("invocation_id"),
            "prompt_digest": accepted_payload.get("prompt_digest"),
            "raw_output_digest": accepted_payload.get("raw_output_digest"),
            "result_digest": llm_result_digest(
                provider.get("provider_cost_usd"),
                provider.get("provider_usage"),
            ),
            "role_result_digest": accepted_payload.get("role_result_digest"),
        }
        errors.extend(
            f"strict_authority_invocation_evidence_{field}_mismatch"
            for field, value in expected.items()
            if evidence.get(field) != value
        )
        if Path(os.path.abspath(os.fspath(evidence_path))) != expected_log_path:
            errors.append(
                "strict_authority_invocation_evidence_log_identity_mismatch"
            )
    if errors:
        raise StrictAuthorityError(errors)


def bound_invocation_evidence(
    call: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the exact journal-bound evidence and revalidate its live log."""

    (
        accepted_event,
        accepted_payload,
        provider,
        input_payload,
    ) = _invocation_evidence_authority(call)
    store = _store()
    events = [
        event
        for event in store.events(str(call.get("run_id") or ""))
        if event.event_type == INVOCATION_EVIDENCE_BOUND_EVENT
        and event.payload.get("effect_id") == call.get("effect_id")
    ]
    if not events:
        return None
    if len(events) != 1:
        raise StrictAuthorityError(
            "strict_authority_invocation_evidence_binding_count_invalid"
        )
    event = events[0]
    payload = event.payload
    expected_fields = {
        "schema_version",
        "kind",
        "run_id",
        "slot",
        "effect_id",
        "invocation_id",
        "accepted_event_seq",
        "accepted_event_payload_digest",
        "invocation_evidence_digest",
        "invocation_evidence",
    }
    evidence = payload.get("invocation_evidence")
    if (
        set(payload) != expected_fields
        or event.payload_digest != content_digest(payload)
        or payload.get("schema_version") != 1
        or payload.get("kind") != INVOCATION_EVIDENCE_BINDING_KIND
        or payload.get("run_id") != call.get("run_id")
        or payload.get("slot") != call.get("slot")
        or payload.get("effect_id") != call.get("effect_id")
        or payload.get("invocation_id") != call.get("invocation_id")
        or payload.get("accepted_event_seq") != int(accepted_event.seq)
        or payload.get("accepted_event_payload_digest")
        != accepted_event.payload_digest
        or payload.get("invocation_evidence_digest")
        != content_digest(_json_value(evidence))
    ):
        raise StrictAuthorityError(
            "strict_authority_invocation_evidence_binding_invalid"
        )
    _validate_bound_invocation_evidence(
        evidence,
        call=call,
        accepted_payload=accepted_payload,
        provider=provider,
        generation_binding=input_payload["generation_binding"],
    )
    return deepcopy(evidence)


def bind_invocation_evidence(
    call: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Idempotently bind one accepted strict role to its log evidence."""

    existing = bound_invocation_evidence(call)
    if existing is not None:
        if _json_value(existing) != _json_value(evidence):
            raise StrictAuthorityError(
                "strict_authority_invocation_evidence_rebind_mismatch"
            )
        return existing
    (
        accepted_event,
        accepted_payload,
        provider,
        input_payload,
    ) = _invocation_evidence_authority(call)
    _validate_bound_invocation_evidence(
        evidence,
        call=call,
        accepted_payload=accepted_payload,
        provider=provider,
        generation_binding=input_payload["generation_binding"],
    )
    payload = {
        "schema_version": 1,
        "kind": INVOCATION_EVIDENCE_BINDING_KIND,
        "run_id": call["run_id"],
        "slot": call["slot"],
        "effect_id": call["effect_id"],
        "invocation_id": call["invocation_id"],
        "accepted_event_seq": int(accepted_event.seq),
        "accepted_event_payload_digest": accepted_event.payload_digest,
        "invocation_evidence_digest": content_digest(_json_value(evidence)),
        "invocation_evidence": _json_value(evidence),
    }
    try:
        _store().append_event(
            call["run_id"],
            INVOCATION_EVIDENCE_BOUND_EVENT,
            payload,
            causation_id=(
                "strict-invocation-evidence-bound:" + str(call["effect_id"])
            ),
        )
    except WorkflowConflict as exc:
        raise StrictAuthorityError(
            "strict_authority_invocation_evidence_binding_conflict"
        ) from exc
    rebound = bound_invocation_evidence(call)
    if rebound is None:
        raise StrictAuthorityError(
            "strict_authority_invocation_evidence_binding_missing"
        )
    return rebound


def record_bound_invocation_evidence(
    call: dict[str, Any],
    *,
    log_file: str | Path,
) -> dict[str, Any]:
    """Return an existing binding or seal and bind its sole recoverable log."""

    existing = bound_invocation_evidence(call)
    if existing is not None:
        return existing
    _accepted_event, accepted_payload, provider, input_payload = (
        _invocation_evidence_authority(call)
    )
    path = Path(os.path.abspath(os.fspath(log_file)))
    expected_path = (
        _strict_generation_logs_root({
            "generation_binding": input_payload["generation_binding"],
        })
        / "strict_invocations"
        / str(accepted_payload.get("invocation_id") or "")
        / _invocation_evidence_log_name(
            str(call.get("slot") or ""),
            str(accepted_payload.get("actual_role") or ""),
        )
    )
    if path != expected_path:
        raise StrictAuthorityError(
            "strict_authority_invocation_evidence_log_identity_mismatch"
        )
    try:
        mode = path.lstat().st_mode
        if (
            stat.S_ISLNK(mode)
            or not stat.S_ISREG(mode)
            or path.stat().st_size <= 0
        ):
            raise OSError("provider log is not a nonempty regular file")
    except OSError as exc:
        raise StrictAuthorityError(
            "strict_authority_invocation_evidence_provider_log_invalid"
        ) from exc
    from system_strict_bootstrap import (
        SystemStrictBootstrapError,
        llm_result_digest,
        record_llm_invocation_evidence,
    )

    try:
        evidence = record_llm_invocation_evidence(
            invocation_id=str(accepted_payload["invocation_id"]),
            purpose=str(accepted_payload["purpose"]),
            role=str(accepted_payload["actual_role"]),
            prompt_digest=str(accepted_payload["prompt_digest"]),
            raw_output_digest=str(accepted_payload["raw_output_digest"]),
            result_digest=llm_result_digest(
                provider.get("provider_cost_usd"),
                provider.get("provider_usage"),
            ),
            role_result=accepted_payload["role_result"],
            log_file=path,
            recover_or_record=True,
        )
    except SystemStrictBootstrapError as exc:
        raise StrictAuthorityError(exc.errors) from exc
    return bind_invocation_evidence(call, evidence)


# Compatibility aliases for the first implementation name. Active callers use
# the generic API above; keeping these aliases avoids breaking diagnostic code.
bound_master_invocation_evidence = bound_invocation_evidence
bind_master_invocation_evidence = bind_invocation_evidence


def _accepted_events(checkpoint: dict[str, Any]) -> tuple[list[Any], list[str]]:
    try:
        binding = generation_binding(checkpoint)
        run_id = authority_run_id(binding["workflow_run_id"])
        store = _store()
        events = store.events(run_id)
    except Exception as exc:
        return [], [f"strict_authority_journal_unavailable:{type(exc).__name__}"]
    accepted = [event for event in events if event.event_type == ACCEPTED_EVENT]
    return accepted, []


def validate_receipts(
    checkpoint: dict[str, Any],
    *,
    required_slots: Iterable[str],
    expected_role_results: dict[str, Any] | None = None,
    expected_context_bindings: dict[str, dict[str, Any]] | None = None,
    require_no_other_accepted: bool = False,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Re-read effects/events and validate exact accepted slot identities."""

    required = tuple(required_slots)
    expected_role_results = expected_role_results or {}
    expected_context_bindings = expected_context_bindings or {}
    errors: list[str] = []
    if len(set(required)) != len(required) or any(slot not in ALL_SLOTS for slot in required):
        return {}, ["strict_authority_required_slot_set_invalid"]
    try:
        binding = generation_binding(checkpoint)
        run_id = authority_run_id(binding["workflow_run_id"])
        binding_digest = content_digest(binding)
    except StrictAuthorityError as exc:
        return {}, list(exc.errors)
    current_revision = checkpoint.get("checkpoint_revision")
    phase_representatives: dict[str, str] = {}
    for slot in required:
        phase_name, _phase_slots = _authority_phase(slot)
        phase_representatives.setdefault(phase_name, slot)
    phase_anchors: dict[str, int] = {}
    if not _plain_int(current_revision) or int(current_revision) < 0:
        errors.append("strict_authority_checkpoint_revision_invalid")
    else:
        # Final receipt authority covers the complete durable phase, not only
        # its accepted projection.  Re-open every strict EffectRequested row
        # in each required phase so an unaccepted/rejected call cannot smuggle
        # a second checkpoint revision past an otherwise uniform receipt set.
        for phase_name, representative in phase_representatives.items():
            try:
                phase_anchors[phase_name] = _frozen_phase_checkpoint_revision(
                    checkpoint,
                    slot=representative,
                    binding=binding,
                    current_revision=int(current_revision),
                )
            except StrictAuthorityError as exc:
                errors.extend(exc.errors)
    accepted, journal_errors = _accepted_events(checkpoint)
    errors.extend(journal_errors)
    store = _store()
    by_slot: dict[str, list[Any]] = {}
    seen_effects: set[str] = set()
    seen_invocations: set[str] = set()
    seen_provider_events: set[str] = set()
    refs: dict[str, dict[str, Any]] = {}
    revisions: dict[str, int] = {}
    for event in accepted:
        payload = event.payload
        slot = str(payload.get("slot") or "")
        by_slot.setdefault(slot, []).append(event)
        if slot not in ALL_SLOTS:
            errors.append(f"strict_authority_unexpected_slot:{slot}")
            continue
        expected_role, expected_purpose = SLOT_CONTRACTS[slot]
        expected = {
            "schema_version": 1,
            "kind": RECEIPT_KIND,
            "run_id": run_id,
            "slot": slot,
            "role": expected_role,
            "purpose": expected_purpose,
            "generation_binding_digest": binding_digest,
            "schema_valid": True,
            "checkpoint_stage": SLOT_STAGES[slot],
            "parse_contract": SLOT_PARSE_CONTRACTS[slot],
            "model": "sonnet",
            "tools": SLOT_TOOLS[slot],
        }
        for field, value in expected.items():
            if payload.get(field) != value:
                errors.append(f"strict_authority_{slot}_{field}_mismatch")
        receipt_subject = {key: value for key, value in payload.items() if key != "receipt_digest"}
        if payload.get("receipt_digest") != content_digest(receipt_subject):
            errors.append(f"strict_authority_{slot}_receipt_digest_invalid")
        effect_id = str(payload.get("effect_id") or "")
        invocation_id = str(payload.get("invocation_id") or "")
        provider_event_id = str(payload.get("provider_event_id") or "")
        if effect_id in seen_effects:
            errors.append("strict_authority_effect_reused")
        if invocation_id in seen_invocations:
            errors.append("strict_authority_invocation_reused")
        if provider_event_id in seen_provider_events:
            errors.append("strict_authority_provider_event_reused")
        seen_effects.add(effect_id)
        seen_invocations.add(invocation_id)
        seen_provider_events.add(provider_event_id)
        revision = payload.get("checkpoint_revision")
        if not _plain_int(revision) or int(revision) < 0:
            errors.append(f"strict_authority_{slot}_checkpoint_revision_invalid")
        else:
            revisions[slot] = int(revision)
        effect = store.effect(effect_id)
        provider = effect.get("result_payload") or {}
        input_payload = effect.get("input_payload") or {}
        if effect.get("run_id") != run_id or effect.get("kind") != EFFECT_KIND:
            errors.append(f"strict_authority_{slot}_effect_binding_invalid")
        if effect.get("status") != "completed":
            errors.append(f"strict_authority_{slot}_effect_not_completed")
        for field in (
            "slot",
            "role",
            "purpose",
            "invocation_id",
            "generation_binding_digest",
            "context_binding_digest",
            "checkpoint_stage",
            "checkpoint_revision",
            "actual_role",
            "model",
            "tools",
            "prompt_digest",
        ):
            if input_payload.get(field) != payload.get(field) or provider.get(
                field
            ) != payload.get(field):
                errors.append(f"strict_authority_{slot}_effect_{field}_mismatch")
        actual_role = str(payload.get("actual_role") or "")
        if not (
            actual_role == expected_role
            or actual_role.startswith(expected_role + " ")
        ):
            errors.append(f"strict_authority_{slot}_actual_role_mismatch")
        stored_context = input_payload.get("context_binding")
        if (
            not isinstance(stored_context, dict)
            or content_digest(stored_context)
            != payload.get("context_binding_digest")
        ):
            errors.append(f"strict_authority_{slot}_context_binding_invalid")
        if provider.get("provider_event_id") != provider_event_id:
            errors.append(f"strict_authority_{slot}_provider_event_mismatch")
        if provider.get("provider_session_id") != payload.get("provider_session_id"):
            errors.append(f"strict_authority_{slot}_provider_session_mismatch")
        if provider.get("provider_result_digest") is not None:
            errors.append(f"strict_authority_{slot}_provider_shape_invalid")
        result_subject = {key: value for key, value in provider.items() if key != "result_digest"}
        if provider.get("result_digest") != content_digest(result_subject):
            errors.append(f"strict_authority_{slot}_provider_digest_invalid")
        if provider.get("result_digest") != payload.get("provider_result_digest"):
            errors.append(f"strict_authority_{slot}_provider_result_mismatch")
        if provider.get("provider_is_error") is not False or provider.get(
            "provider_subtype"
        ) != "success":
            errors.append(f"strict_authority_{slot}_provider_not_success")
        raw_output = provider.get("raw_output")
        if not isinstance(raw_output, str) or provider.get(
            "raw_output_digest"
        ) != hashlib.sha256(str(raw_output).encode("utf-8")).hexdigest():
            errors.append(f"strict_authority_{slot}_raw_output_invalid")
        binding_mode = provider.get("mode")
        if binding_mode == "terminal_result_text":
            if provider.get("provider_result_output") != raw_output:
                errors.append(f"strict_authority_{slot}_terminal_output_mismatch")
        elif binding_mode == "terminal_structured_output":
            try:
                from llm_query import parse_json_output_with_mode

                parsed_raw, _mode = parse_json_output_with_mode(str(raw_output))
                if _json_value(parsed_raw) != _json_value(
                    provider.get("provider_structured_output")
                ):
                    errors.append(
                        f"strict_authority_{slot}_terminal_structured_mismatch"
                    )
            except Exception:
                errors.append(
                    f"strict_authority_{slot}_terminal_structured_invalid"
                )
        else:
            errors.append(f"strict_authority_{slot}_output_binding_mode_invalid")
        output_binding_subject = {
            "mode": binding_mode,
            "raw_output_digest": provider.get("raw_output_digest"),
            "terminal_result_output_digest": provider.get(
                "terminal_result_output_digest"
            ),
            "terminal_structured_output_digest": provider.get(
                "terminal_structured_output_digest"
            ),
        }
        if provider.get("output_binding_digest") != content_digest(
            output_binding_subject
        ):
            errors.append(f"strict_authority_{slot}_output_binding_digest_invalid")
        if provider.get("parse_contract") != SLOT_PARSE_CONTRACTS[slot]:
            errors.append(f"strict_authority_{slot}_projection_contract_invalid")
        if provider.get("role_projection_valid") is not True:
            errors.append(f"strict_authority_{slot}_role_projection_invalid")
        if provider.get("projected_role_result_digest") != content_digest(
            _json_value(provider.get("projected_role_result"))
        ):
            errors.append(f"strict_authority_{slot}_role_projection_digest_invalid")
        if not _valid_digest(payload.get("role_result_digest")):
            errors.append(f"strict_authority_{slot}_role_result_digest_invalid")
        if payload.get("role_result_digest") != content_digest(
            _json_value(payload.get("role_result"))
        ):
            errors.append(f"strict_authority_{slot}_stored_role_result_mismatch")
        if (
            provider.get("projected_role_result_digest")
            != payload.get("role_result_digest")
            or _json_value(provider.get("projected_role_result"))
            != _json_value(payload.get("role_result"))
        ):
            errors.append(f"strict_authority_{slot}_role_projection_mismatch")
        if slot in expected_role_results:
            expected_digest = content_digest(_json_value(expected_role_results[slot]))
            if payload.get("role_result_digest") != expected_digest:
                errors.append(f"strict_authority_{slot}_role_result_mismatch")
        if slot in expected_context_bindings:
            expected_context_digest = content_digest(
                _json_value(expected_context_bindings[slot])
            )
            if payload.get("context_binding_digest") != expected_context_digest:
                errors.append(f"strict_authority_{slot}_context_binding_mismatch")
        refs[slot] = {
            "schema_version": 1,
            "kind": RECEIPT_KIND,
            "run_id": run_id,
            "slot": slot,
            "effect_id": effect_id,
            "invocation_id": invocation_id,
            "event_seq": int(event.seq),
            "event_payload_digest": event.payload_digest,
            "receipt_digest": payload.get("receipt_digest"),
            "role_result_digest": payload.get("role_result_digest"),
        }
    for slot in required:
        count = len(by_slot.get(slot, []))
        if count != 1:
            errors.append(f"strict_authority_{slot}_accepted_count:{count}")
    if require_no_other_accepted:
        extras = sorted(set(by_slot) - set(required))
        if extras:
            errors.append("strict_authority_unexpected_accepted_slots:" + ",".join(extras))
    for phase_name, representative in phase_representatives.items():
        anchor = phase_anchors.get(phase_name)
        if anchor is None:
            continue
        _name, phase_slots = _authority_phase(representative)
        accepted_phase_revisions = {
            revisions[slot] for slot in phase_slots if slot in revisions
        }
        if accepted_phase_revisions and accepted_phase_revisions != {anchor}:
            errors.append(
                f"strict_authority_phase_checkpoint_revision_drift:{phase_name}"
            )
    master_revisions = {
        revisions[slot] for slot in MASTER_SLOTS if slot in revisions
    }
    if len(master_revisions) > 1:
        errors.append("strict_authority_master_checkpoint_revision_drift")
    if master_revisions:
        master_revision = next(iter(master_revisions))
        review_revision = revisions.get("review")
        critic_revision = revisions.get("critic")
        if review_revision is not None and review_revision < master_revision:
            errors.append("strict_authority_review_revision_precedes_master")
        if (
            review_revision is not None
            and critic_revision is not None
            and critic_revision < review_revision
        ):
            errors.append("strict_authority_critic_revision_precedes_review")
    return refs, list(dict.fromkeys(errors))


def validate_master_final_projection(
    checkpoint: dict[str, Any],
    plan: dict[str, Any],
    *,
    candidate_dir: str | Path,
    project_root: str | Path,
) -> tuple[dict[str, Any], list[str]]:
    """Replay the deterministic post-Master compiler and bind it to the journal.

    ``master:final`` is accepted before the system attaches the architecture
    policy, externalizes an oversized Worker prompt, and builds the runtime
    contract ledger.  The accepted payload itself is retained in WorkflowStore,
    so validation can replay those exact transformations in a private temporary
    tree.  This avoids a lossy "drop compiler fields" inverse and continues to
    work after the real ``.task_context`` directory has been removed.
    """

    if not isinstance(plan, dict):
        return {}, ["strict_authority_master_plan_missing"]
    expected_roles = expected_master_role_results(plan)
    refs, errors = validate_receipts(
        checkpoint,
        required_slots=MASTER_SLOTS,
        expected_role_results=expected_roles,
        expected_context_bindings=expected_master_contexts(plan),
    )
    if errors:
        return {}, errors

    accepted, journal_errors = _accepted_events(checkpoint)
    if journal_errors:
        return {}, journal_errors
    try:
        expected_invocations = expected_master_invocation_evidence(plan)
        for slot in MASTER_SLOTS[:5]:
            slot_events = [
                event
                for event in accepted
                if event.payload.get("slot") == slot
            ]
            if len(slot_events) != 1:
                continue
            bound = bound_invocation_evidence(
                dict(slot_events[0].payload)
            )
            if _json_value(bound) != _json_value(expected_invocations[slot]):
                errors.append(
                    f"strict_authority_{slot}_invocation_evidence_mismatch"
                )
    except StrictAuthorityError as exc:
        errors.extend(exc.errors)
    if errors:
        return {}, list(dict.fromkeys(errors))
    final_events = [
        event for event in accepted
        if event.payload.get("slot") == "master:final"
    ]
    if len(final_events) != 1:
        return {}, [
            f"strict_authority_master:final_accepted_count:{len(final_events)}"
        ]
    accepted_plan = final_events[0].payload.get("role_result")
    if not isinstance(accepted_plan, dict):
        return {}, ["strict_authority_master_final_role_result_invalid"]

    source_v = checkpoint.get("source_v")
    next_v = checkpoint.get("next_v")
    if not _plain_int(source_v) or not _plain_int(next_v):
        return {}, ["strict_authority_master_projection_version_invalid"]
    architecture_policy = plan.get("architecture_policy")
    if not isinstance(architecture_policy, dict):
        return {}, ["strict_authority_master_projection_policy_missing"]

    candidate_dir = Path(candidate_dir).resolve()
    project_root = Path(project_root).resolve()
    projection_errors: list[str] = []
    try:
        from plan_compiler import (
            bind_system_owned_policy_abi,
            bind_system_owned_worker_contract_terms,
            compile_master_plan,
        )
        from runtime_architecture_policy import attach_runtime_contract_ledger
        from tool_planning import _normalize_master_plan_paths
        import tempfile

        replay_plan = deepcopy(accepted_plan)
        replay_plan["architecture_policy"] = deepcopy(architecture_policy)
        replay_plan, _normalization = _normalize_master_plan_paths(
            replay_plan,
            int(source_v),
            int(next_v),
        )
        precompiled, _policy_abi = bind_system_owned_policy_abi(
            replay_plan
        )
        precompiled, _contract = bind_system_owned_worker_contract_terms(
            precompiled
        )

        with tempfile.TemporaryDirectory(prefix="pok-strict-master-replay-") as raw:
            replay_root = Path(raw).resolve()
            replay_target = replay_root / "candidate"
            replay_target.mkdir(parents=True, exist_ok=True)
            replayed, compiler = compile_master_plan(
                precompiled,
                next_v=int(next_v),
                target_dir=replay_target,
                project_root=replay_root,
            )
            compiled_rows = compiler.get("compiled_tasks") or []
            if any(row.get("context_trimmed") is not False for row in compiled_rows):
                projection_errors.append(
                    "strict_authority_master_projection_context_trimmed"
                )

            precompiled_tasks = {
                str(task.get("worker_id", index + 1)): task
                for index, task in enumerate(precompiled.get("tasks") or [])
                if isinstance(task, dict)
            }
            replayed_tasks = {
                str(task.get("worker_id", index + 1)): task
                for index, task in enumerate(replayed.get("tasks") or [])
                if isinstance(task, dict)
            }
            for row in compiled_rows:
                worker_key = str(row.get("worker_id"))
                generated_brief = str(row.get("brief_file") or "")
                source_task = precompiled_tasks.get(worker_key)
                compiled_task = replayed_tasks.get(worker_key)
                if (
                    not generated_brief
                    or not isinstance(source_task, dict)
                    or not isinstance(compiled_task, dict)
                    or compiled_task.get("task_brief_file") != generated_brief
                ):
                    projection_errors.append(
                        "strict_authority_master_projection_compiler_shape_invalid"
                    )
                    continue
                generated_path = replay_root / generated_brief
                try:
                    text = generated_path.read_text(encoding="utf-8")
                except Exception as exc:
                    projection_errors.append(
                        "strict_authority_master_projection_context_unreadable:"
                        + type(exc).__name__
                    )
                    continue
                marker = "## Original Worker Prompt\n\n"
                if (
                    "- trimmed: False\n" not in text
                    or marker not in text
                    or text.split(marker, 1)[1]
                    != str(source_task.get("worker_prompt") or "")
                    or row.get("original_chars")
                    != len(str(source_task.get("worker_prompt") or ""))
                ):
                    projection_errors.append(
                        "strict_authority_master_projection_context_mismatch"
                    )
                    continue

                actual_path = candidate_dir / ".task_context" / generated_path.name
                try:
                    actual_brief = str(actual_path.relative_to(project_root))
                except ValueError:
                    actual_brief = str(actual_path)
                compiled_prompt = str(compiled_task.get("worker_prompt") or "")
                if compiled_prompt.count(generated_brief) != 1:
                    projection_errors.append(
                        "strict_authority_master_projection_prompt_path_invalid"
                    )
                    continue
                compiled_task["task_brief_file"] = actual_brief
                compiled_task["worker_prompt"] = compiled_prompt.replace(
                    generated_brief,
                    actual_brief,
                    1,
                )
                row["brief_file"] = actual_brief

            # ``plan_compiler`` stores the same compiler payload, but do not
            # depend on object aliasing after a future refactor.
            if compiler.get("compiled"):
                replayed["plan_compiler"] = deepcopy(compiler)
            replayed = attach_runtime_contract_ledger(replayed, replace=True)

        if not projection_errors and _json_value(replayed) != _json_value(plan):
            projection_errors.append(
                "strict_authority_master_final_projection_mismatch"
            )
    except Exception as exc:
        projection_errors.append(
            "strict_authority_master_projection_error:"
            f"{type(exc).__name__}:{str(exc)[:300]}"
        )

    proof = {
        "schema_version": 1,
        "kind": "first-strict-master-final-projection-v1",
        "accepted_role_result_digest": final_events[0].payload.get(
            "role_result_digest"
        ),
        "compiled_plan_digest": content_digest(_json_value(plan)),
        "authority_receipt": refs.get("master:final"),
    }
    proof["projection_digest"] = content_digest(proof)
    return (proof if not projection_errors else {}), list(
        dict.fromkeys(projection_errors)
    )


def authority_summary(
    checkpoint: dict[str, Any],
    *,
    required_slots: Iterable[str],
    expected_role_results: dict[str, Any] | None = None,
    expected_context_bindings: dict[str, dict[str, Any]] | None = None,
    expected_invocation_evidence: dict[str, dict[str, Any]] | None = None,
    require_no_other_accepted: bool = False,
) -> dict[str, Any]:
    required_slots = tuple(required_slots)
    expected_invocation_evidence = expected_invocation_evidence or {}
    refs, errors = validate_receipts(
        checkpoint,
        required_slots=required_slots,
        expected_role_results=expected_role_results,
        expected_context_bindings=expected_context_bindings,
        require_no_other_accepted=require_no_other_accepted,
    )
    required_set = set(required_slots)
    invalid_expected_slots = set(expected_invocation_evidence) - (
        required_set & set(INVOCATION_EVIDENCE_SLOTS)
    )
    if invalid_expected_slots:
        errors.append(
            "strict_authority_expected_invocation_evidence_slots_invalid:"
            + ",".join(sorted(invalid_expected_slots))
        )
    bound_slots: list[str] = []
    if set(MASTER_SLOTS).issubset(required_set):
        bound_slots.extend(MASTER_SLOTS[:5])
    bound_slots.extend(slot for slot in GATE_SLOTS if slot in required_set)
    if not errors and bound_slots:
        accepted, journal_errors = _accepted_events(checkpoint)
        errors.extend(journal_errors)
        expected_invocations = dict(expected_invocation_evidence)
        final_context = (expected_context_bindings or {}).get("master:final")
        if isinstance(final_context, dict) and isinstance(
            final_context.get("proposal_packet"), dict
        ):
            try:
                expected_invocations.update(expected_master_invocation_evidence({
                    "proposal_ensemble": final_context["proposal_packet"],
                }))
            except StrictAuthorityError as exc:
                errors.extend(exc.errors)
        for slot in bound_slots:
            slot_events = [
                event
                for event in accepted
                if event.payload.get("slot") == slot
            ]
            if len(slot_events) != 1:
                continue
            try:
                bound = bound_invocation_evidence(
                    dict(slot_events[0].payload)
                )
                if bound is None:
                    errors.append(
                        f"strict_authority_{slot}_invocation_evidence_unbound"
                    )
                elif (
                    slot in expected_invocations
                    and _json_value(bound)
                    != _json_value(expected_invocations[slot])
                ):
                    errors.append(
                        f"strict_authority_{slot}_invocation_evidence_mismatch"
                    )
            except StrictAuthorityError as exc:
                errors.extend(exc.errors)
    if errors:
        raise StrictAuthorityError(errors)
    subject = {
        "schema_version": 1,
        "kind": "first-strict-llm-authority-summary-v1",
        "run_id": authority_run_id(str(checkpoint.get("workflow_run_id") or "")),
        "required_slots": list(required_slots),
        "receipts": {slot: refs[slot] for slot in required_slots},
    }
    return {**subject, "summary_digest": content_digest(subject)}


__all__ = [
    "ALL_SLOTS",
    "GATE_SLOTS",
    "INVOCATION_EVIDENCE_SLOTS",
    "MASTER_SLOTS",
    "StrictAuthorityError",
    "accept_role_result",
    "abandon_authority",
    "authority_run_id",
    "authority_summary",
    "bind_invocation_evidence",
    "bind_master_invocation_evidence",
    "bound_invocation_evidence",
    "bound_master_invocation_evidence",
    "canonical_provider_output",
    "complete_provider_call",
    "dispatch_call",
    "fail_provider_call",
    "generation_binding",
    "gate_call_context",
    "gate_provider_evidence_snapshot_dir",
    "expected_master_contexts",
    "expected_master_invocation_evidence",
    "expected_master_role_results",
    "final_master_call_context",
    "new_call",
    "ballot_call_context",
    "proposal_call_context",
    "record_bound_invocation_evidence",
    "render_gate_provider_prompt",
    "reject_duplicate_proposal",
    "schema_retry_prompt",
    "strict_invocation_log_path",
    "validate_master_final_projection",
    "validate_receipts",
]
