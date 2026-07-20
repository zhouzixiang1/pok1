from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import sys

import pytest
from claude_agent_sdk import ResultMessage


@pytest.fixture
def strict_authority(monkeypatch, tmp_path):
    import evolution_infra
    import strict_authority_workflow as module
    import tool_bot_management
    from workflow_kernel import WorkflowStore

    results_dir = tmp_path / "results"
    store = WorkflowStore(results_dir / "workflow" / "events.sqlite3")
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
    # ``tool_bot_management`` imports ``RESULTS_DIR`` into its own module
    # namespace at import time, so ``terminal_gate_abandon_fence_proof_if_present``
    # (and the abandon reconcile path) read its private binding rather than the
    # patched ``evolution_infra.RESULTS_DIR``.  Without this the validator would
    # reprove a stale Worker fence from the real on-disk event journal and fail
    # with ``WorkerAbandoned_outer_reason_mismatch`` whenever the local
    # ``core/results/workflow/events.sqlite3`` carried a prior run with the same
    # ``workflow_run_id``.
    monkeypatch.setattr(tool_bot_management, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(module, "_store", lambda: store)
    monkeypatch.setattr(
        module,
        "_project_role_result",
        lambda _call, raw_output: json.loads(raw_output),
    )
    return module, store, results_dir


def _checkpoint(candidate):
    return {
        "checkpoint_schema_version": 4,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": {"policy": "strict", "digest": "epoch"},
        "workflow_run_id": "generation:143:workflow-v52",
        "next_v": 143,
        "source_v": 142,
        "parent2_v": None,
        "stage": "review_rejected",
        "checkpoint_revision": 9,
        "master_plan": {"strategy": "test"},
        "audit_context": {"protocol_bootstrap": {"mode": "test"}},
        "gate_results": {},
        "candidate": str(candidate),
    }


def _projected_terminal_review_checkpoint(candidate):
    from bot_artifact import hash_path
    from strict_authority_workflow import (
        LEGACY_REVIEW_TERMINAL_MIGRATION_KIND,
    )
    from workflow_kernel import content_digest

    quality_gate = {"passed": True}
    migration_subject = {
        "schema_version": 1,
        "kind": LEGACY_REVIEW_TERMINAL_MIGRATION_KIND,
        "disposition": "terminal_rejection_only",
        "semantic_upgrade_status": "unavailable_from_legacy_quality_gate",
        "recorded_semantic_inputs_digest": "1" * 64,
        "current_semantic_inputs_digest": "1" * 64,
        "legacy_projection_digest": "1" * 64,
        "current_review_semantic_contract_digest": None,
        "legacy_quality_gate_digest": content_digest(quality_gate),
        "recorded_renderer_contract_digest": "5" * 64,
        "recorded_renderer_static_identity_digest": "6" * 64,
        "recorded_producer_file_sha256": "7" * 64,
        "recorded_producer_function_sha256": "8" * 64,
        "recorded_template_digests_digest": "9" * 64,
    }
    migration = {
        **migration_subject,
        "migration_digest": content_digest(migration_subject),
    }
    role_result = {
        "approved": False,
        "quality_score": 3,
        "feedback": "legacy terminal rejection",
        "change_summary": "reviewed",
        "risk_areas": [],
    }
    candidate_hash = hash_path(candidate)
    gate = {
        "version": 143,
        "source_v": 142,
        "passed": False,
        "approved": False,
        "llm_invoked": True,
        "reviewer_llm_executed": True,
        "schema_valid": True,
        "quality_score": 3,
        "feedback": role_result["feedback"],
        "change_summary": role_result["change_summary"],
        "risk_areas": [],
        "llm_role_result": role_result,
        "llm_authority_receipt": {
            "effect_id": "strict-llm-" + "a" * 64,
            "invocation_id": "b" * 32,
            "receipt_digest": "c" * 64,
        },
        "llm_execution_evidence": {"evidence_digest": "d" * 64},
        "terminal_authority_context_binding": {
            "candidate_artifact_hash": candidate_hash,
        },
        "operator_reconciled_completed_effect": True,
        "terminal_semantic_migration": migration,
    }
    checkpoint = _checkpoint(candidate)
    checkpoint["gate_results"] = {
        "quality": quality_gate,
        "review": gate,
    }
    outcome_subject = {
        "schema_version": 1,
        "kind": "pipeline-terminal-gate-outcome-v1",
        "disposition": "abandon_generation",
        "gate_name": "review",
        "terminal_stage": "review_rejected",
        "reason_code": "review_rejected",
        "failure_class": "strategy_review",
        "workflow_run_id": checkpoint["workflow_run_id"],
        "next_v": 143,
        "source_v": 142,
        "parent2_v": None,
        "evaluation_epoch": checkpoint["evaluation_epoch"],
        "input_checkpoint_stage": "quality_passed",
        "input_checkpoint_revision": 8,
        "projected_checkpoint_revision": 9,
        "candidate_artifact_hash": candidate_hash,
        "gate_payload_digest": content_digest(gate),
    }
    checkpoint["terminal_gate_outcome"] = {
        **outcome_subject,
        "receipt_digest": content_digest(outcome_subject),
    }
    return checkpoint


def _expected_recovered_call(checkpoint):
    gate = checkpoint["gate_results"]["review"]
    receipt = gate["llm_authority_receipt"]
    return {
        "accepted_role_result": deepcopy(gate["llm_role_result"]),
        "accepted_receipt": deepcopy(receipt),
        "context_binding": deepcopy(
            gate["terminal_authority_context_binding"]
        ),
        "terminal_semantic_migration": deepcopy(
            gate["terminal_semantic_migration"]
        ),
        "_expected_bound_invocation_evidence": deepcopy(
            gate["llm_execution_evidence"]
        ),
        "effect_id": receipt["effect_id"],
        "invocation_id": receipt["invocation_id"],
    }


def _install_stubs(
    monkeypatch,
    checkpoint,
    candidate,
    expected_recovered,
):
    import evolution_core
    import evolution_infra
    import gate_outcome
    import pipeline_state
    import strict_authority_workflow
    import system_strict_bootstrap

    monkeypatch.setattr(
        evolution_infra,
        "read_pipeline_checkpoint",
        lambda: checkpoint,
    )
    monkeypatch.setattr(
        evolution_core,
        "get_bot_dir",
        lambda _version: candidate,
    )
    monkeypatch.setattr(
        system_strict_bootstrap,
        "is_declared_native_bootstrap",
        lambda _checkpoint: True,
    )
    monkeypatch.setattr(
        gate_outcome,
        "validate_terminal_gate_outcome",
        lambda *_args, **_kwargs: [],
    )
    def recover(pre_projection, *, gate_name, candidate_dir):
        assert pre_projection["stage"] == "quality_passed"
        assert pre_projection["checkpoint_revision"] == 8
        assert "review" not in pre_projection["gate_results"]
        assert "terminal_gate_outcome" not in pre_projection
        assert gate_name == "review"
        assert candidate_dir == candidate
        return deepcopy(expected_recovered)

    monkeypatch.setattr(
        strict_authority_workflow,
        "recover_terminal_gate_rejection_call",
        recover,
    )
    monkeypatch.setattr(
        strict_authority_workflow,
        "bound_invocation_evidence",
        lambda _call: deepcopy(
            expected_recovered["_expected_bound_invocation_evidence"]
        ),
    )
    monkeypatch.setattr(
        pipeline_state,
        "route_policy",
        lambda value: {
            "intent": "terminal_gate_abandon",
            "next_tool": "abandon_generation",
            "allowed_tools": ["abandon_generation"],
            "terminal_gate_outcome_digest": (
                value["terminal_gate_outcome"]["receipt_digest"]
            ),
        },
    )


def _rebind_digests(checkpoint):
    from workflow_kernel import content_digest

    gate = checkpoint["gate_results"]["review"]
    migration = gate["terminal_semantic_migration"]
    migration["migration_digest"] = content_digest({
        key: value
        for key, value in migration.items()
        if key != "migration_digest"
    })
    outcome = checkpoint["terminal_gate_outcome"]
    outcome["gate_payload_digest"] = content_digest(gate)
    outcome["receipt_digest"] = content_digest({
        key: value
        for key, value in outcome.items()
        if key != "receipt_digest"
    })


def _legacy_renderer_context(module, checkpoint, candidate_hash, quality):
    inputs = {
        "master_plan": deepcopy(checkpoint["master_plan"]),
        "source_v": 142,
        "next_v": 143,
        "strict_bootstrap": True,
        "focus_areas": ["range update"],
    }
    static = {
        "producer_file": "web/core/tool_gates.py",
        "producer_name": "_render_reviewer_provider_prompt",
        "producer_file_sha256": "1" * 64,
        "producer_function_sha256": "2" * 64,
        "template_digests": [[
            "web/core/prompts/reviewer_prompt.md",
            "3" * 64,
        ]],
    }
    semantics_subject = {
        "schema_version": 1,
        "role": "LEAD CODE REVIEWER",
        "invocation_normalization": "fixed-32-byte-sentinel-v1",
        "semantic_inputs": inputs,
        "semantic_inputs_digest": module.content_digest(inputs),
        "renderer_static_identity": static,
        "renderer_static_identity_digest": module.content_digest(static),
        "sentinel_rendered_prompt_sha256": "4" * 64,
        "sentinel_rendered_prompt_chars": 1234,
        "sentinel_evidence_kind": "review_candidate_pair",
        "sentinel_evidence_provenance_sha256": "5" * 64,
        "sentinel_renderer_receipt_digest": "6" * 64,
        "sentinel_evidence_receipt_digest": "7" * 64,
        "sentinel_dispatch_receipt_digest": "8" * 64,
    }
    semantics = {
        **semantics_subject,
        "contract_digest": module.content_digest(semantics_subject),
    }
    return {
        "phase": "review",
        "candidate_artifact_hash": candidate_hash,
        "quality_gate_digest": module.content_digest(quality),
        "master_receipt_digest": "c" * 64,
        "master_plan_digest": "d" * 64,
        "renderer_semantics": semantics,
    }


def test_fenced_real_journal_projection_validates_and_resumes_canonically(
    strict_authority,
    monkeypatch,
    tmp_path,
):
    import bot_namespace
    import checkpoint_schema
    import evolution_core
    import evolution_infra
    import pipeline_infrastructure
    import strict_authority_workflow as authority
    import system_strict_bootstrap
    import terminal_gate_reconcile as reconcile
    import tool_bot_management as management
    from bot_artifact import hash_path
    from gate_outcome import (
        build_terminal_gate_outcome,
        terminal_outcome_abandon_reason,
        validate_terminal_gate_outcome,
    )
    from pipeline_state import route_policy

    module, _store, results_dir = strict_authority
    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
    candidate_hash = hash_path(candidate)
    quality = {
        "version": 143,
        "source_v": 142,
        "passed": True,
        "all_passed": True,
        "critical_scenarios_passed": True,
        "code_fingerprint": candidate_hash,
        "national_architecture_transition": {"ok": True},
        "national_capability_contract": {"ok": True},
    }
    checkpoint = {
        **_checkpoint(candidate),
        "stage": "quality_passed",
        "checkpoint_revision": 8,
        "master_plan": {
            "tasks": [{"worker_id": "W1"}],
            "proposal_binding": {
                "execution_mode": "fixed_blueprint_capability_audit",
            },
        },
        "audit_context": {
            "protocol_bootstrap": {"receipt_digest": "a" * 64},
            "prepared_artifact_contract": {
                "contract_digest": "b" * 64,
                "prepared_artifact_hash": candidate_hash,
            },
            "system_strict_bootstrap": {
                "receipt_digest": "c" * 64,
                "plan_digest": "d" * 64,
            },
            "worker_cot_focus_areas": ["range update"],
        },
        "gate_results": {"quality": quality},
    }
    recorded_context = _legacy_renderer_context(
        module,
        checkpoint,
        candidate_hash,
        quality,
    )
    role_result = {
        "approved": False,
        "quality_score": 3,
        "feedback": "terminal reject",
        "change_summary": "reviewed",
        "risk_areas": [],
    }
    call = module.new_call(
        checkpoint,
        slot="review",
        context_binding=recorded_context,
    )
    module.dispatch_call(
        call,
        full_prompt="legacy five-key reviewer prompt",
        tools=module.SLOT_TOOLS["review"],
        owner="pytest",
    )
    provider_result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="legacy-review-fenced",
        total_cost_usd=0.01,
        usage={},
        result=json.dumps(role_result, sort_keys=True),
    )
    module._observe_provider_result(
        provider_result,
        invocation_id=call["invocation_id"],
        effect_id=call["effect_id"],
    )
    module.complete_provider_call(
        call,
        raw_output=provider_result.result,
        provider_results=[provider_result],
    )
    authority_receipt = module.accept_role_result(
        call,
        role_result=role_result,
        parse_contract=module.SLOT_PARSE_CONTRACTS["review"],
    )
    log_file = module.strict_invocation_log_path(
        call,
        logs_dir=results_dir / "v143" / "logs",
        basename="reviewer_io.txt",
    )
    log_file.write_text("completed provider log\n", encoding="utf-8")
    execution_evidence = module.record_bound_invocation_evidence(
        call,
        log_file=log_file,
    )
    monkeypatch.setattr(
        system_strict_bootstrap,
        "is_declared_native_bootstrap",
        lambda _checkpoint: True,
    )
    recovered = module.recover_terminal_gate_rejection_call(
        checkpoint,
        gate_name="review",
        candidate_dir=candidate,
    )
    migration = recovered["terminal_semantic_migration"]

    gate = {
        "version": 143,
        "source_v": 142,
        "passed": False,
        "approved": False,
        "llm_invoked": True,
        "reviewer_llm_executed": True,
        "schema_valid": True,
        "quality_score": 3,
        "feedback": role_result["feedback"],
        "change_summary": role_result["change_summary"],
        "risk_areas": [],
        "llm_role_result": role_result,
        "llm_authority_receipt": authority_receipt,
        "llm_execution_evidence": execution_evidence,
        "terminal_authority_context_binding": recorded_context,
        "operator_reconciled_completed_effect": True,
        "terminal_semantic_migration": migration,
    }
    outcome = build_terminal_gate_outcome(
        checkpoint,
        gate_name="review",
        gate_payload=gate,
        candidate_dir=candidate,
        reason_code="review_rejected",
        failure_class="strategy_review",
    )
    assert module.abandon_authority(
        checkpoint,
        reason=terminal_outcome_abandon_reason(outcome),
    )["abandoned"] is True
    assert module.recover_terminal_gate_rejection_call(
        checkpoint,
        gate_name="review",
        candidate_dir=candidate,
    )["terminal_semantic_migration"] == migration
    projected = deepcopy(checkpoint)
    projected.update({
        "stage": "review_rejected",
        "checkpoint_revision": 9,
        "gate_results": {"quality": quality, "review": gate},
        "terminal_gate_outcome": outcome,
    })
    # The canonical abandon fences the outer Worker journal first, then the
    # strict child.  This projection is "fenced": reproduce both real journals
    # so ``validate_terminal_gate_outcome`` re-proves the exact terminal prefix
    # from the isolated store rather than reading a stale on-disk event log.
    from worker_workflow import WorkerWorkflow

    WorkerWorkflow.for_checkpoint(projected).abandon(
        terminal_outcome_abandon_reason(outcome)
    )

    monkeypatch.setattr(
        authority,
        "authority_summary",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        authority,
        "expected_master_contexts",
        lambda _master_plan: {},
    )
    monkeypatch.setattr(
        checkpoint_schema,
        "checkpoint_epoch_errors",
        lambda _checkpoint: [],
    )
    monkeypatch.setattr(
        pipeline_infrastructure,
        "normalize_checkpoint_infrastructure",
        lambda value: value,
    )
    monkeypatch.setattr(
        pipeline_infrastructure,
        "infrastructure_route",
        lambda _checkpoint: None,
    )
    monkeypatch.setattr(bot_namespace, "bot_relpath", lambda _version: candidate)
    monkeypatch.setattr(evolution_core, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(
        evolution_infra,
        "read_pipeline_checkpoint",
        lambda: projected,
    )

    assert validate_terminal_gate_outcome(
        projected,
        candidate_dir=candidate,
    ) == []
    assert route_policy(projected)["intent"] == "terminal_gate_abandon"
    inspected = reconcile.inspect_terminal_gate_reconciliation()
    assert inspected["status"] == "reconcilable_terminal_review_abandon"
    captured = {}

    async def abandon(*, reason, **identity):
        captured.update({"reason": reason, **identity})
        return {"abandoned": True}

    monkeypatch.setattr(management, "_do_abandon_generation", abandon)
    result = asyncio.run(reconcile.resume_terminal_review_abandon())
    assert result["status"] == "terminal_review_abandon_executed"
    assert captured["expected_checkpoint_revision"] == 9
    assert captured["expected_terminal_gate_outcome_digest"] == (
        outcome["receipt_digest"]
    )


def test_projected_terminal_review_abandon_dry_run_is_exact_and_read_only(
    tmp_path,
    monkeypatch,
):
    import terminal_gate_reconcile as reconcile

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
    checkpoint = _projected_terminal_review_checkpoint(candidate)
    expected_recovered = _expected_recovered_call(checkpoint)
    _install_stubs(
        monkeypatch,
        checkpoint,
        candidate,
        expected_recovered,
    )

    inspected = reconcile.inspect_terminal_gate_reconciliation()

    assert inspected["status"] == "reconcilable_terminal_review_abandon"
    assert inspected["workflow_run_id"] == checkpoint["workflow_run_id"]
    assert inspected["checkpoint_stage"] == "review_rejected"
    assert inspected["checkpoint_revision"] == 9
    assert inspected["next_v"] == 143
    assert inspected["source_v"] == 142
    assert inspected["candidate_artifact_hash"] == (
        checkpoint["terminal_gate_outcome"]["candidate_artifact_hash"]
    )
    assert inspected["terminal_gate_outcome_digest"] == (
        checkpoint["terminal_gate_outcome"]["receipt_digest"]
    )
    assert inspected["terminal_semantic_migration_digest"] == (
        checkpoint["gate_results"]["review"]
        ["terminal_semantic_migration"]["migration_digest"]
    )
    assert inspected["provider_dispatch_required"] is False


@pytest.mark.parametrize(
    "drift",
    [
        "stage",
        "gate",
        "gate_set",
        "gate_field",
        "reason",
        "failure",
        "outcome_digest",
        "candidate",
        "migration",
        "migration_digest",
        "provider",
        "provider_false_field",
        "operator_effect",
        "approval",
        "top_quality_score",
        "top_feedback",
        "top_change_summary",
        "top_risk_areas",
        "role_result",
        "authority_receipt",
        "authority_context",
        "execution_evidence",
        "route",
    ],
)
def test_projected_terminal_review_abandon_drift_fails_closed(
    tmp_path,
    monkeypatch,
    drift,
):
    import terminal_gate_reconcile as reconcile

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
    checkpoint = _projected_terminal_review_checkpoint(candidate)
    expected_recovered = _expected_recovered_call(checkpoint)
    gate = checkpoint["gate_results"]["review"]
    outcome = checkpoint["terminal_gate_outcome"]
    if drift == "stage":
        checkpoint["stage"] = "quality_passed"
    elif drift == "gate":
        outcome["gate_name"] = "critic"
    elif drift == "gate_set":
        checkpoint["gate_results"]["critic"] = {"approved": True}
    elif drift == "gate_field":
        gate["unexpected"] = True
    elif drift == "reason":
        outcome["reason_code"] = "review_receipt_invalid"
    elif drift == "failure":
        outcome["failure_class"] = "control_plane"
    elif drift == "outcome_digest":
        outcome["receipt_digest"] = "f" * 64
    elif drift == "candidate":
        outcome["candidate_artifact_hash"] = "e" * 64
    elif drift == "migration":
        gate["terminal_semantic_migration"]["disposition"] = "promotion"
    elif drift == "migration_digest":
        gate["terminal_semantic_migration"]["migration_digest"] = "f" * 64
    elif drift == "provider":
        gate["provider_dispatch_required"] = True
    elif drift == "provider_false_field":
        gate["provider_dispatch_required"] = False
    elif drift == "operator_effect":
        gate["operator_reconciled_completed_effect"] = False
    elif drift == "approval":
        gate["approved"] = True
        gate["llm_role_result"]["approved"] = True
    elif drift == "top_quality_score":
        gate["quality_score"] = 2
    elif drift == "top_feedback":
        gate["feedback"] = "changed top-level feedback"
    elif drift == "top_change_summary":
        gate["change_summary"] = "changed top-level summary"
    elif drift == "top_risk_areas":
        gate["risk_areas"] = ["changed"]
    elif drift == "role_result":
        gate["llm_role_result"]["feedback"] = "changed"
    elif drift == "authority_receipt":
        gate["llm_authority_receipt"]["receipt_digest"] = "e" * 64
    elif drift == "authority_context":
        gate["terminal_authority_context_binding"]["extra"] = True
    elif drift == "execution_evidence":
        gate["llm_execution_evidence"]["evidence_digest"] = "e" * 64
    if drift not in {"outcome_digest", "migration_digest"}:
        _rebind_digests(checkpoint)
    _install_stubs(
        monkeypatch,
        checkpoint,
        candidate,
        expected_recovered,
    )
    if drift == "route":
        import pipeline_state

        monkeypatch.setattr(
            pipeline_state,
            "route_policy",
            lambda _value: {
                "intent": "operator_reconcile_checkpoint",
                "next_tool": None,
                "allowed_tools": [],
                "terminal_gate_outcome_digest": outcome["receipt_digest"],
            },
        )

    with pytest.raises(
        reconcile.TerminalGateReconcileError,
        match="terminal_review_abandon_invalid",
    ):
        reconcile._inspect_projected_terminal_review_abandon(checkpoint)


def test_projected_execute_only_calls_exact_canonical_abandon(
    tmp_path,
    monkeypatch,
):
    import strict_authority_workflow as authority
    import terminal_gate_reconcile as reconcile
    import tool_bot_management as management

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
    checkpoint = _projected_terminal_review_checkpoint(candidate)
    expected_recovered = _expected_recovered_call(checkpoint)
    _install_stubs(
        monkeypatch,
        checkpoint,
        candidate,
        expected_recovered,
    )
    inspected = reconcile.inspect_terminal_gate_reconciliation()
    monkeypatch.setattr(
        reconcile,
        "inspect_terminal_gate_reconciliation",
        lambda: inspected,
    )
    forbidden = lambda *_args, **_kwargs: pytest.fail(
        "projected resume must not accept, bind, or dispatch provider work"
    )
    monkeypatch.setattr(authority, "accept_role_result", forbidden)
    monkeypatch.setattr(authority, "record_bound_invocation_evidence", forbidden)
    monkeypatch.setattr(authority, "dispatch_call", forbidden)
    captured = {}

    async def abandon(*, reason, **identity):
        captured.update({"reason": reason, **identity})
        return {"abandoned": True, "removed_directory": "national_v143"}

    monkeypatch.setattr(management, "_do_abandon_generation", abandon)

    result = asyncio.run(reconcile.resume_terminal_review_abandon())

    digest = checkpoint["terminal_gate_outcome"]["receipt_digest"]
    assert result["abandoned"] is True
    assert result["status"] == "terminal_review_abandon_executed"
    assert result["provider_dispatch_required"] is False
    assert captured == {
        "reason": f"terminal_gate_outcome:{digest}",
        "_bypass_rate_limit": True,
        "expected_terminal_gate_outcome_digest": digest,
        "expected_workflow_run_id": checkpoint["workflow_run_id"],
        "expected_next_v": 143,
        "expected_source_v": 142,
        "expected_checkpoint_revision": 9,
        "expected_checkpoint_stage": "review_rejected",
    }


def test_projected_execute_failure_is_not_reported_as_executed(
    tmp_path,
    monkeypatch,
):
    import terminal_gate_reconcile as reconcile
    import tool_bot_management as management

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
    checkpoint = _projected_terminal_review_checkpoint(candidate)
    expected_recovered = _expected_recovered_call(checkpoint)
    _install_stubs(
        monkeypatch,
        checkpoint,
        candidate,
        expected_recovered,
    )

    async def refused_abandon(**_kwargs):
        return {"abandoned": False, "reason": "exact_identity_mismatch"}

    monkeypatch.setattr(management, "_do_abandon_generation", refused_abandon)

    result = asyncio.run(reconcile.resume_terminal_review_abandon())

    assert result["abandoned"] is False
    assert result["status"] == "terminal_review_abandon_failed"


def test_dispatch_preserves_quality_passed_path(monkeypatch):
    import evolution_infra
    import terminal_gate_reconcile as reconcile

    monkeypatch.setattr(
        evolution_infra,
        "read_pipeline_checkpoint",
        lambda: {
            "stage": "quality_passed",
            "workflow_run_id": "generation:143:legacy-quality-path",
        },
    )
    expected = {
        "status": "reconcilable_terminal_review_rejection",
        "provider_dispatch_required": False,
    }
    monkeypatch.setattr(
        reconcile,
        "inspect_completed_review_rejection",
        lambda: expected,
    )

    assert reconcile.inspect_terminal_gate_reconciliation() == expected


def test_cli_reports_projected_abandon_status(monkeypatch, capsys):
    import scripts.reconcile_terminal_gate as cli
    import terminal_gate_reconcile as reconcile

    monkeypatch.setattr(sys, "argv", ["reconcile_terminal_gate.py"])
    monkeypatch.setattr(
        reconcile,
        "inspect_terminal_gate_reconciliation",
        lambda: {
            "status": "reconcilable_terminal_review_abandon",
            "checkpoint": {"private": True},
            "gate": {"private": True},
            "outcome": {"private": True},
            "route": {"private": True},
            "workflow_run_id": "generation:143:workflow-v52",
            "checkpoint_revision": 9,
            "terminal_gate_outcome_digest": "a" * 64,
            "provider_dispatch_required": False,
        },
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "reconcilable_terminal_review_abandon"
    assert payload["checkpoint_revision"] == 9
    assert payload["provider_dispatch_required"] is False
    assert "checkpoint" not in payload
    assert "gate" not in payload
    assert "outcome" not in payload
    assert "route" not in payload
