from __future__ import annotations

import asyncio
import json


def test_first_reject_retries_same_stage_then_conflict_routes_repair(
    tmp_path,
    monkeypatch,
):
    import llm_query
    import system_strict_bootstrap
    import tool_gates

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n")
    checkpoint = {
        "workflow_run_id": "generation:143:review-tool-test",
        "next_v": 143,
        "source_v": 142,
        "parent2_v": None,
        "stage": "quality_passed",
        "checkpoint_revision": 8,
        "master_plan": {"tasks": []},
        "gate_results": {
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
            }
        },
        "audit_context": {},
    }
    provider_results = [
        {
            "approved": False,
            "quality_score": 3,
            "feedback": "dead helper remains",
            "change_summary": "one blocker",
            "risk_areas": ["dead code"],
        },
        {
            "approved": True,
            "quality_score": 8,
            "feedback": "machine gates bound the candidate",
            "change_summary": "acceptable",
            "risk_areas": [],
        },
    ]
    writes = []
    recorded = []
    owned_infra = {
        "schema_version": 1,
        "owner_tool": "run_review",
        "resume_stage": "quality_passed",
        "attempt": 1,
        "effect_id": "review-timeout-effect",
    }

    class UI:
        def log_history(self, *_args, **_kwargs):
            return None

        def get_output(self):
            return ""

    async def no_exhausted(*_args, **_kwargs):
        return None

    async def query(*_args, **_kwargs):
        return json.dumps(provider_results.pop(0)), 0.1, {}

    def write(*_args, **kwargs):
        writes.append(kwargs)
        checkpoint["review_attempt_journal"] = kwargs["review_attempt_journal"]
        checkpoint["checkpoint_revision"] += 1
        return True

    def record(_v, _source_v, _name, gate, **kwargs):
        recorded.append((gate, kwargs))
        return True

    monkeypatch.setattr(tool_gates, "_set_pipeline_status", lambda *_args: None)
    monkeypatch.setattr(tool_gates, "_matching_checkpoint", lambda *_args: checkpoint)
    monkeypatch.setattr(
        tool_gates,
        "_owned_infrastructure_failure",
        lambda *_args: (owned_infra, None),
    )
    monkeypatch.setattr(
        tool_gates,
        "_execute_exhausted_infrastructure_failure",
        no_exhausted,
    )
    monkeypatch.setattr(tool_gates, "_idempotency_check", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_gates, "_quality_gate_ok", lambda _checkpoint: True)
    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_gates, "get_logs_dir", lambda _version: tmp_path / "logs")
    monkeypatch.setattr(tool_gates, "_get_ui", lambda: UI())
    monkeypatch.setattr(
        tool_gates,
        "_llm_gate_infrastructure_identity",
        lambda **_kwargs: ("reviewer", {}),
    )
    monkeypatch.setattr(
        tool_gates,
        "_review_semantic_contract",
        lambda *_args, **_kwargs: {"contract_digest": "a" * 64},
    )
    monkeypatch.setattr(tool_gates, "run_claude_query", query)
    monkeypatch.setattr(tool_gates, "write_pipeline_checkpoint", write)
    monkeypatch.setattr(tool_gates, "_record_gate", record)
    monkeypatch.setattr(tool_gates, "log_system_event", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_gates, "_record_quality_failure", lambda *_a, **_k: None)
    monkeypatch.setattr(
        system_strict_bootstrap,
        "is_declared_native_bootstrap",
        lambda _checkpoint: False,
    )
    monkeypatch.setattr(
        llm_query,
        "render_llm_prompt",
        lambda *_args, **_kwargs: "frozen reviewer prompt",
    )

    first = json.loads(asyncio.run(tool_gates.run_review.handler({
        "version": 143,
        "source_v": 142,
        "plan": [],
    }))["content"][0]["text"])
    assert first["review_retry_scheduled"] is True
    assert first["checkpoint_stage"] == "quality_passed"
    assert first["next_tool"] == "run_review"
    assert len(checkpoint["review_attempt_journal"]) == 1
    assert writes[0]["clear_infra_failure"] is True
    assert writes[0]["infra_failure_owner"] == "run_review"
    assert writes[0]["expected_infra_failure_digest"]
    assert checkpoint["review_attempt_journal"][0][
        "consumed_infrastructure_failure_digest"
    ] == writes[0]["expected_infra_failure_digest"]
    assert checkpoint["review_attempt_journal"][0][
        "consumed_infrastructure_attempt"
    ] == 1
    assert recorded == []

    second = json.loads(asyncio.run(tool_gates.run_review.handler({
        "version": 143,
        "source_v": 142,
        "plan": [],
    }))["content"][0]["text"])
    assert second["approved"] is False
    assert second["review_adjudication"]["consistency"] == "conflict"
    assert second["next_tool"] == "execute_workers"
    assert len(recorded) == 1
    gate, kwargs = recorded[0]
    assert kwargs["stage"] == "repair_planned"
    assert len(kwargs["review_attempt_journal"]) == 2
    assert gate["approved"] is False
    assert gate["review_consistency"] == "conflict"


def test_valid_approval_consumes_timeout_overlay_in_reviewed_cas(
    tmp_path,
    monkeypatch,
):
    import llm_query
    import system_strict_bootstrap
    import tool_gates

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n")
    checkpoint = {
        "workflow_run_id": "generation:143:review-timeout-approve",
        "next_v": 143,
        "source_v": 142,
        "parent2_v": None,
        "stage": "quality_passed",
        "checkpoint_revision": 8,
        "master_plan": {"tasks": []},
        "gate_results": {"quality": {
            "all_passed": True,
            "critical_scenarios_passed": True,
        }},
        "audit_context": {},
    }
    owned_infra = {
        "schema_version": 1,
        "owner_tool": "run_review",
        "resume_stage": "quality_passed",
        "attempt": 1,
        "effect_id": "review-timeout-effect",
    }
    captured = {}

    class UI:
        def get_output(self):
            return ""

    async def no_exhausted(*_args, **_kwargs):
        return None

    async def query(*_args, **_kwargs):
        return json.dumps({
            "approved": True,
            "quality_score": 8,
            "feedback": "machine checks and code are consistent",
            "change_summary": "approved",
            "risk_areas": [],
        }), 0.1, {}

    def record(_v, _source_v, _name, gate, **kwargs):
        captured.update({"gate": gate, **kwargs})
        return True

    monkeypatch.setattr(tool_gates, "_set_pipeline_status", lambda *_args: None)
    monkeypatch.setattr(tool_gates, "_matching_checkpoint", lambda *_args: checkpoint)
    monkeypatch.setattr(
        tool_gates,
        "_owned_infrastructure_failure",
        lambda *_args: (owned_infra, None),
    )
    monkeypatch.setattr(
        tool_gates,
        "_execute_exhausted_infrastructure_failure",
        no_exhausted,
    )
    monkeypatch.setattr(tool_gates, "_idempotency_check", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_gates, "_quality_gate_ok", lambda _checkpoint: True)
    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_gates, "get_logs_dir", lambda _version: tmp_path / "logs")
    monkeypatch.setattr(tool_gates, "_get_ui", lambda: UI())
    monkeypatch.setattr(
        tool_gates,
        "_llm_gate_infrastructure_identity",
        lambda **_kwargs: ("reviewer", {}),
    )
    monkeypatch.setattr(
        tool_gates,
        "_review_semantic_contract",
        lambda *_args, **_kwargs: {"contract_digest": "a" * 64},
    )
    monkeypatch.setattr(tool_gates, "run_claude_query", query)
    monkeypatch.setattr(tool_gates, "_record_gate", record)
    monkeypatch.setattr(tool_gates, "log_system_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        system_strict_bootstrap,
        "is_declared_native_bootstrap",
        lambda _checkpoint: False,
    )
    monkeypatch.setattr(
        llm_query,
        "render_llm_prompt",
        lambda *_args, **_kwargs: "frozen reviewer prompt",
    )

    payload = json.loads(asyncio.run(tool_gates.run_review.handler({
        "version": 143,
        "source_v": 142,
        "plan": [],
    }))["content"][0]["text"])

    assert payload["approved"] is True
    assert captured["stage"] == "reviewed"
    assert captured["clear_infra_failure"] is True
    assert captured["infra_failure_owner"] == "run_review"
    assert captured["expected_infra_failure_digest"]
    assert len(captured["review_attempt_journal"]) == 1
    assert captured["review_attempt_journal"][0][
        "consumed_infrastructure_failure_digest"
    ] == captured["expected_infra_failure_digest"]
    assert captured["review_attempt_journal"][0][
        "consumed_infrastructure_attempt"
    ] == 1
