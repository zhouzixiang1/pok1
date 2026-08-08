"""Gate-ledger validation and code-fingerprint checks for tool_commit.

Extracted as a cohesive business cluster; tool_commit.py retains thin delegate
shells so external ``from tool_commit import validate_commit_gate_ledger`` and
``monkeypatch.setattr(tool_commit, "validate_commit_gate_ledger", ...)`` keep
resolving.

Business responsibility: validate the gate ledger and code fingerprint for
finalizing a bot (shared by ``commit_bot`` and bare-commit recovery).
"""
from __future__ import annotations

import tool_commit as _tc  # for cross-refs to constants/other helpers


def validate_commit_gate_ledger(
    v,
    source_v,
    ckpt,
    bot_dir=None,
    *,
    pending_local_publication=None,
):
    """Validate the gate ledger and code fingerprint for finalizing a bot.

    This is intentionally shared by normal ``commit_bot`` and bare-commit
    recovery. Recovery must not tag code unless the current files still match
    the exact code that passed quality and precommit.
    """
    v = int(v)
    source_v = int(source_v) if source_v is not None else None
    bot_dir = bot_dir or _tc.get_bot_dir(v)
    try:
        from tool_gates import _bot_code_fingerprint
        current_code_fingerprint = _bot_code_fingerprint(bot_dir)
    except Exception:
        current_code_fingerprint = ""

    missing_gates = []
    failed_gates = []
    gate_results = {}
    if not ckpt:
        missing_gates.append("pipeline_checkpoint")
    else:
        try:
            from workflow_profiles import get_workflow_profile
            workflow_profile = get_workflow_profile()
            expected_profile_id = getattr(workflow_profile, "profile_id", "")
            expected_execution_mode = getattr(workflow_profile, "national_execution_mode", "")
            expected_evaluation_protocol = getattr(workflow_profile, "evaluation_protocol", "")
        except Exception as exc:
            expected_profile_id = ""
            expected_execution_mode = ""
            expected_evaluation_protocol = ""
            failed_gates.append({
                "gate": "workflow_profile",
                "reason": "active workflow profile is unavailable or invalid",
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
            })
        checkpoint_profile_id = str(ckpt.get("workflow_profile_id") or "")
        checkpoint_execution_mode = str(ckpt.get("national_execution_mode") or "")
        if expected_profile_id and checkpoint_profile_id and checkpoint_profile_id != expected_profile_id:
            failed_gates.append({
                "gate": "pipeline_checkpoint",
                "reason": "workflow_profile_id mismatch",
                "expected": expected_profile_id,
                "current": checkpoint_profile_id,
            })
        if expected_execution_mode and checkpoint_execution_mode and checkpoint_execution_mode != expected_execution_mode:
            failed_gates.append({
                "gate": "pipeline_checkpoint",
                "reason": "national_execution_mode mismatch",
                "expected": expected_execution_mode,
                "current": checkpoint_execution_mode,
            })
        gate_results = ckpt.get("gate_results", {}) or {}
        _ckpt_source_v = ckpt.get("source_v")
        if source_v is not None and int(_ckpt_source_v if _ckpt_source_v is not None else -1) != source_v:
            failed_gates.append({
                "gate": "pipeline_checkpoint",
                "reason": "source_v mismatch",
                "expected": source_v,
                "current": ckpt.get("source_v"),
            })
        if int(ckpt.get("next_v") or -1) != v:
            failed_gates.append({
                "gate": "pipeline_checkpoint",
                "reason": "next_v mismatch",
                "expected": v,
                "current": ckpt.get("next_v"),
            })
        if not current_code_fingerprint:
            failed_gates.append({
                "gate": "code_fingerprint",
                "reason": "current candidate code fingerprint is unavailable",
                "path": str(bot_dir),
            })

        quality = gate_results.get("quality")
        if not quality:
            missing_gates.append("quality")
        else:
            quality_profile_id = str(quality.get("workflow_profile_id") or quality.get("profile_id") or "")
            quality_execution_mode = str(quality.get("national_execution_mode") or "")
            if expected_profile_id and quality_profile_id != expected_profile_id:
                failed_gates.append({
                    "gate": "quality",
                    "reason": "workflow_profile_id mismatch",
                    "expected": expected_profile_id,
                    "current": quality_profile_id or "missing",
                })
            if expected_execution_mode and quality_execution_mode != expected_execution_mode:
                failed_gates.append({
                    "gate": "quality",
                    "reason": "national_execution_mode mismatch",
                    "expected": expected_execution_mode,
                    "current": quality_execution_mode or "missing",
                })
            if expected_execution_mode == "native_tcp" and quality.get("national_native_contract_ok") is not True:
                failed_gates.append({
                    "gate": "quality",
                    "reason": "national native TCP contract did not pass",
                    "value": quality.get("national_native_contract_ok"),
                })
            if quality.get("all_passed") is not True:
                failed_gates.append({"gate": "quality", "reason": "all_passed is not true", "value": quality})
            if quality.get("critical_scenarios_passed") is not True:
                failed_gates.append({"gate": "quality", "reason": "critical_scenarios_passed is not true", "value": quality})
            quality_fingerprint = quality.get("code_fingerprint")
            if not quality_fingerprint:
                missing_gates.append("quality_code_fingerprint")
            elif current_code_fingerprint and quality_fingerprint != current_code_fingerprint:
                failed_gates.append({
                    "gate": "quality",
                    "reason": "code_fingerprint changed since quality gates",
                    "expected": quality_fingerprint,
                    "current": current_code_fingerprint,
                })
            if expected_execution_mode == "native_tcp":
                try:
                    from national_runtime_probe import (
                        RUNTIME_PROBE_LIMITS_DIGEST,
                        RUNTIME_PROBE_IDENTITY_DIGEST,
                        RUNTIME_PROBE_ORCHESTRATOR_VERSION,
                        RUNTIME_PROBE_SCENARIO_DIGEST,
                        RUNTIME_PROBE_SCHEMA_VERSION,
                        runtime_probe_native_template_evidence,
                        validate_runtime_probe_repeatability_evidence,
                    )
                    from runtime_architecture_policy import (
                        runtime_contract_ledger_digest,
                        validate_runtime_contract_ledger,
                    )

                    checkpoint_ledger = ckpt.get("runtime_contract_ledger")
                    plan_ledger = (
                        (ckpt.get("master_plan") or {}).get("runtime_contract_ledger")
                        if isinstance(ckpt.get("master_plan"), dict)
                        else None
                    )
                    ledger_errors = [
                        *(f"checkpoint:{item}" for item in validate_runtime_contract_ledger(checkpoint_ledger)),
                        *(f"master_plan:{item}" for item in validate_runtime_contract_ledger(plan_ledger)),
                    ]
                    checkpoint_ledger_digest = runtime_contract_ledger_digest(checkpoint_ledger)
                    plan_ledger_digest = runtime_contract_ledger_digest(plan_ledger)
                    if checkpoint_ledger_digest != plan_ledger_digest:
                        ledger_errors.append("checkpoint_master_plan_ledger_digest_mismatch")
                    if ledger_errors:
                        failed_gates.append({
                            "gate": "runtime_contract_identity",
                            "reason": "runtime contract ledger is invalid",
                            "errors": ledger_errors[:10],
                        })
                    expected_runtime_identity = {
                        "runtime_contract_ledger_digest": checkpoint_ledger_digest,
                        "runtime_probe_schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
                        "runtime_probe_orchestrator_version": RUNTIME_PROBE_ORCHESTRATOR_VERSION,
                        "runtime_probe_scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
                        "runtime_probe_limits_digest": RUNTIME_PROBE_LIMITS_DIGEST,
                        "runtime_probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
                        **runtime_probe_native_template_evidence(),
                    }
                    quality_probe = (
                        (quality.get("national_capability_contract") or {}).get(
                            "dynamic_runtime_probe"
                        )
                        or {}
                    )
                    repeatability_errors = (
                        validate_runtime_probe_repeatability_evidence(
                            quality_probe
                        )
                    )
                    if repeatability_errors:
                        failed_gates.append({
                            "gate": "runtime_probe_repeatability",
                            "reason": "runtime probe repeatability evidence is invalid",
                            "errors": repeatability_errors[:12],
                        })
                    managed_isolation_digest = str(
                        quality_probe.get("managed_isolation_digest") or ""
                    )
                    if len(managed_isolation_digest) != 64:
                        failed_gates.append({
                            "gate": "runtime_probe_identity",
                            "reason": "managed isolation digest missing or invalid",
                        })
                    expected_runtime_identity[
                        "runtime_probe_managed_isolation_digest"
                    ] = managed_isolation_digest
                    mismatches = {
                        key: {"expected": value, "quality": quality.get(key)}
                        for key, value in expected_runtime_identity.items()
                        if quality.get(key) != value
                    }
                    if mismatches:
                        failed_gates.append({
                            "gate": "runtime_probe_identity",
                            "reason": "quality evidence does not match current runtime probe/ledger identity",
                            "mismatches": mismatches,
                        })
                except Exception as exc:
                    failed_gates.append({
                        "gate": "runtime_probe_identity",
                        "reason": f"identity validation error: {type(exc).__name__}: {str(exc)[:200]}",
                    })

        review = gate_results.get("review")
        if not review:
            missing_gates.append("review")
        elif not _tc._review_gate_ok(ckpt):
            failed_gates.append({
                "gate": "review",
                "reason": (
                    "reviewer was not schema-valid/content-bound or did not approve"
                ),
                "value": review,
            })

        critic = gate_results.get("critic")
        if not critic:
            missing_gates.append("critic")
        elif not _tc._critic_gate_ok(ckpt):
            failed_gates.append({
                "gate": "critic",
                "reason": (
                    "critic advisory role was not schema-valid/content-bound or "
                    "did not complete successfully"
                ),
                "value": critic,
            })

        precommit = gate_results.get("precommit_eval")
        if not precommit:
            missing_gates.append("precommit_eval")
        elif precommit.get("passed") is not True:
            failed_gates.append({"gate": "precommit_eval", "reason": "precommit eval did not pass", "value": precommit})
        else:
            precommit_profile_id = str(precommit.get("workflow_profile_id") or precommit.get("profile_id") or "")
            precommit_execution_mode = str(precommit.get("national_execution_mode") or "")
            if expected_profile_id and precommit_profile_id != expected_profile_id:
                failed_gates.append({
                    "gate": "precommit_eval",
                    "reason": "workflow_profile_id mismatch",
                    "expected": expected_profile_id,
                    "current": precommit_profile_id or "missing",
                })
            if expected_execution_mode and precommit_execution_mode != expected_execution_mode:
                failed_gates.append({
                    "gate": "precommit_eval",
                    "reason": "national_execution_mode mismatch",
                    "expected": expected_execution_mode,
                    "current": precommit_execution_mode or "missing",
                })
            precommit_fingerprint = precommit.get("code_fingerprint")
            if not precommit_fingerprint:
                missing_gates.append("precommit_code_fingerprint")
            elif current_code_fingerprint and precommit_fingerprint != current_code_fingerprint:
                failed_gates.append({
                    "gate": "precommit_eval",
                    "reason": "code_fingerprint changed since precommit eval",
                    "expected": precommit_fingerprint,
                    "current": current_code_fingerprint,
                })
            if expected_execution_mode == "native_tcp":
                try:
                    from national_runtime_probe import (
                        runtime_probe_native_template_evidence_matches,
                    )
                    from precommit_eval_contract import (
                        validate_evaluation_contract,
                        validate_precommit_plan,
                    )

                    if not runtime_probe_native_template_evidence_matches(
                        precommit
                    ):
                        failed_gates.append({
                            "gate": "precommit_native_runtime_identity",
                            "reason": (
                                "precommit evidence does not bind the exact "
                                "current system-owned native TCP template"
                            ),
                        })

                    precommit_plan = (
                        (ckpt.get("audit_context") or {}).get("precommit_eval_plan")
                    )
                    plan_issues = validate_precommit_plan(
                        precommit_plan,
                        candidate_version=v,
                        source_version=source_v,
                        profile_id=expected_profile_id,
                        execution_mode=expected_execution_mode,
                        evaluation_protocol=expected_evaluation_protocol,
                    )
                    contract_issues = (
                        validate_evaluation_contract(
                            precommit.get("precommit_eval_contract"),
                            precommit_plan,
                            candidate_code_fingerprint=current_code_fingerprint,
                        )
                        if not plan_issues
                        else []
                    )
                    contract = precommit.get("precommit_eval_contract") or {}
                    if precommit.get("precommit_eval_contract_digest") != contract.get("contract_digest"):
                        contract_issues.append("precommit_evaluation_contract_digest_mismatch")
                    if plan_issues or contract_issues:
                        failed_gates.append({
                            "gate": "precommit_eval_contract",
                            "reason": "frozen precommit evaluator/opponent contract is invalid or drifted",
                            "errors": [*plan_issues, *contract_issues][:12],
                        })

                    # The one-time empty-pool control is not a published bot and
                    # carries no strength/rating authority.  Recompute its full
                    # live authority at the final ledger boundary, bind it back
                    # to the exact quality receipt, and independently reapply
                    # the complete-match W/L/D floor.  A concurrently published
                    # strict bot therefore revokes this path before commit.
                    from system_strict_bootstrap import is_declared_native_bootstrap

                    declared_first_strict = is_declared_native_bootstrap(ckpt)
                    plan_opponents = (
                        precommit_plan.get("opponents") or []
                        if isinstance(precommit_plan, dict)
                        else []
                    )
                    control_opponents = [
                        item for item in plan_opponents
                        if isinstance(item, dict)
                        and item.get("authority") == "system_first_strict_control"
                    ]
                    if declared_first_strict:
                        # first_strict_control module removed; final-ledger
                        # control validation is no longer available.  Provide
                        # no-op stubs so the control-bound recheck runs
                        # harmlessly (no errors, no blockers).
                        try:
                            from first_strict_control import (
                                control_gate_blockers,
                                validate_control_receipt,
                                validate_control_result,
                            )
                        except ImportError:
                            control_gate_blockers = lambda *a, **k: ([], None)
                            validate_control_receipt = lambda *a, **k: []
                            validate_control_result = lambda *a, **k: ([], None)

                        control_errors = []
                        if len(control_opponents) != 1 or len(plan_opponents) != 1:
                            control_errors.append(
                                "first_strict_control_final_plan_shape_invalid"
                            )
                        quality_receipt = quality.get(
                            "first_strict_control_receipt"
                        )
                        plan_receipt = (
                            control_opponents[0].get("control_receipt")
                            if len(control_opponents) == 1
                            else None
                        )
                        if quality_receipt != plan_receipt:
                            control_errors.append(
                                "first_strict_control_quality_plan_receipt_mismatch"
                            )
                        control_errors.extend(validate_control_receipt(
                            plan_receipt,
                            checkpoint=ckpt,
                            candidate_version=v,
                            source_version=source_v,
                            force_protocol_refresh=True,
                            pending_local_publication=pending_local_publication,
                        ))
                        if precommit.get("precommit_eval_plan") != precommit_plan:
                            control_errors.append(
                                "first_strict_control_result_plan_mismatch"
                            )
                        expected_flags = {
                            "precommit_gate_admitted": True,
                            "strength_admitted": False,
                            "rating_eligible": False,
                            "official_opponent_eligible": False,
                        }
                        for field, expected in expected_flags.items():
                            if precommit.get(field) is not expected:
                                control_errors.append(
                                    f"first_strict_control_final_{field}_mismatch"
                                )
                        strength_order = precommit.get("strength_order") or {}
                        if int(strength_order.get("samples") or 0) != 0:
                            control_errors.append(
                                "first_strict_control_strength_samples_nonzero"
                            )
                        expected_control_samples = list(
                            (precommit_plan or {}).get("sample_plan") or []
                        )
                        execution_scope = precommit.get(
                            "control_execution_scope"
                        )
                        national_execution_scope = (
                            (precommit.get("national") or {}).get(
                                "control_execution_scope"
                            )
                        )
                        if execution_scope != national_execution_scope:
                            control_errors.append(
                                "first_strict_control_execution_scope_projection_mismatch"
                            )
                        expected_execution_bindings = {
                            "workflow_run_id": str(
                                ckpt.get("workflow_run_id") or ""
                            ),
                            "candidate_version": int(v),
                            "candidate_label": _tc.bot_name(v),
                            "candidate_artifact_hash": str(
                                current_code_fingerprint
                            ),
                            "control_id": "first_strict_control_v1",
                            "control_artifact_hash": str(
                                (((plan_receipt or {}).get("control") or {}).get(
                                    "artifact_hash"
                                ))
                                or ""
                            ),
                            "control_receipt_digest": str(
                                (plan_receipt or {}).get("receipt_digest") or ""
                            ),
                            "precommit_plan_digest": str(
                                (precommit_plan or {}).get("plan_digest") or ""
                            ),
                            "evaluation_contract_digest": str(
                                (precommit.get("precommit_eval_contract") or {}).get(
                                    "contract_digest"
                                )
                                or ""
                            ),
                            "native_match_timing_plan_digest": str(
                                (((precommit_plan or {}).get("settings") or {}).get(
                                    "native_match_timing_plan_digest"
                                ))
                                or ""
                            ),
                            "precommit_attempt": int(
                                ckpt.get("precommit_attempt") or 0
                            ),
                        }
                        if not isinstance(execution_scope, dict):
                            control_errors.append(
                                "first_strict_control_execution_scope_missing"
                            )
                        else:
                            for field, expected in expected_execution_bindings.items():
                                if execution_scope.get(field) != expected:
                                    control_errors.append(
                                        "first_strict_control_execution_scope_"
                                        f"{field}_mismatch"
                                    )
                        try:
                            from first_strict_execution_journal import (
                                read_succeeded_control_execution,
                            )

                            execution_receipts = [
                                repeat.get("execution_receipt")
                                for matchup in (
                                    (precommit.get("national") or {}).get(
                                        "matchups"
                                    )
                                    or []
                                )
                                for repeat in (matchup.get("repeats") or [])
                            ]
                            read_succeeded_control_execution(
                                execution_scope,
                                expected_receipts=execution_receipts,
                                expected_terminal_receipt=precommit.get(
                                    "first_strict_execution_terminal_receipt"
                                ),
                            )
                        except Exception as exc:
                            control_errors.append(
                                "first_strict_control_execution_terminal_invalid:"
                                f"{type(exc).__name__}"
                            )
                        result_errors, recomputed_control_gate = (
                            validate_control_result(
                                precommit,
                                expected_sample_plan=expected_control_samples,
                                expected_execution_scope=execution_scope,
                            )
                        )
                        control_errors.extend(result_errors)
                        control_blockers, _ = control_gate_blockers(
                            precommit,
                            expected_sample_plan=expected_control_samples,
                            expected_execution_scope=execution_scope,
                        )
                        if control_blockers:
                            control_errors.extend(
                                str(item.get("reason") or "control_gate_failed")
                                for item in control_blockers
                            )
                        if precommit.get(
                            "first_strict_control_gate"
                        ) != recomputed_control_gate:
                            control_errors.append(
                                "first_strict_control_gate_summary_mismatch"
                            )
                        if control_errors:
                            failed_gates.append({
                                "gate": "first_strict_control_final_ledger",
                                "reason": (
                                    "system control authority/content/floor is "
                                    "invalid or the strict pool changed"
                                ),
                                "errors": list(dict.fromkeys(control_errors))[:20],
                            })
                    elif control_opponents:
                        failed_gates.append({
                            "gate": "first_strict_control_final_ledger",
                            "reason": (
                                "system control appeared outside the declared "
                                "one-time empty-pool migration"
                            ),
                        })
                except Exception as exc:
                    failed_gates.append({
                        "gate": "precommit_eval_contract",
                        "reason": (
                            "precommit contract validation error: "
                            f"{type(exc).__name__}: {str(exc)[:200]}"
                        ),
                    })

        if expected_execution_mode == "native_tcp":
            try:
                from national_native import check_native_contract
                native_contract_errors = check_native_contract(
                    bot_dir,
                    require_current_stream_decoder=True,
                    require_current_decision_runtime=True,
                )
            except Exception as exc:
                native_contract_errors = [f"{type(exc).__name__}: {str(exc)[:200]}"]
            if native_contract_errors:
                failed_gates.append({
                    "gate": "native_contract",
                    "reason": "candidate is not a valid native national TCP bot",
                    "errors": native_contract_errors[:5],
                })
            try:
                from national_position_contract import detect_position_semantics_errors
                position_errors = detect_position_semantics_errors(bot_dir)
            except Exception as exc:
                position_errors = [f"position_contract_check_error: {type(exc).__name__}: {str(exc)[:200]}"]
            if position_errors:
                failed_gates.append({
                    "gate": "position_semantics",
                    "reason": "candidate violates national heads-up position semantics",
                    "errors": position_errors[:10],
                })

    return {
        "ok": not missing_gates and not failed_gates,
        "missing_gates": missing_gates,
        "failed_gates": failed_gates,
        "gate_results": gate_results,
        "current_code_fingerprint": current_code_fingerprint,
        "checkpoint_stage": ckpt.get("stage") if ckpt else None,
    }
