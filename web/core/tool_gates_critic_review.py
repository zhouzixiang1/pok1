"""Critic + Review stage runner subsystem.

Extracted from tool_gates.py as a single business responsibility: the
advisory schema-valid Lead Code Reviewer (run_review) and the advisory
schema-valid Critic (run_critic), plus their supporting semantic-contract
and reviewer-prompt-rendering helpers. Distinct stage-runner business from
run_quality_gates / smoke / official / prepare_next_gen.

All public symbols are re-exported by tool_gates.py for backward compatibility.
"""

import hashlib
import json
import re
import time
from pathlib import Path

import tool_gates as _tg  # _tg._run_critic + staying helpers; respects test monkeypatches

from bot_namespace import bot_name


def _review_semantic_contract(master_plan, quality_gate):
    """Project the checkpoint-owned meaning of one Reviewer invocation.

    A fixed first-strict blueprint is already materialized by the system.  Its
    selected Master proposal is a capability-audit lens, not a Worker strategy
    implementation contract.  Normal generations keep the stronger mechanism
    implementation semantics.  Bind that distinction and the exact quality
    evidence into the provider input so prompt prose cannot silently swap the
    two modes.
    """

    if not isinstance(master_plan, dict) or not isinstance(quality_gate, dict):
        raise ValueError("Reviewer semantic source must be checkpoint objects")
    binding = master_plan.get("proposal_binding")
    evidence = quality_gate.get("selected_proposal_quality_evidence")
    if not isinstance(binding, dict) or not isinstance(evidence, dict):
        raise ValueError("Reviewer selected proposal evidence missing")
    execution_mode = str(binding.get("execution_mode") or "")
    review_semantic_mode = _tg._REVIEW_SEMANTIC_MODES.get(execution_mode)
    if review_semantic_mode is None:
        raise ValueError("Reviewer execution mode is not recognized")

    proposal_digest = str(binding.get("contract_digest") or "")
    check_id = str(((binding.get("falsifier") or {}).get("test_name")) or "")
    check_evidence_digest = str(evidence.get("check_evidence_digest") or "")
    evidence_proposal_digest = str(
        evidence.get("proposal_contract_digest") or ""
    )
    changed_symbols = evidence.get("changed_reachable_symbols")
    errors = evidence.get("errors")
    if not isinstance(changed_symbols, list) or any(
        not isinstance(item, str) for item in changed_symbols
    ):
        raise ValueError("Reviewer changed reachable symbols are invalid")
    if not isinstance(errors, list) or any(not isinstance(item, str) for item in errors):
        raise ValueError("Reviewer selected capability errors are invalid")

    invalid = []
    architecture_transition = quality_gate.get(
        "national_architecture_transition"
    )
    capability_contract = quality_gate.get("national_capability_contract")
    if not isinstance(architecture_transition, dict):
        architecture_transition = {}
        invalid.append("architecture_transition_missing")
    if not isinstance(capability_contract, dict):
        capability_contract = {}
        invalid.append("capability_contract_missing")
    selected_checks = architecture_transition.get("selected_dynamic_checks")
    selected_failures = architecture_transition.get(
        "selected_dynamic_failures"
    )
    if not isinstance(selected_checks, list) or any(
        not isinstance(item, str) for item in selected_checks
    ):
        selected_checks = []
        invalid.append("selected_dynamic_checks_invalid")
    if not isinstance(selected_failures, list) or any(
        not isinstance(item, str) for item in selected_failures
    ):
        selected_failures = []
        invalid.append("selected_dynamic_failures_invalid")
    transition_checks = (
        ((architecture_transition.get("candidate_capabilities") or {}).get(
            "checks_by_id"
        ) or {})
        if isinstance(architecture_transition.get("candidate_capabilities"), dict)
        else {}
    )
    actual_check_row = (
        transition_checks.get(check_id)
        if isinstance(transition_checks, dict)
        else None
    )
    capability_checks = capability_contract.get("checks_by_id")
    capability_check_row = (
        capability_checks.get(check_id)
        if isinstance(capability_checks, dict)
        else None
    )
    if not isinstance(actual_check_row, dict):
        invalid.append("selected_capability_actual_check_missing")
        actual_check_row = {}
    else:
        from bot_artifact import canonical_digest

        if canonical_digest(actual_check_row) != check_evidence_digest:
            invalid.append("selected_capability_actual_check_digest_mismatch")
        if actual_check_row.get("check_id") != check_id:
            invalid.append("selected_capability_actual_check_id_mismatch")
        if actual_check_row.get("passed") is not True:
            invalid.append("selected_capability_actual_check_not_passed")
    if check_id not in selected_checks:
        invalid.append("selected_capability_not_in_selected_dynamic_checks")
    if check_id in selected_failures:
        invalid.append("selected_capability_in_selected_dynamic_failures")
    if architecture_transition.get("ok") is not True:
        invalid.append("architecture_transition_not_ok")
    if capability_contract.get("ok") is not True:
        invalid.append("capability_contract_not_ok")
    if capability_check_row != actual_check_row:
        invalid.append("capability_contract_check_projection_mismatch")
    if quality_gate.get("all_passed") is not True:
        invalid.append("quality_not_all_passed")
    if quality_gate.get("critical_scenarios_passed") is not True:
        invalid.append("quality_critical_scenarios_not_passed")
    if evidence.get("required") is not True or evidence.get("ok") is not True:
        invalid.append("selected_capability_not_passed")
    if quality_gate.get("selected_proposal_quality_ok") is not True:
        invalid.append("selected_capability_gate_flag_not_passed")
    if check_id == "" or evidence.get("check_id") != check_id:
        invalid.append("selected_capability_check_mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", proposal_digest):
        invalid.append("proposal_contract_digest_invalid")
    if evidence_proposal_digest != proposal_digest:
        invalid.append("proposal_contract_digest_mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", check_evidence_digest):
        invalid.append("selected_capability_evidence_digest_invalid")
    if evidence.get("evidence_scope") != _tg._SELECTED_CAPABILITY_EVIDENCE_SCOPE:
        invalid.append("selected_capability_evidence_scope_invalid")
    if errors:
        invalid.append("selected_capability_evidence_has_errors")

    requires_reachable_delta = execution_mode == "strategy_implementation"
    if evidence.get("reachable_symbol_diff_required") is not requires_reachable_delta:
        invalid.append("reachable_symbol_requirement_mode_mismatch")
    if evidence.get("reachable_symbol_diff_ok") is not True:
        invalid.append("reachable_symbol_evidence_failed")
    reachable_digest = str(evidence.get("reachable_symbol_diff_digest") or "")
    if requires_reachable_delta:
        if not changed_symbols:
            invalid.append("strategy_reachable_symbol_delta_missing")
        if not re.fullmatch(r"[0-9a-f]{64}", reachable_digest):
            invalid.append("strategy_reachable_symbol_digest_invalid")
    elif changed_symbols or reachable_digest:
        invalid.append("fixed_audit_forbids_reachable_delta_claim")
    if invalid:
        raise ValueError("Reviewer semantic contract invalid: " + ",".join(invalid))

    capability_projection = {
        "schema_version": 1,
        "check_id": check_id,
        "check_evidence_digest": check_evidence_digest,
        "proposal_contract_digest": evidence_proposal_digest,
        "evidence_scope": evidence["evidence_scope"],
        "reachable_symbol_diff_required": requires_reachable_delta,
        "reachable_symbol_diff_ok": True,
        "changed_reachable_symbols": list(changed_symbols),
        "reachable_symbol_diff_digest": reachable_digest,
        "actual_check_row": json.loads(json.dumps(actual_check_row)),
        "selected_dynamic_checks": list(selected_checks),
        "selected_dynamic_failures": list(selected_failures),
        "capability_contract_projection_equal": True,
    }
    subject = {
        "schema_version": 1,
        "review_semantic_mode": review_semantic_mode,
        "execution_mode": execution_mode,
        "selected_proposal_id": str(binding.get("selected_proposal_id") or ""),
        "proposal_contract_digest": proposal_digest,
        "selected_capability_evidence": capability_projection,
        "selected_capability_evidence_digest": _tg._canonical_digest(
            capability_projection
        ),
        "quality_gate_digest": _tg._canonical_digest(quality_gate),
    }
    return {**subject, "contract_digest": _tg._canonical_digest(subject)}


def _quality_review_evidence_projection(quality_result):
    """Select the exact Quality fields persisted for the downstream Reviewer.

    This projection is merged into the same ``quality`` gate payload passed to
    ``_record_gate``.  It deliberately does not reconstruct evidence from the
    broader architecture/capability reports on recovery.
    """

    if not isinstance(quality_result, dict):
        raise ValueError("Quality result must be an object")
    evidence = quality_result.get("selected_proposal_quality_evidence")
    ok = quality_result.get("selected_proposal_quality_ok")
    if not isinstance(evidence, dict) or not isinstance(ok, bool):
        raise ValueError("Quality selected proposal projection missing")
    return {
        "selected_proposal_quality_evidence": json.loads(json.dumps(evidence)),
        "selected_proposal_quality_ok": ok,
    }


def _render_reviewer_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    expected = {
        "master_plan", "source_v", "next_v", "strict_bootstrap",
        "invocation_id", "authority_slot", "focus_areas",
        "review_semantic_contract",
    }
    if not isinstance(inputs, dict) or set(inputs) != expected:
        raise ValueError("Reviewer renderer input contract mismatch")
    master_plan = inputs["master_plan"]
    focus_areas = inputs["focus_areas"]
    semantic_contract = inputs["review_semantic_contract"]
    if (
        not isinstance(master_plan, dict)
        or not isinstance(focus_areas, list)
        or not isinstance(semantic_contract, dict)
    ):
        raise ValueError("Reviewer renderer typed input mismatch")
    semantic_subject = {
        key: value
        for key, value in semantic_contract.items()
        if key != "contract_digest"
    }
    if semantic_contract.get("contract_digest") != _tg._canonical_digest(
        semantic_subject
    ):
        raise ValueError("Reviewer semantic contract digest mismatch")
    review_semantic_mode = semantic_contract.get("review_semantic_mode")
    fixed_capability_audit = (
        review_semantic_mode == "fixed_blueprint_capability_audit_v1"
    )
    if fixed_capability_audit:
        semantic_instructions = (
            "FIXED BLUEPRINT CAPABILITY AUDIT: the system, not the selected "
            "proposal prose, owns the prepared policy bytes. Review those "
            "bytes for code correctness, the five-file ABI, national protocol "
            "safety, deadlines/sandbox boundaries, dead code, and the bound "
            "selected-capability quality projection below. The proposal's "
            "structural_change, reachable_chain, helper names, field names, "
            "counterfactual, and expected_diff are an audit lens only. Do not "
            "reject because the fixed blueprint uses different identifiers or "
            "code structure. Do not claim proposal causality or poker strength. "
            "The content-bound quality projection is the authority that the "
            "named typed capability check executed and passed; do not replace "
            "it with a new prose-derived implementation requirement."
        )
    elif review_semantic_mode == "strategy_implementation_v1":
        semantic_instructions = (
            "STRATEGY IMPLEMENTATION REVIEW: the selected proposal is the "
            "Worker implementation contract. Require the selected mechanism, "
            "declared target files, materially changed reachable chain, typed "
            "falsifier evidence, and Worker task to agree. Reject telemetry-only "
            "or unreachable implementations and unrelated strategy drift."
        )
    else:
        raise ValueError("Reviewer semantic mode is not recognized")
    source_v = int(inputs["source_v"])
    next_v = int(inputs["next_v"])
    strict_bootstrap = bool(inputs["strict_bootstrap"])
    authority_slot = str(inputs["authority_slot"] or "")
    if authority_slot not in {"review", "review:retry"} or (
        not strict_bootstrap and authority_slot != "review"
    ):
        raise ValueError("Reviewer authority slot is invalid")
    text = (
        Path(__file__).resolve().parent / "prompts" / "reviewer_prompt.md"
    ).read_text(encoding="utf-8")
    text = text.replace(
        "{master_plan}",
        json.dumps(master_plan, indent=2, ensure_ascii=False),
    )
    text = text.replace("{version}", str(next_v))
    text = text.replace("{parent_version}", str(source_v))
    text = text.replace(
        "{review_semantic_contract}",
        json.dumps(semantic_contract, indent=2, ensure_ascii=False),
    )
    text = text.replace(
        "{review_semantic_instructions}", semantic_instructions
    )
    if strict_bootstrap:
        prompt_fields = {
            "{review_tool_contract}": (
                "Read the prepared target only; Bash and historical lineage are unavailable"
            ),
            "{review_lineage_contract}": (
                f"Prepared `bots/{bot_name(next_v)}/` is the sole readable code artifact. "
                f"v{source_v} is numeric high-water only, not a parent or readable path."
            ),
            "{review_evaluation_step_one}": (
                (
                    "Read the prepared policy regions needed to check code "
                    "correctness and the bound capability evidence. Treat "
                    "functions named by the frozen Master plan only as "
                    "navigation hints, never as required identifiers or a "
                    "required implementation structure; the system Worker "
                    "boundary already proved the five-file scope and preimage "
                    "delta."
                )
                if fixed_capability_audit
                else (
                    "Read the target functions named by the frozen Master plan; "
                    "the system Worker boundary already proved the five-file "
                    "scope and preimage delta."
                )
            ),
            "{review_size_baseline_contract}": (
                "This prepared strict-bootstrap candidate has no readable historical "
                "parent. Apply the 2000-line base and 2500-line hard cap to current "
                "`policy.py`; the preceding quality gate already proved the exact "
                "candidate size and scope receipt."
            ),
        }
    else:
        prompt_fields = {
            "{review_tool_contract}": (
                "Read source/target files; Bash is limited to direct, statically "
                "bounded reads and source-to-target comparisons"
            ),
            "{review_lineage_contract}": (
                f"Exact current-epoch source: `bots/{bot_name(source_v)}/`; "
                f"candidate: `bots/{bot_name(next_v)}/`."
            ),
            "{review_evaluation_step_one}": (
                f"Compare only explicit source/target files under "
                f"`bots/{bot_name(source_v)}/` and `bots/{bot_name(next_v)}/`; "
                "do not inspect Git history."
            ),
            "{review_size_baseline_contract}": (
                "The quality gate's adaptive limit permits a child to match or "
                "shrink, but not grow beyond, an inherited oversized source. Compare "
                f"explicit `policy.py` line counts under `bots/{bot_name(source_v)}/` "
                f"and `bots/{bot_name(next_v)}/`. Reject growth beyond an oversized source; "
                "treat shrink/maintenance as marginal and flag it in `risk_areas`. "
                "If the source is within limits and the child exceeds them, apply "
                "the normal reject/marginal rule."
            ),
        }
    for placeholder, value in prompt_fields.items():
        text = text.replace(placeholder, value)
    from strategy_reference_pack import current_strict_runtime_prompt_overlay

    text += "\n\n" + current_strict_runtime_prompt_overlay()
    invocation_id = str(inputs["invocation_id"] or "")
    if strict_bootstrap:
        if not invocation_id:
            raise ValueError("Reviewer strict invocation id missing")
        text += (
            "\n\nSYSTEM CALL BINDING (copying this value does not grant authority): "
            f"invocation_id={invocation_id}; "
            f"purpose=system_strict_bootstrap_gate:{authority_slot}."
        )
    if focus_areas:
        text += (
            "\n\n# Worker CoT Audit Findings (from execute_workers)\n"
            "The Worker Chain-of-Thought audit detected these concerns.\n"
            "Pay EXTRA attention to these areas during your review:\n"
            + "".join(f"- {str(item)}\n" for item in focus_areas)
            + "\n"
        )
    prompt_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    return LLMRenderedMaterial(
        text=text,
        evidence_kind="review_candidate_pair",
        evidence_provenance={
            "source_v": int(inputs["source_v"]),
            "next_v": int(inputs["next_v"]),
            "review_prompt_digest": prompt_digest,
            "review_semantic_contract_digest": str(
                semantic_contract["contract_digest"]
            ),
            "review_authority_slot": authority_slot,
            "focus_areas_digest": hashlib.sha256(
                json.dumps(
                    focus_areas,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        },
    )


def _critic_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "approved", "approve"}
    return bool(value)


async def run_review(args):
    _t0 = time.time()
    v, source_v = _tg._resolve_version_args(args)
    if v is None or source_v is None:
        return _tg._json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    supplied_plan = args.get("plan", [])

    _tg._set_pipeline_status(f"Reviewing v{v}")

    ckpt = _tg._matching_checkpoint(v, source_v)
    _review_infra, _review_infra_error = _tg._owned_infrastructure_failure(
        ckpt,
        "run_review",
    )
    if _review_infra_error:
        return _tg._state_blocked(_review_infra_error, v, source_v, ckpt)
    _review_exhausted = await _tg._execute_exhausted_infrastructure_failure(
        v,
        source_v,
        owner_tool="run_review",
    )
    if _review_exhausted is not None:
        return _tg._json_tool_result(_review_exhausted)

    # Idempotency guard: skip if review already approved
    _cached = _tg._idempotency_check(
        v, source_v,
        stage_set=("reviewed", "critic_checked", "verified", "archived"),
        gate_name="review",
        directive="Review ALREADY PASSED. Call run_critic next.",
        cache_validator=lambda checkpoint, _gate: _tg._review_gate_ok(checkpoint),
    )
    if _cached:
        return _cached

    if not _tg._quality_gate_ok(ckpt):
        return _tg._state_blocked(
            "run_review requires run_quality_gates all_passed=true and critical_scenarios_passed=true for the same version/source_v.",
            v,
            source_v,
            ckpt,
        )

    authoritative_plan = (
        ckpt.get("master_plan")
        if isinstance(ckpt, dict) and isinstance(ckpt.get("master_plan"), dict)
        else {"tasks": supplied_plan if isinstance(supplied_plan, list) else []}
    )
    if supplied_plan and supplied_plan != authoritative_plan.get("tasks"):
        _tg.log_system_event(
            "pipeline.review_plan_argument_ignored",
            "warn",
            f"run_review v{v} ignored a plan argument that differed from checkpoint authority",
            {"version": v, "source_v": source_v},
        )

    from system_strict_bootstrap import is_declared_native_bootstrap

    _strict_bootstrap = is_declared_native_bootstrap(ckpt)
    _review_semantics = None
    _strict_review_base_context = None
    if _strict_bootstrap:
        from strict_authority_workflow import (
            StrictAuthorityError,
            gate_call_context,
        )

        try:
            # The base review context is the cycle identity for both verdict
            # attempts.  Attempt two has its own authority slot/context, but it
            # may not change the artifact, Quality handoff, or prompt semantics.
            _strict_review_base_context = gate_call_context(
                ckpt,
                gate_name="review",
                candidate_dir=_tg.get_bot_dir(v),
            )
            _review_cycle_contract_digest = _tg._canonical_digest(
                _strict_review_base_context
            )
        except (StrictAuthorityError, ValueError) as exc:
            return _tg._json_tool_result(
                await _tg._abandon_strict_gate_authority(
                    ckpt,
                    gate_name="review",
                    error=exc,
                )
            )
    else:
        try:
            _review_semantics = _review_semantic_contract(
                authoritative_plan,
                (ckpt.get("gate_results") or {}).get("quality") or {},
            )
            _review_cycle_contract_digest = str(
                _review_semantics["contract_digest"]
            )
        except (TypeError, ValueError) as exc:
            return _tg._state_blocked(
                f"run_review semantic contract invalid: {exc}",
                v,
                source_v,
                ckpt,
            )

    from reviewer_retry import (
        ReviewRetryError,
        current_review_attempts,
        review_attempt_action,
        validate_strict_review_attempt_authority,
    )

    try:
        _current_review_attempts = current_review_attempts(
            ckpt,
            candidate_dir=_tg.get_bot_dir(v),
            review_semantic_contract_digest=_review_cycle_contract_digest,
        )
        _review_retry_action = review_attempt_action(_current_review_attempts)
    except ReviewRetryError as exc:
        return _tg._state_blocked(
            "run_review durable attempt journal invalid: "
            + "; ".join(exc.errors),
            v,
            source_v,
            ckpt,
        )
    if _review_retry_action.get("action") != "dispatch":
        return _tg._state_blocked(
            "run_review verdict attempts are already adjudicated for this "
            "artifact/Quality cycle.",
            v,
            source_v,
            ckpt,
        )
    _review_verdict_attempt = int(_review_retry_action["attempt"])
    _review_authority_slot = (
        "review" if _review_verdict_attempt == 1 else "review:retry"
    )

    _review_invocation_id = None
    _review_strict_call = None
    _review_infra_harness_identity = None
    if _strict_bootstrap:
        from strict_authority_workflow import new_call, render_gate_provider_prompt

        try:
            _review_strict_call = new_call(
                ckpt,
                slot=_review_authority_slot,
                context_binding=(
                    _strict_review_base_context
                    if _review_verdict_attempt == 1
                    else gate_call_context(
                        ckpt,
                        gate_name="review:retry",
                        candidate_dir=_tg.get_bot_dir(v),
                    )
                ),
            )
            if _review_verdict_attempt == 2:
                # A structurally valid checkpoint row is not enough to authorize
                # another provider. Reopen the first verdict's exact Master,
                # provider, evidence, role-result and context authority before
                # any fresh review:retry dispatch. A recovered completed retry
                # may already be an accepted suffix, so that no-provider-replay
                # case validates the suffix without treating its presence as a
                # fresh-dispatch conflict; the full two-row authority is checked
                # again before checkpoint projection below.
                _prior_authority_errors = (
                    validate_strict_review_attempt_authority(
                        ckpt,
                        journal=_current_review_attempts,
                        candidate_dir=_tg.get_bot_dir(v),
                        require_no_other_accepted=not bool(
                            _review_strict_call.get("replay_provider")
                        ),
                    )
                )
                if _prior_authority_errors:
                    return _tg._json_tool_result(
                        await _tg._abandon_strict_gate_authority(
                            ckpt,
                            gate_name="review",
                            error=StrictAuthorityError(
                                _prior_authority_errors
                            ),
                        )
                    )
            _review_invocation_id = _review_strict_call["invocation_id"]
            rendered_prompt = render_gate_provider_prompt(_review_strict_call)
            _review_infra_harness_identity = (
                _tg._strict_review_infrastructure_harness_identity(
                    _review_strict_call
                )
            )
        except (StrictAuthorityError, ValueError) as exc:
            return _tg._json_tool_result(
                await _tg._abandon_strict_gate_authority(
                    ckpt,
                    gate_name="review",
                    error=exc,
                )
            )
    else:
        # Inject Worker CoT audit_focus_areas into the ordinary reviewer.  The
        # strict path above derives this evidence once inside its descriptor.
        _review_ckpt = _tg._matching_checkpoint(v, source_v)
        _focus_areas = []
        if _review_ckpt:
            _audit_context = _review_ckpt.get("audit_context", {}) or {}
            _focus_areas = _audit_context.get("worker_cot_focus_areas", [])
            if not _focus_areas:
                _worker_gate = _review_ckpt.get("gate_results", {}).get("workers", {})
                _focus_areas = _worker_gate.get("audit_focus_areas", [])

        from llm_query import render_llm_prompt

        rendered_prompt = render_llm_prompt(
            "LEAD CODE REVIEWER",
            producer=_render_reviewer_provider_prompt,
            renderer_inputs={
                "master_plan": authoritative_plan,
                "source_v": int(source_v),
                "next_v": int(v),
                "strict_bootstrap": False,
                "invocation_id": "",
                "authority_slot": "review",
                "focus_areas": [str(item) for item in _focus_areas],
                "review_semantic_contract": _review_semantics,
            },
        )
    _review_attempt_key, _review_infra_metadata = _tg._llm_gate_infrastructure_identity(
        component="reviewer_llm",
        role="LEAD CODE REVIEWER",
        candidate_dir=_tg.get_bot_dir(v),
        source_dir=None if _strict_bootstrap else _tg.get_bot_dir(source_v),
        prompt_text=str(rendered_prompt),
        checkpoint=ckpt,
        harness_identity_override=_review_infra_harness_identity,
        source_fingerprint_override=(
            hashlib.sha256(
                f"numeric-high-water-only:v{source_v}".encode("ascii")
            ).hexdigest()
            if _strict_bootstrap
            else None
        ),
    )

    log_file = _tg.get_logs_dir(v) / "reviewer_io.txt"
    if _review_verdict_attempt == 2:
        log_file = _tg.get_logs_dir(v) / "reviewer_retry_io.txt"
    if _review_strict_call is not None:
        from strict_authority_workflow import (
            StrictAuthorityError,
            strict_invocation_log_path,
        )

        try:
            log_file = strict_invocation_log_path(
                _review_strict_call,
                logs_dir=log_file.parent,
                basename=log_file.name,
            )
        except StrictAuthorityError as exc:
            return _tg._json_tool_result(
                await _tg._abandon_strict_gate_authority(
                    ckpt,
                    gate_name="review",
                    error=exc,
                )
            )

    ui = _tg._get_ui()
    try:
        output, _review_cost_usd, _review_usage = await _tg.run_claude_query(
            rendered_prompt,
            [],
            ui,
            "LEAD CODE REVIEWER",
            log_file,
            tools=["Read"] if _strict_bootstrap else ["Bash", "Read"],
            allowed_read_dirs=(
                [_tg.get_bot_dir(v)]
                if _strict_bootstrap
                else [_tg.get_bot_dir(source_v), _tg.get_bot_dir(v)]
            ),
            strict_authority=_review_strict_call,
        )
    except _tg.LLMAvailabilityBlocked:
        # Provider availability is persisted by run_claude_query and owned by
        # the orchestrator pause state. It is not one of the Reviewer's three
        # infrastructure attempts.
        raise
    except Exception as e:
        if _strict_bootstrap:
            from strict_authority_workflow import StrictAuthorityError

            if isinstance(e, StrictAuthorityError):
                return _tg._json_tool_result(
                    await _tg._abandon_strict_gate_authority(
                        ckpt,
                        gate_name="review",
                        error=e,
                    )
                )
        issue = f"{type(e).__name__}: {str(e)[:500]}"
        infra_result = await _tg._record_infrastructure_failure(
            v,
            source_v,
            owner_tool="run_review",
            resume_stage="quality_passed",
            component="reviewer_llm",
            code="reviewer_llm_unavailable",
            attempt_key=_review_attempt_key,
            issues=[issue],
            max_attempts=3,
            metadata=_review_infra_metadata,
            master_plan=authoritative_plan,
        )
        attempt = (infra_result.get("infra_failure") or {}).get("attempt")
        _tg.log_system_event(
            "pipeline.review_infra_error",
            "error" if infra_result.get("action") == "abandon_generation" else "warn",
            f"Reviewer v{v} unavailable (infrastructure attempt {attempt or '?'}/3)",
            {"version": v, "source_v": source_v, "issue": issue, **infra_result},
        )
        ui.log_history(f"Reviewer infrastructure failure (not a code rejection): {issue}", "warn")
        return _tg._json_tool_result({
            **infra_result,
            "llm_failed": True,
            "approved": None,
            "directive": (
                "Reviewer infrastructure retry exhausted; generation was safely abandoned."
                if infra_result.get("action") == "abandon_generation"
                else "Retry run_review for the same candidate; do not run workers or Master."
            ),
            "logs": ui.get_output(),
        })
    from llm_query import parse_json_output_with_mode
    data, _review_mode = parse_json_output_with_mode(output)
    _review_schema_errors = []
    if data and isinstance(data, dict) and "approved" in data:
        from output_schema import validate_agent_output

        data, _review_schema_errors = validate_agent_output("reviewer", data)

    if not (data and "approved" in data) or _review_schema_errors:
        error_msg = (
            "Reviewer schema validation failed: " + "; ".join(_review_schema_errors[:5])
            if _review_schema_errors
            else
            "Reviewer returned valid JSON but missing 'approved' field"
            if data and isinstance(data, dict)
            else f"Reviewer failed to produce valid JSON (mode={_review_mode})"
        )
        infra_result = await _tg._record_infrastructure_failure(
            v,
            source_v,
            owner_tool="run_review",
            resume_stage="quality_passed",
            component="reviewer_llm",
            code="reviewer_llm_unavailable",
            attempt_key=_review_attempt_key,
            issues=[error_msg],
            max_attempts=3,
            metadata={
                **_review_infra_metadata,
                "parse_mode": _review_mode,
                "raw_output_digest": hashlib.sha256(
                    (output or "").encode("utf-8")
                ).hexdigest(),
            },
            master_plan=authoritative_plan,
        )
        attempt = (infra_result.get("infra_failure") or {}).get("attempt")
        _tg.log_system_event(
            "pipeline.review_parse_error",
            "error" if infra_result.get("action") == "abandon_generation" else "warn",
            f"Reviewer v{v} output was unusable (infrastructure attempt {attempt or '?'}/3)",
            {
                "version": v,
                "source_v": source_v,
                "mode": _review_mode,
                "error": error_msg,
                **infra_result,
            },
        )
        ui.log_history(f"Reviewer output parse error (NOT a code rejection): {error_msg}", "warn")
        result = {
            **infra_result,
            "directive": (
                "Reviewer output remained unusable and the generation was safely abandoned."
                if infra_result.get("action") == "abandon_generation"
                else "Call run_review again for the same candidate; do not run workers or Master."
            ),
            "llm_failed": True,
            "parse_error": True,
            "approved": None,
            "logs": ui.get_output(),
        }
        try:
            _tg.log_system_event(
                "pipeline.review_done",
                "info",
                f"Review finished for v{v} in {time.time() - _t0:.1f}s",
                {
                    "version": v,
                    "approved": False,
                    "parse_error": True,
                    "elapsed_sec": round(time.time() - _t0, 2),
                },
            )
        except Exception:
            pass
        return _tg._json_tool_result(result)

    if data and "approved" in data:
        approved = data["approved"] is True
        feedback = data.get("feedback", "")
        try:
            _tg.log_system_event(
                "pipeline.review_passed" if approved else "pipeline.review_rejected",
                "success" if approved else "warn",
                f"Review {'approved' if approved else 'rejected'} v{v} (score={data.get('quality_score', 0)})",
                {"version": v, "score": data.get("quality_score", 0), "approved": approved},
            )
        except Exception:
            pass
        gate = _tg._gate_payload(
            v,
            source_v,
            approved,
            approved=approved,
            llm_invoked=True,
            reviewer_llm_executed=True,
            schema_valid=True,
            quality_score=data.get("quality_score", 0),
            feedback=feedback,
            change_summary=data.get("change_summary", ""),
            risk_areas=data.get("risk_areas", []),
        )
        if _strict_bootstrap:
            from system_strict_bootstrap import (
                SystemStrictBootstrapError,
                build_system_gate_receipt,
            )

            try:
                from strict_authority_workflow import (
                    StrictAuthorityError,
                    accept_role_result,
                    record_bound_invocation_evidence,
                )

                gate["llm_role_result"] = data
                gate["llm_authority_receipt"] = accept_role_result(
                    _review_strict_call,
                    role_result=data,
                    parse_contract="reviewer-output-schema-v1",
                )
                gate["llm_execution_evidence"] = (
                    record_bound_invocation_evidence(
                        _review_strict_call,
                        log_file=log_file,
                    )
                )
                gate["terminal_authority_context_binding"] = (
                    _review_strict_call.get("context_binding")
                )
            except (SystemStrictBootstrapError, StrictAuthorityError) as exc:
                from system_strict_bootstrap import abandon_rejected_blueprint

                terminal_gate = gate
                if not (
                    isinstance(gate.get("llm_authority_receipt"), dict)
                    and isinstance(gate.get("llm_execution_evidence"), dict)
                ):
                    from workflow_kernel import content_digest

                    terminal_gate = {
                        "version": v,
                        "source_v": source_v,
                        "passed": False,
                        "approved": False,
                        "schema_valid": False,
                        "terminal_control_failure": True,
                        "failure_class": "control_plane",
                        "validation_errors": list(exc.errors),
                        "observed_role_result_digest": content_digest(data),
                    }
                rejected = await abandon_rejected_blueprint(
                    ckpt,
                    reason="system_strict_bootstrap_review_receipt_invalid",
                    result={
                        "error": "SYSTEM_STRICT_BOOTSTRAP_REVIEW_RECEIPT_INVALID",
                        "approved": False,
                        "success": False,
                        "action": "abandon_generation",
                        "failure_class": "control_plane",
                        "validation_errors": list(exc.errors),
                        "terminal_gate_name": "review",
                        "terminal_reason_code": "review_receipt_invalid",
                        "terminal_gate_payload": terminal_gate,
                        "directive": (
                            "The LLM Review succeeded but its content-chain receipt failed. "
                            "Abandon; never treat the receipt as a Reviewer waiver."
                        ),
                    },
                )
                return _tg._json_tool_result(rejected)
        from reviewer_retry import (
            ReviewRetryError,
            build_review_adjudication,
            build_review_attempt_receipt,
            review_attempt_action,
        )

        try:
            attempt_receipt = build_review_attempt_receipt(
                ckpt,
                gate_payload=gate,
                candidate_dir=_tg.get_bot_dir(v),
                attempt=_review_verdict_attempt,
                authority_slot=_review_authority_slot,
                review_semantic_contract_digest=_review_cycle_contract_digest,
                consumed_infrastructure_failure=_review_infra,
            )
            current_attempts = [*_current_review_attempts, attempt_receipt]
            review_action = review_attempt_action(current_attempts)
            adjudication = (
                build_review_adjudication(current_attempts)
                if review_action["action"] in {"approve", "repair"}
                else None
            )
        except ReviewRetryError as exc:
            return _tg._state_blocked(
                "run_review attempt receipt invalid: " + "; ".join(exc.errors),
                v,
                source_v,
                ckpt,
            )
        prospective_journal = [
            *(ckpt.get("review_attempt_journal") or []),
            attempt_receipt,
        ]
        if _strict_bootstrap:
            authority_errors = validate_strict_review_attempt_authority(
                ckpt,
                journal=current_attempts,
                candidate_dir=_tg.get_bot_dir(v),
            )
            if authority_errors:
                from system_strict_bootstrap import abandon_rejected_blueprint

                rejected = await abandon_rejected_blueprint(
                    ckpt,
                    reason="system_strict_bootstrap_review_attempt_authority_invalid",
                    result={
                        "error": "SYSTEM_STRICT_BOOTSTRAP_REVIEW_ATTEMPT_AUTHORITY_INVALID",
                        "approved": False,
                        "success": False,
                        "action": "abandon_generation",
                        "failure_class": "control_plane",
                        "validation_errors": authority_errors,
                        "terminal_gate_name": "review",
                        "terminal_reason_code": "review_authority_invalid",
                        "terminal_gate_payload": gate,
                        "directive": (
                            "The Reviewer provider attempt could not be replayed "
                            "from its strict authority journal. Preserve the "
                            "content-bound failure; never infer a verdict."
                        ),
                    },
                )
                return _tg._json_tool_result(rejected)

        if review_action["action"] == "dispatch":
            checkpoint_recorded = _tg.write_pipeline_checkpoint(
                v,
                source_v,
                "quality_passed",
                master_plan=authoritative_plan,
                generation_attempt=ckpt.get("generation_attempt", 0),
                review_attempt_journal=prospective_journal,
                clear_infra_failure=_review_infra is not None,
                infra_failure_owner=(
                    "run_review" if _review_infra is not None else None
                ),
                expected_infra_failure_digest=(
                    _tg.infrastructure_failure_digest(_review_infra)
                    if _review_infra is not None
                    else None
                ),
                expected_checkpoint_revision=int(ckpt["checkpoint_revision"]),
                expected_checkpoint_stage="quality_passed",
                expected_workflow_run_id=str(ckpt["workflow_run_id"]),
            )
            if not checkpoint_recorded:
                return _tg._json_tool_result({
                    "error": "REVIEW_ATTEMPT_CHECKPOINT_CAS_CONFLICT",
                    "approved": None,
                    "success": False,
                    "action": "retry_same_tool",
                    "failure_class": "control_plane",
                    "review_verdict_attempt": _review_verdict_attempt,
                    "next_v": v,
                    "source_v": source_v,
                    "directive": (
                        "The provider verdict is content-bound, but its append-only "
                        "checkpoint CAS did not commit. Re-enter run_review to replay "
                        "the exact authority effect; do not dispatch a different role "
                        "or rerun earlier stages."
                    ),
                })
            result = {
                "approved": False,
                "llm_invoked": True,
                "reviewer_llm_executed": True,
                "schema_valid": True,
                "quality_score": data.get("quality_score", 0),
                "change_summary": data.get("change_summary", ""),
                "risk_areas": data.get("risk_areas", []),
                "feedback": feedback,
                "review_verdict_attempt": _review_verdict_attempt,
                "review_retry_scheduled": True,
                "next_tool": "run_review",
                "checkpoint_stage": "quality_passed",
                "checkpoint_recorded": checkpoint_recorded,
                "directive": (
                    "The first schema-valid Reviewer rejected. Dispatch exactly "
                    "one independent second Reviewer for the same frozen "
                    "artifact/Quality inputs; do not rerun Master, Worker, or Quality."
                ),
                "logs": ui.get_output(),
            }
            return _tg._json_tool_result(result)

        if adjudication is not None:
            gate = {
                **gate,
                "review_verdict_attempt": _review_verdict_attempt,
                "review_attempt_receipts": [
                    {
                        "attempt": row["attempt"],
                        "authority_slot": row["authority_slot"],
                        "receipt_digest": row["receipt_digest"],
                        "approved": row["approved"],
                    }
                    for row in current_attempts
                ],
                "review_adjudication": adjudication,
            }
        final_approved = review_action["action"] == "approve"
        if not final_approved:
            combined_feedback = "\n\n".join(
                f"Reviewer attempt {row['attempt']}: "
                + str((row.get("gate_payload") or {}).get("feedback") or "")
                for row in current_attempts
            ).strip()
            feedback = combined_feedback or feedback
            gate.update({
                "approved": False,
                "passed": False,
                "feedback": feedback,
                "review_consistency": review_action.get("consistency"),
            })
        elif _strict_bootstrap:
            try:
                gate["system_verifier_receipt"] = build_system_gate_receipt(
                    {**ckpt, "review_attempt_journal": prospective_journal},
                    gate_name="review",
                    candidate_dir=_tg.get_bot_dir(v),
                    llm_gate=gate,
                )
            except SystemStrictBootstrapError as exc:
                from system_strict_bootstrap import abandon_rejected_blueprint

                rejected = await abandon_rejected_blueprint(
                    ckpt,
                    reason="system_strict_bootstrap_review_receipt_invalid",
                    result={
                        "error": "SYSTEM_STRICT_BOOTSTRAP_REVIEW_RECEIPT_INVALID",
                        "approved": False,
                        "success": False,
                        "action": "abandon_generation",
                        "failure_class": "control_plane",
                        "validation_errors": list(exc.errors),
                        "terminal_gate_name": "review",
                        "terminal_reason_code": "review_receipt_invalid",
                        "terminal_gate_payload": gate,
                        "directive": (
                            "The approved Reviewer adjudication could not be "
                            "bound to the strict system receipt. Preserve the "
                            "provider evidence and abandon; never waive it."
                        ),
                    },
                )
                return _tg._json_tool_result(rejected)
        if _strict_bootstrap and not final_approved:
            # The first strict candidate is an exact system-owned blueprint.
            # A two-verdict rejection cannot authorize an LLM Worker to edit
            # those fixed bytes, so ``repair_planned`` would be an executable
            # dead end and would loop after restart.  Project both immutable
            # attempt receipts and the conservative adjudication into the
            # canonical terminal checkpoint CAS, then use its receipt as the
            # sole durable abandon authority.
            from system_strict_bootstrap import abandon_rejected_blueprint

            rejected = await abandon_rejected_blueprint(
                ckpt,
                reason="system_strict_bootstrap_review_rejected",
                result={
                    "error": "SYSTEM_STRICT_BOOTSTRAP_REVIEW_REJECTED",
                    "approved": False,
                    "success": False,
                    "failure_class": "strategy_review",
                    "feedback": feedback,
                    "review_verdict_attempt": _review_verdict_attempt,
                    "review_adjudication": adjudication,
                    "review_attempt_receipts": gate.get(
                        "review_attempt_receipts"
                    ),
                    "terminal_gate_name": "review",
                    "terminal_reason_code": "review_rejected",
                    "terminal_gate_payload": gate,
                    "terminal_review_attempt_journal": prospective_journal,
                    "directive": (
                        "Both bounded Reviewer verdicts are terminal for the "
                        "fixed first-strict blueprint. The exact journal and "
                        "gate receipt were routed to canonical abandon; never "
                        "dispatch execute_workers or a third Reviewer."
                    ),
                },
            )
            return _tg._json_tool_result(rejected)
        checkpoint_recorded = _tg._record_gate(
            v,
            source_v,
            "review",
            gate,
            stage="reviewed" if final_approved else "repair_planned",
            master_plan=authoritative_plan,
            reviewer_feedback=feedback,
            clear_infra_failure=_review_infra is not None,
            infra_failure_owner="run_review" if _review_infra is not None else None,
            expected_infra_failure_digest=(
                _tg.infrastructure_failure_digest(_review_infra)
                if _review_infra is not None
                else None
            ),
            review_attempt_journal=prospective_journal,
        )
        if not checkpoint_recorded:
            return _tg._json_tool_result({
                "error": "REVIEW_ADJUDICATION_CHECKPOINT_CAS_CONFLICT",
                "approved": None,
                "success": False,
                "action": "retry_same_tool",
                "failure_class": "control_plane",
                "review_verdict_attempt": _review_verdict_attempt,
                "next_v": v,
                "source_v": source_v,
                "directive": (
                    "The Reviewer authority is durable but its reviewed/repair "
                    "projection did not commit. Retry run_review to replay the "
                    "same effect and exact adjudication; never dispatch a third "
                    "Reviewer or infer success."
                ),
            })
        if not final_approved:
            _tg._record_quality_failure(v, "reviewer", "Code Reviewer",
                                    f"Rejected (score={data.get('quality_score', 0)}): {feedback[:2000]}")
        result = {
            "approved": final_approved,
            "llm_invoked": True,
            "reviewer_llm_executed": True,
            "schema_valid": True,
            "quality_score": data.get("quality_score", 0),
            "change_summary": data.get("change_summary", ""),
            "risk_areas": data.get("risk_areas", []),
            "feedback": feedback,
            "review_verdict_attempt": _review_verdict_attempt,
            "review_adjudication": adjudication,
            "next_tool": "run_critic" if final_approved else "execute_workers",
            "checkpoint_recorded": checkpoint_recorded,
            "logs": ui.get_output(),
        }
        if gate.get("system_verifier_receipt"):
            result["system_verifier_receipt"] = gate["system_verifier_receipt"]
    try:
        _tg.log_system_event("pipeline.review_done", "info",
                         f"Review finished for v{v} in {time.time() - _t0:.1f}s",
                         {"version": v, "approved": result.get("approved", False),
                          "score": result.get("quality_score", 0), "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass

    return _tg._json_tool_result(result)


async def run_critic(args):
    _t0 = time.time()
    v, source_v = _tg._resolve_version_args(args)
    if v is None or source_v is None:
        return _tg._json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    supplied_plan = args.get("plan", [])
    supplied_reviewer_feedback = args.get("reviewer_feedback", "")
    force_advance = bool(args.get("force_advance", False))

    _tg._set_pipeline_status(f"Critic evaluating v{v}")

    ckpt = _tg._matching_checkpoint(v, source_v)
    _critic_infra, _critic_infra_error = _tg._owned_infrastructure_failure(
        ckpt,
        "run_critic",
    )
    if _critic_infra_error:
        return _tg._state_blocked(_critic_infra_error, v, source_v, ckpt)
    _critic_exhausted = await _tg._execute_exhausted_infrastructure_failure(
        v,
        source_v,
        owner_tool="run_critic",
    )
    if _critic_exhausted is not None:
        return _tg._json_tool_result(_critic_exhausted)

    # Idempotency guard: skip when the advisory role already completed.
    _cached = _tg._idempotency_check(
        v, source_v,
        stage_set=("critic_checked", "verified", "archived"),
        gate_name="critic",
        directive="Critic ALREADY PASSED. Call run_precommit_eval next.",
        cache_validator=lambda checkpoint, _gate: _tg._critic_gate_ok(checkpoint),
    )
    if _cached:
        return _cached

    if not _tg._quality_gate_ok(ckpt) or not _tg._review_gate_ok(ckpt):
        return _tg._state_blocked(
            "run_critic requires passing quality gates and reviewer approval for the same version/source_v.",
            v,
            source_v,
            ckpt,
        )

    # Reviewer feedback is checkpoint-owned evidence.  The tool argument is a
    # convenience projection for weak controllers, not authority that may
    # rewrite the reviewed checkpoint while the Critic runs.
    reviewer_feedback = (
        str(ckpt.get("reviewer_feedback") or "")
        if isinstance(ckpt, dict)
        else ""
    )
    if (
        supplied_reviewer_feedback
        and str(supplied_reviewer_feedback) != reviewer_feedback
    ):
        _tg.log_system_event(
            "pipeline.critic_reviewer_feedback_argument_ignored",
            "warn",
            f"run_critic v{v} ignored reviewer feedback that differed from checkpoint authority",
            {"version": v, "source_v": source_v},
        )

    authoritative_plan = (
        ckpt.get("master_plan")
        if isinstance(ckpt, dict) and isinstance(ckpt.get("master_plan"), dict)
        else {"tasks": supplied_plan if isinstance(supplied_plan, list) else []}
    )
    if supplied_plan and supplied_plan != authoritative_plan.get("tasks"):
        _tg.log_system_event(
            "pipeline.critic_plan_argument_ignored",
            "warn",
            f"run_critic v{v} ignored a plan argument that differed from checkpoint authority",
            {"version": v, "source_v": source_v},
        )
    master_plan_str = json.dumps(authoritative_plan, indent=2, ensure_ascii=False)
    # Match _record_gate exactly: after a successful replacement the checkpoint
    # stores the current gate as ``prev_critic``.  Binding that same object before
    # dispatch keeps prompt semantics stable across the write and later verifier
    # reconstruction.
    prev_critic = _tg._critic_result_to_preserve(ckpt)
    critic_prompt_source = _tg.PROJECT_ROOT / "web" / "core" / "prompts" / "critic_prompt.md"
    critic_prompt_identity = (
        critic_prompt_source.read_text(encoding="utf-8")
        if critic_prompt_source.exists()
        else "critic_prompt_missing"
    )
    critic_prompt_identity += "\n" + master_plan_str + "\n" + json.dumps(
        prev_critic or {}, sort_keys=True, ensure_ascii=False
    )
    from system_strict_bootstrap import is_declared_native_bootstrap

    _strict_bootstrap = is_declared_native_bootstrap(ckpt)
    _critic_attempt_key, _critic_infra_metadata = _tg._llm_gate_infrastructure_identity(
        component="critic_llm",
        role="STRATEGY CRITIC",
        candidate_dir=_tg.get_bot_dir(v),
        source_dir=None if _strict_bootstrap else _tg.get_bot_dir(source_v),
        prompt_text=critic_prompt_identity,
        checkpoint=ckpt,
        source_fingerprint_override=(
            hashlib.sha256(
                f"numeric-high-water-only:v{source_v}".encode("ascii")
            ).hexdigest()
            if _strict_bootstrap
            else None
        ),
    )
    ui = _tg._get_ui()
    _critic_invocation_id = None
    _critic_strict_call = None
    if _strict_bootstrap:
        from strict_authority_workflow import (
            StrictAuthorityError,
            gate_call_context,
            new_call,
        )

        try:
            _critic_strict_call = new_call(
                ckpt,
                slot="critic",
                context_binding=gate_call_context(
                    ckpt,
                    gate_name="critic",
                    candidate_dir=_tg.get_bot_dir(v),
                ),
            )
            _critic_invocation_id = _critic_strict_call["invocation_id"]
        except StrictAuthorityError as exc:
            return _tg._json_tool_result(
                await _tg._abandon_strict_gate_authority(
                    ckpt,
                    gate_name="critic",
                    error=exc,
                )
            )
    try:
        data = await _tg._run_critic(
            v,
            source_v,
            master_plan_str,
            ui,
            prev_critic_result=prev_critic,
            execution_invocation_id=_critic_invocation_id,
            strict_authority=_critic_strict_call,
        )
    except Exception as exc:
        if _strict_bootstrap:
            from strict_authority_workflow import StrictAuthorityError

            if isinstance(exc, StrictAuthorityError):
                return _tg._json_tool_result(
                    await _tg._abandon_strict_gate_authority(
                        ckpt,
                        gate_name="critic",
                        error=exc,
                    )
                )
        raise
    _critic_execution_material = (
        data.pop("_llm_execution_material", None)
        if isinstance(data, dict)
        else None
    )
    if isinstance(data, dict) and not data.get("llm_failed") and not data.get(
        "parse_failed"
    ):
        from output_schema import validate_agent_output

        data, _critic_schema_errors = validate_agent_output("critic", data)
        if _critic_schema_errors:
            data = {
                "parse_failed": True,
                "llm_failed": True,
                "error": (
                    "Critic schema validation failed: "
                    + "; ".join(_critic_schema_errors[:8])
                ),
            }

    # No strategic verdict exists when the role call or its schema collapses.
    # Persist an infrastructure overlay instead of manufacturing score=0 debt.
    if not isinstance(data, dict) or data.get("llm_failed") or data.get("parse_failed"):
        issue = (
            str((data or {}).get("error") or (data or {}).get("feedback") or "critic output unavailable")
            if isinstance(data, dict)
            else f"critic_result_not_object:{type(data).__name__}"
        )
        infra_result = await _tg._record_infrastructure_failure(
            v,
            source_v,
            owner_tool="run_critic",
            resume_stage="reviewed",
            component="critic_llm",
            code="critic_llm_unavailable",
            attempt_key=_critic_attempt_key,
            issues=[issue],
            max_attempts=3,
            metadata=_critic_infra_metadata,
            master_plan=authoritative_plan,
            reviewer_feedback=reviewer_feedback,
        )
        attempt = (infra_result.get("infra_failure") or {}).get("attempt")
        _tg.log_system_event(
            "pipeline.critic_infra_error",
            "error" if infra_result.get("action") == "abandon_generation" else "warn",
            f"Critic v{v} unavailable (infrastructure attempt {attempt or '?'}/3)",
            {"version": v, "source_v": source_v, "issue": issue[:500], **infra_result},
        )
        return _tg._json_tool_result({
            **infra_result,
            "llm_failed": True,
            "approved": None,
            "score": None,
            "directive": (
                "Critic infrastructure retry exhausted; generation was safely abandoned."
                if infra_result.get("action") == "abandon_generation"
                else "Retry run_critic for the same candidate; do not run workers or Master."
            ),
            "logs": ui.get_output(),
        })

    if not isinstance(data, dict):
        data = {}
    score = data.get("score", 0)
    try:
        score_num = float(score)
    except (TypeError, ValueError):
        score_num = 0.0
    raw_approved = data.get("approved", score_num >= 6)
    advisory_approved = _critic_bool(raw_approved) and score_num >= 6
    # Successful schema-valid execution completes the role. The raw verdict is
    # retained as advice; it cannot replace the native-TCP statistical gate.
    approved = True
    force_advanced = bool(force_advance)
    gate = _tg._gate_payload(
        v,
        source_v,
        approved,
        approved=approved,
        llm_invoked=True,
        critic_llm_executed=True,
        schema_valid=True,
        raw_approved=raw_approved,
        advisory_approved=advisory_approved,
        advisory_score=score_num,
        score=score_num,
        feedback=data.get("feedback", ""),
        strategic_assessment=data.get("strategic_assessment", ""),
        local_optima_warning=data.get("local_optima_warning", False),
        force_advanced=force_advanced,
    )

    if _strict_bootstrap:
        from system_strict_bootstrap import (
            SystemStrictBootstrapError,
            build_system_gate_receipt,
        )

        try:
            from strict_authority_workflow import (
                StrictAuthorityError,
                accept_role_result,
                record_bound_invocation_evidence,
                strict_invocation_log_path,
            )

            _critic_log_file = strict_invocation_log_path(
                _critic_strict_call,
                logs_dir=_tg.get_logs_dir(v),
                basename="critic_io.txt",
            )
            if (
                not isinstance(_critic_execution_material, dict)
                or Path(str(
                    _critic_execution_material.get("log_file") or ""
                )).resolve()
                != _critic_log_file.resolve()
            ):
                raise StrictAuthorityError(
                    "strict_authority_critic_execution_log_mismatch"
                )

            gate["llm_role_result"] = data
            gate["llm_authority_receipt"] = accept_role_result(
                _critic_strict_call,
                role_result=data,
                parse_contract="critic-output-schema-v1",
            )
            gate["llm_execution_evidence"] = record_bound_invocation_evidence(
                _critic_strict_call,
                log_file=_critic_log_file,
            )
            gate["system_verifier_receipt"] = build_system_gate_receipt(
                ckpt,
                gate_name="critic",
                candidate_dir=_tg.get_bot_dir(v),
                llm_gate=gate,
            )
        except (SystemStrictBootstrapError, StrictAuthorityError) as exc:
            from system_strict_bootstrap import abandon_rejected_blueprint

            gate["terminal_authority_context_binding"] = (
                _critic_strict_call.get("context_binding")
            )
            terminal_gate = gate
            if not (
                isinstance(gate.get("llm_authority_receipt"), dict)
                and isinstance(gate.get("llm_execution_evidence"), dict)
            ):
                from workflow_kernel import content_digest

                terminal_gate = {
                    "version": v,
                    "source_v": source_v,
                    "passed": False,
                    "approved": False,
                    "schema_valid": False,
                    "terminal_control_failure": True,
                    "failure_class": "control_plane",
                    "validation_errors": list(exc.errors),
                    "observed_role_result_digest": content_digest(data),
                }
            rejected = await abandon_rejected_blueprint(
                ckpt,
                reason="system_strict_bootstrap_critic_receipt_invalid",
                result={
                    "error": "SYSTEM_STRICT_BOOTSTRAP_CRITIC_RECEIPT_INVALID",
                    "approved": False,
                    "success": False,
                    "action": "abandon_generation",
                    "failure_class": "control_plane",
                    "validation_errors": list(exc.errors),
                    "terminal_gate_name": "critic",
                    "terminal_reason_code": "critic_receipt_invalid",
                    "terminal_gate_payload": terminal_gate,
                    "directive": (
                        "The schema-valid Critic completed but the deterministic content "
                        "chain drifted. Abandon; the verifier is adjunct evidence, not a "
                        "Critic waiver."
                    ),
                },
            )
            return _tg._json_tool_result(rejected)

    current_attempt = (ckpt.get("generation_attempt", 0) or 0) if ckpt else 0
    next_attempt = current_attempt

    checkpoint_recorded = _tg._record_gate(
        v,
        source_v,
        "critic",
        gate,
        stage="critic_checked",
        master_plan=authoritative_plan,
        reviewer_feedback=reviewer_feedback,
        generation_attempt=next_attempt,
        clear_infra_failure=_critic_infra is not None,
        infra_failure_owner="run_critic" if _critic_infra is not None else None,
        expected_infra_failure_digest=(
            _tg.infrastructure_failure_digest(_critic_infra)
            if _critic_infra is not None
            else None
        ),
    )
    try:
        # LOG GAP FIX (2026-06-30): enrich critic event with feedback/reasoning so
        # the reject rationale is visible in the event stream (not just worker_failures.jsonl).
        _critic_payload = {
            "version": v,
            "score": score_num,
            "approved": approved,
            "advisory_approved": advisory_approved,
            "generation_attempt": next_attempt,
        }
        if not advisory_approved:
            _critic_payload["feedback"] = str(data.get("feedback", ""))[:500] if isinstance(data, dict) else ""
            _critic_payload["local_optima_warning"] = data.get("local_optima_warning") if isinstance(data, dict) else None
            _critic_payload["strategic_assessment"] = str(data.get("strategic_assessment", ""))[:300] if isinstance(data, dict) else ""
        _tg.log_system_event(
            "pipeline.critic_advisory_completed",
            "success" if advisory_approved else "warn",
            f"Critic advisory completed for v{v} (score={score_num})",
            _critic_payload,
        )
    except Exception:
        pass

    # Critic evidence remains inside the checkpoint-bound role result. It is
    # never copied into a mutable cross-generation Markdown store.
    evidence = data.get("evidence") if isinstance(data, dict) else None

    # fix-8: check for fabricated replay citations in critic output
    critic_citation_errors = []
    try:
        from tool_planning import _check_citations, _load_replay_anchor_map
        if evidence:
            critic_texts = evidence.get("h2h_weaknesses", []) + evidence.get("diff_refs", [])
            critic_citation_errors = _check_citations(
                [str(t) for t in critic_texts], _load_replay_anchor_map()
            )
        if critic_citation_errors:
            _tg.log_system_event("fabricated_citation", "warn",
                             f"Critic cited {len(critic_citation_errors)} fabricated replay(s)",
                             {"version": v, "errors": critic_citation_errors})
    except Exception:
        pass  # Non-critical: citation check should not block pipeline

    result = {
        **data,
        "approved": approved,
        "llm_invoked": True,
        "critic_llm_executed": True,
        "schema_valid": True,
        "raw_approved": raw_approved,
        "score": score_num,
        "advisory_score": score_num,
        "advisory_approved": advisory_approved,
        "citation_penalties": len(critic_citation_errors),
        "logs": ui.get_output(),
        "action": "proceed_to_precommit",
        "directive": (
            "Critic advisory completed. Call run_precommit_eval next regardless "
            "of advisory score; native TCP precommit is the final strategy gate."
        ),
        "reviewer_feedback": reviewer_feedback,
        "generation_attempt": next_attempt,
        "force_advanced": force_advanced,
        "checkpoint_recorded": checkpoint_recorded,
    }
    result["fabricated_citations"] = critic_citation_errors
    if gate.get("system_verifier_receipt"):
        result["system_verifier_receipt"] = gate["system_verifier_receipt"]
    try:
        _tg.log_system_event("pipeline.critic_done", "info",
                         f"Critic finished for v{v} in {time.time() - _t0:.1f}s",
                         {"version": v, "approved": approved, "score": score_num,
                          "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass
    return _tg._json_tool_result(result)
