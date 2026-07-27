"""Fenced provider and schema receipts for strict bootstrap generations.

The first strict generation and a singleton-parent successor both run without
a normal frozen strength snapshot. A checkpoint flag or an ``*_io.txt`` file
cannot prove that their Master/Reviewer/Critic was really executed. This
module therefore records each provider dispatch as a
``WorkflowStore`` effect and records deterministic schema acceptance as a
separate domain event.

Several on-disk kind strings retain the historical ``first-strict`` name for
schema compatibility with already published v143 receipts. That spelling is
an immutable storage identity, not a restriction on generation scope.

The authority stream deliberately uses a run id distinct from the Worker
stream while sharing ``RESULTS_DIR/workflow/events.sqlite3``::

    {workflow_run_id}:strict-authority-v3

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

import logging
_log = logging.getLogger("pok.strict_authority")

from claude_agent_sdk import ResultMessage
from workflow_kernel import WorkflowConflict, WorkflowStore, content_digest

import strict_authority_receipts as _sr  # noqa: E402  (circular by design)

# Generation/call-context builder and gate prompt-rendering companion.  Hosts
# the generation-binding, proposal/ballot/master-final call-context builders,
# expected-master-context/role/evidence derivation, the gate renderer semantic
# contract and the gate provider prompt rendering.  The companion imports
# ``strict_authority_workflow`` itself (``import strict_authority_call_context
# as _cc`` is aliased here as ``_cc``) and resolves cross-references lazily at
# call time, so this top-level import does not create a load-time cycle.  Every
# moved symbol is re-exposed below as a thin delegate shell so legacy
# ``from strict_authority_workflow import <name>`` sites and
# ``strict_authority_workflow.<name>`` monkeypatches keep working.
import strict_authority_call_context as _cc  # noqa: E402  (circular by design)


DEFINITION_VERSION = 3
RUN_SUFFIX = "strict-authority-v3"
EFFECT_KIND = "first-strict-llm-provider-call-v3"
ACCEPTED_EVENT = "StrictRoleAccepted"
REJECTED_EVENT = "StrictRoleRejected"
INVOCATION_EVIDENCE_BOUND_EVENT = "StrictInvocationEvidenceBound"
RECEIPT_KIND = "first-strict-llm-authority-receipt-v3"
INVOCATION_EVIDENCE_BINDING_KIND = (
    "first-strict-invocation-evidence-binding-v3"
)
MAX_SCHEMA_ATTEMPTS_PER_SLOT = 2

LEGACY_REVIEW_TERMINAL_MIGRATION_KIND = (
    "first-strict-review-terminal-semantic-migration-v1"
)
_LEGACY_REVIEW_SEMANTIC_INPUT_KEYS = frozenset({
    "focus_areas",
    "master_plan",
    "next_v",
    "source_v",
    "strict_bootstrap",
})
_CURRENT_REVIEW_SEMANTIC_INPUT_KEYS = (
    _LEGACY_REVIEW_SEMANTIC_INPUT_KEYS | {"review_semantic_contract"}
)
_RENDERER_SEMANTIC_CONTRACT_KEYS = frozenset({
    "schema_version",
    "role",
    "invocation_normalization",
    "semantic_inputs",
    "semantic_inputs_digest",
    "renderer_static_identity",
    "renderer_static_identity_digest",
    "sentinel_rendered_prompt_sha256",
    "sentinel_rendered_prompt_chars",
    "sentinel_evidence_kind",
    "sentinel_evidence_provenance_sha256",
    "sentinel_renderer_receipt_digest",
    "sentinel_evidence_receipt_digest",
    "sentinel_dispatch_receipt_digest",
    "contract_digest",
})
_RENDERER_STATIC_IDENTITY_KEYS = frozenset({
    "producer_file",
    "producer_name",
    "producer_file_sha256",
    "producer_function_sha256",
    "template_digests",
})

MASTER_SLOTS = (
    "proposal:mechanism",
    "proposal:counterfactual",
    "proposal:compute_memory",
    "ballot:falsification",
    "ballot:scope",
    "master:final",
)
GATE_SLOTS = ("review", "critic", "review:retry")
# The canonical success path remains the historical eight ordered slots.
# ``review:retry`` is an optional, mutually-exclusive rejection branch and is
# therefore known authority without becoming a required success-path suffix.
ALL_SLOTS = MASTER_SLOTS + ("review", "critic")
KNOWN_SLOTS = ALL_SLOTS + ("review:retry",)
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
    "review:retry": (
        "LEAD CODE REVIEWER",
        "system_strict_bootstrap_gate:review:retry",
    ),
    "critic": (
        "STRATEGY CRITIC",
        "system_strict_bootstrap_gate:critic",
    ),
}
SLOT_STAGES = {
    **{slot: "direction_audited" for slot in MASTER_SLOTS},
    "review": "quality_passed",
    "review:retry": "quality_passed",
    "critic": "reviewed",
}
SLOT_PARSE_CONTRACTS = {
    **{slot: "master-proposal-v4" for slot in MASTER_SLOTS[:3]},
    **{slot: "master-proposal-ballot-v1" for slot in MASTER_SLOTS[3:5]},
    "master:final": "master-plan-schema-v1",
    "review": "reviewer-output-schema-v1",
    "review:retry": "reviewer-output-schema-v1",
    "critic": "critic-output-schema-v1",
}
SLOT_TOOLS = {
    **{slot: ["Read"] for slot in MASTER_SLOTS[:3]},
    **{slot: [] for slot in MASTER_SLOTS[3:5]},
    "master:final": [],
    "review": ["Read"],
    "review:retry": ["Read"],
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


def strict_authority_abandon_event_identity(
    checkpoint: dict[str, Any],
    *,
    reason: str,
) -> tuple[dict[str, str], str]:
    """Return the sole exact strict-authority terminal event identity."""

    workflow_run_id = str((checkpoint or {}).get("workflow_run_id") or "")
    run_id = authority_run_id(workflow_run_id)
    payload = {
        "reason": str(reason)[:1000],
        "workflow_run_id": workflow_run_id,
    }
    return (
        payload,
        f"strict-authority-abandoned:{run_id}:{content_digest(payload)}",
    )


def abandon_authority(
    checkpoint: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    """Fence the strict child journal when its generation is abandoned."""

    run_id = authority_run_id(str((checkpoint or {}).get("workflow_run_id") or ""))
    store = _store()
    payload, causation_id = strict_authority_abandon_event_identity(
        checkpoint,
        reason=reason,
    )
    instance = store.instance(run_id)
    if not instance:
        # A descriptor that never reached dispatch has no instance.  Create
        # its tombstone and sole terminal event in one SQLite transaction so a
        # crash cannot leave an unbound reason-less abandoned row.
        try:
            store.create_terminal_transition(
                run_id,
                definition_version=DEFINITION_VERSION,
                event_type="StrictAuthorityAbandoned",
                payload=payload,
                causation_id=causation_id,
                status="abandoned",
            )
        except WorkflowConflict:
            # A concurrent owner may have completed the identical transition;
            # the exact validator below decides whether it is reusable.
            pass
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
    if not terminal_events:
        history = store.events(run_id)
        effects = store.effects_for_run(run_id)
        if not history and not effects:
            exact_empty_instance = bool(
                int(instance.get("definition_version") or -1)
                == DEFINITION_VERSION
                and int(instance.get("stream_version", -1)) == 0
                and int(instance.get("fence_epoch", -1)) == 0
            )
            if instance.get("status") == "abandoned":
                outcome = (checkpoint or {}).get("terminal_gate_outcome") or {}
                receipt_digest = str(outcome.get("receipt_digest") or "")
                expected_reason = f"terminal_gate_outcome:{receipt_digest}"
                exact_legacy_tombstone = bool(
                    exact_empty_instance
                    and checkpoint.get("stage")
                    in {
                        "quality_rejected",
                        "review_rejected",
                        "critic_rejected",
                    }
                    and outcome.get("workflow_run_id")
                    == checkpoint.get("workflow_run_id")
                    and outcome.get("terminal_stage")
                    == checkpoint.get("stage")
                    and len(receipt_digest) == 64
                    and all(
                        char in "0123456789abcdef"
                        for char in receipt_digest
                    )
                    and str(reason) == expected_reason
                )
                if not exact_legacy_tombstone:
                    raise StrictAuthorityError(
                        "strict_authority_abandon_tombstone_invalid"
                    )
            elif not (
                instance.get("status") == "running"
                and exact_empty_instance
            ):
                raise StrictAuthorityError(
                    "strict_authority_abandon_tombstone_invalid"
                )
        elif instance.get("status") == "abandoned":
            raise StrictAuthorityError(
                "strict_authority_abandon_tombstone_invalid"
            )
        try:
            store.terminal_transition(
                run_id,
                event_type="StrictAuthorityAbandoned",
                payload=payload,
                causation_id=causation_id,
                expected_version=int(instance["stream_version"]),
                status="abandoned",
            )
        except WorkflowConflict:
            current = store.instance(run_id)
            if current.get("status") != "abandoned":
                raise
    current = _validated_strict_abandon_fence(
        checkpoint,
        reason=str(reason),
        store=store,
    )
    return {
        "run_id": run_id,
        "present": True,
        "abandoned": True,
        "fence_epoch": int(current.get("fence_epoch") or 0),
        "stream_version": int(current.get("stream_version") or 0),
    }


def _validated_strict_abandon_fence(
    checkpoint: dict[str, Any],
    *,
    reason: str,
    store: WorkflowStore | None = None,
) -> dict[str, Any]:
    """Reprove the exact strict-authority terminal fence without reopening it."""

    workflow_run_id = str((checkpoint or {}).get("workflow_run_id") or "")
    run_id = authority_run_id(workflow_run_id)
    store = store or _store()
    instance = store.instance(run_id)
    events = store.events(run_id)
    terminal = [
        event
        for event in events
        if event.event_type == "StrictAuthorityAbandoned"
    ]
    expected_payload, expected_causation_id = (
        strict_authority_abandon_event_identity(
            checkpoint,
            reason=reason,
        )
    )
    if (
        int(instance.get("definition_version") or -1) != DEFINITION_VERSION
        or instance.get("status") != "abandoned"
        or int(instance.get("fence_epoch") or 0) < 1
        or len(terminal) != 1
        or terminal[0].seq != len(events)
        or int(instance.get("stream_version") or -1) != terminal[0].seq
        or terminal[0].schema_version != 1
        or terminal[0].payload != expected_payload
        or terminal[0].causation_id != expected_causation_id
    ):
        raise StrictAuthorityError(
            "strict_authority_abandon_fence_identity_invalid"
        )
    for event in events:
        if event.event_type != "EffectRequested":
            continue
        effect_id = str(event.payload.get("effect_id") or "")
        effect = store.effect(effect_id)
        if (
            not effect_id
            or effect.get("run_id") != run_id
            or effect.get("status")
            not in {"completed", "exhausted", "abandoned"}
        ):
            raise StrictAuthorityError(
                "strict_authority_abandon_fence_effect_live"
            )
    return instance


def generation_binding(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Delegate to strict_authority_call_context."""
    return _cc.generation_binding(checkpoint)


def proposal_call_context(
    *,
    context_digest: str,
    source_code_digest: str,
    direction: str,
    allowed_primaries: Iterable[str] | None = None,
    evidence_mode: str | None = None,
) -> dict[str, Any]:
    """Delegate to strict_authority_call_context."""
    return _cc.proposal_call_context(
        context_digest=context_digest,
        source_code_digest=source_code_digest,
        direction=direction,
        allowed_primaries=allowed_primaries,
        evidence_mode=evidence_mode,
    )


def _architecture_proposal_primaries(
    architecture_policy: dict[str, Any] | None,
) -> tuple[str, ...] | None:
    """Delegate to strict_authority_call_context."""
    return _cc._architecture_proposal_primaries(architecture_policy)


def ballot_call_context(
    *,
    context_digest: str,
    source_code_digest: str,
    critic_id: str,
    proposal_ids: Iterable[str],
    critic_criteria: dict[str, Any],
) -> dict[str, Any]:
    """Delegate to strict_authority_call_context."""
    return _cc.ballot_call_context(
        context_digest=context_digest,
        source_code_digest=source_code_digest,
        critic_id=critic_id,
        proposal_ids=proposal_ids,
        critic_criteria=critic_criteria,
    )


def final_master_call_context(
    proposal_packet: dict[str, Any],
    architecture_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Delegate to strict_authority_call_context."""
    return _cc.final_master_call_context(proposal_packet, architecture_policy)


def expected_master_contexts(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Delegate to strict_authority_call_context."""
    return _cc.expected_master_contexts(plan)


def expected_master_role_results(plan: dict[str, Any]) -> dict[str, Any]:
    """Delegate to strict_authority_call_context."""
    return _cc.expected_master_role_results(plan)


def expected_master_invocation_evidence(
    plan: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Delegate to strict_authority_call_context."""
    return _cc.expected_master_invocation_evidence(plan)


# Re-export the gate-render invocation sentinel and the first-strict no-strength
# Critic contract from the call-context companion so legacy
# ``strict_authority_workflow._GATE_RENDER_INVOCATION_SENTINEL`` /
# ``strict_authority_workflow._STRICT_CRITIC_NO_STRENGTH_CONTRACT`` references
# keep resolving.  These are resolved lazily via __getattr__ below to avoid a
# circular-import crash when strict_authority_call_context is imported first
# (it imports strict_authority_workflow at its own module-init, and these
# constants are not yet defined on the companion at that point).


def __getattr__(name: str):
    if name == "_GATE_RENDER_INVOCATION_SENTINEL":
        return _cc._GATE_RENDER_INVOCATION_SENTINEL
    if name == "_STRICT_CRITIC_NO_STRENGTH_CONTRACT":
        return _cc._STRICT_CRITIC_NO_STRENGTH_CONTRACT
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _normalized_reviewer_focus_areas(
    checkpoint: dict[str, Any],
) -> list[str]:
    """Delegate to strict_authority_call_context."""
    return _cc._normalized_reviewer_focus_areas(checkpoint)


def _gate_renderer_components(gate_name: str):
    """Delegate to strict_authority_call_context."""
    return _cc._gate_renderer_components(gate_name)


def _render_registered_gate_prompt(
    gate_name: str,
    renderer_inputs: dict[str, Any],
):
    """Delegate to strict_authority_call_context."""
    return _cc._render_registered_gate_prompt(gate_name, renderer_inputs)


def _gate_renderer_semantic_contract(
    gate_name: str,
    semantic_inputs: dict[str, Any],
) -> dict[str, Any]:
    """Delegate to strict_authority_call_context."""
    return _cc._gate_renderer_semantic_contract(gate_name, semantic_inputs)


def _gate_semantic_inputs(
    checkpoint: dict[str, Any],
    *,
    gate_name: str,
    candidate_dir: Path,
    candidate_artifact_hash: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Delegate to strict_authority_call_context."""
    return _cc._gate_semantic_inputs(
        checkpoint,
        gate_name=gate_name,
        candidate_dir=candidate_dir,
        candidate_artifact_hash=candidate_artifact_hash,
    )


def gate_call_context(
    checkpoint: dict[str, Any],
    *,
    gate_name: str,
    candidate_dir: str | Path,
) -> dict[str, Any]:
    """Delegate to strict_authority_call_context."""
    return _cc.gate_call_context(
        checkpoint,
        gate_name=gate_name,
        candidate_dir=candidate_dir,
    )


def render_gate_provider_prompt(call: dict[str, Any]):
    """Delegate to strict_authority_call_context."""
    return _cc.render_gate_provider_prompt(call)


def gate_provider_evidence_snapshot_dir(
    call: dict[str, Any],
) -> Path | None:
    """Delegate to strict_authority_call_context."""
    return _cc.gate_provider_evidence_snapshot_dir(call)


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


def recover_accepted_master_final_result(
    checkpoint: dict[str, Any],
    *,
    architecture_policy: dict[str, Any],
) -> dict[str, Any] | None:
    """Recover a sealed final-Master projection before rebuilding scouts.

    The final Master result is already an append-only authority once accepted.
    A duplicate outer ``run_master`` entry must not re-render the Scout packet
    and then compare a newly assembled packet against the sealed final slot:
    that is wasteful and can turn an otherwise valid recovery into a context
    drift.  Reconstruct the exact expected context from the sealed role result
    and current system-owned architecture policy, then reuse the ordinary
    descriptor recovery path.  Any malformed role result or policy drift still
    fails closed before a provider call is possible.
    """

    if not isinstance(architecture_policy, dict) or not architecture_policy:
        raise StrictAuthorityError(
            "strict_authority_master_final_recovery_policy_missing"
        )
    binding = generation_binding(checkpoint)
    run_id = authority_run_id(binding["workflow_run_id"])
    store = _store()
    try:
        instance = store.instance(run_id)
        if not instance:
            return None
        if instance.get("status") == "abandoned":
            raise StrictAuthorityError(
                "strict_authority_master_final_recovery_journal_abandoned"
            )
        final_events = [
            event
            for event in store.events(run_id)
            if event.event_type == ACCEPTED_EVENT
            and event.payload.get("slot") == "master:final"
        ]
    except StrictAuthorityError:
        raise
    except Exception as exc:
        raise StrictAuthorityError(
            "strict_authority_master_final_recovery_journal_unavailable:"
            f"{type(exc).__name__}"
        ) from exc
    if not final_events:
        return None
    if len(final_events) != 1:
        raise StrictAuthorityError(
            "strict_authority_master:final_accepted_count:"
            f"{len(final_events)}"
        )
    role_result = final_events[0].payload.get("role_result")
    if not isinstance(role_result, dict):
        raise StrictAuthorityError(
            "strict_authority_master_final_recovery_role_result_invalid"
        )
    proposal_packet = role_result.get("proposal_ensemble")
    if not isinstance(proposal_packet, dict):
        raise StrictAuthorityError(
            "strict_authority_master_final_recovery_packet_missing"
        )
    # ``architecture_policy`` is a system-owned dispatch input.  It is bound
    # into the final call context but intentionally is not copied into the
    # provider's accepted role result.  Reattach the current policy before
    # rebuilding all six expected contexts; otherwise a complete, valid
    # journal is spuriously rejected as a final-context mismatch on recovery.
    projection_plan = deepcopy(role_result)
    projection_plan["architecture_policy"] = deepcopy(architecture_policy)
    _refs, packet_errors = validate_receipts(
        checkpoint,
        required_slots=MASTER_SLOTS,
        expected_role_results=expected_master_role_results(projection_plan),
        expected_context_bindings=expected_master_contexts(projection_plan),
        require_no_other_accepted=True,
    )
    if packet_errors:
        raise StrictAuthorityError(packet_errors)
    try:
        expected_invocations = expected_master_invocation_evidence(
            projection_plan
        )
        accepted_events = [
            event
            for event in store.events(run_id)
            if event.event_type == ACCEPTED_EVENT
        ]
        for slot in MASTER_SLOTS[:5]:
            slot_events = [
                event
                for event in accepted_events
                if event.payload.get("slot") == slot
            ]
            if len(slot_events) != 1:
                raise StrictAuthorityError(
                    f"strict_authority_{slot}_accepted_count:{len(slot_events)}"
                )
            bound = bound_invocation_evidence(dict(slot_events[0].payload))
            if _json_value(bound) != _json_value(expected_invocations[slot]):
                raise StrictAuthorityError(
                    f"strict_authority_{slot}_invocation_evidence_mismatch"
                )
    except StrictAuthorityError:
        raise
    except Exception as exc:
        raise StrictAuthorityError(
            "strict_authority_master_final_recovery_invocation_evidence_"
            f"unavailable:{type(exc).__name__}"
        ) from exc
    descriptor = new_call(
        checkpoint,
        slot="master:final",
        context_binding=final_master_call_context(
            proposal_packet,
            architecture_policy,
        ),
    )
    if (
        descriptor.get("replay_provider") is not True
        or not isinstance(descriptor.get("accepted_role_result"), dict)
    ):
        raise StrictAuthorityError(
            "strict_authority_master_final_recovery_descriptor_invalid"
        )
    return deepcopy(descriptor["accepted_role_result"])


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


def _legacy_review_terminal_migration_contract(
    recorded_semantics: dict[str, Any],
    current_semantics: dict[str, Any] | None,
    *,
    legacy_projection: dict[str, Any] | None = None,
    legacy_quality_gate_digest: str | None = None,
) -> dict[str, Any] | None:
    """Bind the sole old-five-input -> current-review rejection migration.

    The old provider prompt did not contain ``review_semantic_contract``.  It
    can therefore never authorize approval under the new semantics.  For an
    already completed rejection only, the system may prove that removing this
    one newly-added field from the current source-owned projection recreates
    the recorded semantic inputs byte-for-byte.  The historical renderer and
    template identity remain bound by their original digests.
    """

    if not isinstance(recorded_semantics, dict):
        return None
    recorded_inputs = recorded_semantics.get("semantic_inputs")
    if (
        set(recorded_semantics) != _RENDERER_SEMANTIC_CONTRACT_KEYS
        or recorded_semantics.get("schema_version") != 1
        or recorded_semantics.get("role") != "LEAD CODE REVIEWER"
        or not isinstance(recorded_inputs, dict)
        or set(recorded_inputs) != _LEGACY_REVIEW_SEMANTIC_INPUT_KEYS
        or recorded_semantics.get("semantic_inputs_digest")
        != content_digest(recorded_inputs)
    ):
        return None

    recorded_static = recorded_semantics.get("renderer_static_identity")
    if (
        not isinstance(recorded_static, dict)
        or set(recorded_static) != _RENDERER_STATIC_IDENTITY_KEYS
        or recorded_static.get("producer_file") != "web/core/tool_gates.py"
        or recorded_static.get("producer_name")
        != "_render_reviewer_provider_prompt"
        or recorded_semantics.get("renderer_static_identity_digest")
        != content_digest(recorded_static)
        or recorded_semantics.get("contract_digest")
        != content_digest({
            key: value for key, value in recorded_semantics.items()
            if key != "contract_digest"
        })
    ):
        return None
    digest_fields = (
        "producer_file_sha256",
        "producer_function_sha256",
    )
    if any(not _valid_digest(recorded_static.get(field)) for field in digest_fields):
        return None
    template_digests = recorded_static.get("template_digests")
    if (
        not isinstance(template_digests, list)
        or len(template_digests) != 1
        or not isinstance(template_digests[0], list)
        or len(template_digests[0]) != 2
        or template_digests[0][0]
        != "web/core/prompts/reviewer_prompt.md"
        or not _valid_digest(template_digests[0][1])
    ):
        return None
    for field in (
        "sentinel_rendered_prompt_sha256",
        "sentinel_evidence_provenance_sha256",
        "sentinel_renderer_receipt_digest",
        "sentinel_evidence_receipt_digest",
        "sentinel_dispatch_receipt_digest",
    ):
        if not _valid_digest(recorded_semantics.get(field)):
            return None
    if (
        recorded_semantics.get("invocation_normalization")
        != "fixed-32-byte-sentinel-v1"
        or recorded_semantics.get("sentinel_evidence_kind")
        != "review_candidate_pair"
        or not _plain_int(recorded_semantics.get("sentinel_rendered_prompt_chars"))
        or int(recorded_semantics["sentinel_rendered_prompt_chars"]) <= 0
    ):
        return None

    review_contract_digest = None
    semantic_upgrade_status = "current_review_contract_available"
    if isinstance(current_semantics, dict):
        current_inputs = current_semantics.get("semantic_inputs")
        if (
            set(current_semantics) != _RENDERER_SEMANTIC_CONTRACT_KEYS
            or current_semantics.get("schema_version") != 1
            or current_semantics.get("role") != "LEAD CODE REVIEWER"
            or not isinstance(current_inputs, dict)
            or set(current_inputs) != _CURRENT_REVIEW_SEMANTIC_INPUT_KEYS
            or current_semantics.get("semantic_inputs_digest")
            != content_digest(current_inputs)
        ):
            return None
        derived_legacy_projection = {
            key: deepcopy(value)
            for key, value in current_inputs.items()
            if key != "review_semantic_contract"
        }
        if derived_legacy_projection != recorded_inputs:
            return None
        review_contract = current_inputs.get("review_semantic_contract")
        if not isinstance(review_contract, dict) or review_contract.get(
            "contract_digest"
        ) != content_digest({
            key: value for key, value in review_contract.items()
            if key != "contract_digest"
        }):
            return None
        review_contract_digest = review_contract["contract_digest"]
        legacy_projection = derived_legacy_projection
    else:
        semantic_upgrade_status = "unavailable_from_legacy_quality_gate"
        current_inputs = legacy_projection
        if (
            not isinstance(current_inputs, dict)
            or set(current_inputs) != _LEGACY_REVIEW_SEMANTIC_INPUT_KEYS
            or current_inputs != recorded_inputs
            or not _valid_digest(legacy_quality_gate_digest)
        ):
            return None

    subject = {
        "schema_version": 1,
        "kind": LEGACY_REVIEW_TERMINAL_MIGRATION_KIND,
        "disposition": "terminal_rejection_only",
        "semantic_upgrade_status": semantic_upgrade_status,
        "recorded_semantic_inputs_digest": content_digest(recorded_inputs),
        "current_semantic_inputs_digest": content_digest(current_inputs),
        "legacy_projection_digest": content_digest(legacy_projection),
        "current_review_semantic_contract_digest": review_contract_digest,
        "legacy_quality_gate_digest": legacy_quality_gate_digest,
        "recorded_renderer_contract_digest": recorded_semantics[
            "contract_digest"
        ],
        "recorded_renderer_static_identity_digest": recorded_semantics[
            "renderer_static_identity_digest"
        ],
        "recorded_producer_file_sha256": recorded_static[
            "producer_file_sha256"
        ],
        "recorded_producer_function_sha256": recorded_static[
            "producer_function_sha256"
        ],
        "recorded_template_digests_digest": content_digest(template_digests),
    }
    return {**subject, "migration_digest": content_digest(subject)}


def _legacy_review_terminal_checkpoint_projection(
    checkpoint: dict[str, Any],
    *,
    candidate_dir: str | Path,
) -> tuple[dict[str, Any], dict[str, Any], str] | None:
    """Rebuild the old five-input context from an exact legacy checkpoint."""

    from bot_artifact import hash_path
    from system_strict_bootstrap import is_declared_native_bootstrap

    if not is_declared_native_bootstrap(checkpoint):
        return None
    master_plan = checkpoint.get("master_plan")
    quality = ((checkpoint.get("gate_results") or {}).get("quality") or {})
    audit = checkpoint.get("audit_context") or {}
    master_receipt = audit.get("system_strict_bootstrap") or {}
    binding = master_plan.get("proposal_binding") if isinstance(master_plan, dict) else None
    if (
        not isinstance(master_plan, dict)
        or not isinstance(quality, dict)
        or not isinstance(binding, dict)
        or binding.get("execution_mode") != "fixed_blueprint_capability_audit"
        or "selected_proposal_quality_evidence" in quality
        or "selected_proposal_quality_ok" in quality
        or quality.get("all_passed") is not True
        or quality.get("critical_scenarios_passed") is not True
        or quality.get("passed") is not True
        or not _valid_digest(master_receipt.get("receipt_digest"))
        or not _valid_digest(master_receipt.get("plan_digest"))
    ):
        return None
    candidate_hash = hash_path(Path(candidate_dir))
    if (
        not _valid_digest(candidate_hash)
        or quality.get("code_fingerprint") != candidate_hash
    ):
        return None
    semantic_inputs = {
        "master_plan": _json_value(master_plan),
        "source_v": int(checkpoint["source_v"]),
        "next_v": int(checkpoint["next_v"]),
        "strict_bootstrap": True,
        "focus_areas": _normalized_reviewer_focus_areas(checkpoint),
    }
    quality_digest = content_digest(quality)
    context_subject = {
        "phase": "review",
        "candidate_artifact_hash": candidate_hash,
        "quality_gate_digest": quality_digest,
        "master_receipt_digest": master_receipt["receipt_digest"],
        "master_plan_digest": master_receipt["plan_digest"],
    }
    return context_subject, semantic_inputs, quality_digest


def recover_terminal_gate_rejection_call(
    checkpoint: dict[str, Any],
    *,
    gate_name: str,
    candidate_dir: str | Path,
) -> dict[str, Any] | None:
    """Recover one old-code schema-valid rejection without a provider replay.

    This is deliberately one-way reconciliation.  A changed renderer source
    identity may be ignored only after the durable provider projection is a
    rejection (Reviewer ``approved is False``).  Every semantic input,
    candidate/quality/master digest, workflow binding, completed-effect digest,
    and accepted-event digest remains exact.  An approval is never returned
    through this path and therefore can never be promoted under changed gate
    code.
    """

    if gate_name != "review":
        raise StrictAuthorityError(
            "strict_authority_terminal_recovery_gate_not_supported"
        )
    if str((checkpoint or {}).get("stage") or "") != "quality_passed":
        raise StrictAuthorityError(
            "strict_authority_terminal_recovery_stage_invalid"
        )
    binding = generation_binding(checkpoint)
    run_id = authority_run_id(binding["workflow_run_id"])
    legacy_checkpoint_projection = None
    try:
        current_context = gate_call_context(
            checkpoint,
            gate_name=gate_name,
            candidate_dir=Path(candidate_dir),
        )
    except StrictAuthorityError as exc:
        if exc.errors != (
            "strict_authority_review_semantic_contract_invalid",
        ):
            raise
        legacy_checkpoint_projection = (
            _legacy_review_terminal_checkpoint_projection(
                checkpoint,
                candidate_dir=candidate_dir,
            )
        )
        if legacy_checkpoint_projection is None:
            raise
        current_context = None
    store = _store()
    if not store.instance(run_id):
        return None
    events = store.events(run_id)
    effect_ids = list(dict.fromkeys(
        str(event.payload.get("effect_id") or "")
        for event in events
        if event.event_type == "StrictProviderResultObserved"
        and event.payload.get("slot") == gate_name
    ))
    matches: list[dict[str, Any]] = []
    for effect_id in effect_ids:
        if not effect_id:
            continue
        effect = store.effect(effect_id)
        input_payload = effect.get("input_payload") or {}
        provider = effect.get("result_payload") or {}
        recorded_context = input_payload.get("context_binding")
        projected = provider.get("projected_role_result")
        if (
            effect.get("status") != "completed"
            or effect.get("kind") != EFFECT_KIND
            or effect.get("run_id") != run_id
            or input_payload.get("slot") != gate_name
            or input_payload.get("role") != SLOT_CONTRACTS[gate_name][0]
            or input_payload.get("purpose") != SLOT_CONTRACTS[gate_name][1]
            or input_payload.get("generation_binding") != binding
            or input_payload.get("generation_binding_digest")
            != content_digest(binding)
            or input_payload.get("checkpoint_stage") != "quality_passed"
            or not _plain_int(input_payload.get("checkpoint_revision"))
            or int(input_payload["checkpoint_revision"])
            > int(checkpoint.get("checkpoint_revision") or -1)
            or not isinstance(recorded_context, dict)
            or input_payload.get("context_binding_digest")
            != content_digest(recorded_context)
            or not isinstance(projected, dict)
            or projected.get("approved") is not False
            or provider.get("role_projection_valid") is not True
        ):
            continue

        # Renderer implementation identity can rotate when the repair itself
        # lands.  Only the normalized role inputs may bridge that change.
        recorded_semantics = recorded_context.get("renderer_semantics") or {}
        if current_context is not None:
            current_semantics = current_context.get("renderer_semantics") or {}
            context_mismatch = any(
                recorded_context.get(key) != current_context.get(key)
                for key in set(recorded_context) | set(current_context)
                if key != "renderer_semantics"
            )
            exact_semantics = not any(
                recorded_semantics.get(key) != current_semantics.get(key)
                for key in (
                    "schema_version",
                    "role",
                    "semantic_inputs",
                    "semantic_inputs_digest",
                )
            )
            migration_contract = (
                None
                if exact_semantics
                else _legacy_review_terminal_migration_contract(
                    recorded_semantics,
                    current_semantics,
                )
            )
        else:
            legacy_subject, legacy_inputs, legacy_quality_digest = (
                legacy_checkpoint_projection
            )
            context_mismatch = any(
                recorded_context.get(key) != legacy_subject.get(key)
                for key in set(recorded_context) | set(legacy_subject)
                if key != "renderer_semantics"
            )
            exact_semantics = False
            migration_contract = _legacy_review_terminal_migration_contract(
                recorded_semantics,
                None,
                legacy_projection=legacy_inputs,
                legacy_quality_gate_digest=legacy_quality_digest,
            )
        if context_mismatch or (not exact_semantics and migration_contract is None):
            continue
        provider_subject = {
            key: value for key, value in provider.items()
            if key != "result_digest"
        }
        if (
            provider.get("result_digest") != content_digest(provider_subject)
            or provider.get("projected_role_result_digest")
            != content_digest(_json_value(projected))
            or provider.get("raw_output_digest")
            != hashlib.sha256(
                str(provider.get("raw_output") or "").encode("utf-8")
            ).hexdigest()
        ):
            raise StrictAuthorityError(
                "strict_authority_terminal_recovery_provider_invalid"
            )
        descriptor = {
            "schema_version": 1,
            "run_id": run_id,
            "slot": gate_name,
            "role": input_payload["role"],
            "purpose": input_payload["purpose"],
            "invocation_id": input_payload.get("invocation_id"),
            "generation_binding": deepcopy(binding),
            "generation_binding_digest": input_payload[
                "generation_binding_digest"
            ],
            "checkpoint_stage": input_payload["checkpoint_stage"],
            "checkpoint_revision": int(input_payload["checkpoint_revision"]),
            "context_binding": deepcopy(recorded_context),
            "context_binding_digest": input_payload["context_binding_digest"],
        }
        recovered = _recover_accepted_call(descriptor)
        if recovered is None:
            recovered = _recover_completed_unaccepted_call(descriptor)
        if recovered is None:
            raise StrictAuthorityError(
                "strict_authority_terminal_recovery_effect_unrecoverable"
            )
        recovered_result = (
            recovered.get("accepted_role_result")
            or recovered.get("projected_role_result")
        )
        if not isinstance(recovered_result, dict) or recovered_result.get(
            "approved"
        ) is not False:
            raise StrictAuthorityError(
                "strict_authority_terminal_recovery_not_rejection"
            )
        recovered["terminal_reconciliation"] = True
        if migration_contract is not None:
            recovered["terminal_semantic_migration"] = migration_contract
        matches.append(recovered)
    if not matches:
        return None
    unique_effects = {str(item.get("effect_id") or "") for item in matches}
    if len(matches) != 1 or len(unique_effects) != 1:
        raise StrictAuthorityError(
            f"strict_authority_terminal_recovery_count:{len(matches)}"
        )
    return matches[0]


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
        + _schema_repair_hints(prior.get("projection_errors") or ())
    )


def _schema_repair_hints(errors) -> str:
    """Render concrete, error-specific repair guidance for common schema failures.

    These are clarifying examples of the exact format the validator accepts,
    not a relaxation of the schema. They help the LLM produce a compliant
    object on the first retry instead of repeating the same structural error.
    """

    error_text = " ".join(str(e) for e in errors)
    hints = []
    if "change_symbol_not_chain_terminal" in error_text:
        hints.append(
            "FIX change_symbol/chain: reachable_chain must be a direct "
            "caller->callee path of 2-8 symbols that ENDS exactly at "
            "change_symbol. CRITICAL: look at the FULL VALIDATED EDGE INDEX "
            "in the prompt — only edges listed there are accepted. If "
            "get_baseline_decision appears only as a CALLER (left side of ->), "
            "not as a CALLEE (right side), then you CANNOT end a chain at it. "
            "Instead, pick a CALLEE of get_baseline_decision as your "
            "change_symbol. For example, if the index shows "
            "'policy.py:get_baseline_decision -> policy.py:_hole_ids', then "
            "change_symbol=\"policy.py:_hole_ids\" with "
            "reachable_chain=[\"policy.py:get_baseline_decision\","
            "\"policy.py:_hole_ids\"] is VALID. Do NOT use edges that are "
            "not in the index (e.g. iter_decisions -> get_baseline_decision "
            "is NOT in the index even though iter_decisions is an entrypoint)."
        )
    if "reachable_chain_count_invalid" in error_text:
        hints.append(
            "FIX reachable_chain length: reachable_chain MUST contain 2 to 8 "
            "symbols. A single-element chain is INVALID. Use the FULL "
            "VALIDATED EDGE INDEX to find a valid 2-symbol edge. Only edges "
            "EXACTLY as shown in the index are accepted — do NOT invent edges."
        )
    if "reachable_chain_edge_not_current" in error_text:
        hints.append(
            "FIX reachable_chain edge: the edge you used is NOT in the "
            "SYSTEM-VERIFIED SOURCE CALL INDEX. Look at the FULL VALIDATED "
            "EDGE INDEX in the prompt — it lists every accepted caller->callee "
            "edge. Your reachable_chain must use ONLY edges from that index. "
            "Common mistake: using 'iter_decisions -> get_baseline_decision' "
            "which is NOT in the index. Instead use an edge that IS listed, "
            "such as 'get_baseline_decision -> _hole_ids' or "
            "'get_baseline_decision -> preflop_equity'."
        )
    if "shared_leaf_requires_full_namespace" in error_text:
        hints.append(
            "FIX shared-leaf namespace: every shared leaf (e.g. fold_to_raise) "
            "must be owner-qualified in structural_change, expected_diff, and "
            "falsifier.intervention. Either write the full path "
            "(opponent.rates.fold_to_raise) OR use the root-scoped shorthand "
            "opponent.rates (aggression, fold_to_raise) immediately after the "
            "selectable root. A bare fold_to_raise without a namespace owner "
            "is ambiguous and rejected."
        )
    if "reachable_chain_count_invalid" in error_text:
        hints.append(
            "FIX reachable_chain length: reachable_chain must contain 2 to 8 "
            "symbols (not 1, not >8) in direct caller->callee order, ending "
            "exactly at change_symbol. Example with 2 items: "
            "[\"policy.py:get_baseline_decision\",\"policy.py:_hole_ids\"] "
            "requires change_symbol=\"policy.py:_hole_ids\"."
        )
    if hints:
        return "\n" + "\n".join(hints)
    return ""


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
    matching_abandon_reason: str | None = None,
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
            if matching_abandon_reason is None:
                raise StrictAuthorityError(
                    f"strict_authority_phase_journal_abandoned:{phase_name}"
                )
            _validated_strict_abandon_fence(
                checkpoint,
                reason=matching_abandon_reason,
                store=store,
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
        if effect_slot not in KNOWN_SLOTS:
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
        _detail_parts = []
        for i, rej in enumerate(rejections):
            _errs = rej.get("projection_errors") or []
            _kind = rej.get("rejection_kind") or "schema_projection"
            _detail_parts.append(
                f"  attempt {i+1}: kind={_kind} errors={list(_errs)}"
            )
        _detail = "\n".join(_detail_parts)
        _log.error(
            "Slot %s exhausted %d schema retries:\n%s",
            slot, MAX_SCHEMA_ATTEMPTS_PER_SLOT, _detail
        )
        try:
            import event_bus
            event_bus.emit(
                "pipeline.strict_authority_schema_retry_exhausted",
                "error",
                f"Slot {slot} exhausted {MAX_SCHEMA_ATTEMPTS_PER_SLOT} schema retries",
                slot=slot,
                role=expected_role,
                max_attempts=MAX_SCHEMA_ATTEMPTS_PER_SLOT,
                rejection_count=len(rejections),
                rejection_details=_detail,
            )
        except Exception:
            pass
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
            _canonical_proposal_primaries,
            _master_proposal_projection_hints,
            _source_symbol_graph,
            _validated_master_proposal,
        )
        from evolution_infra import get_bot_dir

        direction = slot.split(":", 1)[1]
        try:
            allowed_primaries = _canonical_proposal_primaries(
                context.get("allowed_primaries")
            )
        except ValueError as exc:
            raise StrictAuthorityError(
                "strict_authority_projection_allowed_primaries_invalid"
            ) from exc
        candidate_dir = get_bot_dir(int(binding.get("next_v")))
        source_graph, source_digest = _source_symbol_graph(candidate_dir)
        if source_digest != context.get("source_code_digest"):
            raise StrictAuthorityError(
                "strict_authority_projection_source_digest_mismatch"
            )
        evidence_mode = str(
            context.get("evidence_mode")
            or "fresh_strict_control_no_strength"
        )
        if evidence_mode == "fresh_strict_control_no_strength":
            execution_mode = "fixed_blueprint_capability_audit"
            expected_measurement_target = None
        elif evidence_mode == "singleton_parent_no_strength":
            execution_mode = "strategy_implementation"
            from bot_namespace import bot_name

            expected_measurement_target = bot_name(int(binding.get("source_v")))
        else:
            raise StrictAuthorityError(
                "strict_authority_projection_evidence_mode_invalid"
            )
        projected = _validated_master_proposal(
            raw_output,
            direction,
            source_graph=source_graph,
            snapshot_dir=(
                candidate_dir / ".protocol_bootstrap_no_strength_evidence"
                if evidence_mode == "fresh_strict_control_no_strength"
                else None
            ),
            national_policy_only=True,
            require_snapshot_evidence=False,
            execution_mode=execution_mode,
            evidence_mode=evidence_mode,
            expected_measurement_target=expected_measurement_target,
            forbidden_measurement_target=None,
            allowed_primaries=allowed_primaries,
            actual_role=str(call.get("actual_role") or ""),
        )
        if not isinstance(projected, dict):
            hints = _master_proposal_projection_hints(
                raw_output,
                source_graph=source_graph,
                snapshot_dir=(
                    candidate_dir / ".protocol_bootstrap_no_strength_evidence"
                    if evidence_mode == "fresh_strict_control_no_strength"
                    else None
                ),
                national_policy_only=True,
                evidence_mode=evidence_mode,
                allowed_primaries=allowed_primaries,
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
    elif slot in {"review", "review:retry", "critic"}:
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
                "reviewer" if slot in {"review", "review:retry"} else "critic",
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
        _log.warning(
            "Strict role %s rejected (slot=%s): %s",
            call.get("role"), call.get("slot"), list(projection_errors)
        )
        try:
            import event_bus
            event_bus.emit(
                "pipeline.strict_role_rejected",
                "warn",
                f"{call.get('role')}: schema projection rejected",
                slot=call.get("slot"),
                role=call.get("role"),
                rejection_kind="schema_projection",
                projection_errors=list(projection_errors),
                parse_contract=str(SLOT_PARSE_CONTRACTS.get(call["slot"], "")),
            )
        except Exception:
            pass
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
    _log.warning(
        "Strict role %s rejected (slot=%s): %s",
        call.get("role"), slot,
        ["strict_authority_proposal_identity_collision"]
    )
    try:
        import event_bus
        event_bus.emit(
            "pipeline.strict_role_rejected",
            "warn",
            f"{call.get('role')}: proposal identity collision",
            slot=slot,
            role=call.get("role"),
            rejection_kind="proposal_identity_collision",
            projection_errors=["strict_authority_proposal_identity_collision"],
            parse_contract=str(SLOT_PARSE_CONTRACTS.get(slot, "")),
            proposal_id=str(proposal_id),
            conflicting_slots=list(accepted_collisions),
        )
    except Exception:
        pass
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
    if slot == "review:retry" and actual_role == "LEAD CODE REVIEWER":
        return "reviewer_retry_io.txt"
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


def master_provider_retry_state(
    checkpoint: dict[str, Any],
    *,
    failed_slot: str,
) -> dict[str, Any]:
    """Project durable, role-local retry state for a partial Master packet.

    This is diagnostic/control input only; accepted receipts remain the sole
    proposal/ballot authority. Provider availability pauses and controlled
    cancellations are attempt-neutral and therefore excluded from the local
    failure count.
    """

    if failed_slot not in MASTER_SLOTS[:5]:
        raise StrictAuthorityError(
            f"strict_authority_master_retry_slot_invalid:{failed_slot}"
        )
    binding = generation_binding(checkpoint)
    binding_digest = content_digest(binding)
    run_id = authority_run_id(binding["workflow_run_id"])
    store = _store()
    try:
        effects = store.effects_for_run(run_id)
        events = store.events(run_id)
    except Exception as exc:
        raise StrictAuthorityError(
            "strict_authority_master_retry_journal_unavailable:"
            f"{type(exc).__name__}"
        ) from exc

    ignored_error_prefixes = (
        "LLMAvailabilityBlocked:",
        "CancelledError:",
        "asyncio.CancelledError:",
        "str: asyncio.CancelledError",
        "str: controlled shutdown",
    )
    failed_effect_ids: list[str] = []
    for effect in effects:
        input_payload = effect.get("input_payload") or {}
        last_error = str(effect.get("last_error") or "")
        if (
            effect.get("kind") != EFFECT_KIND
            or input_payload.get("generation_binding_digest") != binding_digest
            or input_payload.get("slot") != failed_slot
            or effect.get("status") not in {"failed", "exhausted"}
            or last_error.startswith(ignored_error_prefixes)
        ):
            continue
        failed_effect_ids.append(str(effect.get("effect_id") or ""))

    accepted_slots = sorted({
        str(event.payload.get("slot") or "")
        for event in events
        if event.event_type == ACCEPTED_EVENT
        and event.payload.get("generation_binding_digest") == binding_digest
        and event.payload.get("slot") in MASTER_SLOTS[:5]
    })
    pending_slots = [
        slot for slot in MASTER_SLOTS[:5] if slot not in accepted_slots
    ]
    return {
        "run_id": run_id,
        "failed_slot": failed_slot,
        "role_attempt": max(1, len(failed_effect_ids)),
        "failed_effect_ids": sorted(failed_effect_ids),
        "accepted_slots": accepted_slots,
        "pending_slots": pending_slots,
    }


def validate_receipts(*args, **kwargs):
    """Delegate to strict_authority_receipts."""
    return _sr.validate_receipts(*args, **kwargs)


def validate_master_final_projection(*args, **kwargs):
    """Delegate to strict_authority_receipts."""
    return _sr.validate_master_final_projection(*args, **kwargs)


def authority_summary(*args, **kwargs):
    """Delegate to strict_authority_receipts."""
    return _sr.authority_summary(*args, **kwargs)


__all__ = [
    "ALL_SLOTS",
    "GATE_SLOTS",
    "INVOCATION_EVIDENCE_SLOTS",
    "LEGACY_REVIEW_TERMINAL_MIGRATION_KIND",
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
    "recover_accepted_master_final_result",
    "recover_terminal_gate_rejection_call",
    "render_gate_provider_prompt",
    "reject_duplicate_proposal",
    "schema_retry_prompt",
    "strict_invocation_log_path",
    "validate_master_final_projection",
    "validate_receipts",
]
