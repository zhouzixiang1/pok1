from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest
from claude_agent_sdk import ResultMessage


def _checkpoint(candidate, *, stage="workers_done", revision=7):
    return {
        "checkpoint_schema_version": 4,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": {"policy": "strict", "digest": "epoch"},
        "workflow_run_id": "generation:143:terminal-fence-test",
        "next_v": 143,
        "source_v": 142,
        "parent2_v": None,
        "stage": stage,
        "checkpoint_revision": revision,
        "master_plan": {"strategy": "test"},
        "audit_context": {"protocol_bootstrap": {"mode": "test"}},
        "gate_results": {},
        "candidate": str(candidate),
    }


def test_rev9_terminal_route_revalidates_exact_already_fenced_lifecycle(
    tmp_path,
    monkeypatch,
):
    import bot_namespace
    import checkpoint_schema
    import evolution_infra
    import gate_outcome
    import pipeline_infrastructure
    import pipeline_state
    import strict_authority_workflow as authority
    import tool_bot_management as management
    from bot_artifact import hash_path
    from worker_workflow import WorkerWorkflow
    from workflow_kernel import WorkflowStore

    results = tmp_path / "results"
    store = WorkflowStore(results / "workflow" / "events.sqlite3")
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(management, "RESULTS_DIR", results)
    monkeypatch.setattr(authority, "_store", lambda: store)
    monkeypatch.setattr(
        authority,
        "_project_role_result",
        lambda _call, raw: json.loads(raw),
    )
    # This regression exercises the terminal lifecycle and Reviewer receipt.
    # Master receipt breadth is independently covered by the strict-authority
    # suite; reducing its tuple here keeps the state shape focused and real.
    monkeypatch.setattr(authority, "MASTER_SLOTS", ())
    monkeypatch.setattr(authority, "expected_master_contexts", lambda _plan: {})

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
    }
    checkpoint = _checkpoint(
        candidate,
        stage="quality_passed",
        revision=8,
    )
    checkpoint.update({
        "master_plan": {"strategy": "frozen"},
        "audit_context": {
            "protocol_bootstrap": {"receipt_digest": "a" * 64},
            "prepared_artifact_contract": {
                "contract_digest": "b" * 64,
                "prepared_artifact_hash": "c" * 64,
            },
        },
        "gate_results": {"quality": quality},
    })
    context = {
        "phase": "review",
        "candidate_artifact_hash": candidate_hash,
        "quality_gate_digest": authority.content_digest(quality),
        "master_receipt_digest": "d" * 64,
        "master_plan_digest": "e" * 64,
    }
    monkeypatch.setattr(
        authority,
        "gate_call_context",
        lambda *_args, **_kwargs: deepcopy(context),
    )
    role_result = {
        "approved": False,
        "quality_score": 3,
        "feedback": "typed terminal rejection",
        "change_summary": "reviewed",
        "risk_areas": [],
    }
    call = authority.new_call(
        checkpoint,
        slot="review",
        context_binding=context,
    )
    authority.dispatch_call(
        call,
        full_prompt="frozen reviewer prompt",
        tools=["Read"],
        owner="pytest",
    )
    provider = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="terminal-rev9",
        total_cost_usd=0.01,
        usage={},
        result=json.dumps(role_result, sort_keys=True),
    )
    authority._observe_provider_result(
        provider,
        invocation_id=call["invocation_id"],
        effect_id=call["effect_id"],
    )
    authority.complete_provider_call(
        call,
        raw_output=provider.result,
        provider_results=[provider],
    )
    receipt = authority.accept_role_result(
        call,
        role_result=role_result,
        parse_contract="reviewer-output-schema-v1",
    )
    invocation_evidence = {"evidence_digest": "f" * 64}
    monkeypatch.setattr(
        authority,
        "bound_invocation_evidence",
        lambda _call: deepcopy(invocation_evidence),
    )
    gate = {
        "version": 143,
        "source_v": 142,
        "passed": False,
        "approved": False,
        "llm_invoked": True,
        "reviewer_llm_executed": True,
        "schema_valid": True,
        "llm_role_result": role_result,
        "llm_authority_receipt": receipt,
        "llm_execution_evidence": invocation_evidence,
        "terminal_authority_context_binding": context,
    }
    outcome = gate_outcome.build_terminal_gate_outcome(
        checkpoint,
        gate_name="review",
        gate_payload=gate,
        candidate_dir=candidate,
        reason_code="review_rejected",
        failure_class="strategy_review",
    )
    terminal = {
        **deepcopy(checkpoint),
        "stage": "review_rejected",
        "checkpoint_revision": 9,
        "gate_results": {"quality": quality, "review": gate},
        "terminal_gate_outcome": outcome,
    }
    reason = gate_outcome.terminal_outcome_abandon_reason(outcome)

    worker = WorkerWorkflow.for_checkpoint(terminal)
    worker.abandon(reason)
    # Crash boundary after the outer Worker was fenced but before the strict
    # child fence: the exact Worker terminal prefix is replay-safe and lets the
    # same canonical action finish, while a mismatched prefix would fail.
    assert gate_outcome.validate_terminal_gate_outcome(
        terminal,
        candidate_dir=candidate,
    ) == []
    prefix_proof = management.terminal_gate_abandon_fence_proof_if_present(
        terminal,
        reason=reason,
    )
    assert set(prefix_proof) == {"worker"}
    assert worker.abandon(reason)["abandon_reason"] == reason
    authority.abandon_authority(terminal, reason=reason)
    events_before = tuple(
        (event.event_type, event.payload_digest)
        for event in store.events(authority.authority_run_id(
            terminal["workflow_run_id"]
        ))
    )

    with pytest.raises(
        authority.StrictAuthorityError,
        match="strict_authority_phase_journal_abandoned:review",
    ):
        authority.authority_summary(
            terminal,
            required_slots=("review",),
            expected_role_results={"review": role_result},
            expected_context_bindings={"review": context},
            expected_invocation_evidence={"review": invocation_evidence},
            require_no_other_accepted=True,
        )

    assert gate_outcome.validate_terminal_gate_outcome(
        terminal,
        candidate_dir=candidate,
    ) == []
    assert management.validate_terminal_gate_abandon_fences(
        terminal,
        reason=reason,
    )["strict_authority"]["terminal_reason"] == reason

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
    # This is both the same-call second guard and the crash-retry shape: the
    # two journals are already fenced at revision 9, yet the sole route remains
    # the content-bound canonical abandon and no provider/effect is created.
    assert pipeline_state.generic_abandon_block(
        terminal,
        reason=reason,
    ) is None
    assert pipeline_state.generic_abandon_block(
        terminal,
        reason=reason,
    ) is None
    assert events_before == tuple(
        (event.event_type, event.payload_digest)
        for event in store.events(authority.authority_run_id(
            terminal["workflow_run_id"]
        ))
    )

    with pytest.raises(
        RuntimeError,
        match="terminal_gate_abandon_fence_identity_invalid",
    ):
        management.validate_terminal_gate_abandon_fences(
            terminal,
            reason="terminal_gate_outcome:" + "0" * 64,
        )

    # Quality has no provider role result, but an already-fenced lifecycle is
    # still authority.  It cannot relabel the same terminal journals with a
    # different quality-outcome reason and reach canonical action routing.
    quality_input = {
        **deepcopy(checkpoint),
        "stage": "workers_done",
        "checkpoint_revision": 8,
        "gate_results": {},
    }
    rejected_quality = {
        "version": 143,
        "source_v": 142,
        "passed": False,
        "all_passed": False,
        "failures": ["deterministic quality rejection"],
    }
    quality_outcome = gate_outcome.build_terminal_gate_outcome(
        quality_input,
        gate_name="quality",
        gate_payload=rejected_quality,
        candidate_dir=candidate,
        reason_code="quality_gate_rejected",
        failure_class="quality_gate",
    )
    quality_terminal = {
        **quality_input,
        "stage": "quality_rejected",
        "checkpoint_revision": 9,
        "gate_results": {"quality": rejected_quality},
        "terminal_gate_outcome": quality_outcome,
    }
    quality_errors = gate_outcome.validate_terminal_gate_outcome(
        quality_terminal,
        candidate_dir=candidate,
    )
    assert any(
        "terminal_outcome_abandon_fence_invalid" in issue
        and "outer_reason_mismatch" in issue
        for issue in quality_errors
    )

    (candidate / "policy.py").write_text("VALUE = 2\n", encoding="utf-8")
    candidate_drift = pipeline_state.generic_abandon_block(
        terminal,
        reason=reason,
    )
    assert candidate_drift["reason"] == "terminal_gate_outcome_invalid"
    assert any(
        "candidate_artifact_hash_mismatch" in issue
        for issue in candidate_drift["issues"]
    )
    (candidate / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")

    receipt_drift = deepcopy(terminal)
    receipt_drift["terminal_gate_outcome"]["receipt_digest"] = "0" * 64
    invalid_receipt = pipeline_state.generic_abandon_block(
        receipt_drift,
        reason=reason,
    )
    assert invalid_receipt["reason"] == "terminal_gate_outcome_invalid"
    assert any(
        "receipt_digest_invalid" in issue
        for issue in invalid_receipt["issues"]
    )

    from workflow_kernel import canonical_json, content_digest

    strict_run_id = authority.authority_run_id(terminal["workflow_run_id"])
    with sqlite3.connect(store.path) as connection:
        worker_row = connection.execute(
            "SELECT payload, payload_digest, causation_id FROM workflow_events "
            "WHERE run_id = ? AND event_type = 'WorkerAbandoned'",
            (terminal["workflow_run_id"],),
        ).fetchone()
        strict_row = connection.execute(
            "SELECT payload, payload_digest, causation_id FROM workflow_events "
            "WHERE run_id = ? AND event_type = 'StrictAuthorityAbandoned'",
            (strict_run_id,),
        ).fetchone()

        worker_extra = {"reason": reason, "extra": True}
        worker_encoded = canonical_json(worker_extra)
        connection.execute(
            "UPDATE workflow_events SET payload = ?, payload_digest = ? "
            "WHERE run_id = ? AND event_type = 'WorkerAbandoned'",
            (
                worker_encoded,
                hashlib.sha256(worker_encoded.encode()).hexdigest(),
                terminal["workflow_run_id"],
            ),
        )
        connection.commit()
        with pytest.raises(RuntimeError, match="reason_unbound"):
            management.validate_terminal_gate_abandon_fences(
                terminal,
                reason=reason,
            )
        connection.execute(
            "UPDATE workflow_events SET payload = ?, payload_digest = ?, "
            "causation_id = ? WHERE run_id = ? "
            "AND event_type = 'WorkerAbandoned'",
            (*worker_row, terminal["workflow_run_id"]),
        )

        wrong_cycle = str(worker_row[2]).replace("cycle-0:", "cycle-9:")
        connection.execute(
            "UPDATE workflow_events SET causation_id = ? WHERE run_id = ? "
            "AND event_type = 'WorkerAbandoned'",
            (wrong_cycle, terminal["workflow_run_id"]),
        )
        connection.commit()
        with pytest.raises(RuntimeError, match="reason_unbound"):
            management.validate_terminal_gate_abandon_fences(
                terminal,
                reason=reason,
            )
        connection.execute(
            "UPDATE workflow_events SET causation_id = ? WHERE run_id = ? "
            "AND event_type = 'WorkerAbandoned'",
            (worker_row[2], terminal["workflow_run_id"]),
        )

        strict_extra = {
            "reason": reason,
            "workflow_run_id": terminal["workflow_run_id"],
            "extra": True,
        }
        strict_encoded = canonical_json(strict_extra)
        connection.execute(
            "UPDATE workflow_events SET payload = ?, payload_digest = ?, "
            "causation_id = ? WHERE run_id = ? "
            "AND event_type = 'StrictAuthorityAbandoned'",
            (
                strict_encoded,
                hashlib.sha256(strict_encoded.encode()).hexdigest(),
                f"strict-authority-abandoned:{strict_run_id}:"
                f"{content_digest(strict_extra)}",
                strict_run_id,
            ),
        )
        connection.commit()
        with pytest.raises(RuntimeError, match="binding_invalid"):
            management.validate_terminal_gate_abandon_fences(
                terminal,
                reason=reason,
            )
        connection.execute(
            "UPDATE workflow_events SET payload = ?, payload_digest = ?, "
            "causation_id = ? WHERE run_id = ? "
            "AND event_type = 'StrictAuthorityAbandoned'",
            (*strict_row, strict_run_id),
        )
        connection.commit()

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE effects SET status = 'running' WHERE effect_id = ?",
            (call["effect_id"],),
        )
        connection.commit()
    with pytest.raises(
        RuntimeError,
        match="effects_still_live",
    ):
        management.validate_terminal_gate_abandon_fences(
            terminal,
            reason=reason,
        )
