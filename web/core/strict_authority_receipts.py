"""Top-level authority projection for strict_authority_workflow.

Extracted as a cohesive business cluster; strict_authority_workflow.py retains
thin delegate shells so external ``from strict_authority_workflow import <name>``
and ``monkeypatch.setattr(strict_authority_workflow, "<name>", ...)`` keep resolving.

Business responsibility
-----------------------
Read-only projection over accepted events: validate all required slots'
receipts, validate the final master projection, and produce the summary used
by gates.

IMPORTANT -- shared-symbol access model
---------------------------------------
Every module-level name referenced by these bodies (slot/contract tables,
``_store`` thread-local, generation/call context builders, invocation-evidence
helpers, and ``validate_receipts`` itself) remains in
``strict_authority_workflow`` because it is part of that module's monkeypatch
surface -- the test suite patches ``strict_authority_workflow._store``,
``strict_authority_workflow._accepted_events``,
``strict_authority_workflow.expected_master_role_results``,
``strict_authority_workflow.expected_master_contexts``,
``strict_authority_workflow.validate_receipts``, and reads them back through
these projection code paths.  Binding them at import time would freeze the
pre-patch value and silently break the audit.

Every such reference in this file is written ``_sa.<name>`` so it resolves
against the live module attribute at call time.  References between members of
*this* module are written as bare globals, exactly as they were inline.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Any, Iterable

from workflow_kernel import content_digest

import strict_authority_workflow as _sa  # noqa: E402  (circular by design)


def validate_receipts(
    checkpoint: dict[str, Any],
    *,
    required_slots: Iterable[str],
    expected_role_results: dict[str, Any] | None = None,
    expected_context_bindings: dict[str, dict[str, Any]] | None = None,
    require_no_other_accepted: bool = False,
    permitted_other_accepted_slots: Iterable[str] = (),
    matching_abandon_reason: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Re-read effects/events and validate exact accepted slot identities.

    ``permitted_other_accepted_slots`` exists only for replaying an immutable
    earlier-phase receipt after a named later phase has appended its authority.
    Every permitted event still traverses the full slot/effect/provider/receipt
    validation below; callers must also validate the current phase as required
    authority.  The option therefore cannot turn an unknown slot into evidence.
    """

    required = tuple(required_slots)
    permitted_other = tuple(permitted_other_accepted_slots)
    expected_role_results = expected_role_results or {}
    expected_context_bindings = expected_context_bindings or {}
    errors: list[str] = []
    if len(set(required)) != len(required) or any(slot not in _sa.KNOWN_SLOTS for slot in required):
        return {}, ["strict_authority_required_slot_set_invalid"]
    if (
        len(set(permitted_other)) != len(permitted_other)
        or any(slot not in _sa.ALL_SLOTS for slot in permitted_other)
        or set(permitted_other) & set(required)
        or (permitted_other and not require_no_other_accepted)
    ):
        return {}, ["strict_authority_permitted_other_slot_set_invalid"]
    if permitted_other and required + permitted_other != _sa.ALL_SLOTS[
        : len(required) + len(permitted_other)
    ]:
        return {}, ["strict_authority_permitted_other_slot_sequence_invalid"]
    try:
        binding = _sa.generation_binding(checkpoint)
        run_id = _sa.authority_run_id(binding["workflow_run_id"])
        binding_digest = content_digest(binding)
    except _sa.StrictAuthorityError as exc:
        return {}, list(exc.errors)
    current_revision = checkpoint.get("checkpoint_revision")
    phase_representatives: dict[str, str] = {}
    for slot in required:
        phase_name, _phase_slots = _sa._authority_phase(slot)
        phase_representatives.setdefault(phase_name, slot)
    phase_anchors: dict[str, int] = {}
    if not _sa._plain_int(current_revision) or int(current_revision) < 0:
        errors.append("strict_authority_checkpoint_revision_invalid")
    else:
        # Final receipt authority covers the complete durable phase, not only
        # its accepted projection.  Re-open every strict EffectRequested row
        # in each required phase so an unaccepted/rejected call cannot smuggle
        # a second checkpoint revision past an otherwise uniform receipt set.
        for phase_name, representative in phase_representatives.items():
            try:
                phase_anchors[phase_name] = _sa._frozen_phase_checkpoint_revision(
                    checkpoint,
                    slot=representative,
                    binding=binding,
                    current_revision=int(current_revision),
                    matching_abandon_reason=matching_abandon_reason,
                )
            except _sa.StrictAuthorityError as exc:
                errors.extend(exc.errors)
    accepted, journal_errors = _sa._accepted_events(checkpoint)
    errors.extend(journal_errors)
    store = _sa._store()
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
        if slot not in _sa.KNOWN_SLOTS:
            errors.append(f"strict_authority_unexpected_slot:{slot}")
            continue
        expected_role, expected_purpose = _sa.SLOT_CONTRACTS[slot]
        expected = {
            "schema_version": 1,
            "kind": _sa.RECEIPT_KIND,
            "run_id": run_id,
            "slot": slot,
            "role": expected_role,
            "purpose": expected_purpose,
            "generation_binding_digest": binding_digest,
            "schema_valid": True,
            "checkpoint_stage": _sa.SLOT_STAGES[slot],
            "parse_contract": _sa.SLOT_PARSE_CONTRACTS[slot],
            "model": "sonnet",
            "tools": _sa.SLOT_TOOLS[slot],
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
        if not _sa._plain_int(revision) or int(revision) < 0:
            errors.append(f"strict_authority_{slot}_checkpoint_revision_invalid")
        else:
            revisions[slot] = int(revision)
        effect = store.effect(effect_id)
        provider = effect.get("result_payload") or {}
        input_payload = effect.get("input_payload") or {}
        if effect.get("run_id") != run_id or effect.get("kind") != _sa.EFFECT_KIND:
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
                if _sa._json_value(parsed_raw) != _sa._json_value(
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
        if provider.get("parse_contract") != _sa.SLOT_PARSE_CONTRACTS[slot]:
            errors.append(f"strict_authority_{slot}_projection_contract_invalid")
        if provider.get("role_projection_valid") is not True:
            errors.append(f"strict_authority_{slot}_role_projection_invalid")
        if provider.get("projected_role_result_digest") != content_digest(
            _sa._json_value(provider.get("projected_role_result"))
        ):
            errors.append(f"strict_authority_{slot}_role_projection_digest_invalid")
        if not _sa._valid_digest(payload.get("role_result_digest")):
            errors.append(f"strict_authority_{slot}_role_result_digest_invalid")
        if payload.get("role_result_digest") != content_digest(
            _sa._json_value(payload.get("role_result"))
        ):
            errors.append(f"strict_authority_{slot}_stored_role_result_mismatch")
        if (
            provider.get("projected_role_result_digest")
            != payload.get("role_result_digest")
            or _sa._json_value(provider.get("projected_role_result"))
            != _sa._json_value(payload.get("role_result"))
        ):
            errors.append(f"strict_authority_{slot}_role_projection_mismatch")
        if slot in expected_role_results:
            expected_digest = content_digest(_sa._json_value(expected_role_results[slot]))
            if payload.get("role_result_digest") != expected_digest:
                errors.append(f"strict_authority_{slot}_role_result_mismatch")
        if slot in expected_context_bindings:
            expected_context_digest = content_digest(
                _sa._json_value(expected_context_bindings[slot])
            )
            if payload.get("context_binding_digest") != expected_context_digest:
                errors.append(f"strict_authority_{slot}_context_binding_mismatch")
        refs[slot] = {
            "schema_version": 1,
            "kind": _sa.RECEIPT_KIND,
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
        extras = sorted(set(by_slot) - set(required) - set(permitted_other))
        if extras:
            errors.append("strict_authority_unexpected_accepted_slots:" + ",".join(extras))
        for slot in permitted_other:
            count = len(by_slot.get(slot, []))
            if count > 1:
                errors.append(f"strict_authority_{slot}_accepted_count:{count}")
    if permitted_other:
        # A permitted slot is an append-only suffix, never an unordered
        # whitelist.  Missing trailing slots are valid while a later phase has
        # not happened yet, but a suffix cannot skip an earlier permitted slot
        # and every observed suffix event must be strictly later than the
        # complete required boundary.  Master slots themselves remain free to
        # complete concurrently in any event order.
        present_suffix = [
            slot for slot in permitted_other if len(by_slot.get(slot, [])) == 1
        ]
        if present_suffix != list(permitted_other[: len(present_suffix)]):
            errors.append("strict_authority_permitted_other_slot_gap")
        required_events = [
            by_slot[slot][0]
            for slot in required
            if len(by_slot.get(slot, [])) == 1
        ]
        suffix_events = [by_slot[slot][0] for slot in present_suffix]
        if len(required_events) == len(required) and suffix_events:
            required_boundary = max(int(event.seq) for event in required_events)
            previous_seq = required_boundary
            for slot, event in zip(present_suffix, suffix_events):
                event_seq = int(event.seq)
                if event_seq <= previous_seq:
                    errors.append(
                        "strict_authority_permitted_other_event_order_invalid:"
                        + slot
                    )
                previous_seq = event_seq
    for phase_name, representative in phase_representatives.items():
        anchor = phase_anchors.get(phase_name)
        if anchor is None:
            continue
        _name, phase_slots = _sa._authority_phase(representative)
        accepted_phase_revisions = {
            revisions[slot] for slot in phase_slots if slot in revisions
        }
        if accepted_phase_revisions and accepted_phase_revisions != {anchor}:
            errors.append(
                f"strict_authority_phase_checkpoint_revision_drift:{phase_name}"
            )
    master_revisions = {
        revisions[slot] for slot in _sa.MASTER_SLOTS if slot in revisions
    }
    if len(master_revisions) > 1:
        errors.append("strict_authority_master_checkpoint_revision_drift")
    if master_revisions:
        master_revision = next(iter(master_revisions))
        review_revision = revisions.get("review")
        review_retry_revision = revisions.get("review:retry")
        critic_revision = revisions.get("critic")
        if review_revision is not None and review_revision <= master_revision:
            errors.append("strict_authority_review_revision_precedes_master")
        if (
            review_revision is not None
            and review_retry_revision is not None
            and review_retry_revision <= review_revision
        ):
            errors.append("strict_authority_review_retry_revision_precedes_review")
        if review_retry_revision is not None and critic_revision is not None:
            errors.append("strict_authority_critic_after_review_retry_forbidden")
        if (
            review_revision is not None
            and critic_revision is not None
            and critic_revision <= review_revision
        ):
            errors.append("strict_authority_critic_revision_precedes_review")
    return refs, list(dict.fromkeys(errors))


def validate_master_final_projection(
    checkpoint: dict[str, Any],
    plan: dict[str, Any],
    *,
    candidate_dir: str | Path,
    project_root: str | Path,
    require_no_other_accepted: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """Replay the deterministic post-Master compiler and bind it to the journal.

    ``master:final`` is accepted before the system attaches the architecture
    policy and builds the runtime-contract ledger.  The accepted payload itself
    is retained in WorkflowStore, so validation can replay those exact lossless
    transformations in a private temporary tree.  Generated task briefs are a
    generic compaction aid, never a strict-Master authority form.
    """

    if not isinstance(plan, dict):
        return {}, ["strict_authority_master_plan_missing"]
    if any(
        isinstance(task, dict)
        and any(
            field in task
            for field in (
                "worker_prompt_compiled",
                "worker_prompt_original_chars",
                "task_brief_file",
            )
        )
        for task in (plan.get("tasks") or [])
    ):
        return {}, [
            "strict_authority_master_projection_externalization_forbidden"
        ]
    expected_roles = _sa.expected_master_role_results(plan)
    refs, errors = _sa.validate_receipts(
        checkpoint,
        required_slots=_sa.MASTER_SLOTS,
        expected_role_results=expected_roles,
        expected_context_bindings=_sa.expected_master_contexts(plan),
        require_no_other_accepted=bool(require_no_other_accepted),
    )
    if errors:
        return {}, errors

    accepted, journal_errors = _sa._accepted_events(checkpoint)
    if journal_errors:
        return {}, journal_errors
    try:
        expected_invocations = _sa.expected_master_invocation_evidence(plan)
        for slot in _sa.MASTER_SLOTS[:5]:
            slot_events = [
                event
                for event in accepted
                if event.payload.get("slot") == slot
            ]
            if len(slot_events) != 1:
                continue
            bound = _sa.bound_invocation_evidence(
                dict(slot_events[0].payload)
            )
            if _sa._json_value(bound) != _sa._json_value(expected_invocations[slot]):
                errors.append(
                    f"strict_authority_{slot}_invocation_evidence_mismatch"
                )
    except _sa.StrictAuthorityError as exc:
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
    if not _sa._plain_int(source_v) or not _sa._plain_int(next_v):
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
        # The production compiler owns both policy-ABI and Worker-contract
        # binding.  It must receive the normalized accepted role result exactly
        # once: pre-binding its input would change the compiler receipt
        # (``bound``/``bound_tasks``) and make an otherwise byte-identical plan
        # fail replay.
        #
        # ``compile_master_plan`` writes its context from the intermediate,
        # bound prompt, however.  Derive that *comparison-only* source through
        # the same pure binders without feeding it back into the compiler.  This
        # makes the context-content check follow production while preserving the
        # compiler's one-pass provenance.
        context_source, expected_policy_binding = bind_system_owned_policy_abi(
            replay_plan
        )
        context_source, expected_contract_binding = (
            bind_system_owned_worker_contract_terms(context_source)
        )

        with tempfile.TemporaryDirectory(prefix="pok-strict-master-replay-") as raw:
            replay_root = Path(raw).resolve()
            replay_target = replay_root / "candidate"
            replay_target.mkdir(parents=True, exist_ok=True)
            replayed, compiler = compile_master_plan(
                replay_plan,
                next_v=int(next_v),
                target_dir=replay_target,
                project_root=replay_root,
            )
            replay_contract = compiler.get("contract_binding") or {}
            if _sa._json_value(compiler.get("policy_abi_binding") or {}) != _sa._json_value(
                expected_policy_binding
            ) or _sa._json_value(replay_contract) != _sa._json_value(
                expected_contract_binding
            ):
                projection_errors.append(
                    "strict_authority_master_projection_compiler_binding_mismatch"
                )
            if any(replay_contract.get(key) for key in (
                "invalid_contract_tasks",
                "invalid_prompt_tasks",
                "overflow_tasks",
            )):
                projection_errors.append(
                    "strict_authority_master_projection_compiler_binding_invalid"
                )
            if compiler.get("compiled"):
                # Strict authority compares a lossless, sealed Master result.
                # A generated task brief is a generic compaction aid, not a
                # replayable strict-Master authority form.
                projection_errors.append(
                    "strict_authority_master_projection_externalization_forbidden"
                )
            compiled_rows = compiler.get("compiled_tasks") or []
            if any(row.get("context_trimmed") is not False for row in compiled_rows):
                projection_errors.append(
                    "strict_authority_master_projection_context_trimmed"
                )

            context_source_tasks = {
                str(task.get("worker_id", index + 1)): task
                for index, task in enumerate(context_source.get("tasks") or [])
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
                source_task = context_source_tasks.get(worker_key)
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
                # This path substitution changes the compiled prompt length
                # whenever the real candidate path differs from the temporary
                # replay root.  Keep the compiler receipt self-consistent so
                # the exact replayed plan can be content-compared below.
                row["compiled_chars"] = len(compiled_task["worker_prompt"])

            # ``plan_compiler`` stores the same compiler payload, but do not
            # depend on object aliasing after a future refactor.
            if compiler.get("compiled"):
                replayed["plan_compiler"] = deepcopy(compiler)
            replayed = attach_runtime_contract_ledger(replayed, replace=True)

        if not projection_errors and _sa._json_value(replayed) != _sa._json_value(plan):
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
        "compiled_plan_digest": content_digest(_sa._json_value(plan)),
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
    permitted_other_accepted_slots: Iterable[str] = (),
    matching_abandon_reason: str | None = None,
) -> dict[str, Any]:
    required_slots = tuple(required_slots)
    expected_invocation_evidence = expected_invocation_evidence or {}
    refs, errors = _sa.validate_receipts(
        checkpoint,
        required_slots=required_slots,
        expected_role_results=expected_role_results,
        expected_context_bindings=expected_context_bindings,
        require_no_other_accepted=require_no_other_accepted,
        permitted_other_accepted_slots=permitted_other_accepted_slots,
        matching_abandon_reason=matching_abandon_reason,
    )
    required_set = set(required_slots)
    invalid_expected_slots = set(expected_invocation_evidence) - (
        required_set & set(_sa.INVOCATION_EVIDENCE_SLOTS)
    )
    if invalid_expected_slots:
        errors.append(
            "strict_authority_expected_invocation_evidence_slots_invalid:"
            + ",".join(sorted(invalid_expected_slots))
        )
    bound_slots: list[str] = []
    if set(_sa.MASTER_SLOTS).issubset(required_set):
        bound_slots.extend(_sa.MASTER_SLOTS[:5])
    bound_slots.extend(slot for slot in _sa.GATE_SLOTS if slot in required_set)
    if not errors and bound_slots:
        accepted, journal_errors = _sa._accepted_events(checkpoint)
        errors.extend(journal_errors)
        expected_invocations = dict(expected_invocation_evidence)
        final_context = (expected_context_bindings or {}).get("master:final")
        if isinstance(final_context, dict) and isinstance(
            final_context.get("proposal_packet"), dict
        ):
            try:
                expected_invocations.update(_sa.expected_master_invocation_evidence({
                    "proposal_ensemble": final_context["proposal_packet"],
                }))
            except _sa.StrictAuthorityError as exc:
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
                bound = _sa.bound_invocation_evidence(
                    dict(slot_events[0].payload)
                )
                if bound is None:
                    errors.append(
                        f"strict_authority_{slot}_invocation_evidence_unbound"
                    )
                elif (
                    slot in expected_invocations
                    and _sa._json_value(bound)
                    != _sa._json_value(expected_invocations[slot])
                ):
                    errors.append(
                        f"strict_authority_{slot}_invocation_evidence_mismatch"
                    )
            except _sa.StrictAuthorityError as exc:
                errors.extend(exc.errors)
    if errors:
        raise _sa.StrictAuthorityError(errors)
    subject = {
        "schema_version": 1,
        "kind": "first-strict-llm-authority-summary-v1",
        "run_id": _sa.authority_run_id(str(checkpoint.get("workflow_run_id") or "")),
        "required_slots": list(required_slots),
        "receipts": {slot: refs[slot] for slot in required_slots},
    }
    return {**subject, "summary_digest": content_digest(subject)}
