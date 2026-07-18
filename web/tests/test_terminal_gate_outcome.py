from __future__ import annotations

from copy import deepcopy
import asyncio
from itertools import product

import pytest


_VALID_TERMINAL_SEMANTICS = frozenset({
    ("quality", "quality_gate_rejected", "quality_gate"),
    ("quality", "quality_receipt_invalid", "control_plane"),
    ("review", "review_rejected", "strategy_review"),
    ("review", "review_receipt_invalid", "control_plane"),
    ("review", "review_authority_invalid", "control_plane"),
    ("critic", "critic_receipt_invalid", "control_plane"),
    ("critic", "critic_authority_invalid", "control_plane"),
})
_TERMINAL_GATES = ("quality", "review", "critic")
_TERMINAL_REASONS = (
    "quality_gate_rejected",
    "quality_receipt_invalid",
    "review_rejected",
    "review_receipt_invalid",
    "review_authority_invalid",
    "critic_receipt_invalid",
    "critic_authority_invalid",
)
_TERMINAL_FAILURE_CLASSES = (
    "quality_gate",
    "strategy_review",
    "control_plane",
)
_INVALID_TERMINAL_SEMANTICS = tuple(sorted(
    set(product(
        _TERMINAL_GATES,
        _TERMINAL_REASONS,
        _TERMINAL_FAILURE_CLASSES,
    )) - _VALID_TERMINAL_SEMANTICS
))
_INPUT_STAGE_BY_GATE = {
    "quality": "workers_done",
    "review": "quality_passed",
    "critic": "reviewed",
}
_TERMINAL_STAGE_BY_GATE = {
    "quality": "quality_rejected",
    "review": "review_rejected",
    "critic": "critic_rejected",
}
_BASE_SEMANTICS_BY_GATE = {
    "quality": ("quality_gate_rejected", "quality_gate"),
    "review": ("review_rejected", "strategy_review"),
    "critic": ("critic_receipt_invalid", "control_plane"),
}


def _checkpoint(candidate, *, stage="workers_done", revision=7):
    return {
        "checkpoint_schema_version": 4,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": {"policy": "strict", "digest": "epoch"},
        "workflow_run_id": "generation:143:terminal-outcome-test",
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


def _checkpoint_for_gate(candidate, gate_name):
    checkpoint = _checkpoint(
        candidate,
        stage=_INPUT_STAGE_BY_GATE[gate_name],
    )
    if gate_name in {"review", "critic"}:
        checkpoint["gate_results"]["quality"] = {
            "passed": True,
            "gate": "quality",
        }
    if gate_name == "critic":
        checkpoint["gate_results"]["review"] = {
            "passed": True,
            "approved": True,
            "gate": "review",
        }
    return checkpoint


def _project_terminal_outcome(checkpoint, gate_name, gate_payload, outcome):
    return {
        **deepcopy(checkpoint),
        "stage": _TERMINAL_STAGE_BY_GATE[gate_name],
        "checkpoint_revision": checkpoint["checkpoint_revision"] + 1,
        "gate_results": {
            **deepcopy(checkpoint["gate_results"]),
            gate_name: deepcopy(gate_payload),
        },
        "terminal_gate_outcome": deepcopy(outcome),
    }


@pytest.mark.parametrize(
    ("gate_name", "reason_code", "failure_class"),
    sorted(_VALID_TERMINAL_SEMANTICS),
)
def test_every_canonical_gate_semantics_tuple_builds_and_validates(
    tmp_path,
    gate_name,
    reason_code,
    failure_class,
):
    from gate_outcome import (
        build_terminal_gate_outcome,
        validate_terminal_gate_outcome,
    )

    candidate = tmp_path / f"national_v143_{gate_name}_{reason_code}"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
    checkpoint = _checkpoint_for_gate(candidate, gate_name)
    gate_payload = {"passed": False, "gate": gate_name}
    outcome = build_terminal_gate_outcome(
        checkpoint,
        gate_name=gate_name,
        gate_payload=gate_payload,
        candidate_dir=candidate,
        reason_code=reason_code,
        failure_class=failure_class,
    )
    projected = _project_terminal_outcome(
        checkpoint,
        gate_name,
        gate_payload,
        outcome,
    )

    assert validate_terminal_gate_outcome(
        projected,
        candidate_dir=candidate,
    ) == []


@pytest.mark.parametrize(
    ("gate_name", "reason_code", "failure_class"),
    _INVALID_TERMINAL_SEMANTICS,
)
def test_builder_rejects_every_noncanonical_gate_semantics_cross_product(
    tmp_path,
    gate_name,
    reason_code,
    failure_class,
):
    from gate_outcome import (
        TerminalGateOutcomeError,
        build_terminal_gate_outcome,
    )

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
    with pytest.raises(
        TerminalGateOutcomeError,
        match="terminal_outcome_gate_semantics_invalid",
    ):
        build_terminal_gate_outcome(
            _checkpoint_for_gate(candidate, gate_name),
            gate_name=gate_name,
            gate_payload={"passed": False, "gate": gate_name},
            candidate_dir=candidate,
            reason_code=reason_code,
            failure_class=failure_class,
        )


@pytest.mark.parametrize(
    ("gate_name", "reason_code", "failure_class"),
    _INVALID_TERMINAL_SEMANTICS,
)
def test_validator_rejects_every_resigned_noncanonical_semantics_cross_product(
    tmp_path,
    gate_name,
    reason_code,
    failure_class,
):
    from gate_outcome import (
        build_terminal_gate_outcome,
        validate_terminal_gate_outcome,
    )
    from workflow_kernel import content_digest

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
    checkpoint = _checkpoint_for_gate(candidate, gate_name)
    gate_payload = {"passed": False, "gate": gate_name}
    base_reason, base_failure = _BASE_SEMANTICS_BY_GATE[gate_name]
    outcome = build_terminal_gate_outcome(
        checkpoint,
        gate_name=gate_name,
        gate_payload=gate_payload,
        candidate_dir=candidate,
        reason_code=base_reason,
        failure_class=base_failure,
    )
    outcome["reason_code"] = reason_code
    outcome["failure_class"] = failure_class
    outcome["receipt_digest"] = content_digest({
        key: value
        for key, value in outcome.items()
        if key != "receipt_digest"
    })
    projected = _project_terminal_outcome(
        checkpoint,
        gate_name,
        gate_payload,
        outcome,
    )

    errors = validate_terminal_gate_outcome(
        projected,
        candidate_dir=candidate,
    )

    assert "terminal_outcome_gate_semantics_invalid" in errors


@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_validator_rejects_resigned_non_exact_outcome_schema(
    tmp_path,
    mutation,
):
    from gate_outcome import (
        build_terminal_gate_outcome,
        validate_terminal_gate_outcome,
    )
    from workflow_kernel import content_digest

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
    checkpoint = _checkpoint_for_gate(candidate, "quality")
    gate_payload = {"passed": False, "gate": "quality"}
    outcome = build_terminal_gate_outcome(
        checkpoint,
        gate_name="quality",
        gate_payload=gate_payload,
        candidate_dir=candidate,
        reason_code="quality_gate_rejected",
        failure_class="quality_gate",
    )
    if mutation == "extra":
        outcome["provider_reason"] = "untrusted extension"
    else:
        del outcome["role_result_digest"]
    outcome["receipt_digest"] = content_digest({
        key: value
        for key, value in outcome.items()
        if key != "receipt_digest"
    })
    projected = _project_terminal_outcome(
        checkpoint,
        "quality",
        gate_payload,
        outcome,
    )

    errors = validate_terminal_gate_outcome(
        projected,
        candidate_dir=candidate,
    )

    assert "terminal_outcome_schema_keys_invalid" in errors
    assert "terminal_outcome_receipt_digest_invalid" not in errors


def test_quality_terminal_outcome_binds_candidate_parent_epoch_and_gate(tmp_path):
    from gate_outcome import (
        build_terminal_gate_outcome,
        validate_terminal_gate_outcome,
    )

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
    checkpoint = _checkpoint(candidate)
    gate = {
        "version": 143,
        "source_v": 142,
        "passed": False,
        "all_passed": False,
        "failures": ["selected contract missing"],
    }
    outcome = build_terminal_gate_outcome(
        checkpoint,
        gate_name="quality",
        gate_payload=gate,
        candidate_dir=candidate,
        reason_code="quality_gate_rejected",
        failure_class="quality_gate",
    )
    projected = {
        **deepcopy(checkpoint),
        "stage": "quality_rejected",
        "checkpoint_revision": 8,
        "gate_results": {"quality": gate},
        "terminal_gate_outcome": outcome,
    }

    assert validate_terminal_gate_outcome(
        projected,
        candidate_dir=candidate,
    ) == []
    changed_parent = {**projected, "parent2_v": 141}
    assert "terminal_outcome_parent2_v_mismatch" in validate_terminal_gate_outcome(
        changed_parent,
        candidate_dir=candidate,
    )
    (candidate / "policy.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert "terminal_outcome_candidate_artifact_hash_mismatch" in (
        validate_terminal_gate_outcome(projected, candidate_dir=candidate)
    )


@pytest.mark.parametrize(
    ("gate_payload", "reason_code", "error"),
    [
        ({}, "quality_gate_rejected", "terminal_outcome_gate_payload_invalid"),
        (
            {"passed": False},
            "free_form_reason_from_provider",
            "terminal_outcome_reason_code_invalid",
        ),
    ],
)
def test_arbitrary_reason_or_empty_gate_cannot_mint_terminal_receipt(
    tmp_path,
    gate_payload,
    reason_code,
    error,
):
    from gate_outcome import (
        TerminalGateOutcomeError,
        build_terminal_gate_outcome,
    )

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
    with pytest.raises(TerminalGateOutcomeError, match=error):
        build_terminal_gate_outcome(
            _checkpoint(candidate),
            gate_name="quality",
            gate_payload=gate_payload,
            candidate_dir=candidate,
            reason_code=reason_code,
            failure_class="quality_gate",
        )


def test_terminal_stage_transition_is_one_way():
    from pipeline_state import validate_stage_transition

    assert validate_stage_transition(
        "quality_passed", "review_rejected"
    ) == (True, "terminal_gate_outcome_projection")
    allowed, reason = validate_stage_transition(
        "review_rejected", "reviewed"
    )
    assert allowed is False
    assert "requires_canonical_abandon" in reason


def test_free_form_abandon_reason_cannot_fabricate_terminal_request(
    tmp_path,
    monkeypatch,
):
    import evolution_infra
    import system_strict_bootstrap as bootstrap

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
    checkpoint = _checkpoint(candidate, stage="quality_passed")
    monkeypatch.setattr(
        bootstrap,
        "is_declared_native_bootstrap",
        lambda _checkpoint: True,
    )
    monkeypatch.setattr(
        evolution_infra,
        "read_pipeline_checkpoint",
        lambda: checkpoint,
    )
    writes = []
    monkeypatch.setattr(
        evolution_infra,
        "write_pipeline_checkpoint",
        lambda *_args, **_kwargs: writes.append(True) or True,
    )

    result = asyncio.run(bootstrap.abandon_rejected_blueprint(
        checkpoint,
        reason="provider_invented_cleanup_reason",
        result={
            "failure_class": "strategy_review",
            "approved": False,
        },
    ))

    assert result["abandoned"] is False
    assert result["abandon_result"]["reason"] == (
        "system_bootstrap_abandon_exception"
    )
    assert "terminal_gate_request_missing_or_invalid" in result["abandon_error"]
    assert writes == []


def test_terminal_checkpoint_rejects_noncanonical_abandon_reason(
    tmp_path,
    monkeypatch,
):
    import evolution_core
    import tool_bot_management as management

    state_file = tmp_path / "pipeline_state.json"
    state_file.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
    checkpoint = {
        "workflow_run_id": "generation:143:terminal-reason-test",
        "next_v": 143,
        "source_v": 142,
        "checkpoint_revision": 8,
        "stage": "review_rejected",
        "terminal_gate_outcome": {"receipt_digest": "a" * 64},
    }
    monkeypatch.setattr(
        management,
        "_load_live_abandon_claim",
        lambda: None,
    )
    monkeypatch.setattr(
        management,
        "read_pipeline_checkpoint",
        lambda: checkpoint,
    )

    result = asyncio.run(management._do_abandon_generation(
        reason="free_form_forced_reason",
        expected_workflow_run_id=checkpoint["workflow_run_id"],
        expected_next_v=143,
        expected_source_v=142,
        expected_checkpoint_revision=8,
        expected_checkpoint_stage="review_rejected",
        expected_terminal_gate_outcome_digest="a" * 64,
    ))

    assert result["abandoned"] is False
    assert result["reason"] == "terminal_gate_abandon_authority_mismatch"


def test_invalid_terminal_receipt_projects_operator_reconcile(
    monkeypatch,
):
    import checkpoint_schema
    import gate_outcome
    import pipeline_infrastructure
    from pipeline_state import route_policy

    checkpoint = {
        "workflow_run_id": "generation:143:invalid-terminal-route",
        "next_v": 143,
        "source_v": 142,
        "stage": "review_rejected",
        "checkpoint_revision": 8,
        "gate_results": {},
        "terminal_gate_outcome": {"receipt_digest": "a" * 64},
    }
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
    monkeypatch.setattr(
        gate_outcome,
        "validate_terminal_gate_outcome",
        lambda *_args, **_kwargs: ["terminal_outcome_receipt_digest_invalid"],
    )

    route = route_policy(checkpoint)

    assert route["intent"] == "operator_reconcile_checkpoint"
    assert route["blocked"] is True
    assert route["recoverable"] is False
    assert route["next_tool"] is None
    assert route["allowed_tools"] == []


def test_operator_reconcile_binds_legacy_migration_without_provider_dispatch(
    tmp_path,
    monkeypatch,
):
    import evolution_core
    import strict_authority_workflow as authority
    import system_strict_bootstrap as bootstrap
    import terminal_gate_reconcile as reconcile

    checkpoint = {
        "workflow_run_id": "generation:143:legacy-reconcile",
        "next_v": 143,
        "source_v": 142,
        "stage": "quality_passed",
        "checkpoint_revision": 8,
    }
    role_result = {
        "approved": False,
        "quality_score": 3,
        "feedback": "legacy terminal rejection",
        "change_summary": "reviewed",
        "risk_areas": [],
    }
    migration = {
        "schema_version": 1,
        "kind": authority.LEGACY_REVIEW_TERMINAL_MIGRATION_KIND,
        "disposition": "terminal_rejection_only",
        "migration_digest": "a" * 64,
    }
    call = {
        "effect_id": "strict-llm-" + "b" * 64,
        "invocation_id": "c" * 32,
        "context_binding": {"candidate_artifact_hash": "d" * 64},
        "terminal_semantic_migration": migration,
    }
    monkeypatch.setattr(
        reconcile,
        "inspect_completed_review_rejection",
        lambda: {
            "checkpoint": checkpoint,
            "call": call,
            "role_result": role_result,
            "effect_id": call["effect_id"],
            "invocation_id": call["invocation_id"],
            "terminal_semantic_migration": migration,
        },
    )
    monkeypatch.setattr(
        evolution_core,
        "get_logs_dir",
        lambda _version: tmp_path,
    )
    monkeypatch.setattr(
        authority,
        "strict_invocation_log_path",
        lambda *_args, **_kwargs: tmp_path / "reviewer_io.txt",
    )
    accepted = []
    monkeypatch.setattr(
        authority,
        "accept_role_result",
        lambda *_args, **_kwargs: accepted.append(True) or {
            "receipt_digest": "e" * 64,
        },
    )
    monkeypatch.setattr(
        authority,
        "record_bound_invocation_evidence",
        lambda *_args, **_kwargs: {"evidence_digest": "f" * 64},
    )
    dispatches = []
    monkeypatch.setattr(
        authority,
        "dispatch_call",
        lambda *_args, **_kwargs: dispatches.append(True),
    )
    captured = {}

    async def abandon(_checkpoint, *, reason, result):
        captured.update({
            "checkpoint": _checkpoint,
            "reason": reason,
            "result": result,
        })
        return {"abandoned": True, "checkpoint_stage": "abandoned"}

    monkeypatch.setattr(bootstrap, "abandon_rejected_blueprint", abandon)

    result = asyncio.run(reconcile.reconcile_completed_review_rejection())

    assert accepted == [True]
    assert dispatches == []
    assert result["provider_dispatch_required"] is False
    assert result["abandoned"] is True
    terminal_gate = captured["result"]["terminal_gate_payload"]
    assert terminal_gate["terminal_semantic_migration"] == migration
    assert captured["result"]["provider_dispatch_required"] is False
