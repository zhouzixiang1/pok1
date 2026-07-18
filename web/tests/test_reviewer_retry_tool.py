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


def test_strict_reviewer_infra_identity_ignores_nonce_and_exhausts_budget(
    tmp_path,
):
    import tool_gates
    from pipeline_infrastructure import build_infrastructure_failure

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
    checkpoint = {
        "runtime_contract_ledger": {"ledger_digest": "d" * 64},
    }

    def strict_call(invocation_id, *, context_digest="b" * 64):
        return {
            "slot": "review",
            "purpose": "system_strict_bootstrap_gate:review",
            "invocation_id": invocation_id,
            "generation_binding_digest": "a" * 64,
            "checkpoint_stage": "quality_passed",
            "checkpoint_revision": 8,
            "context_binding_digest": context_digest,
        }

    first_call = strict_call("1" * 32)
    restarted_call = strict_call("2" * 32)
    first_harness = tool_gates._strict_review_infrastructure_harness_identity(
        first_call
    )
    restarted_harness = (
        tool_gates._strict_review_infrastructure_harness_identity(
            restarted_call
        )
    )
    assert first_harness == restarted_harness

    keys = []
    metadata_rows = []
    for call, prompt in (
        (first_call, "review invocation_id=" + "1" * 32),
        (restarted_call, "review invocation_id=" + "2" * 32),
    ):
        harness = tool_gates._strict_review_infrastructure_harness_identity(call)
        key, metadata = tool_gates._llm_gate_infrastructure_identity(
            component="reviewer_llm",
            role="LEAD CODE REVIEWER",
            candidate_dir=candidate,
            source_dir=None,
            prompt_text=prompt,
            checkpoint=checkpoint,
            source_fingerprint_override="c" * 64,
            harness_identity_override=harness,
        )
        keys.append(key)
        metadata_rows.append(metadata)

    assert keys[0] == keys[1]
    assert metadata_rows[0]["prompt_digest"] != metadata_rows[1]["prompt_digest"]
    assert {
        row["attempt_harness_identity"] for row in metadata_rows
    } == {first_harness}
    assert {
        row["attempt_harness_identity_mode"] for row in metadata_rows
    } == {"stable_override_v1"}

    overlay = None
    attempts = []
    for attempt in range(1, 4):
        overlay = build_infrastructure_failure(
            overlay,
            component="reviewer_llm",
            code="reviewer_llm_unavailable",
            owner_tool="run_review",
            resume_stage="quality_passed",
            attempt_key=keys[(attempt - 1) % 2],
            issues=["timeout"],
            max_attempts=3,
            now=float(attempt),
        )
        attempts.append(overlay["attempt"])
    assert attempts == [1, 2, 3]
    assert overlay["exhausted"] is True
    assert overlay["action"] == "abandon_generation"

    changed = tool_gates._strict_review_infrastructure_harness_identity(
        strict_call("3" * 32, context_digest="e" * 64)
    )
    assert changed != first_harness


def test_strict_retry_reopens_first_authority_before_provider_dispatch(
    tmp_path,
    monkeypatch,
):
    import reviewer_retry
    import strict_authority_workflow
    import system_strict_bootstrap
    import tool_gates

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n", encoding="utf-8")
    first_attempt = {"attempt": 1, "approved": False}
    checkpoint = {
        "workflow_run_id": "generation:143:strict-predispatch-review",
        "next_v": 143,
        "source_v": 142,
        "parent2_v": None,
        "stage": "quality_passed",
        "checkpoint_revision": 9,
        "master_plan": {"tasks": []},
        "gate_results": {
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
            }
        },
        "review_attempt_journal": [first_attempt],
        "audit_context": {},
    }
    observed = {
        "rendered": 0,
        "provider": 0,
        "authority": [],
        "abandon": [],
    }

    async def no_exhausted(*_args, **_kwargs):
        return None

    async def provider(*_args, **_kwargs):
        observed["provider"] += 1
        raise AssertionError("review:retry provider must not be dispatched")

    async def abandon(checkpoint_arg, *, gate_name, error):
        observed["abandon"].append((checkpoint_arg, gate_name, error.errors))
        return {
            "abandoned": True,
            "error": "SYSTEM_STRICT_BOOTSTRAP_REVIEW_AUTHORITY_INVALID",
            "validation_errors": list(error.errors),
        }

    def validate(checkpoint_arg, *, journal, candidate_dir, **kwargs):
        observed["authority"].append({
            "checkpoint": checkpoint_arg,
            "journal": journal,
            "candidate_dir": candidate_dir,
            **kwargs,
        })
        return ["strict_authority_review_receipt_missing"]

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
    monkeypatch.setattr(tool_gates, "_idempotency_check", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_gates, "_quality_gate_ok", lambda _checkpoint: True)
    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_gates, "run_claude_query", provider)
    monkeypatch.setattr(tool_gates, "_abandon_strict_gate_authority", abandon)
    monkeypatch.setattr(
        system_strict_bootstrap,
        "is_declared_native_bootstrap",
        lambda _checkpoint: True,
    )
    monkeypatch.setattr(
        reviewer_retry,
        "current_review_attempts",
        lambda *_args, **_kwargs: [first_attempt],
    )
    monkeypatch.setattr(
        reviewer_retry,
        "review_attempt_action",
        lambda _journal: {
            "action": "dispatch",
            "attempt": 2,
            "consistency": "initial_reject",
        },
    )
    monkeypatch.setattr(
        reviewer_retry,
        "validate_strict_review_attempt_authority",
        validate,
    )
    monkeypatch.setattr(
        strict_authority_workflow,
        "gate_call_context",
        lambda _checkpoint, *, gate_name, candidate_dir: {
            "phase": gate_name,
            "candidate": str(candidate_dir),
        },
    )
    monkeypatch.setattr(
        strict_authority_workflow,
        "new_call",
        lambda *_args, **_kwargs: {
            "slot": "review:retry",
            "purpose": "system_strict_bootstrap_gate:review:retry",
            "invocation_id": "1" * 32,
            "generation_binding_digest": "a" * 64,
            "checkpoint_stage": "quality_passed",
            "checkpoint_revision": 9,
            "context_binding_digest": "b" * 64,
        },
    )

    def render(*_args, **_kwargs):
        observed["rendered"] += 1
        raise AssertionError("invalid first authority must block prompt rendering")

    monkeypatch.setattr(
        strict_authority_workflow,
        "render_gate_provider_prompt",
        render,
    )

    payload = json.loads(asyncio.run(tool_gates.run_review.handler({
        "version": 143,
        "source_v": 142,
        "plan": [],
    }))["content"][0]["text"])

    assert payload["abandoned"] is True
    assert payload["validation_errors"] == [
        "strict_authority_review_receipt_missing"
    ]
    assert observed["provider"] == 0
    assert observed["rendered"] == 0
    assert len(observed["authority"]) == 1
    assert observed["authority"][0]["journal"] == [first_attempt]
    assert observed["authority"][0]["require_no_other_accepted"] is True
    assert observed["abandon"][0][1] == "review"
