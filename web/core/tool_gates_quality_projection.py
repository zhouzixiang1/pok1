"""Quality-gate helpers extracted verbatim from the run_quality_gates monolith.

Three pure-construction blocks lifted from the monolith body:
- _quality_cache_current_impl: cache-validity check (originally a nested def).
- _build_quality_scorecard: the ScoreCard / GateResult chain + infra mutation.
- _build_failed_gates_detail: the failed-gate diagnostic list (+ rejection recording).

Closure dependencies are passed as explicit kwargs; parent-module globals
route through the _tg alias so test monkeypatches remain authoritative.
"""

from __future__ import annotations

import tool_gates as _tg  # parent; respects test monkeypatches


def _quality_cache_current_impl(
    gate,
    *,
    bot_dir,
    code_fingerprint,
    native_tcp_mode,
    runtime_contract_ledger_digest,
    source_v,
    v,
    workflow_profile,
    _master_plan_for_scope,
):
    """Quality gate cache-validity check, extracted verbatim from the
    run_quality_gates monolith nested function. Closure deps passed as
    explicit kwargs; parent globals route through _tg."""
    if _tg._transient_task_context_errors(bot_dir):
        return False
    try:
        from candidate_hygiene import forbidden_runtime_dependency_errors

        if forbidden_runtime_dependency_errors(bot_dir):
            return False
    except Exception:
        return False
    current_proposal_binding = (
        _master_plan_for_scope.get("proposal_binding")
        if isinstance(_master_plan_for_scope, dict)
        else None
    )
    if isinstance(current_proposal_binding, dict):
        cached_proposal = gate.get("selected_proposal_quality_evidence") or {}
        expected_check_id = str(
            (current_proposal_binding.get("falsifier") or {}).get(
                "test_name"
            )
            or ""
        )
        if (
            cached_proposal.get("required") is not True
            or cached_proposal.get("ok") is not True
            or cached_proposal.get("proposal_contract_digest")
            != current_proposal_binding.get("contract_digest")
            or cached_proposal.get("check_id") != expected_check_id
            or _tg.re.fullmatch(
                r"[0-9a-f]{64}",
                str(cached_proposal.get("check_evidence_digest") or ""),
            )
            is None
            or (
                current_proposal_binding.get("execution_mode")
                == "strategy_implementation"
                and (
                    cached_proposal.get("reachable_symbol_diff_required")
                    is not True
                    or cached_proposal.get("reachable_symbol_diff_ok")
                    is not True
                    or _tg.re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(
                            cached_proposal.get(
                                "reachable_symbol_diff_digest"
                            )
                            or ""
                        ),
                    )
                    is None
                )
            )
        ):
            return False
    cached_profile_id = str(gate.get("workflow_profile_id") or gate.get("profile_id") or "")
    cached_execution_mode = str(gate.get("national_execution_mode") or "")
    expected_execution_mode = "native_tcp"
    if cached_profile_id != workflow_profile.profile_id or cached_execution_mode != expected_execution_mode:
        _tg.log_system_event(
            "pipeline.quality_cache_profile_stale",
            "warn",
            f"Quality gate cache stale for v{v}; cached workflow "
            f"{cached_profile_id or 'unknown'}/{cached_execution_mode or 'unknown'} "
            f"does not match active workflow {workflow_profile.profile_id}/{expected_execution_mode}.",
            {
                "version": v,
                "source_v": source_v,
                "cached_workflow_profile_id": cached_profile_id,
                "cached_execution_mode": cached_execution_mode,
                "active_workflow_profile_id": workflow_profile.profile_id,
                "active_execution_mode": expected_execution_mode,
            },
        )
        return False
    if native_tcp_mode and gate.get("national_native_contract_ok") is not True:
        _tg.log_system_event(
            "pipeline.quality_cache_native_contract_stale",
            "warn",
            f"Quality gate cache stale for v{v}; native TCP contract was not recorded as passed.",
            {
                "version": v,
                "source_v": source_v,
                "cached_native_contract_ok": gate.get("national_native_contract_ok"),
            },
        )
        return False
    cached_acceptance = gate.get("national_acceptance")
    cached_acceptance = (
        cached_acceptance if isinstance(cached_acceptance, dict) else {}
    )
    if native_tcp_mode and not (
        gate.get("national_acceptance_ok") is True
        and cached_acceptance.get("executed") is True
        and cached_acceptance.get("skipped") is False
        and cached_acceptance.get("passed") is True
        and cached_acceptance.get("conclusive") is True
    ):
        _tg.log_system_event(
            "pipeline.quality_cache_national_acceptance_stale",
            "warn",
            f"Quality gate cache stale for v{v}; native acceptance lacks "
            "executed, conclusive pass evidence.",
            {
                "version": v,
                "source_v": source_v,
                "cached_national_acceptance_ok": gate.get(
                    "national_acceptance_ok"
                ),
                "cached_national_acceptance": cached_acceptance,
            },
        )
        return False
    if native_tcp_mode:
        try:
            from national_capability_contract import NATIONAL_CAPABILITY_DETECTOR_VERSION
            from national_runtime_probe import (
                RUNTIME_PROBE_LIMITS_DIGEST,
                RUNTIME_PROBE_IDENTITY_DIGEST,
                RUNTIME_PROBE_ORCHESTRATOR_VERSION,
                RUNTIME_PROBE_SCENARIO_DIGEST,
                RUNTIME_PROBE_SCHEMA_VERSION,
                runtime_probe_native_template_evidence,
                validate_runtime_probe_repeatability_evidence,
            )
            from runtime_architecture_policy import RUNTIME_ARCHITECTURE_POLICY_VERSION
        except Exception:
            return False
        cached_capability = gate.get("national_capability_contract")
        cached_capability = (
            cached_capability if isinstance(cached_capability, dict) else {}
        )
        cached_transition = gate.get("national_architecture_transition")
        cached_transition = (
            cached_transition if isinstance(cached_transition, dict) else {}
        )
        if (
            cached_capability.get("detector_version") != NATIONAL_CAPABILITY_DETECTOR_VERSION
            or cached_transition.get("policy_version") != RUNTIME_ARCHITECTURE_POLICY_VERSION
        ):
            _tg.log_system_event(
                "pipeline.quality_cache_architecture_policy_stale",
                "warn",
                f"Quality gate cache stale for v{v}; runtime architecture detector/policy changed.",
                {
                    "version": v,
                    "source_v": source_v,
                    "cached_detector_version": cached_capability.get("detector_version"),
                    "current_detector_version": NATIONAL_CAPABILITY_DETECTOR_VERSION,
                    "cached_policy_version": cached_transition.get("policy_version"),
                    "current_policy_version": RUNTIME_ARCHITECTURE_POLICY_VERSION,
                },
            )
            return False
        expected_probe_identity = {
            "runtime_probe_schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
            "runtime_probe_orchestrator_version": RUNTIME_PROBE_ORCHESTRATOR_VERSION,
            "runtime_probe_scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
            "runtime_probe_limits_digest": RUNTIME_PROBE_LIMITS_DIGEST,
            "runtime_probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
            "runtime_contract_ledger_digest": runtime_contract_ledger_digest,
            **runtime_probe_native_template_evidence(),
        }
        cached_dynamic_probe = (
            cached_capability.get("dynamic_runtime_probe") or {}
        )
        repeatability_errors = validate_runtime_probe_repeatability_evidence(
            cached_dynamic_probe
        )
        if repeatability_errors:
            _tg.log_system_event(
                "pipeline.quality_cache_runtime_probe_repeatability_stale",
                "warn",
                f"Quality gate cache stale for v{v}; runtime probe "
                "repeatability evidence is malformed or stale.",
                {
                    "version": v,
                    "source_v": source_v,
                    "errors": repeatability_errors[:12],
                },
            )
            return False
        managed_isolation_digest = str(
            cached_dynamic_probe.get("managed_isolation_digest") or ""
        )
        if len(managed_isolation_digest) != 64:
            return False
        expected_probe_identity["runtime_probe_managed_isolation_digest"] = (
            managed_isolation_digest
        )
        if any(gate.get(key) != value for key, value in expected_probe_identity.items()):
            _tg.log_system_event(
                "pipeline.quality_cache_runtime_probe_stale",
                "warn",
                f"Quality gate cache stale for v{v}; runtime probe or contract identity changed.",
                {
                    "version": v,
                    "source_v": source_v,
                    "expected": expected_probe_identity,
                    "cached": {key: gate.get(key) for key in expected_probe_identity},
                },
            )
            return False
    if gate.get("embedded_selftests_ok") is not True:
        _tg.log_system_event(
            "pipeline.quality_cache_embedded_selftests_stale",
            "warn",
            f"Quality gate cache stale for v{v}; embedded self-tests were not recorded as passed.",
            {
                "version": v,
                "source_v": source_v,
                "cached_embedded_selftests_ok": gate.get("embedded_selftests_ok"),
            },
        )
        return False
    cached_fingerprint = gate.get("code_fingerprint")
    if cached_fingerprint and cached_fingerprint == code_fingerprint:
        return True
    _tg.log_system_event(
        "pipeline.quality_cache_stale", "warn",
        f"Quality gate cache stale for v{v}; bot code changed since cached gate, rerunning quality gates.",
        {
            "version": v,
            "source_v": source_v,
            "cached_fingerprint": cached_fingerprint,
            "current_fingerprint": code_fingerprint,
        },
    )
    return False


def _build_quality_scorecard(
    *,
    candidate_gate_checks_passed,
    code_changed,
    code_fingerprint,
    compile_errors,
    decision_detail,
    decision_ok,
    decision_rate,
    decision_skill_layers,
    decision_total,
    declared_scope_errors,
    declared_scope_metrics,
    declared_scope_ok,
    embedded_selftest_errors,
    import_errors,
    issue,
    national_acceptance_errors,
    national_acceptance_ok,
    national_acceptance_payload,
    national_architecture_transition,
    national_capability_blockers,
    national_capability_contract,
    national_capability_ok,
    national_capability_required,
    national_protocol_errors,
    native_contract_errors,
    native_tcp_mode,
    official_local_status,
    official_smoke_blocking,
    official_smoke_classification,
    official_smoke_errors,
    official_smoke_inconclusive,
    official_smoke_ok,
    official_smoke_payload,
    position_semantics_errors,
    position_semantics_ok,
    post_master_changed_files,
    post_master_delta_ok,
    post_master_delta_required,
    prepared_artifact_hash,
    protected_contract_errors,
    quality_infra_issues,
    quality_infrastructure,
    reachability_ok,
    reachability_warnings,
    runtime_contract_identity_errors,
    runtime_contract_identity_ok,
    runtime_contract_ledger_digest,
    source_python_changed,
):
    """Build the quality ScoreCard (GateResult chain + infra gate
    mutation), extracted verbatim from the run_quality_gates monolith.
    All referenced locals passed as kwargs; parent globals route via _tg."""
    scorecard = _tg.ScoreCard(name="quality")
    scorecard.add(_tg.GateResult.from_bool(
        "code_changed",
        code_changed,
        failures=(
            []
            if code_changed
            else ["no decision-artifact file changed after the frozen prepared baseline"]
        ),
        metrics={
            "prepared_artifact_hash": prepared_artifact_hash,
            "candidate_artifact_hash": code_fingerprint,
            "changed_files": post_master_changed_files[:20],
            "source_python_changed": source_python_changed,
        },
    ))
    scorecard.add(_tg.GateResult.from_bool(
        "post_master_delta",
        post_master_delta_ok,
        blocking=post_master_delta_required,
        hidden=not post_master_delta_required,
        failures=(
            []
            if post_master_delta_ok
            else ["candidate has no file delta after the frozen prepared baseline"]
        ),
        metrics={
            "required": post_master_delta_required,
            "prepared_artifact_hash": prepared_artifact_hash,
            "candidate_artifact_hash": code_fingerprint,
        },
    ))
    scorecard.add(_tg.GateResult.from_bool(
        "declared_scope",
        declared_scope_ok,
        metrics=declared_scope_metrics,
        failures=declared_scope_errors[:6],
    ))
    scorecard.add(_tg.GateResult.from_bool("compile", len(compile_errors) == 0, failures=compile_errors[:3]))
    scorecard.add(_tg.GateResult.from_bool("runtime_import", len(import_errors) == 0, failures=[str(e) for e in import_errors[:3]]))
    scorecard.add(_tg.GateResult.from_bool("protected_contract", len(protected_contract_errors) == 0, failures=protected_contract_errors[:3]))
    scorecard.add(_tg.GateResult.from_bool(
        "national_native_contract",
        len(native_contract_errors) == 0,
        failures=native_contract_errors[:5],
        metrics={"execution_mode": "native_tcp"},
        blocking=native_tcp_mode,
        hidden=not native_tcp_mode,
    ))
    capability_required_failures = national_capability_contract.get("required_failures") or []
    capability_advisory_warnings = national_capability_contract.get("advisory_warnings") or []
    capability_failures = national_capability_blockers
    scorecard.add(_tg.GateResult.from_bool(
        "national_capability_contract",
        national_capability_ok,
        failures=[
            str(item.get("name", item))[:300] if isinstance(item, dict) else str(item)[:300]
            for item in capability_failures[:8]
        ],
        metrics={
            "execution_mode": "native_tcp",
            "required": national_capability_required,
            "required_failure_count": len(capability_required_failures),
            "advisory_warning_count": len(capability_advisory_warnings),
            "regression_count": len(national_architecture_transition.get("regressions") or []),
            "unresolved_focus_count": len(
                national_architecture_transition.get("unresolved_focus_checks") or []
            ),
        },
        artifacts={
            "contract": national_capability_contract,
            "transition": national_architecture_transition,
        },
        blocking=native_tcp_mode,
        hidden=not native_tcp_mode,
    ))
    scorecard.add(_tg.GateResult.from_bool(
        "runtime_probe_infrastructure",
        not quality_infrastructure["active"],
        failures=[str(item)[:300] for item in quality_infrastructure["issues"][:8]],
        metrics={
            "attempt": quality_infrastructure["attempt"],
            "max_attempts": quality_infrastructure["max_attempts"],
            "retryable": quality_infrastructure["retryable"],
            "failure_class": quality_infrastructure.get("failure_class", ""),
        },
        blocking=native_tcp_mode,
        hidden=not native_tcp_mode,
    ))
    scorecard.add(_tg.GateResult.from_bool(
        "runtime_contract_identity",
        runtime_contract_identity_ok,
        failures=runtime_contract_identity_errors[:8],
        metrics={"ledger_digest": runtime_contract_ledger_digest},
        blocking=native_tcp_mode,
        hidden=not native_tcp_mode,
    ))
    scorecard.add(_tg.GateResult.from_bool(
        "embedded_selftests",
        len(embedded_selftest_errors) == 0,
        failures=embedded_selftest_errors[:5],
    ))
    scorecard.add(_tg.GateResult.from_bool(
        "smoke",
        len(smoke_errors) == 0,
        failures=smoke_errors[:3],
        metrics={
            "execution_mode": smoke_payload.get("execution_mode", "native_tcp"),
            "hands": smoke_payload.get("hands"),
        },
        artifacts={"report": smoke_payload} if smoke_payload else {},
    ))
    scorecard.add(_tg.GateResult.from_bool("national_protocol", len(national_protocol_errors) == 0, failures=national_protocol_errors[:3]))
    acceptance_metrics = (
        dict(national_acceptance_payload.get("summary") or {})
        if isinstance(national_acceptance_payload, dict)
        else {}
    )
    acceptance_metrics.update({
        "executed": national_acceptance_payload.get("executed") is True,
        "skipped": national_acceptance_payload.get("skipped") is True,
        "passed": national_acceptance_payload.get("passed") is True,
        "conclusive": national_acceptance_payload.get("conclusive") is True,
        "outcome": national_acceptance_payload.get("outcome"),
        "coverage_ok": national_acceptance_payload.get("coverage_ok") is True,
        "report_consistent": (
            national_acceptance_payload.get("report_consistent") is True
        ),
        "expected_hands": national_acceptance_payload.get("expected_hands"),
        "observed_hands": national_acceptance_payload.get("observed_hands") or [],
    })
    scorecard.add(_tg.GateResult(
        name="national_acceptance",
        status=(
            "passed"
            if national_acceptance_ok
            else "skipped"
            if national_acceptance_payload.get("skipped") is True
            else "failed"
        ),
        blocking=True,
        failures=national_acceptance_errors[:5],
        metrics=acceptance_metrics,
        artifacts={"report": national_acceptance_payload}
        if national_acceptance_payload
        else {},
    ))
    scorecard.add(_tg.GateResult.from_bool(
        "official_smoke",
        official_smoke_ok,
        failures=official_smoke_errors[:5],
        metrics={
            "status": official_smoke_payload.get("status"),
            "mode": official_smoke_payload.get("mode"),
            "queued": official_smoke_payload.get("queued"),
            "cache_hit": official_smoke_payload.get("cache_hit"),
            "blocking": official_smoke_blocking,
            "inconclusive": official_smoke_inconclusive,
            "classification": official_smoke_classification,
        } if isinstance(official_smoke_payload, dict) else {},
        artifacts={"report": official_smoke_payload} if official_smoke_payload else {},
        blocking=official_smoke_blocking,
    ))
    official_status_error = (
        str(official_local_status.get("error"))
        if isinstance(official_local_status, dict) and official_local_status.get("error")
        else ""
    )
    scorecard.add(_tg.GateResult.from_bool(
        "official_status_persistence",
        not bool(official_status_error),
        failures=[official_status_error] if official_status_error else [],
        blocking=native_tcp_mode,
        hidden=not native_tcp_mode or not candidate_gate_checks_passed,
    ))
    scorecard.add(_tg.GateResult.from_bool(
        "decision",
        decision_ok,
        metrics={"pass_rate": round(decision_rate, 4), "total": decision_total},
        artifacts={"skill_layers": decision_skill_layers} if decision_skill_layers else {},
        failures=[str(f)[:300] for f in (decision_detail.get("failures", []) or [])[:5]],
    ))
    scorecard.add(_tg.GateResult.from_bool("size", len(oversized) == 0, failures=[f"{n}:{l}/{lim}" for n, l, lim in oversized]))
    scorecard.add(_tg.GateResult.from_bool("reachability", reachability_ok, failures=reachability_warnings[:6]))
    scorecard.add(_tg.GateResult.from_bool("position_semantics", position_semantics_ok, failures=position_semantics_errors[:6]))
    if quality_infrastructure["active"]:
        phase_gate_names = {
            "candidate_hygiene": {"national_native_contract"},
            "contract_identity": {"runtime_contract_identity"},
            "declared_scope": {"declared_scope"},
            "compile": {"compile"},
            "runtime_import": {"runtime_import"},
            "protected_contract": {"protected_contract"},
            "native_contract": {"national_native_contract"},
            "runtime_architecture": {"national_capability_contract"},
            "embedded_selftest": {"embedded_selftests"},
            "reachability": {"reachability"},
            "position_semantics": {"position_semantics"},
            "workflow_smoke": {"smoke"},
            "national_protocol": {"national_protocol"},
            "national_acceptance": {"national_acceptance"},
            "official_smoke": {"official_smoke"},
            "official_status_persistence": {"official_status_persistence"},
            "decision_tests": {"decision"},
            "code_size": {"size"},
        }
        infra_failures_by_gate: dict[str, list[str]] = {}
        for item in quality_infra_issues:
            for gate_name in phase_gate_names.get(str(item.get("phase") or ""), set()):
                infra_failures_by_gate.setdefault(gate_name, []).extend(
                    str(issue) for issue in item.get("issues") or []
                )
        for gate in scorecard.gates:
            if gate.name == "runtime_probe_infrastructure":
                gate.status = "error"
                gate.metrics = {**gate.metrics, "failure_class": "infrastructure"}
            if gate.name in infra_failures_by_gate:
                gate.status = "error"
                gate.failures = infra_failures_by_gate[gate.name][:6]
                gate.metrics = {**gate.metrics, "failure_class": "infrastructure"}
    return scorecard


def _build_failed_gates_detail(
    *,
    code_changed,
    compile_errors,
    decision_ok,
    decision_rate,
    declared_scope_errors,
    declared_scope_ok,
    embedded_selftest_errors,
    import_errors,
    national_acceptance_ok,
    national_capability_blockers,
    national_capability_ok,
    national_protocol_errors,
    native_contract_errors,
    official_smoke_blocking,
    official_smoke_ok,
    position_semantics_errors,
    position_semantics_ok,
    post_master_delta_ok,
    protected_contract_errors,
    quality_infrastructure,
    reachability_ok,
    reachability_warnings,
    runtime_contract_identity_errors,
    runtime_contract_identity_ok,
    selected_proposal_quality_evidence,
    selected_proposal_quality_ok,
    v,
):
    """Build the failed_gates_detail diagnostic list, extracted verbatim
    from the run_quality_gates monolith. Records gate rejections to
    worker_failures.jsonl via _tg._record_quality_failure. Returns the list."""
    failed_gates_detail = []
    if compile_errors:
        failed_gates_detail.append("compile")
    if import_errors:
        first_import = import_errors[0]
        failed_gates_detail.append(
            f"runtime_import({first_import.get('module')}: "
            f"{first_import.get('exception')} {first_import.get('message')})"
        )
    if protected_contract_errors:
        failed_gates_detail.append("protected_contract")
    if native_contract_errors:
        failed_gates_detail.append(f"national_native_contract({'; '.join(native_contract_errors[:3])})")
        for err in (native_contract_errors[:6] if not quality_infrastructure["active"] else []):
            _tg._record_quality_failure(
                v,
                "national_native_contract",
                "native_tcp",
                f"Native national TCP contract violation: {err}",
            )
    if quality_infrastructure["active"]:
        issue_text = "; ".join(str(item) for item in quality_infrastructure["issues"][:3])
        failed_gates_detail.append(
            f"runtime_probe_infrastructure({issue_text[:500]})"
        )
    if not national_capability_ok and not quality_infrastructure["active"]:
        failures = national_capability_blockers
        failed_gates_detail.append(
            f"national_capability_contract({'; '.join(str(item.get('name', item))[:120] for item in failures[:3])})"
        )
        for item in failures[:6]:
            _tg._record_quality_failure(
                v,
                "national_capability_contract",
                str(item.get("name", "runtime_architecture")),
                f"National runtime architecture contract issue: {item.get('guidance') or item}",
            )
    if not runtime_contract_identity_ok:
        failed_gates_detail.append(
            "runtime_contract_identity(" + "; ".join(runtime_contract_identity_errors[:3]) + ")"
        )
    if not selected_proposal_quality_ok:
        failed_gates_detail.append(
            "selected_proposal_quality("
            + "; ".join(
                selected_proposal_quality_evidence.get("errors") or []
            )[:500]
            + ")"
        )
    if embedded_selftest_errors:
        failed_gates_detail.append(
            f"embedded_selftests({'; '.join(e[:120] for e in embedded_selftest_errors[:3])})"
        )
        for err in (embedded_selftest_errors[:6] if not quality_infrastructure["active"] else []):
            _tg._record_quality_failure(
                v,
                "embedded_selftests",
                "bot_selftest",
                f"Embedded bot self-test failure: {err[:2000]}",
            )
    if smoke_errors:
        failed_gates_detail.append("smoke_test")
    if national_protocol_errors:
        failed_gates_detail.append("national_protocol_tests")
    if not national_acceptance_ok:
        failed_gates_detail.append("national_acceptance")
    if not official_smoke_ok and official_smoke_blocking:
        failed_gates_detail.append("official_smoke")
    if not decision_ok:
        failed_gates_detail.append(f"decision_tests({decision_rate:.0%})")
    if not code_changed:
        failed_gates_detail.append(
            f"no_code_changes(v{v} has no decision-artifact file delta after prepared baseline)"
        )
    if not post_master_delta_ok:
        failed_gates_detail.append(
            "no_post_master_delta(candidate has no file delta after frozen prepared baseline)"
        )
    if not declared_scope_ok:
        failed_gates_detail.append(f"declared_scope({'; '.join(declared_scope_errors[:3])})")
    if oversized:
        failed_gates_detail.append(f"file_size({', '.join(f'{n}:{l}L/{lim}L' for n, l, lim in oversized)})")
    if not reachability_ok:
        failed_gates_detail.append(
            f"reachability({'; '.join(w[:120] for w in reachability_warnings[:3])})"
        )
        for w in (reachability_warnings if not quality_infrastructure["active"] else []):
            _tg._record_quality_failure(
                v, "reachability", "dead_code",
                f"R1 reachability violation: {w[:2000]}",
            )
    if not position_semantics_ok:
        failed_gates_detail.append(
            f"position_semantics({'; '.join(e[:120] for e in position_semantics_errors[:3])})"
        )
        for err in (position_semantics_errors[:6] if not quality_infrastructure["active"] else []):
            _tg._record_quality_failure(
                v, "position_semantics", "national_rules",
                f"Position semantics violation: {err}",
            )

    if quality_infrastructure["active"]:
        failed_gates_detail = [
            "quality_infrastructure("
            + "; ".join(str(item) for item in quality_infrastructure["issues"][:3])[:800]
            + ")"
        ]
    return failed_gates_detail
