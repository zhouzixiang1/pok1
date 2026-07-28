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
import strict_authority_call_recovery as _sacr  # noqa: E402,F401  (call-recovery cluster)
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

# Journal slot management, abandon-fence and retry-state projection cluster.
# Hosts the strict-authority abandon fence (``abandon_authority`` and its exact
# terminal-event identity/validator), the immutable invocation-evidence log
# allocation and journal-bound evidence bind/record/read APIs, the accepted
# event projection, and the role-local master retry state.  The companion
# imports ``strict_authority_workflow`` itself and resolves cross-references
# lazily at call time, so this top-level import does not create a load-time
# cycle.  Every moved symbol is re-exposed below as a thin delegate shell so
# legacy ``from strict_authority_workflow import <name>`` sites and
# ``monkeypatch.setattr(strict_authority_workflow, "<name>", ...)`` patches
# keep resolving to the parent module attribute.
import strict_authority_journal as _sj  # noqa: E402  (circular by design)


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
    """Delegate to strict_authority_journal."""
    return _sj.strict_authority_abandon_event_identity(checkpoint, reason=reason)


def abandon_authority(
    checkpoint: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    """Delegate to strict_authority_journal."""
    return _sj.abandon_authority(checkpoint, reason=reason)


def _validated_strict_abandon_fence(
    checkpoint: dict[str, Any],
    *,
    reason: str,
    store: WorkflowStore | None = None,
) -> dict[str, Any]:
    """Delegate to strict_authority_journal."""
    return _sj._validated_strict_abandon_fence(checkpoint, reason=reason, store=store)


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
    """Delegate to strict_authority_call_recovery."""
    return _sacr._recover_accepted_call(descriptor)


def recover_accepted_master_final_result(
    checkpoint: dict[str, Any],
    *,
    architecture_policy: dict[str, Any],
) -> dict[str, Any] | None:
    """Delegate to strict_authority_call_recovery."""
    return _sacr.recover_accepted_master_final_result(checkpoint, architecture_policy=architecture_policy)


def _recover_completed_unaccepted_call(
    descriptor: dict[str, Any],
) -> dict[str, Any] | None:
    """Delegate to strict_authority_call_recovery."""
    return _sacr._recover_completed_unaccepted_call(descriptor)


def _legacy_review_terminal_migration_contract(
    recorded_semantics: dict[str, Any],
    current_semantics: dict[str, Any] | None,
    *,
    legacy_projection: dict[str, Any] | None = None,
    legacy_quality_gate_digest: str | None = None,
) -> dict[str, Any] | None:
    """Delegate to strict_authority_call_recovery."""
    return _sacr._legacy_review_terminal_migration_contract(recorded_semantics, current_semantics, legacy_projection=legacy_projection, legacy_quality_gate_digest=legacy_quality_gate_digest)


def _legacy_review_terminal_checkpoint_projection(
    checkpoint: dict[str, Any],
    *,
    candidate_dir: str | Path,
) -> tuple[dict[str, Any], dict[str, Any], str] | None:
    """Delegate to strict_authority_call_recovery."""
    return _sacr._legacy_review_terminal_checkpoint_projection(checkpoint, candidate_dir=candidate_dir)


def recover_terminal_gate_rejection_call(
    checkpoint: dict[str, Any],
    *,
    gate_name: str,
    candidate_dir: str | Path,
) -> dict[str, Any] | None:
    """Delegate to strict_authority_call_recovery."""
    return _sacr.recover_terminal_gate_rejection_call(checkpoint, gate_name=gate_name, candidate_dir=candidate_dir)


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
    """Delegate to strict_authority_call_recovery."""
    return _sacr._input_matches_descriptor(input_payload, descriptor)


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
    """Delegate to strict_authority_journal."""
    return _sj.schema_retry_prompt(call)


def _schema_repair_hints(errors) -> str:
    """Delegate to strict_authority_journal."""
    return _sj._schema_repair_hints(errors)


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
    """Delegate to strict_authority_journal."""
    return _sj._invocation_evidence_log_name(slot, actual_role)


def _strict_generation_logs_root(subject: dict[str, Any]) -> Path:
    """Delegate to strict_authority_journal."""
    return _sj._strict_generation_logs_root(subject)


def strict_invocation_log_path(
    call: dict[str, Any],
    *,
    logs_dir: str | Path,
    basename: str,
) -> Path:
    """Delegate to strict_authority_journal."""
    return _sj.strict_invocation_log_path(
        call, logs_dir=logs_dir, basename=basename
    )


def _invocation_evidence_authority(
    call: dict[str, Any],
) -> tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Delegate to strict_authority_journal."""
    return _sj._invocation_evidence_authority(call)


def _validate_bound_invocation_evidence(
    evidence: Any,
    *,
    call: dict[str, Any],
    accepted_payload: dict[str, Any],
    provider: dict[str, Any],
    generation_binding: dict[str, Any],
) -> None:
    """Delegate to strict_authority_journal."""
    return _sj._validate_bound_invocation_evidence(
        evidence,
        call=call,
        accepted_payload=accepted_payload,
        provider=provider,
        generation_binding=generation_binding,
    )


def bound_invocation_evidence(
    call: dict[str, Any],
) -> dict[str, Any] | None:
    """Delegate to strict_authority_journal."""
    return _sj.bound_invocation_evidence(call)


def bind_invocation_evidence(
    call: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Delegate to strict_authority_journal."""
    return _sj.bind_invocation_evidence(call, evidence)


def record_bound_invocation_evidence(
    call: dict[str, Any],
    *,
    log_file: str | Path,
) -> dict[str, Any]:
    """Delegate to strict_authority_journal."""
    return _sj.record_bound_invocation_evidence(call, log_file=log_file)


# Compatibility aliases for the first implementation name. Active callers use
# the generic API above; keeping these aliases avoids breaking diagnostic code.
# Resolve through the companion so a monkeypatch on the parent attribute still
# wins for direct callers without breaking alias identity for import sites.
bound_master_invocation_evidence = bound_invocation_evidence
bind_master_invocation_evidence = bind_invocation_evidence


def _accepted_events(checkpoint: dict[str, Any]) -> tuple[list[Any], list[str]]:
    """Delegate to strict_authority_journal."""
    return _sj._accepted_events(checkpoint)


def master_provider_retry_state(
    checkpoint: dict[str, Any],
    *,
    failed_slot: str,
) -> dict[str, Any]:
    """Delegate to strict_authority_journal."""
    return _sj.master_provider_retry_state(checkpoint, failed_slot=failed_slot)


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
