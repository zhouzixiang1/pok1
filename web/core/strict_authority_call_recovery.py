"""Call-recovery subsystem for strict_authority_workflow.

Extracted as a cohesive business cluster; ``strict_authority_workflow.py``
retains thin delegate shells so external
``from strict_authority_workflow import <name>`` and
``monkeypatch.setattr(strict_authority_workflow, "<name>", ...)`` keep
resolving.

Business responsibility (single cohesive domain):
* Recovery of accepted calls (``_recover_accepted_call`` and
  ``recover_accepted_master_final_result``).
* Recovery of completed-but-unaccepted calls.
* Legacy review-terminal migration-contract / checkpoint-projection helpers.
* Recovery of terminal gate-rejection calls.
* Input-vs-descriptor matching (``_input_matches_descriptor``).

Pure recovery/projection logic over recorded call state: deterministic,
side-effect-free apart from reading the workflow store.

Cross-references to symbols that remain in ``strict_authority_workflow`` (the
event/kind/slot constants, the StrictAuthorityError class, the store/json/digest
helpers, the context/evidence builders, ``new_call``, and ``validate_receipts``)
are reached through ``_sa.<name>`` so that test monkeypatches on
``strict_authority_workflow.<name>`` propagate.

CRITICAL (wave-3 lesson): EVERY intra-companion call to a moved symbol ALSO
routes through ``_sa.<name>(...)`` so monkeypatches on
``strict_authority_workflow.<name>`` propagate even when both call sites now
live in this companion.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import strict_authority_workflow as _sa  # for cross-refs


def _recover_accepted_call(descriptor: dict[str, Any]) -> dict[str, Any] | None:
    """Recover one accepted slot after a crash before checkpoint projection."""

    store = _sa._store()
    if not store.instance(descriptor["run_id"]):
        return None
    events = [
        event
        for event in store.events(descriptor["run_id"])
        if event.event_type == _sa.ACCEPTED_EVENT
        and event.payload.get("slot") == descriptor["slot"]
    ]
    if not events:
        return None
    if len(events) != 1:
        raise _sa.StrictAuthorityError(
            f"strict_authority_{descriptor['slot']}_accepted_count:{len(events)}"
        )
    event = events[0]
    payload = event.payload
    receipt_subject = {
        key: value for key, value in payload.items() if key != "receipt_digest"
    }
    if payload.get("receipt_digest") != _sa.content_digest(receipt_subject):
        raise _sa.StrictAuthorityError("strict_authority_recovery_receipt_invalid")
    if payload.get("role_result_digest") != _sa.content_digest(
        _sa._json_value(payload.get("role_result"))
    ):
        raise _sa.StrictAuthorityError("strict_authority_recovery_role_result_invalid")
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
            raise _sa.StrictAuthorityError(
                f"strict_authority_recovery_{field}_mismatch:{descriptor['slot']}"
            )
    effect = store.effect(str(payload.get("effect_id") or ""))
    provider = effect.get("result_payload") or {}
    input_payload = effect.get("input_payload") or {}
    if effect.get("status") != "completed" or not isinstance(
        provider.get("raw_output"), str
    ):
        raise _sa.StrictAuthorityError("strict_authority_recovery_provider_missing")
    provider_subject = {
        key: value for key, value in provider.items() if key != "result_digest"
    }
    if provider.get("result_digest") != _sa.content_digest(provider_subject):
        raise _sa.StrictAuthorityError("strict_authority_recovery_provider_digest_invalid")
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
            raise _sa.StrictAuthorityError(
                f"strict_authority_recovery_effect_{field}_mismatch"
            )
    if hashlib.sha256(provider["raw_output"].encode("utf-8")).hexdigest() != provider.get(
        "raw_output_digest"
    ):
        raise _sa.StrictAuthorityError("strict_authority_recovery_raw_output_invalid")
    if (
        provider.get("role_projection_valid") is not True
        or provider.get("projected_role_result_digest")
        != payload.get("role_result_digest")
        or _sa._json_value(provider.get("projected_role_result"))
        != _sa._json_value(payload.get("role_result"))
    ):
        raise _sa.StrictAuthorityError(
            "strict_authority_recovery_role_projection_invalid"
        )
    receipt_ref = {
        "schema_version": 1,
        "kind": _sa.RECEIPT_KIND,
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
        raise _sa.StrictAuthorityError(
            "strict_authority_master_final_recovery_policy_missing"
        )
    binding = _sa.generation_binding(checkpoint)
    run_id = _sa.authority_run_id(binding["workflow_run_id"])
    store = _sa._store()
    try:
        instance = store.instance(run_id)
        if not instance:
            return None
        if instance.get("status") == "abandoned":
            raise _sa.StrictAuthorityError(
                "strict_authority_master_final_recovery_journal_abandoned"
            )
        final_events = [
            event
            for event in store.events(run_id)
            if event.event_type == _sa.ACCEPTED_EVENT
            and event.payload.get("slot") == "master:final"
        ]
    except _sa.StrictAuthorityError:
        raise
    except Exception as exc:
        raise _sa.StrictAuthorityError(
            "strict_authority_master_final_recovery_journal_unavailable:"
            f"{type(exc).__name__}"
        ) from exc
    if not final_events:
        return None
    if len(final_events) != 1:
        raise _sa.StrictAuthorityError(
            "strict_authority_master:final_accepted_count:"
            f"{len(final_events)}"
        )
    role_result = final_events[0].payload.get("role_result")
    if not isinstance(role_result, dict):
        raise _sa.StrictAuthorityError(
            "strict_authority_master_final_recovery_role_result_invalid"
        )
    proposal_packet = role_result.get("proposal_ensemble")
    if not isinstance(proposal_packet, dict):
        raise _sa.StrictAuthorityError(
            "strict_authority_master_final_recovery_packet_missing"
        )
    # ``architecture_policy`` is a system-owned dispatch input.  It is bound
    # into the final call context but intentionally is not copied into the
    # provider's accepted role result.  Reattach the current policy before
    # rebuilding all six expected contexts; otherwise a complete, valid
    # journal is spuriously rejected as a final-context mismatch on recovery.
    projection_plan = deepcopy(role_result)
    projection_plan["architecture_policy"] = deepcopy(architecture_policy)
    _refs, packet_errors = _sa.validate_receipts(
        checkpoint,
        required_slots=_sa.MASTER_SLOTS,
        expected_role_results=_sa.expected_master_role_results(projection_plan),
        expected_context_bindings=_sa.expected_master_contexts(projection_plan),
        require_no_other_accepted=True,
    )
    if packet_errors:
        raise _sa.StrictAuthorityError(packet_errors)
    try:
        expected_invocations = _sa.expected_master_invocation_evidence(
            projection_plan
        )
        accepted_events = [
            event
            for event in store.events(run_id)
            if event.event_type == _sa.ACCEPTED_EVENT
        ]
        for slot in _sa.MASTER_SLOTS[:5]:
            slot_events = [
                event
                for event in accepted_events
                if event.payload.get("slot") == slot
            ]
            if len(slot_events) != 1:
                raise _sa.StrictAuthorityError(
                    f"strict_authority_{slot}_accepted_count:{len(slot_events)}"
                )
            bound = _sa.bound_invocation_evidence(dict(slot_events[0].payload))
            if _sa._json_value(bound) != _sa._json_value(expected_invocations[slot]):
                raise _sa.StrictAuthorityError(
                    f"strict_authority_{slot}_invocation_evidence_mismatch"
                )
    except _sa.StrictAuthorityError:
        raise
    except Exception as exc:
        raise _sa.StrictAuthorityError(
            "strict_authority_master_final_recovery_invocation_evidence_"
            f"unavailable:{type(exc).__name__}"
        ) from exc
    descriptor = _sa.new_call(
        checkpoint,
        slot="master:final",
        context_binding=_sa.final_master_call_context(
            proposal_packet,
            architecture_policy,
        ),
    )
    if (
        descriptor.get("replay_provider") is not True
        or not isinstance(descriptor.get("accepted_role_result"), dict)
    ):
        raise _sa.StrictAuthorityError(
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

    store = _sa._store()
    if not store.instance(descriptor["run_id"]):
        return None
    events = store.events(descriptor["run_id"])
    accepted_ids = {
        str(event.payload.get("effect_id") or "")
        for event in events
        if event.event_type == _sa.ACCEPTED_EVENT
    }
    rejected_ids = {
        str(event.payload.get("effect_id") or "")
        for event in events
        if event.event_type == _sa.REJECTED_EVENT
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
            or effect.get("kind") != _sa.EFFECT_KIND
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
            provider.get("result_digest") != _sa.content_digest(provider_subject)
            or provider.get("projected_role_result_digest")
            != _sa.content_digest(_sa._json_value(projected))
            or provider.get("raw_output_digest")
            != hashlib.sha256(provider["raw_output"].encode("utf-8")).hexdigest()
        ):
            raise _sa.StrictAuthorityError(
                "strict_authority_completed_recovery_payload_invalid"
            )
        matches.append((effect, input_payload, provider))
    if not matches:
        return None
    if len(matches) != 1:
        raise _sa.StrictAuthorityError(
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
        set(recorded_semantics) != _sa._RENDERER_SEMANTIC_CONTRACT_KEYS
        or recorded_semantics.get("schema_version") != 1
        or recorded_semantics.get("role") != "LEAD CODE REVIEWER"
        or not isinstance(recorded_inputs, dict)
        or set(recorded_inputs) != _sa._LEGACY_REVIEW_SEMANTIC_INPUT_KEYS
        or recorded_semantics.get("semantic_inputs_digest")
        != _sa.content_digest(recorded_inputs)
    ):
        return None

    recorded_static = recorded_semantics.get("renderer_static_identity")
    if (
        not isinstance(recorded_static, dict)
        or set(recorded_static) != _sa._RENDERER_STATIC_IDENTITY_KEYS
        or recorded_static.get("producer_file") != "web/core/tool_gates.py"
        or recorded_static.get("producer_name")
        != "_render_reviewer_provider_prompt"
        or recorded_semantics.get("renderer_static_identity_digest")
        != _sa.content_digest(recorded_static)
        or recorded_semantics.get("contract_digest")
        != _sa.content_digest({
            key: value for key, value in recorded_semantics.items()
            if key != "contract_digest"
        })
    ):
        return None
    digest_fields = (
        "producer_file_sha256",
        "producer_function_sha256",
    )
    if any(not _sa._valid_digest(recorded_static.get(field)) for field in digest_fields):
        return None
    template_digests = recorded_static.get("template_digests")
    if (
        not isinstance(template_digests, list)
        or len(template_digests) != 1
        or not isinstance(template_digests[0], list)
        or len(template_digests[0]) != 2
        or template_digests[0][0]
        != "web/core/prompts/reviewer_prompt.md"
        or not _sa._valid_digest(template_digests[0][1])
    ):
        return None
    for field in (
        "sentinel_rendered_prompt_sha256",
        "sentinel_evidence_provenance_sha256",
        "sentinel_renderer_receipt_digest",
        "sentinel_evidence_receipt_digest",
        "sentinel_dispatch_receipt_digest",
    ):
        if not _sa._valid_digest(recorded_semantics.get(field)):
            return None
    if (
        recorded_semantics.get("invocation_normalization")
        != "fixed-32-byte-sentinel-v1"
        or recorded_semantics.get("sentinel_evidence_kind")
        != "review_candidate_pair"
        or not _sa._plain_int(recorded_semantics.get("sentinel_rendered_prompt_chars"))
        or int(recorded_semantics["sentinel_rendered_prompt_chars"]) <= 0
    ):
        return None

    review_contract_digest = None
    semantic_upgrade_status = "current_review_contract_available"
    if isinstance(current_semantics, dict):
        current_inputs = current_semantics.get("semantic_inputs")
        if (
            set(current_semantics) != _sa._RENDERER_SEMANTIC_CONTRACT_KEYS
            or current_semantics.get("schema_version") != 1
            or current_semantics.get("role") != "LEAD CODE REVIEWER"
            or not isinstance(current_inputs, dict)
            or set(current_inputs) != _sa._CURRENT_REVIEW_SEMANTIC_INPUT_KEYS
            or current_semantics.get("semantic_inputs_digest")
            != _sa.content_digest(current_inputs)
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
        ) != _sa.content_digest({
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
            or set(current_inputs) != _sa._LEGACY_REVIEW_SEMANTIC_INPUT_KEYS
            or current_inputs != recorded_inputs
            or not _sa._valid_digest(legacy_quality_gate_digest)
        ):
            return None

    subject = {
        "schema_version": 1,
        "kind": _sa.LEGACY_REVIEW_TERMINAL_MIGRATION_KIND,
        "disposition": "terminal_rejection_only",
        "semantic_upgrade_status": semantic_upgrade_status,
        "recorded_semantic_inputs_digest": _sa.content_digest(recorded_inputs),
        "current_semantic_inputs_digest": _sa.content_digest(current_inputs),
        "legacy_projection_digest": _sa.content_digest(legacy_projection),
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
        "recorded_template_digests_digest": _sa.content_digest(template_digests),
    }
    return {**subject, "migration_digest": _sa.content_digest(subject)}



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
        or not _sa._valid_digest(master_receipt.get("receipt_digest"))
        or not _sa._valid_digest(master_receipt.get("plan_digest"))
    ):
        return None
    candidate_hash = hash_path(Path(candidate_dir))
    if (
        not _sa._valid_digest(candidate_hash)
        or quality.get("code_fingerprint") != candidate_hash
    ):
        return None
    semantic_inputs = {
        "master_plan": _sa._json_value(master_plan),
        "source_v": int(checkpoint["source_v"]),
        "next_v": int(checkpoint["next_v"]),
        "strict_bootstrap": True,
        "focus_areas": _sa._normalized_reviewer_focus_areas(checkpoint),
    }
    quality_digest = _sa.content_digest(quality)
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
        raise _sa.StrictAuthorityError(
            "strict_authority_terminal_recovery_gate_not_supported"
        )
    if str((checkpoint or {}).get("stage") or "") != "quality_passed":
        raise _sa.StrictAuthorityError(
            "strict_authority_terminal_recovery_stage_invalid"
        )
    binding = _sa.generation_binding(checkpoint)
    run_id = _sa.authority_run_id(binding["workflow_run_id"])
    legacy_checkpoint_projection = None
    try:
        current_context = _sa.gate_call_context(
            checkpoint,
            gate_name=gate_name,
            candidate_dir=Path(candidate_dir),
        )
    except _sa.StrictAuthorityError as exc:
        if exc.errors != (
            "strict_authority_review_semantic_contract_invalid",
        ):
            raise
        legacy_checkpoint_projection = (
            _sa._legacy_review_terminal_checkpoint_projection(
                checkpoint,
                candidate_dir=candidate_dir,
            )
        )
        if legacy_checkpoint_projection is None:
            raise
        current_context = None
    store = _sa._store()
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
            or effect.get("kind") != _sa.EFFECT_KIND
            or effect.get("run_id") != run_id
            or input_payload.get("slot") != gate_name
            or input_payload.get("role") != _sa.SLOT_CONTRACTS[gate_name][0]
            or input_payload.get("purpose") != _sa.SLOT_CONTRACTS[gate_name][1]
            or input_payload.get("generation_binding") != binding
            or input_payload.get("generation_binding_digest")
            != _sa.content_digest(binding)
            or input_payload.get("checkpoint_stage") != "quality_passed"
            or not _sa._plain_int(input_payload.get("checkpoint_revision"))
            or int(input_payload["checkpoint_revision"])
            > int(checkpoint.get("checkpoint_revision") or -1)
            or not isinstance(recorded_context, dict)
            or input_payload.get("context_binding_digest")
            != _sa.content_digest(recorded_context)
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
                else _sa._legacy_review_terminal_migration_contract(
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
            migration_contract = _sa._legacy_review_terminal_migration_contract(
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
            provider.get("result_digest") != _sa.content_digest(provider_subject)
            or provider.get("projected_role_result_digest")
            != _sa.content_digest(_sa._json_value(projected))
            or provider.get("raw_output_digest")
            != hashlib.sha256(
                str(provider.get("raw_output") or "").encode("utf-8")
            ).hexdigest()
        ):
            raise _sa.StrictAuthorityError(
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
        recovered = _sa._recover_accepted_call(descriptor)
        if recovered is None:
            recovered = _sa._recover_completed_unaccepted_call(descriptor)
        if recovered is None:
            raise _sa.StrictAuthorityError(
                "strict_authority_terminal_recovery_effect_unrecoverable"
            )
        recovered_result = (
            recovered.get("accepted_role_result")
            or recovered.get("projected_role_result")
        )
        if not isinstance(recovered_result, dict) or recovered_result.get(
            "approved"
        ) is not False:
            raise _sa.StrictAuthorityError(
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
        raise _sa.StrictAuthorityError(
            f"strict_authority_terminal_recovery_count:{len(matches)}"
        )
    return matches[0]



def _input_matches_descriptor(
    input_payload: dict[str, Any], descriptor: dict[str, Any]
) -> bool:
    return all(
        input_payload.get(field) == descriptor.get(field)
        for field in _sa._STABLE_CALL_FIELDS
    )



