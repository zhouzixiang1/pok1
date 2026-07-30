"""Journal slot management, abandon-fence, retry-state projection and
schema-repair prompt rendering for ``strict_authority_workflow``.

Extracted as a cohesive business cluster; ``strict_authority_workflow.py``
retains thin delegate shells so external ``from strict_authority_workflow
import <name>`` imports and ``monkeypatch.setattr(strict_authority_workflow,
"<name>", ...)`` patches keep resolving to the parent module attribute.  The
companion imports ``strict_authority_workflow as _sa`` (circular by design)
and resolves parent-owned symbols lazily at call time.  Every symbol moved
here is re-exposed on the parent as a delegate shell, so legacy attribute
access continues to work and monkeypatches that replace the parent attribute
remain authoritative for any caller that resolves through the parent module
(including the other companions, which call ``_sa.<name>``).
"""

from __future__ import annotations

from copy import deepcopy
import os
import re
import stat
from pathlib import Path
from typing import Any

from workflow_kernel import WorkflowConflict, WorkflowStore

import strict_authority_workflow as _sa  # noqa: E402  (circular by design)


def schema_retry_prompt(call: dict[str, Any]) -> str:
    """Render the system-owned one-shot repair suffix after a durable rejection."""

    if not isinstance(call, dict) or not call.get("schema_retry_required"):
        return ""
    prior = call.get("prior_schema_rejection") or {}
    errors = ", ".join(map(str, prior.get("projection_errors") or ()))
    parse_contract = _sa.SLOT_PARSE_CONTRACTS.get(str(call.get("slot") or ""))
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
            "SYSTEM-VERIFIED SOURCE CALL INDEX. Look at the FULL "
            "VALIDATED EDGE INDEX in the prompt — it lists every accepted "
            "caller->callee edge. Your reachable_chain must use ONLY edges "
            "from that index. Common mistake: using 'iter_decisions -> "
            "get_baseline_decision' which is NOT in the index. Instead use "
            "an edge that IS listed, such as 'get_baseline_decision -> "
            "_hole_ids' or 'get_baseline_decision -> preflop_equity'."
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


def strict_authority_abandon_event_identity(
    checkpoint: dict[str, Any],
    *,
    reason: str,
) -> tuple[dict[str, str], str]:
    """Return the sole exact strict-authority terminal event identity."""

    workflow_run_id = str((checkpoint or {}).get("workflow_run_id") or "")
    run_id = _sa.authority_run_id(workflow_run_id)
    payload = {
        "reason": str(reason)[:1000],
        "workflow_run_id": workflow_run_id,
    }
    return (
        payload,
        f"strict-authority-abandoned:{run_id}:{_sa.content_digest(payload)}",
    )


def abandon_authority(
    checkpoint: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    """Fence the strict child journal when its generation is abandoned."""

    run_id = _sa.authority_run_id(
        str((checkpoint or {}).get("workflow_run_id") or "")
    )
    store = _sa._store()
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
                definition_version=_sa.DEFINITION_VERSION,
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
                == _sa.DEFINITION_VERSION
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
                    raise _sa.StrictAuthorityError(
                        "strict_authority_abandon_tombstone_invalid"
                    )
            elif not (
                instance.get("status") == "running"
                and exact_empty_instance
            ):
                raise _sa.StrictAuthorityError(
                    "strict_authority_abandon_tombstone_invalid"
                )
        elif instance.get("status") == "abandoned":
            raise _sa.StrictAuthorityError(
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
    run_id = _sa.authority_run_id(workflow_run_id)
    store = store or _sa._store()
    instance = store.instance(run_id)
    events = store.events(run_id)
    terminal = [
        event
        for event in events
        if event.event_type == "StrictAuthorityAbandoned"
    ]
    # The persisted terminal event is the single source of truth for the abandon
    # reason once a strict-authority instance is already fenced: it was written
    # by the original owner through this same fence (payload bound to
    # causation_id + payload_digest, and store.events() rejects any row whose
    # content_digest(payload) != payload_digest on read), so reproof must
    # reproduce THAT reason, not whatever reason a later caller supplies. This
    # mirrors WorkerWorkflow.abandon's accept_existing_reason (worker_workflow
    # 1714-1718) and closes the v12/v13 reason-drift death loop: an outer
    # checkpoint that terminalized its strict child under a concrete executor
    # reason must still reprove that child when a router replay supplies the
    # abstract routing constant. Only reproofs of an already-abandoned instance
    # adopt the persisted reason; first creation keeps using the caller reason.
    # All non-reason fields remain exact-bound below.
    verified_reason = reason
    if (
        instance.get("status") == "abandoned"
        and len(terminal) == 1
        and terminal[0].schema_version == 1
    ):
        persisted_reason = str((terminal[0].payload or {}).get("reason") or "")
        if persisted_reason and persisted_reason != str(reason):
            verified_reason = persisted_reason
    expected_payload, expected_causation_id = (
        strict_authority_abandon_event_identity(
            checkpoint,
            reason=verified_reason,
        )
    )
    if (
        int(instance.get("definition_version") or -1) != _sa.DEFINITION_VERSION
        or instance.get("status") != "abandoned"
        or int(instance.get("fence_epoch") or 0) < 1
        or len(terminal) != 1
        or terminal[0].seq != len(events)
        or int(instance.get("stream_version") or -1) != terminal[0].seq
        or terminal[0].schema_version != 1
        or terminal[0].payload != expected_payload
        or terminal[0].causation_id != expected_causation_id
    ):
        raise _sa.StrictAuthorityError(
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
            raise _sa.StrictAuthorityError(
                "strict_authority_abandon_fence_effect_live"
            )
    return instance


def _invocation_evidence_log_name(slot: str, actual_role: str) -> str:
    """Return the sole log filename admitted for an evidence-bearing role."""

    if slot in _sa.MASTER_SLOTS[:3]:
        direction = slot.split(":", 1)[1]
        base_role = f"MASTER PROPOSAL {direction}"
        if actual_role == base_role:
            suffix = ""
        elif actual_role == base_role + " SCHEMA RETRY":
            suffix = "_schema_retry"
        elif actual_role == base_role + " DISTINCTNESS RETRY":
            suffix = "_distinctness_retry"
        else:
            raise _sa.StrictAuthorityError(
                f"strict_authority_invocation_evidence_role_invalid:{slot}"
            )
        return f"master_proposal_{direction}{suffix}_io.txt"
    if slot in _sa.MASTER_SLOTS[3:5]:
        critic_id = slot.split(":", 1)[1]
        base_role = f"MASTER PROPOSAL CRITIC {critic_id}"
        if actual_role == base_role:
            suffix = ""
        elif actual_role == base_role + " SCHEMA RETRY":
            suffix = "_schema_retry"
        else:
            raise _sa.StrictAuthorityError(
                f"strict_authority_invocation_evidence_role_invalid:{slot}"
            )
        return f"master_proposal_critic_{critic_id}{suffix}_io.txt"
    if slot == "review" and actual_role == "LEAD CODE REVIEWER":
        return "reviewer_io.txt"
    if slot == "review:retry" and actual_role == "LEAD CODE REVIEWER":
        return "reviewer_retry_io.txt"
    if slot == "critic" and actual_role == "STRATEGY CRITIC":
        return "critic_io.txt"
    raise _sa.StrictAuthorityError(
        f"strict_authority_invocation_evidence_slot_invalid:{slot}"
    )


def _strict_generation_logs_root(subject: dict[str, Any]) -> Path:
    """Derive the only log root admitted by one generation binding."""

    binding = (subject or {}).get("generation_binding")
    next_v = binding.get("next_v") if isinstance(binding, dict) else None
    if not _sa._plain_int(next_v) or int(next_v) < 1:
        raise _sa.StrictAuthorityError(
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
        raise _sa.StrictAuthorityError(
            "strict_authority_invocation_log_invocation_id_invalid"
        )
    basename = str(basename or "")
    if (
        Path(basename).name != basename
        or not re.fullmatch(r"[a-z0-9_]+_io\.txt", basename)
    ):
        raise _sa.StrictAuthorityError(
            "strict_authority_invocation_log_basename_invalid"
        )

    try:
        root = Path(os.path.abspath(os.fspath(logs_dir)))
        if root != _strict_generation_logs_root(call):
            raise _sa.StrictAuthorityError(
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
                raise _sa.StrictAuthorityError(
                    "strict_authority_invocation_log_parent_invalid"
                )
        root.mkdir(parents=True, exist_ok=True)
        for component in reversed(chain):
            metadata = os.lstat(component)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
                metadata.st_mode
            ):
                raise _sa.StrictAuthorityError(
                    "strict_authority_invocation_log_parent_invalid"
                )
        strict_root = root / "strict_invocations"
        strict_root.mkdir(mode=0o700, exist_ok=True)
        if strict_root.is_symlink() or not strict_root.is_dir():
            raise _sa.StrictAuthorityError(
                "strict_authority_invocation_log_root_invalid"
            )
        invocation_dir = strict_root / invocation_id
        invocation_dir.mkdir(mode=0o700, exist_ok=True)
        if invocation_dir.is_symlink() or not invocation_dir.is_dir():
            raise _sa.StrictAuthorityError(
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
                raise _sa.StrictAuthorityError(
                    "strict_authority_invocation_log_path_invalid"
                )
        return path
    except _sa.StrictAuthorityError:
        raise
    except OSError as exc:
        raise _sa.StrictAuthorityError(
            "strict_authority_invocation_log_filesystem_invalid:"
            f"{type(exc).__name__}"
        ) from exc


def _invocation_evidence_authority(
    call: dict[str, Any],
) -> tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Re-open the exact accepted role and completed provider effect."""

    slot = str((call or {}).get("slot") or "")
    if slot not in _sa.INVOCATION_EVIDENCE_SLOTS:
        raise _sa.StrictAuthorityError(
            f"strict_authority_invocation_evidence_slot_invalid:{slot}"
        )
    run_id = str(call.get("run_id") or "")
    effect_id = str(call.get("effect_id") or "")
    store = _sa._store()
    accepted = [
        event
        for event in store.events(run_id)
        if event.event_type == _sa.ACCEPTED_EVENT
        and event.payload.get("effect_id") == effect_id
        and event.payload.get("slot") == slot
    ]
    if len(accepted) != 1:
        raise _sa.StrictAuthorityError(
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
        accepted_event.payload_digest != _sa.content_digest(accepted_payload)
        or accepted_payload.get("receipt_digest")
        != _sa.content_digest(accepted_subject)
        or accepted_payload.get("role_result_digest")
        != _sa.content_digest(_sa._json_value(accepted_payload.get("role_result")))
    ):
        raise _sa.StrictAuthorityError(
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
        or effect.get("kind") != _sa.EFFECT_KIND
        or effect.get("run_id") != run_id
        or provider.get("result_digest") != _sa.content_digest(provider_subject)
        or any(
            input_payload.get(field) != accepted_payload.get(field)
            or provider.get(field) != accepted_payload.get(field)
            or call.get(field) != accepted_payload.get(field)
            for field in stable_fields
        )
        or not isinstance(input_payload.get("generation_binding"), dict)
        or _sa.content_digest(_sa._json_value(input_payload.get("generation_binding")))
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
        raise _sa.StrictAuthorityError(
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
        raise _sa.StrictAuthorityError(errors)


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
    store = _sa._store()
    events = [
        event
        for event in store.events(str(call.get("run_id") or ""))
        if event.event_type == _sa.INVOCATION_EVIDENCE_BOUND_EVENT
        and event.payload.get("effect_id") == call.get("effect_id")
    ]
    if not events:
        return None
    if len(events) != 1:
        raise _sa.StrictAuthorityError(
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
        or event.payload_digest != _sa.content_digest(payload)
        or payload.get("schema_version") != 1
        or payload.get("kind") != _sa.INVOCATION_EVIDENCE_BINDING_KIND
        or payload.get("run_id") != call.get("run_id")
        or payload.get("slot") != call.get("slot")
        or payload.get("effect_id") != call.get("effect_id")
        or payload.get("invocation_id") != call.get("invocation_id")
        or payload.get("accepted_event_seq") != int(accepted_event.seq)
        or payload.get("accepted_event_payload_digest")
        != accepted_event.payload_digest
        or payload.get("invocation_evidence_digest")
        != _sa.content_digest(_sa._json_value(evidence))
    ):
        raise _sa.StrictAuthorityError(
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
        if _sa._json_value(existing) != _sa._json_value(evidence):
            raise _sa.StrictAuthorityError(
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
        "kind": _sa.INVOCATION_EVIDENCE_BINDING_KIND,
        "run_id": call["run_id"],
        "slot": call["slot"],
        "effect_id": call["effect_id"],
        "invocation_id": call["invocation_id"],
        "accepted_event_seq": int(accepted_event.seq),
        "accepted_event_payload_digest": accepted_event.payload_digest,
        "invocation_evidence_digest": _sa.content_digest(_sa._json_value(evidence)),
        "invocation_evidence": _sa._json_value(evidence),
    }
    try:
        _sa._store().append_event(
            call["run_id"],
            _sa.INVOCATION_EVIDENCE_BOUND_EVENT,
            payload,
            causation_id=(
                "strict-invocation-evidence-bound:" + str(call["effect_id"])
            ),
        )
    except WorkflowConflict as exc:
        raise _sa.StrictAuthorityError(
            "strict_authority_invocation_evidence_binding_conflict"
        ) from exc
    rebound = bound_invocation_evidence(call)
    if rebound is None:
        raise _sa.StrictAuthorityError(
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
        raise _sa.StrictAuthorityError(
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
        raise _sa.StrictAuthorityError(
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
        raise _sa.StrictAuthorityError(exc.errors) from exc
    return bind_invocation_evidence(call, evidence)


# Compatibility aliases for the first implementation name. Active callers use
# the generic API above; keeping these aliases avoids breaking diagnostic code.
bound_master_invocation_evidence = bound_invocation_evidence
bind_master_invocation_evidence = bind_invocation_evidence


def _accepted_events(checkpoint: dict[str, Any]) -> tuple[list[Any], list[str]]:
    try:
        binding = _sa.generation_binding(checkpoint)
        run_id = _sa.authority_run_id(binding["workflow_run_id"])
        store = _sa._store()
        events = store.events(run_id)
    except Exception as exc:
        return [], [f"strict_authority_journal_unavailable:{type(exc).__name__}"]
    accepted = [
        event for event in events if event.event_type == _sa.ACCEPTED_EVENT
    ]
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

    if failed_slot not in _sa.MASTER_SLOTS[:5]:
        raise _sa.StrictAuthorityError(
            f"strict_authority_master_retry_slot_invalid:{failed_slot}"
        )
    binding = _sa.generation_binding(checkpoint)
    binding_digest = _sa.content_digest(binding)
    run_id = _sa.authority_run_id(binding["workflow_run_id"])
    store = _sa._store()
    try:
        effects = store.effects_for_run(run_id)
        events = store.events(run_id)
    except Exception as exc:
        raise _sa.StrictAuthorityError(
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
            effect.get("kind") != _sa.EFFECT_KIND
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
        if event.event_type == _sa.ACCEPTED_EVENT
        and event.payload.get("generation_binding_digest") == binding_digest
        and event.payload.get("slot") in _sa.MASTER_SLOTS[:5]
    })
    pending_slots = [
        slot for slot in _sa.MASTER_SLOTS[:5] if slot not in accepted_slots
    ]
    return {
        "run_id": run_id,
        "failed_slot": failed_slot,
        "role_attempt": max(1, len(failed_effect_ids)),
        "failed_effect_ids": sorted(failed_effect_ids),
        "accepted_slots": accepted_slots,
        "pending_slots": pending_slots,
    }
