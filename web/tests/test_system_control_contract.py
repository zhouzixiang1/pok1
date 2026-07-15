from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest


SYSTEM_BOOTSTRAP_FILES = {
    "scripts/reset_national_tcp_policy_epoch.py",
    "web/core/system_strict_bootstrap.py",
    "web/core/strict_authority_workflow.py",
    "web/core/bootstrap_assets/strict_v1/manifest.json",
    "web/core/bootstrap_assets/strict_v1/policy.py",
    "web/core/bootstrap_assets/strict_v1/prepared_policy.py",
}
FIRST_STRICT_CONTROL_FILES = {
    "web/core/first_strict_execution_journal.py",
    "web/core/first_strict_control.py",
    "web/core/bootstrap_assets/first_strict_control_v1/manifest.json",
    "web/core/bootstrap_assets/first_strict_control_v1/policy.py",
}
FORMAL_BOOTSTRAP_FILES = {
    "web/core/official_bootstrap.py",
    "web/core/official_bootstrap_control.json",
}
LLM_CONTROL_FILES = {
    "web/core/llm_availability.py",
    "web/core/llm_availability_store.py",
}
ORCHESTRATOR_ROOT_GUARD_FILES = {
    "web/core/orchestrator_context.py",
    "web/core/epoch_authority.py",
}
MASTER_EVIDENCE_PRODUCER_FILES = {
    "web/core/master_context_contract.py",
    "web/core/replay_spotlight.py",
}
SHARED_TOOL_FACADE_FILES = {
    "web/core/evolution_core.py",
    "web/core/tool_pipeline.py",
    "web/core/tools.py",
}
OPERATIONAL_CONTINUITY_FILES = {
    "web/core/daemon_management.py",
    "web/core/stability_observation.py",
}
WORKER_PROMPT_INPUTS = {
    "web/core/prompts/debug_worker_prompt.md",
    "web/core/prompts/worker_cot_check.md",
    "web/core/prompts/worker_profile_national_native.md",
    "web/core/prompts/worker_prompt.md",
}
OFFICIAL_PROMPT_INPUTS = {
    "web/core/prompts/official_platform_analysis.md",
}


def test_system_control_plane_is_exact_and_restart_critical_at_every_stage():
    import evaluation_contract
    import evolution_scope

    expected = (
        SYSTEM_BOOTSTRAP_FILES
        | FIRST_STRICT_CONTROL_FILES
        | LLM_CONTROL_FILES
        | ORCHESTRATOR_ROOT_GUARD_FILES
        | FORMAL_BOOTSTRAP_FILES
    )
    assert evaluation_contract.CONTRACT_VERSION == 31
    assert SYSTEM_BOOTSTRAP_FILES == set(
        evolution_scope.CRITICAL_SYSTEM_BOOTSTRAP_EXACT
    )
    assert FIRST_STRICT_CONTROL_FILES == set(
        evolution_scope.CRITICAL_FIRST_STRICT_CONTROL_EXACT
    )
    assert LLM_CONTROL_FILES == set(evolution_scope.CRITICAL_LLM_CONTROL_EXACT)
    assert ORCHESTRATOR_ROOT_GUARD_FILES <= (
        evolution_scope.CRITICAL_GENERATION_EXACT
    )
    assert expected <= evaluation_contract.ALWAYS_CRITICAL_EXACT
    assert SYSTEM_BOOTSTRAP_FILES | FIRST_STRICT_CONTROL_FILES <= (
        evolution_scope.CRITICAL_EVALUATION_GATE_EXACT
    )
    assert FORMAL_BOOTSTRAP_FILES <= (
        evolution_scope.CRITICAL_EVALUATION_GATE_EXACT
    )
    assert expected <= evolution_scope.CRITICAL_GENERATION_EXACT

    for stage in (
        "prepared",
        "direction_audited",
        "master_planned",
        "workers_done",
        "quality_passed",
        "reviewed",
        "critic_checked",
        "verified",
        "official_bootstrap_required",
    ):
        assert expected <= evaluation_contract.critical_exact_for_stage(
            stage,
            national_execution_mode="native_tcp",
        )

    assert evolution_scope.classify_path(
        "web/core/results/llm_availability_pause.json",
        candidate_v=300,
    ) == "runtime"


def test_blueprint_manifest_is_bound_to_runtime_policy_and_complete_oracle_set():
    import runtime_architecture_policy
    import system_strict_bootstrap

    manifest = system_strict_bootstrap.load_blueprint_manifest()
    assert manifest["official_policy_id"] == (
        runtime_architecture_policy.OFFICIAL_FULL_POLICY_ID
    )
    assert manifest["official_oracles"] == (
        runtime_architecture_policy.OFFICIAL_ORACLE_DOC_DIGESTS
    )
    assert system_strict_bootstrap.validate_blueprint_package(
        manifest,
        verify_source=False,
    ) == []

    incomplete = deepcopy(manifest)
    incomplete["official_oracles"].pop(
        "docs/official-terminal-settlement-oracle-2026-07-11.md"
    )
    errors = system_strict_bootstrap.validate_blueprint_package(
        incomplete,
        verify_source=False,
    )
    assert "system_bootstrap_official_oracle_set_mismatch" in errors


def test_first_strict_review_records_final_provider_prompt_authority(
    tmp_path,
    monkeypatch,
):
    import evolution_infra
    import strict_authority_workflow
    import system_strict_bootstrap
    import tool_gates

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    results_dir = tmp_path / "results"
    logs_dir = results_dir / "v143" / "logs"
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
    checkpoint = {
        "next_v": 143,
        "source_v": 142,
        "stage": "quality_passed",
        "checkpoint_revision": 9,
        "workflow_run_id": "generation:143:review-prompt-authority",
        "master_plan": {"tasks": []},
        "gate_results": {
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
            }
        },
        "audit_context": {},
    }
    strict_call = {
        "invocation_id": "1" * 32,
        "effect_id": "strict-llm-" + "1" * 64,
        "prompt_digest": "",
        "generation_binding": {"next_v": 143},
    }
    final_provider_prompt_digest = "a" * 64
    captured = {}

    class UI:
        def log_history(self, *_args, **_kwargs):
            return None

        def get_output(self):
            return ""

    async def no_exhausted(*_args, **_kwargs):
        return None

    async def approved_query(
        _rendered,
        _context,
        _ui,
        _role,
        _log_file,
        *,
        strict_authority,
        **_kwargs,
    ):
        assert strict_authority is strict_call
        captured["provider_log_file"] = _log_file
        strict_authority["prompt_digest"] = final_provider_prompt_digest
        return (
            json.dumps({
                "approved": True,
                "quality_score": 9,
                "feedback": "approved",
                "change_summary": "strict review complete",
                "risk_areas": [],
            }),
            0.25,
            {"input_tokens": 10, "output_tokens": 5},
        )

    def record_evidence(call, *, log_file):
        captured["evidence_call"] = call
        captured["evidence_log_file"] = log_file
        return {
            "kind": "test-llm-execution-evidence",
            "prompt_digest": call["prompt_digest"],
        }

    def record_gate(_v, _source_v, gate_name, gate, **kwargs):
        captured["gate_name"] = gate_name
        captured["gate"] = gate
        captured["stage"] = kwargs.get("stage")
        return True

    monkeypatch.setattr(tool_gates, "_set_pipeline_status", lambda *_args: None)
    monkeypatch.setattr(tool_gates, "_matching_checkpoint", lambda *_args: checkpoint)
    monkeypatch.setattr(
        tool_gates,
        "_owned_infrastructure_failure",
        lambda *_args: (None, None),
    )
    monkeypatch.setattr(
        tool_gates,
        "_execute_exhausted_infrastructure_failure",
        no_exhausted,
    )
    monkeypatch.setattr(tool_gates, "_idempotency_check", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool_gates, "_quality_gate_ok", lambda _checkpoint: True)
    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(
        tool_gates,
        "get_logs_dir",
        lambda _version: logs_dir,
    )
    monkeypatch.setattr(tool_gates, "_get_ui", lambda: UI())
    monkeypatch.setattr(
        tool_gates,
        "_llm_gate_infrastructure_identity",
        lambda **_kwargs: ("review-attempt", {}),
    )
    monkeypatch.setattr(tool_gates, "run_claude_query", approved_query)
    monkeypatch.setattr(tool_gates, "_record_gate", record_gate)
    monkeypatch.setattr(tool_gates, "log_system_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        system_strict_bootstrap,
        "is_declared_native_bootstrap",
        lambda _checkpoint: True,
    )
    monkeypatch.setattr(
        system_strict_bootstrap,
        "build_system_gate_receipt",
        lambda *_args, **_kwargs: {"kind": "test-system-review-receipt"},
    )
    monkeypatch.setattr(
        strict_authority_workflow,
        "gate_call_context",
        lambda *_args, **_kwargs: {"candidate": "bound"},
    )
    monkeypatch.setattr(
        strict_authority_workflow,
        "new_call",
        lambda *_args, **_kwargs: strict_call,
    )
    monkeypatch.setattr(
        strict_authority_workflow,
        "render_gate_provider_prompt",
        lambda _call: "sealed strict reviewer prompt",
    )
    monkeypatch.setattr(
        strict_authority_workflow,
        "accept_role_result",
        lambda *_args, **_kwargs: {"kind": "test-authority-receipt"},
    )
    monkeypatch.setattr(
        strict_authority_workflow,
        "record_bound_invocation_evidence",
        record_evidence,
    )

    result = asyncio.run(
        tool_gates.run_review.handler({
            "version": 143,
            "source_v": 142,
            "plan": [],
        })
    )
    payload = json.loads(result["content"][0]["text"])

    assert payload["approved"] is True
    assert payload["checkpoint_recorded"] is True
    assert captured["gate"]["llm_execution_evidence"]["prompt_digest"] == (
        final_provider_prompt_digest
    )
    assert captured["evidence_call"] is strict_call
    assert captured["provider_log_file"] == captured["evidence_log_file"]
    assert captured["evidence_log_file"] == (
        logs_dir
        / "strict_invocations"
        / strict_call["invocation_id"]
        / "reviewer_io.txt"
    )
    assert captured["gate_name"] == "review"
    assert captured["stage"] == "reviewed"
    assert captured["gate"]["system_verifier_receipt"] == {
        "kind": "test-system-review-receipt"
    }


@pytest.mark.parametrize("mismatched_log", [False, True])
def test_strict_critic_execution_and_evidence_share_invocation_log(
    tmp_path,
    monkeypatch,
    mismatched_log,
):
    import evolution_infra
    import strict_authority_workflow
    import system_strict_bootstrap
    import tool_gates

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    results_dir = tmp_path / "results"
    logs_dir = results_dir / "v143" / "logs"
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
    checkpoint = {
        "next_v": 143,
        "source_v": 142,
        "stage": "reviewed",
        "checkpoint_revision": 12,
        "workflow_run_id": "generation:143:critic-log-authority",
        "master_plan": {"tasks": []},
        "reviewer_feedback": "approved",
        "gate_results": {
            "quality": {"all_passed": True, "critical_scenarios_passed": True},
            "review": {"approved": True, "passed": True},
        },
        "audit_context": {},
    }
    strict_call = {
        "invocation_id": "3" * 32,
        "effect_id": "strict-llm-" + "3" * 64,
        "generation_binding": {"next_v": 143},
    }
    expected_log = (
        logs_dir
        / "strict_invocations"
        / strict_call["invocation_id"]
        / "critic_io.txt"
    )
    captured = {}

    class UI:
        def get_output(self):
            return ""

    async def no_exhausted(*_args, **_kwargs):
        return None

    async def critic_result(*_args, **_kwargs):
        return {
            "score": 8,
            "approved": True,
            "strategic_assessment": "bounded mechanism is reachable",
            "evidence": {"h2h_weaknesses": [], "diff_refs": ["policy.py"]},
            "feedback": "",
            "local_optima_warning": False,
            "local_optima_reason": None,
            "_llm_execution_material": {
                "log_file": str(
                    logs_dir / "critic_io.txt"
                    if mismatched_log
                    else expected_log
                ),
            },
        }

    def record_evidence(call, *, log_file):
        captured["evidence_call"] = call
        captured["evidence_log_file"] = log_file
        return {"kind": "test-critic-execution-evidence"}

    def record_gate(_v, _source_v, gate_name, gate, **kwargs):
        captured["gate_name"] = gate_name
        captured["gate"] = gate
        captured["stage"] = kwargs.get("stage")
        return True

    async def abandon(_checkpoint, *, reason, result):
        captured["abandon_reason"] = reason
        captured["abandon_result"] = result
        return {**result, "abandoned": True}

    monkeypatch.setattr(tool_gates, "_set_pipeline_status", lambda *_args: None)
    monkeypatch.setattr(tool_gates, "_matching_checkpoint", lambda *_args: checkpoint)
    monkeypatch.setattr(
        tool_gates,
        "_owned_infrastructure_failure",
        lambda *_args: (None, None),
    )
    monkeypatch.setattr(
        tool_gates,
        "_execute_exhausted_infrastructure_failure",
        no_exhausted,
    )
    monkeypatch.setattr(tool_gates, "_idempotency_check", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool_gates, "_quality_gate_ok", lambda _checkpoint: True)
    monkeypatch.setattr(tool_gates, "_review_gate_ok", lambda _checkpoint: True)
    monkeypatch.setattr(tool_gates, "_critic_result_to_preserve", lambda _checkpoint: None)
    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_gates, "get_logs_dir", lambda _version: logs_dir)
    monkeypatch.setattr(tool_gates, "_get_ui", lambda: UI())
    monkeypatch.setattr(
        tool_gates,
        "_llm_gate_infrastructure_identity",
        lambda **_kwargs: ("critic-attempt", {}),
    )
    monkeypatch.setattr(tool_gates, "_run_critic", critic_result)
    monkeypatch.setattr(tool_gates, "_record_gate", record_gate)
    monkeypatch.setattr(tool_gates, "log_system_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        system_strict_bootstrap,
        "is_declared_native_bootstrap",
        lambda _checkpoint: True,
    )
    monkeypatch.setattr(
        system_strict_bootstrap,
        "build_system_gate_receipt",
        lambda *_args, **_kwargs: {"kind": "test-system-critic-receipt"},
    )
    monkeypatch.setattr(
        system_strict_bootstrap,
        "abandon_rejected_blueprint",
        abandon,
    )
    monkeypatch.setattr(
        strict_authority_workflow,
        "gate_call_context",
        lambda *_args, **_kwargs: {"candidate": "bound"},
    )
    monkeypatch.setattr(
        strict_authority_workflow,
        "new_call",
        lambda *_args, **_kwargs: strict_call,
    )
    monkeypatch.setattr(
        strict_authority_workflow,
        "accept_role_result",
        lambda *_args, **_kwargs: {"kind": "test-authority-receipt"},
    )
    monkeypatch.setattr(
        strict_authority_workflow,
        "record_bound_invocation_evidence",
        record_evidence,
    )

    result = asyncio.run(tool_gates.run_critic.handler({
        "version": 143,
        "source_v": 142,
        "plan": [],
        "reviewer_feedback": "approved",
        "force_advance": False,
    }))
    payload = json.loads(result["content"][0]["text"])

    if mismatched_log:
        assert payload["abandoned"] is True
        assert captured["abandon_reason"] == (
            "system_strict_bootstrap_critic_receipt_invalid"
        )
        assert "strict_authority_critic_execution_log_mismatch" in (
            captured["abandon_result"]["validation_errors"]
        )
        assert "evidence_call" not in captured
    else:
        assert payload["approved"] is True
        assert captured["evidence_call"] is strict_call
        assert captured["evidence_log_file"] == expected_log
        assert captured["gate_name"] == "critic"
        assert captured["stage"] == "critic_checked"
        assert captured["gate"]["system_verifier_receipt"] == {
            "kind": "test-system-critic-receipt"
        }


@pytest.mark.parametrize(
    ("gate_name", "stage", "error_code"),
    [
        ("review", "quality_passed", "SYSTEM_STRICT_BOOTSTRAP_REVIEW_AUTHORITY_INVALID"),
        ("critic", "reviewed", "SYSTEM_STRICT_BOOTSTRAP_CRITIC_AUTHORITY_INVALID"),
    ],
)
def test_strict_gate_predispatch_authority_drift_canonically_abandons(
    tmp_path,
    monkeypatch,
    gate_name,
    stage,
    error_code,
):
    import strict_authority_workflow as authority
    import system_strict_bootstrap
    import tool_gates

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    checkpoint = {
        "next_v": 143,
        "source_v": 142,
        "stage": stage,
        "checkpoint_revision": 9,
        "workflow_run_id": f"generation:143:{gate_name}-authority-drift",
        "master_plan": {"tasks": []},
        "gate_results": {
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
            },
            "review": {"approved": True, "passed": True},
        },
        "audit_context": {},
    }
    captured = {}

    async def no_exhaustion(*_args, **_kwargs):
        return None

    async def abandon(_checkpoint, *, reason, result):
        captured.update({"checkpoint": _checkpoint, "reason": reason, **result})
        return {**result, "action": "abandon_generation", "abandoned": True}

    def drift(*_args, **_kwargs):
        raise authority.StrictAuthorityError(f"{gate_name} context drift")

    monkeypatch.setattr(tool_gates, "_set_pipeline_status", lambda *_args: None)
    monkeypatch.setattr(tool_gates, "_matching_checkpoint", lambda *_args: checkpoint)
    monkeypatch.setattr(
        tool_gates,
        "_owned_infrastructure_failure",
        lambda *_args: (None, None),
    )
    monkeypatch.setattr(
        tool_gates,
        "_execute_exhausted_infrastructure_failure",
        no_exhaustion,
    )
    monkeypatch.setattr(tool_gates, "_idempotency_check", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_gates, "_quality_gate_ok", lambda _ckpt: True)
    monkeypatch.setattr(tool_gates, "_review_gate_ok", lambda _ckpt: True)
    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(
        tool_gates,
        "_llm_gate_infrastructure_identity",
        lambda **_kwargs: ("attempt", {}),
    )
    monkeypatch.setattr(
        system_strict_bootstrap,
        "is_declared_native_bootstrap",
        lambda _checkpoint: True,
    )
    monkeypatch.setattr(
        system_strict_bootstrap,
        "abandon_rejected_blueprint",
        abandon,
    )
    monkeypatch.setattr(authority, "gate_call_context", drift)

    tool = tool_gates.run_review if gate_name == "review" else tool_gates.run_critic
    raw_handler = tool.handler.__wrapped__
    result = asyncio.run(raw_handler({"version": 143, "source_v": 142}))
    payload = json.loads(result["content"][0]["text"])

    assert payload["error"] == error_code
    assert payload["failure_class"] == "control_plane"
    assert payload["abandoned"] is True
    assert captured["reason"] == (
        f"system_strict_bootstrap_{gate_name}_authority_invalid"
    )
    assert captured["validation_errors"] == [f"{gate_name} context drift"]


def test_strict_review_log_allocation_failure_canonically_abandons(
    tmp_path,
    monkeypatch,
):
    import evolution_infra
    import strict_authority_workflow as authority
    import system_strict_bootstrap
    import tool_gates

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    results_dir = tmp_path / "results"
    logs_dir = results_dir / "v143" / "logs"
    logs_dir.mkdir(parents=True)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
    (logs_dir / "strict_invocations").write_text("collision\n")
    checkpoint = {
        "next_v": 143,
        "source_v": 142,
        "stage": "quality_passed",
        "checkpoint_revision": 9,
        "workflow_run_id": "generation:143:review-log-collision",
        "master_plan": {"tasks": []},
        "gate_results": {
            "quality": {"all_passed": True, "critical_scenarios_passed": True},
        },
        "audit_context": {},
    }
    strict_call = {
        "invocation_id": "4" * 32,
        "generation_binding": {"next_v": 143},
    }
    captured = {}

    async def no_exhaustion(*_args, **_kwargs):
        return None

    async def abandon(_checkpoint, *, reason, result):
        captured.update({"reason": reason, **result})
        return {**result, "action": "abandon_generation", "abandoned": True}

    async def forbidden_provider(*_args, **_kwargs):
        raise AssertionError("provider must not run after log allocation failure")

    monkeypatch.setattr(tool_gates, "_set_pipeline_status", lambda *_args: None)
    monkeypatch.setattr(tool_gates, "_matching_checkpoint", lambda *_args: checkpoint)
    monkeypatch.setattr(
        tool_gates,
        "_owned_infrastructure_failure",
        lambda *_args: (None, None),
    )
    monkeypatch.setattr(
        tool_gates,
        "_execute_exhausted_infrastructure_failure",
        no_exhaustion,
    )
    monkeypatch.setattr(tool_gates, "_idempotency_check", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_gates, "_quality_gate_ok", lambda _checkpoint: True)
    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_gates, "get_logs_dir", lambda _version: logs_dir)
    monkeypatch.setattr(
        tool_gates,
        "_llm_gate_infrastructure_identity",
        lambda **_kwargs: ("attempt", {}),
    )
    monkeypatch.setattr(tool_gates, "run_claude_query", forbidden_provider)
    monkeypatch.setattr(
        system_strict_bootstrap,
        "is_declared_native_bootstrap",
        lambda _checkpoint: True,
    )
    monkeypatch.setattr(
        system_strict_bootstrap,
        "abandon_rejected_blueprint",
        abandon,
    )
    monkeypatch.setattr(
        authority,
        "gate_call_context",
        lambda *_args, **_kwargs: {"candidate": "bound"},
    )
    monkeypatch.setattr(
        authority,
        "new_call",
        lambda *_args, **_kwargs: strict_call,
    )
    monkeypatch.setattr(
        authority,
        "render_gate_provider_prompt",
        lambda _call: "sealed reviewer prompt",
    )

    result = asyncio.run(tool_gates.run_review.handler({
        "version": 143,
        "source_v": 142,
        "plan": [],
    }))
    payload = json.loads(result["content"][0]["text"])

    assert payload["error"] == "SYSTEM_STRICT_BOOTSTRAP_REVIEW_AUTHORITY_INVALID"
    assert payload["abandoned"] is True
    assert captured["reason"] == "system_strict_bootstrap_review_authority_invalid"
    assert captured["validation_errors"] == [
        "strict_authority_invocation_log_filesystem_invalid:FileExistsError"
    ]


def test_strict_reviewer_context_normalizes_and_seals_real_renderer(
    tmp_path,
):
    import strict_authority_workflow as authority
    from workflow_kernel import content_digest

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("POLICY = 1\n", encoding="utf-8")
    checkpoint = {
        "source_v": 142,
        "next_v": 143,
        "master_plan": {"tasks": [{"worker_id": "W1"}]},
        "gate_results": {"quality": {"all_passed": True}},
        "audit_context": {
            "worker_cot_focus_areas": [
                "  sizing   boundary ",
                "range update",
                "range update",
                "sizing boundary",
            ],
            "system_strict_bootstrap": {
                "receipt_digest": "a" * 64,
                "plan_digest": "b" * 64,
            },
        },
    }

    context = authority.gate_call_context(
        checkpoint,
        gate_name="review",
        candidate_dir=candidate,
    )
    semantics = context["renderer_semantics"]
    assert semantics["semantic_inputs"]["focus_areas"] == [
        "  sizing   boundary ",
        "range update",
        "range update",
        "sizing boundary",
    ]
    static = semantics["renderer_static_identity"]
    assert static["producer_file"] == "web/core/tool_gates.py"
    assert len(static["producer_function_sha256"]) == 64
    assert static["template_digests"]
    assert len(semantics["sentinel_rendered_prompt_sha256"]) == 64

    call = {
        "slot": "review",
        "invocation_id": "1" * 32,
        "context_binding": context,
        "context_binding_digest": content_digest(context),
    }
    rendered = authority.render_gate_provider_prompt(call)
    replay_inputs = json.loads(rendered.renderer_inputs_json)
    assert replay_inputs["focus_areas"] == [
        "  sizing   boundary ",
        "range update",
        "range update",
        "sizing boundary",
    ]
    assert replay_inputs["invocation_id"] == "1" * 32
    assert authority.gate_call_context(
        deepcopy(checkpoint),
        gate_name="review",
        candidate_dir=candidate,
    ) == context

    changed = deepcopy(checkpoint)
    changed["audit_context"]["worker_cot_focus_areas"] = ["different focus"]
    assert authority.gate_call_context(
        changed,
        gate_name="review",
        candidate_dir=candidate,
    ) != context
    tampered_call = deepcopy(call)
    tampered_call["context_binding"]["renderer_semantics"][
        "sentinel_rendered_prompt_sha256"
    ] = "0" * 64
    tampered_call["context_binding_digest"] = content_digest(
        tampered_call["context_binding"]
    )
    with pytest.raises(
        authority.StrictAuthorityError,
        match="strict_authority_gate_renderer_semantics_drift:review",
    ):
        authority.render_gate_provider_prompt(tampered_call)


def test_strict_critic_context_binds_all_reconstructable_semantics(
    tmp_path,
    monkeypatch,
):
    import agent_review
    import strict_authority_workflow as authority
    from workflow_kernel import content_digest

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    policy = "def decide(context):\n    return {'kind': 'pass'}\n"
    (candidate / "policy.py").write_text(policy, encoding="utf-8")
    monkeypatch.setattr(
        agent_review,
        "_critic_h2h_snapshot_material",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("first-strict Critic must not open strength snapshots")
        ),
    )
    checkpoint = {
        "source_v": 142,
        "next_v": 143,
        "stage": "reviewed",
        "master_plan": {"tasks": [{"worker_id": "W1", "goal": "value"}]},
        "gate_results": {
            "quality": {"all_passed": True},
            "review": {
                "system_verifier_receipt": {"receipt_digest": "c" * 64}
            },
            "critic": {
                "score": 4,
                "approved": False,
                "feedback": "fix sizing",
            },
        },
        "audit_context": {
            "system_strict_bootstrap": {
                "receipt_digest": "a" * 64,
                "plan_digest": "b" * 64,
            },
        },
    }

    context = authority.gate_call_context(
        checkpoint,
        gate_name="critic",
        candidate_dir=candidate,
    )
    inputs = context["renderer_semantics"]["semantic_inputs"]
    assert json.loads(inputs["master_plan"])["tasks"][0]["goal"] == "value"
    assert inputs["code_evidence"]["target_artifact_hash"] == context[
        "candidate_artifact_hash"
    ]
    assert policy.rstrip() in inputs["code_evidence"]["prompt_section"]
    assert "FIRST-STRICT NO-STRENGTH" in inputs["h2h_snapshot_contract"]
    assert inputs["previous_critic"] == {
        "approved": False,
        "feedback": "fix sizing",
        "score": 4,
    }
    call = {
        "slot": "critic",
        "invocation_id": "2" * 32,
        "context_binding": context,
        "context_binding_digest": content_digest(context),
    }
    assert "FIRST-STRICT NO-STRENGTH" in authority.render_gate_provider_prompt(call)
    assert authority.gate_provider_evidence_snapshot_dir(call) is None
    assert context["provider_evidence_scope"][
        "allowed_evidence_snapshot_dir"
    ] is None
    assert authority.gate_call_context(
        deepcopy(checkpoint),
        gate_name="critic",
        candidate_dir=candidate,
    ) == context

    variants = []
    changed_plan = deepcopy(checkpoint)
    changed_plan["master_plan"]["tasks"][0]["goal"] = "changed"
    variants.append(changed_plan)
    changed_previous = deepcopy(checkpoint)
    changed_previous["gate_results"]["critic"]["score"] = 5
    variants.append(changed_previous)
    for changed in variants:
        assert authority.gate_call_context(
            changed,
            gate_name="critic",
            candidate_dir=candidate,
        ) != context

    projected = deepcopy(checkpoint)
    projected["stage"] = "critic_checked"
    projected["gate_results"]["critic"] = {
        "score": 8,
        "approved": True,
        "feedback": "current result",
        "prev_critic": deepcopy(checkpoint["gate_results"]["critic"]),
    }
    assert authority.gate_call_context(
        projected,
        gate_name="critic",
        candidate_dir=candidate,
    ) == context

    (candidate / "policy.py").write_text(policy + "# changed\n", encoding="utf-8")
    assert authority.gate_call_context(
        checkpoint,
        gate_name="critic",
        candidate_dir=candidate,
    ) != context


def test_master_evidence_producers_are_exact_at_master_stage():
    import evaluation_contract
    import evolution_scope

    assert MASTER_EVIDENCE_PRODUCER_FILES <= evolution_scope.CRITICAL_GENERATION_EXACT
    assert MASTER_EVIDENCE_PRODUCER_FILES <= evaluation_contract.MASTER_STAGE_EXACT
    assert MASTER_EVIDENCE_PRODUCER_FILES <= evaluation_contract.critical_exact_for_stage(
        "direction_audited",
        national_execution_mode="native_tcp",
    )


def test_next_gate_entrypoints_and_shared_facades_are_restart_critical():
    import evaluation_contract

    assert SHARED_TOOL_FACADE_FILES <= evaluation_contract.ALWAYS_CRITICAL_EXACT
    expected_by_stage = {
        "selected": {
            "web/core/tool_gates.py",
            "web/core/tool_commit.py",
        },
        "preparing": {
            "web/core/tool_gates.py",
            "web/core/tool_commit.py",
        },
        "crossover_running": {
            "web/core/tool_gates.py",
            "web/core/tool_commit.py",
        },
        "workers_done": {"web/core/tool_gates.py"},
        "quality_passed": {
            "web/core/tool_gates.py",
            "web/core/agent_review.py",
        },
        "reviewed": {
            "web/core/tool_gates.py",
            "web/core/agent_review.py",
        },
    }
    for stage, owners in expected_by_stage.items():
        exact = evaluation_contract.critical_exact_for_stage(
            stage,
            national_execution_mode="native_tcp",
        )
        assert SHARED_TOOL_FACADE_FILES | owners <= exact

    assert SHARED_TOOL_FACADE_FILES <= evaluation_contract.FULL_PIPELINE_EXACT
    assert OPERATIONAL_CONTINUITY_FILES <= evaluation_contract.ALWAYS_CRITICAL_EXACT


def test_fresh_first_strict_evaluation_contract_never_walks_numeric_high_water(
    tmp_path,
    monkeypatch,
):
    import os

    import evaluation_contract

    retired = tmp_path / "bots" / "national_v142"
    candidate = tmp_path / "bots" / "national_v143"
    retired.mkdir(parents=True)
    candidate.mkdir(parents=True)
    (retired / "policy.py").write_text("RETIRED_POISON = True\n", encoding="utf-8")
    (candidate / "policy.py").write_text("CURRENT = 1\n", encoding="utf-8")
    monkeypatch.setattr(evaluation_contract, "_git_ls_files", lambda *_args: [])
    real_walk = os.walk
    walked = []

    def active_only_walk(path, *args, **kwargs):
        resolved = str(path)
        walked.append(resolved)
        if "national_v142" in resolved:
            raise AssertionError("numeric-only v142 must never be walked")
        return real_walk(path, *args, **kwargs)

    monkeypatch.setattr(evaluation_contract.os, "walk", active_only_walk)
    checkpoint = {
        "stage": "selected",
        "source_v": 142,
        "next_v": 143,
        "audit_context": {
            "protocol_bootstrap": {
                "mode": "fresh_national_policy_bootstrap",
                "source_v": 142,
                "next_v": 143,
                "source_artifact_inherited": False,
            },
        },
    }
    contract = evaluation_contract.build_evaluation_contract(
        tmp_path,
        candidate_v=143,
        source_v=142,
        checkpoint=checkpoint,
        stage="selected",
        national_execution_mode="native_tcp",
    )

    assert contract["bot_versions"] == [143]
    assert "bots/national_v143/" in contract["path_prefixes"]
    assert "bots/national_v142/" not in contract["path_prefixes"]
    before = evaluation_contract.evaluation_contract_hash(tmp_path, contract)
    (retired / "policy.py").write_text("RETIRED_POISON = 'changed'\n", encoding="utf-8")
    assert evaluation_contract.evaluation_contract_hash(tmp_path, contract) == before
    (candidate / "policy.py").write_text("CURRENT = 2\n", encoding="utf-8")
    assert evaluation_contract.evaluation_contract_hash(tmp_path, contract) != before
    assert walked and all("national_v142" not in path for path in walked)


def test_every_checkpoint_stage_binds_its_exact_llm_prompt_inputs():
    import evaluation_contract

    expected_by_stage = {
        "selected": {
            "web/core/prompts/crossover_compatibility.md",
            "web/core/prompts/crossover_prompt.md",
        },
        "preparing": {
            "web/core/prompts/crossover_compatibility.md",
            "web/core/prompts/crossover_prompt.md",
        },
        "prepared": {
            "web/core/prompts/direction_auditor_prompt.md",
            "web/core/prompts/literature_probe_prompt.md",
        },
        "crossover_running": {
            "web/core/prompts/crossover_compatibility.md",
            "web/core/prompts/crossover_prompt.md",
        },
        "direction_audited": {
            "web/core/prompts/combined_analyst.md",
            "web/core/prompts/degeneration_diagnosis.md",
            "web/core/prompts/cycle_archivist.md",
            "web/core/prompts/literature_probe_prompt.md",
            "web/core/prompts/master_plan_audit.md",
            "web/core/prompts/master_prompt.md",
        },
        "master_planned": WORKER_PROMPT_INPUTS,
        "quality_failed": WORKER_PROMPT_INPUTS,
        "precommit_failed": WORKER_PROMPT_INPUTS,
        "repair_planned": WORKER_PROMPT_INPUTS,
        "rework_running": WORKER_PROMPT_INPUTS,
        "workers_done": set(),
        "quality_passed": {"web/core/prompts/reviewer_prompt.md"},
        "reviewed": {"web/core/prompts/critic_prompt.md"},
        "critic_checked": set(),
        "verified": OFFICIAL_PROMPT_INPUTS,
        "official_bootstrap_required": OFFICIAL_PROMPT_INPUTS,
        "official_certifying": OFFICIAL_PROMPT_INPUTS,
        "publishing": OFFICIAL_PROMPT_INPUTS,
    }

    assert set(expected_by_stage) == set(evaluation_contract._STAGE_EXACT)
    for stage, expected in expected_by_stage.items():
        assert expected <= evaluation_contract.critical_exact_for_stage(
            stage,
            national_execution_mode="native_tcp",
        ), stage


def test_evaluation_hash_tracks_blueprint_but_not_mutable_pause_record(
    tmp_path: Path,
    monkeypatch,
):
    import evaluation_contract

    manifest = tmp_path / "web/core/bootstrap_assets/strict_v1/manifest.json"
    pause = tmp_path / "web/core/results/llm_availability_pause.json"
    manifest.parent.mkdir(parents=True)
    pause.parent.mkdir(parents=True)
    manifest.write_text("blueprint-v1\n", encoding="utf-8")
    pause.write_text("pause-v1\n", encoding="utf-8")

    monkeypatch.setattr(
        evaluation_contract,
        "_git_ls_files",
        lambda _root, _pathspecs: [
            "web/core/bootstrap_assets/strict_v1/manifest.json",
            "web/core/results/llm_availability_pause.json",
        ],
    )
    contract = evaluation_contract.build_evaluation_contract(
        tmp_path,
        stage="verified",
        national_execution_mode="native_tcp",
    )
    assert evaluation_contract.is_contract_path(
        "web/core/bootstrap_assets/strict_v1/manifest.json",
        contract,
    )
    assert not evaluation_contract.is_contract_path(
        "web/core/results/llm_availability_pause.json",
        contract,
    )

    before = evaluation_contract.evaluation_contract_hash(tmp_path, contract)
    pause.write_text("pause-v2\n", encoding="utf-8")
    after_pause = evaluation_contract.evaluation_contract_hash(tmp_path, contract)
    manifest.write_text("blueprint-v2\n", encoding="utf-8")
    after_blueprint = evaluation_contract.evaluation_contract_hash(tmp_path, contract)

    assert after_pause == before
    assert after_blueprint != before
    assert len(after_blueprint) == hashlib.sha256().digest_size * 2


def test_pinned_official_oracle_bytes_remain_exact():
    import runtime_architecture_policy

    root = Path(__file__).resolve().parents[2]
    observed = {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in runtime_architecture_policy.OFFICIAL_ORACLE_DOC_DIGESTS
    }
    assert observed == runtime_architecture_policy.OFFICIAL_ORACLE_DOC_DIGESTS
