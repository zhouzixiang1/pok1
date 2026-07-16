import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot_artifact import hash_path
from bot_namespace import STRICT_ARTIFACT_FILES, strict_artifact_layout_errors
from llm_availability import LLMAvailabilityBlocked, classify_llm_availability


class _UI:
    costs = {}

    def log_history(self, *_args, **_kwargs):
        pass

    def clear_io(self):
        pass

    def set_status(self, *_args, **_kwargs):
        pass

    def get_output(self):
        return ""


def _blocked(role="test", *, auth=False):
    evidence = (
        "HTTP 401 authentication_error: invalid API key"
        if auth
        else "API Error: 403 usage limit for this billing cycle"
    )
    issue = classify_llm_availability(
        [evidence],
        statuses=[401 if auth else 403],
    )
    assert issue is not None
    return LLMAvailabilityBlocked(issue, role=role)


def _checkpoint(next_v, source_v, stage, *, parent2_v=None, **extra):
    payload = {
        "next_v": next_v,
        "source_v": source_v,
        "stage": stage,
        "checkpoint_revision": 7,
        "workflow_run_id": f"wf-{next_v}",
        "generation_attempt": 0,
        "audit_attempt": 0,
        "gate_results": {},
        "audit_context": {},
        **extra,
    }
    if parent2_v is not None:
        payload["parent2_v"] = parent2_v
    return payload


def _write_checkpoint_bytes(path, checkpoint):
    path.write_text(
        json.dumps(checkpoint, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return path.read_bytes()


def _write_strict_bot(root, *, policy_value=1, completed=False):
    root.mkdir(parents=True)
    payloads = {
        "national_bot.py": "# system runtime\n",
        "precompute.py": "FACT = 1\n",
        "policy.py": (
            "def get_baseline_decision(context):\n"
            f"    return {{'kind': 'pass', 'value': {policy_value}}}\n\n"
            "def iter_decisions(context, baseline, deadline):\n"
            "    return ()\n"
        ),
        "national_runtime_manifest.json": "{}\n",
        "policy_epoch_receipt.json": "{}\n",
    }
    assert frozenset(payloads) == STRICT_ARTIFACT_FILES
    for relative, payload in payloads.items():
        (root / relative).write_text(payload, encoding="utf-8")
    if completed:
        (root / ".completed").touch()
    assert strict_artifact_layout_errors(root) == []
    return root


def _valid_master_plan():
    return {
        "analysis": "targeted turn decision-path change",
        "targeted_failure": "missed turn semi-bluff raise",
        "expected_behavior_change": "raise selected draws instead of folding",
        "do_not_touch": ["national_bot.py", "precompute.py"],
        "measurement_plan": "compare the candidate against its parent",
        "tasks": [{
            "worker_id": 1,
            "role": "Algorithmic Logic Architect",
            "target_files": ["policy.py"],
            "worker_prompt": "Change policy.py in the prepared target bot.",
        }],
    }


def test_combined_analyst_availability_bubbles_without_retry(
    tmp_path,
    monkeypatch,
):
    import combined_analyst

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "combined_analyst.md").write_text(
        "Inspect the frozen evidence and return the required JSON.",
        encoding="utf-8",
    )
    calls = 0

    async def unavailable(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise _blocked("combined-analyst")

    monkeypatch.setattr(combined_analyst, "PROMPTS_DIR", prompts)
    monkeypatch.setattr(
        combined_analyst,
        "get_logs_dir",
        lambda _version: tmp_path,
    )
    monkeypatch.setattr(
        combined_analyst,
        "_statistical_stagnation_check",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(combined_analyst, "run_claude_query", unavailable)

    row = {
        "name": "national_v7",
        "selection_score": 0.5,
        "leaderboard_score": 0.5,
        "h2h_avg_wr": 0.5,
        "h2h_coverage": 1.0,
        "h2h_opponents": 1,
        "h2h_opponents_total": 1,
    }
    with pytest.raises(LLMAvailabilityBlocked):
        asyncio.run(combined_analyst._run_combined_analysis(
            7,
            ["national_v7"],
            {},
            _UI(),
            h2h_data={},
            bot_stats_data={},
            selection_rows_data=[row],
            rating_history_data=[],
        ))

    assert calls == 1


def test_prepare_degeneration_availability_is_checkpoint_and_candidate_neutral(
    tmp_path,
    monkeypatch,
):
    """The advisory degeneration role may not publish a selected generation."""
    import audit_agents
    import combined_analyst
    import epoch_authority
    import evidence_snapshot
    import evolution_infra
    import generation_scheduler as scheduler
    import orchestrator_cost_policy
    import post_publication_handoff
    import repo_state
    import tool_runtime_guard
    import workflow_profiles

    checkpoint_file = tmp_path / "pipeline_state.json"
    checkpoint_bytes = _write_checkpoint_bytes(
        checkpoint_file,
        _checkpoint(144, 143, "archived"),
    )
    candidate = _write_strict_bot(tmp_path / "national_v145")
    candidate_hash = hash_path(candidate)

    profile = SimpleNamespace(
        profile_id="national_native",
        national_execution_mode="native_tcp",
        eval_wait_rd_threshold=90.0,
        eval_wait_rd_min_games=1,
        eval_wait_min_games=1,
    )
    epoch_projection = {
        "initialized": True,
        "current_v": 144,
        "published_high_water": 144,
        "abandoned_receipt_floor": 0,
        "abandoned_receipt_head_digest": None,
        "allocation_floor": 144,
        "next_v": 145,
        "next_v_authority": "paired_annotated_tag_high_water",
        "active_bots": ["national_v143", "national_v144"],
        "active_generation": None,
        "ignored_checkpoint": None,
    }
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "none"},
    )
    monkeypatch.setattr(
        epoch_authority,
        "strict_epoch_projection",
        lambda **_kwargs: dict(epoch_projection),
    )
    monkeypatch.setattr(workflow_profiles, "get_workflow_profile", lambda: profile)
    monkeypatch.setattr(
        tool_runtime_guard,
        "ensure_runtime_git_guard",
        lambda *_a, **_k: (True, {}),
    )
    monkeypatch.setattr(
        evolution_infra,
        "ensure_publish_ready_for_new_generation",
        lambda: (True, {}),
    )
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 144)
    monkeypatch.setattr(evolution_infra, "find_latest_active_v", lambda: 144)
    monkeypatch.setattr(
        evolution_infra,
        "get_active_bots",
        lambda: ["national_v143", "national_v144"],
    )
    monkeypatch.setattr(evolution_infra, "find_max_committed_v", lambda: 144)
    monkeypatch.setattr(evolution_infra, "find_abandoned_version_floor", lambda: 0)
    monkeypatch.setattr(
        evolution_infra,
        "compute_next_generation_v",
        lambda **_kwargs: 145,
    )

    async def evaluation_ready(*_args, **_kwargs):
        return True

    monkeypatch.setattr(evolution_infra, "wait_for_daemon_eval", evaluation_ready)
    monkeypatch.setattr(repo_state, "log_git_worktree_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr(
        evidence_snapshot,
        "ensure_generation_h2h_snapshot",
        lambda *_a, **_k: {"available": True, "manifest_digest": "m" * 64},
    )
    monkeypatch.setattr(
        evidence_snapshot,
        "load_generation_evaluation_snapshot",
        lambda *_a, **_k: {"available": True},
    )
    frozen_evidence = scheduler.EvaluationEvidence(
        active_bots=("national_v143", "national_v144"),
        ratings={},
        bot_stats={},
        h2h={},
        selection_rows=(),
        rating_history_tail=(),
        games=100,
        rd=50.0,
        readiness_reason="test_ready",
        cutoffs={"cycle_manifest_digest": "c" * 64},
    )
    monkeypatch.setattr(
        scheduler,
        "_load_post_wait_evaluation_evidence",
        lambda **_kwargs: frozen_evidence,
    )
    monkeypatch.setattr(
        scheduler,
        "_build_selection_view",
        lambda _evidence: SimpleNamespace(digest="s" * 64),
    )
    monkeypatch.setattr(
        scheduler,
        "_decide_strategy",
        lambda *_a, **_k: ("master", 144, ()),
    )
    monkeypatch.setattr(
        scheduler,
        "_mechanical_urgent_intervention_eligible",
        lambda *_a, **_k: False,
    )
    monkeypatch.setattr(scheduler, "_cleanup_incomplete", lambda: None)
    monkeypatch.setattr(scheduler, "_ensure_priority_eval_signal", lambda *_a: None)
    monkeypatch.setattr(
        scheduler,
        "_bind_prepare_generation_cost_scope",
        lambda *_a, **_k: "prepare-workflow-v145",
    )
    monkeypatch.setattr(scheduler, "_bind_prepare_log_context", lambda *_a: 145)
    monkeypatch.setattr(scheduler, "log_system_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        orchestrator_cost_policy,
        "assert_operator_cost_limit_available",
        lambda: None,
    )

    async def declining(*_args, **_kwargs):
        return {"trend": "declining", "is_stagnant": False}

    monkeypatch.setattr(combined_analyst, "_run_combined_analysis", declining)

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "degeneration_diagnosis.md").write_text(
        "Diagnose {generation_history} {rating_curve} {strategy_changes}",
        encoding="utf-8",
    )
    monkeypatch.setattr(audit_agents, "PROMPTS_DIR", prompts)
    monkeypatch.setattr(audit_agents, "get_logs_dir", lambda _v: tmp_path / "logs")
    calls = 0

    async def unavailable(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise _blocked("degeneration-diagnosis")

    monkeypatch.setattr(audit_agents, "run_claude_query", unavailable)
    monkeypatch.setattr(
        evolution_infra,
        "write_pipeline_checkpoint",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("availability pause published selected checkpoint")
        ),
    )

    with pytest.raises(LLMAvailabilityBlocked):
        asyncio.run(scheduler.prepare_generation(None, _UI(), min_games=1))

    assert calls == 1
    assert checkpoint_file.read_bytes() == checkpoint_bytes
    assert hash_path(candidate) == candidate_hash


def _master_tool_fixture(tmp_path, monkeypatch, *, next_v=145, source_v=144):
    import audit_agents
    import evolution_infra
    import pipeline_state
    import tool_planning
    import tool_runtime_guard
    import workflow_profiles
    from prepared_baseline_contract import build_prepared_artifact_contract

    source = tmp_path / f"national_v{source_v}"
    candidate = tmp_path / f"national_v{next_v}"
    _write_strict_bot(source)
    _write_strict_bot(candidate)
    checkpoint = _checkpoint(
        next_v,
        source_v,
        "direction_audited",
        direction_audit={"repetition_detected": False, "llm_failed": False},
        audit_context={
            "prepared_artifact_contract": build_prepared_artifact_contract(
                candidate,
                source_v=source_v,
                next_v=next_v,
            ),
        },
    )
    checkpoint_file = tmp_path / "pipeline_state.json"
    before = _write_checkpoint_bytes(checkpoint_file, checkpoint)

    monkeypatch.setattr(tool_planning, "_matching_checkpoint", lambda *_a, **_k: checkpoint)
    monkeypatch.setattr(
        tool_planning,
        "_build_generation_architecture_policy",
        lambda *_a, **_k: {"outcome": "ok", "policy": None, "capabilities": {}},
    )
    monkeypatch.setattr(
        tool_runtime_guard,
        "ensure_runtime_git_guard",
        lambda *_a, **_k: (True, {}),
    )
    monkeypatch.setattr(
        workflow_profiles,
        "get_workflow_profile",
        lambda: SimpleNamespace(
            profile_id="national_native",
            national_execution_mode="native_tcp",
        ),
    )
    monkeypatch.setattr(
        tool_planning,
        "get_bot_dir",
        lambda version: source if int(version) == source_v else candidate,
    )
    monkeypatch.setattr(tool_planning, "_get_ui", lambda: _UI())
    monkeypatch.setattr(tool_planning, "log_system_event", lambda *_a, **_k: None)
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(
        pipeline_state,
        "write_pipeline_runtime_heartbeat",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        pipeline_state,
        "clear_pipeline_runtime_heartbeat",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        tool_planning,
        "write_pipeline_checkpoint",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("availability pause advanced the semantic checkpoint")
        ),
    )
    monkeypatch.setattr(
        tool_planning,
        "_bump_master_fail_count",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError(
                "availability pause consumed a Master attempt: "
                + repr(_k.get("audit_context"))
            )
        ),
    )
    return SimpleNamespace(
        tool=tool_planning,
        audit_agents=audit_agents,
        checkpoint=checkpoint,
        checkpoint_file=checkpoint_file,
        checkpoint_bytes=before,
        candidate=candidate,
        candidate_hash=hash_path(candidate),
        next_v=next_v,
        source_v=source_v,
    )


def test_direction_audit_availability_is_checkpoint_and_candidate_neutral(
    tmp_path,
    monkeypatch,
):
    import tool_planning

    candidate = _write_strict_bot(tmp_path / "national_v146")
    checkpoint = _checkpoint(146, 145, "prepared")
    checkpoint_file = tmp_path / "pipeline_state.json"
    before = _write_checkpoint_bytes(checkpoint_file, checkpoint)
    candidate_before = hash_path(candidate)

    monkeypatch.setattr(tool_planning, "_matching_checkpoint", lambda *_a, **_k: checkpoint)
    monkeypatch.setattr(tool_planning, "_get_ui", lambda: _UI())
    monkeypatch.setattr(tool_planning, "_protocol_bootstrap_direction_audit", lambda *_a, **_k: None)

    async def unavailable(*_args, **_kwargs):
        raise _blocked("direction-audit")

    monkeypatch.setattr(tool_planning, "_run_direction_audit", unavailable)
    monkeypatch.setattr(
        tool_planning,
        "write_pipeline_checkpoint",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("direction availability wrote a checkpoint")
        ),
    )

    with pytest.raises(LLMAvailabilityBlocked):
        asyncio.run(
            tool_planning.run_direction_audit.handler({"source_v": 145, "next_v": 146})
        )

    assert checkpoint_file.read_bytes() == before
    assert checkpoint["checkpoint_revision"] == 7
    assert checkpoint["audit_attempt"] == 0
    assert hash_path(candidate) == candidate_before


def test_master_availability_does_not_write_checkpoint_or_consume_attempt(
    tmp_path,
    monkeypatch,
):
    fixture = _master_tool_fixture(tmp_path, monkeypatch)

    async def unavailable(*_args, **_kwargs):
        raise _blocked("master", auth=True)

    monkeypatch.setattr(fixture.tool, "_run_master_analysis", unavailable)

    with pytest.raises(LLMAvailabilityBlocked):
        asyncio.run(
            fixture.tool.run_master.handler({
                "next_v": fixture.next_v,
                "source_v": fixture.source_v,
            })
        )

    assert fixture.checkpoint_file.read_bytes() == fixture.checkpoint_bytes
    assert fixture.checkpoint["checkpoint_revision"] == 7
    assert fixture.checkpoint["audit_attempt"] == 0
    assert hash_path(fixture.candidate) == fixture.candidate_hash


def test_master_plan_audit_availability_is_attempt_neutral(
    tmp_path,
    monkeypatch,
):
    fixture = _master_tool_fixture(tmp_path, monkeypatch, next_v=147, source_v=146)

    async def valid_master(*_args, **_kwargs):
        return _valid_master_plan()

    async def unavailable_audit(*_args, **_kwargs):
        raise _blocked("master-plan-audit")

    monkeypatch.setattr(fixture.tool, "_run_master_analysis", valid_master)
    monkeypatch.setattr(
        fixture.audit_agents,
        "_run_master_plan_audit",
        unavailable_audit,
    )

    with pytest.raises(LLMAvailabilityBlocked):
        asyncio.run(
            fixture.tool.run_master.handler({
                "next_v": fixture.next_v,
                "source_v": fixture.source_v,
            })
        )

    assert fixture.checkpoint_file.read_bytes() == fixture.checkpoint_bytes
    assert fixture.checkpoint["checkpoint_revision"] == 7
    assert fixture.checkpoint["audit_attempt"] == 0
    assert hash_path(fixture.candidate) == fixture.candidate_hash


def _gate_tool_fixture(tmp_path, monkeypatch, *, stage):
    import tool_gates

    source_v, next_v = 147, 148
    source = tmp_path / f"national_v{source_v}"
    candidate = tmp_path / f"national_v{next_v}"
    _write_strict_bot(source, policy_value=1)
    _write_strict_bot(candidate, policy_value=2)
    gate_results = {
        "quality": {
            "all_passed": True,
            "critical_scenarios_passed": True,
        },
    }
    if stage == "reviewed":
        gate_results["review"] = {
            "approved": True,
            "llm_invoked": True,
            "reviewer_llm_executed": True,
            "schema_valid": True,
        }
    checkpoint = _checkpoint(
        next_v,
        source_v,
        stage,
        gate_results=gate_results,
        master_plan=_valid_master_plan(),
    )
    checkpoint_file = tmp_path / "pipeline_state.json"
    before = _write_checkpoint_bytes(checkpoint_file, checkpoint)
    monkeypatch.setattr(tool_gates, "_matching_checkpoint", lambda *_a, **_k: checkpoint)
    monkeypatch.setattr(tool_gates, "_get_ui", lambda: _UI())
    monkeypatch.setattr(tool_gates, "_quality_gate_ok", lambda _ckpt: True)
    monkeypatch.setattr(tool_gates, "_review_gate_ok", lambda _ckpt: True)
    monkeypatch.setattr(
        tool_gates,
        "get_bot_dir",
        lambda version: source if int(version) == source_v else candidate,
    )
    monkeypatch.setattr(
        tool_gates,
        "_llm_gate_infrastructure_identity",
        lambda **_kwargs: ("attempt-key", {}),
    )

    async def no_exhausted(*_args, **_kwargs):
        return None

    async def forbidden_infra(*_args, **_kwargs):
        raise AssertionError("availability pause counted as infrastructure attempt")

    monkeypatch.setattr(tool_gates, "_execute_exhausted_infrastructure_failure", no_exhausted)
    monkeypatch.setattr(tool_gates, "_record_infrastructure_failure", forbidden_infra)
    return SimpleNamespace(
        tool=tool_gates,
        checkpoint=checkpoint,
        checkpoint_file=checkpoint_file,
        checkpoint_bytes=before,
        candidate=candidate,
        candidate_hash=hash_path(candidate),
        next_v=next_v,
        source_v=source_v,
    )


def test_review_availability_does_not_count_infrastructure_or_mutate_candidate(
    tmp_path,
    monkeypatch,
):
    fixture = _gate_tool_fixture(tmp_path, monkeypatch, stage="quality_passed")

    async def unavailable(*_args, **_kwargs):
        raise _blocked("reviewer")

    monkeypatch.setattr(fixture.tool, "run_claude_query", unavailable)

    with pytest.raises(LLMAvailabilityBlocked):
        asyncio.run(
            fixture.tool.run_review.handler({
                "version": fixture.next_v,
                "source_v": fixture.source_v,
                "plan": [],
            })
        )

    assert fixture.checkpoint_file.read_bytes() == fixture.checkpoint_bytes
    assert fixture.checkpoint["checkpoint_revision"] == 7
    assert fixture.checkpoint.get("infra_failure") is None
    assert hash_path(fixture.candidate) == fixture.candidate_hash


def test_critic_availability_does_not_count_infrastructure_or_mutate_candidate(
    tmp_path,
    monkeypatch,
):
    fixture = _gate_tool_fixture(tmp_path, monkeypatch, stage="reviewed")

    async def unavailable(*_args, **_kwargs):
        raise _blocked("critic", auth=True)

    monkeypatch.setattr(fixture.tool, "_run_critic", unavailable)

    with pytest.raises(LLMAvailabilityBlocked):
        asyncio.run(
            fixture.tool.run_critic.handler({
                "version": fixture.next_v,
                "source_v": fixture.source_v,
                "plan": [],
                "reviewer_feedback": "",
                "force_advance": False,
            })
        )

    assert fixture.checkpoint_file.read_bytes() == fixture.checkpoint_bytes
    assert fixture.checkpoint["checkpoint_revision"] == 7
    assert fixture.checkpoint.get("infra_failure") is None
    assert hash_path(fixture.candidate) == fixture.candidate_hash


def _tool_crossover_fixture(tmp_path, monkeypatch):
    import national_capability_contract
    import national_position_contract
    import runtime_architecture_policy
    import tool_commit
    import workflow_profiles

    parent_a = _write_strict_bot(tmp_path / "national_v150", completed=True)
    parent_b = _write_strict_bot(tmp_path / "national_v149", completed=True)
    target = tmp_path / "national_v151"
    checkpoint = _checkpoint(151, 150, "selected", parent2_v=149)
    checkpoint.update({
        "checkpoint_schema_version": 2,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": {
            "binding_digest": "e" * 64,
            "published_parent_identities": [
                {"tag_artifact_hash": hash_path(parent_a)},
                {"tag_artifact_hash": hash_path(parent_b)},
            ],
        },
    })
    checkpoint_file = tmp_path / "pipeline_state.json"
    before = _write_checkpoint_bytes(checkpoint_file, checkpoint)

    monkeypatch.setattr(tool_commit, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(
        tool_commit,
        "get_bot_dir",
        lambda version: {150: parent_a, 149: parent_b, 151: target}[int(version)],
    )
    monkeypatch.setattr(
        tool_commit,
        "get_active_bots",
        lambda: ["national_v149", "national_v150"],
    )
    monkeypatch.setattr(tool_commit, "git_has_tag", lambda _v: True)
    monkeypatch.setattr(tool_commit, "git_dir_is_committed", lambda _v: False)
    monkeypatch.setattr(
        national_position_contract,
        "detect_position_semantics_errors",
        lambda _path: [],
    )
    monkeypatch.setattr(
        workflow_profiles,
        "get_workflow_profile",
        lambda: SimpleNamespace(national_execution_mode="native_tcp"),
    )
    monkeypatch.setattr(
        national_capability_contract,
        "evaluate_national_capabilities",
        lambda _path: {"detector_version": "test", "checks": [], "decision_path_risks": {}},
    )
    monkeypatch.setattr(
        runtime_architecture_policy,
        "build_architecture_policy",
        lambda *_a, **_k: {"policy_digest": "p" * 64},
    )
    monkeypatch.setattr(
        tool_commit,
        "_record_crossover_infrastructure",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("availability counted as crossover infrastructure")
        ),
    )
    return SimpleNamespace(
        tool=tool_commit,
        checkpoint=checkpoint,
        checkpoint_file=checkpoint_file,
        checkpoint_bytes=before,
        target=target,
    )


def test_crossover_compatibility_availability_bubbles_without_abandon(
    tmp_path,
    monkeypatch,
):
    import audit_agents

    fixture = _tool_crossover_fixture(tmp_path, monkeypatch)

    async def unavailable(*_args, **_kwargs):
        raise _blocked("crossover-compatibility")

    monkeypatch.setattr(audit_agents, "_run_crossover_compatibility_audit", unavailable)
    monkeypatch.setattr(
        fixture.tool,
        "_run_crossover",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("compatibility pause reached synthesis")
        ),
    )

    with pytest.raises(LLMAvailabilityBlocked):
        asyncio.run(
            fixture.tool.run_crossover.handler({
                "parent_a": 150,
                "parent_b": 149,
                "target_v": 151,
            })
        )

    assert fixture.checkpoint_file.read_bytes() == fixture.checkpoint_bytes
    assert fixture.checkpoint["generation_attempt"] == 0
    assert not fixture.target.exists()


def test_crossover_synthesis_availability_is_not_retried_or_abandoned(
    tmp_path,
    monkeypatch,
):
    import audit_agents

    fixture = _tool_crossover_fixture(tmp_path, monkeypatch)
    calls = 0

    async def compatible(*_args, **_kwargs):
        return {"compatible": True, "compatibility_score": 8}

    async def unavailable(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise _blocked("crossover")

    monkeypatch.setattr(audit_agents, "_run_crossover_compatibility_audit", compatible)
    monkeypatch.setattr(fixture.tool, "_run_crossover", unavailable)

    with pytest.raises(LLMAvailabilityBlocked):
        asyncio.run(
            fixture.tool.run_crossover.handler({
                "parent_a": 150,
                "parent_b": 149,
                "target_v": 151,
            })
        )

    assert calls == 1
    assert fixture.checkpoint_file.read_bytes() == fixture.checkpoint_bytes
    assert fixture.checkpoint["generation_attempt"] == 0
    assert not fixture.target.exists()


def test_crossover_role_uses_isolated_workspace_before_availability_pause(
    tmp_path,
    monkeypatch,
):
    import agent_review
    import candidate_hygiene
    import evolution_infra
    import workflow_profiles

    bots = tmp_path / "bots"
    prompts = tmp_path / "prompts"
    logs = tmp_path / "logs"
    results = tmp_path / "results"
    parent_a = _write_strict_bot(bots / "national_v150", policy_value=1)
    parent_b = _write_strict_bot(bots / "national_v149", policy_value=2)
    target = _write_strict_bot(bots / "national_v151", policy_value=3)
    prompts.mkdir()
    logs.mkdir()
    (prompts / "crossover_prompt.md").write_text(
        "crossover {{version}}",
        encoding="utf-8",
    )
    checkpoint = _checkpoint(151, 150, "selected", parent2_v=149)
    checkpoint.update({
        "checkpoint_schema_version": 2,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": {
            "binding_digest": "e" * 64,
            "published_parent_identities": [
                {"tag_artifact_hash": hash_path(parent_a)},
                {"tag_artifact_hash": hash_path(parent_b)},
            ],
        },
    })
    checkpoint_file = tmp_path / "pipeline_state.json"
    before = _write_checkpoint_bytes(checkpoint_file, checkpoint)
    target_before = hash_path(target)
    calls = 0
    captured_prompt = ""

    def bot_dir(version):
        return {150: parent_a, 149: parent_b, 151: target}[int(version)]

    async def unavailable(*args, **kwargs):
        nonlocal calls, captured_prompt
        calls += 1
        captured_prompt = str(args[0])
        write_scope = kwargs["allowed_write_dir"]
        assert set(write_scope) == {"files"}
        assert len(write_scope["files"]) == 1
        policy_path = Path(kwargs["allowed_read_dirs"][-1]) / "policy.py"
        assert write_scope == {"files": [policy_path]}
        policy_path.write_text("partial = True\n", encoding="utf-8")
        raise _blocked("crossover")

    monkeypatch.setattr(agent_review, "PROMPTS_DIR", prompts)
    monkeypatch.setattr(agent_review, "RESULTS_DIR", results)
    monkeypatch.setattr(agent_review, "MAX_CROSSOVER_RETRIES", 3)
    monkeypatch.setattr(agent_review, "get_bot_dir", bot_dir)
    monkeypatch.setattr(agent_review, "get_logs_dir", lambda _v: logs)
    monkeypatch.setattr(agent_review, "run_claude_query", unavailable)
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(
        evolution_infra,
        "write_pipeline_checkpoint",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("availability projected crossover checkpoint")
        ),
    )
    monkeypatch.setattr(candidate_hygiene, "sanitize_candidate_dir", lambda *_a, **_k: {})
    monkeypatch.setattr(
        workflow_profiles,
        "get_workflow_profile",
        lambda: SimpleNamespace(national_execution_mode="native_tcp"),
    )

    import audit_agents
    import checkpoint_schema
    from worker_workflow import WorkerArtifactStore

    monkeypatch.setattr(checkpoint_schema, "checkpoint_epoch_errors", lambda _c: [])
    monkeypatch.setattr(
        checkpoint_schema,
        "live_checkpoint_parent_authority_errors",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(audit_agents, "RESULTS_DIR", results)
    monkeypatch.setattr(audit_agents, "get_bot_dir", bot_dir)
    monkeypatch.setattr(
        audit_agents,
        "frozen_crossover_parent_architecture",
        lambda _bundle: {
            "architecture_policy": {},
            "capability_context": {},
        },
    )
    snapshot_bundle = audit_agents.capture_crossover_parent_snapshots(
        150,
        149,
        151,
        checkpoint=checkpoint,
        checkpoint_reader=lambda: checkpoint,
        artifact_store=WorkerArtifactStore(results / "workflow" / "artifacts"),
    )

    with pytest.raises(LLMAvailabilityBlocked):
        asyncio.run(agent_review._run_crossover(
            150,
            149,
            151,
            _UI(),
            compatibility={
                "parent_snapshot_receipt": snapshot_bundle["receipt"],
            },
        ))

    assert calls == 1
    assert checkpoint_file.read_bytes() == before
    assert checkpoint["checkpoint_revision"] == 7
    assert checkpoint["generation_attempt"] == 0
    assert hash_path(target) == target_before
    assert "'value': 3" in (target / "policy.py").read_text(encoding="utf-8")
    assert "provider-dispatch Runtime Path Contract" in captured_prompt
    assert "exact lease policy.py write target" in captured_prompt
    assert "system quality gate owns compilation" in captured_prompt
    assert "py_compile" not in captured_prompt
    assert "bots/national_v151/policy.py" not in captured_prompt


def test_runtime_heartbeat_is_identity_bound_and_restart_stale(tmp_path, monkeypatch):
    import pipeline_state

    heartbeat_file = tmp_path / "pipeline_runtime_heartbeat.json"
    checkpoint = _checkpoint(52, 51, "direction_audited")
    monkeypatch.setattr(
        pipeline_state,
        "PIPELINE_RUNTIME_HEARTBEAT_FILE",
        heartbeat_file,
    )

    assert pipeline_state.write_pipeline_runtime_heartbeat(
        checkpoint,
        phase="master_plan_audit_start",
        audit_attempt=0,
    )
    live = pipeline_state.read_pipeline_runtime_heartbeat(checkpoint)
    assert live["phase"] == "master_plan_audit_start"
    assert live["checkpoint_revision"] == 7

    changed = dict(checkpoint, checkpoint_revision=8)
    assert pipeline_state.read_pipeline_runtime_heartbeat(changed) is None

    # A process restart/PID reuse must not let the old sidecar extend a stage.
    monkeypatch.setattr(pipeline_state, "_process_start_token", lambda _pid: "new-process")
    assert pipeline_state.read_pipeline_runtime_heartbeat(checkpoint) is None
    assert pipeline_state.pipeline_runtime_activity_ts(checkpoint) == 0.0


def test_native_match_heartbeat_is_checkpoint_bound_and_schema_fail_closed(
    tmp_path,
    monkeypatch,
):
    import pipeline_state

    heartbeat_file = tmp_path / "pipeline_runtime_heartbeat.json"
    checkpoint = _checkpoint(52, 51, "workers_done")
    from national_native import build_native_match_timing_plan

    timing_plan = build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=600.0,
    )
    checkpoint["audit_context"] = {
        "quality_native_match_timing_plan": timing_plan.snapshot(),
        "quality_native_match_timing_plan_digest": timing_plan.digest(),
    }
    monkeypatch.setattr(
        pipeline_state,
        "PIPELINE_RUNTIME_HEARTBEAT_FILE",
        heartbeat_file,
    )
    nonce = "d" * 32
    token = pipeline_state.activate_native_match_dispatch_nonce(nonce)
    try:
        reporter = pipeline_state.make_native_match_heartbeat_reporter(
            checkpoint,
            owner_tool="run_quality_gates",
        )
        assert reporter is not None
        now = __import__("time").time()
        operation_started = now - 100.0
        phase_budget_us = timing_plan.effective_timeout_us
        progress = {
            "schema_version": 1,
            "event_type": "hand_start",
            "hand": 1,
            "hands": 70,
            "timing_plan_digest": timing_plan.digest(),
            "match_identity_digest": "b" * 64,
            "liveness_phase": "engine_running",
            "phase_budget_us": phase_budget_us,
            "operation_started_at_epoch": operation_started,
            "operation_deadline_epoch": (
                operation_started
                + timing_plan.first_strict_lease_timeout_us / 1_000_000.0
            ),
            "operation_budget_us": timing_plan.first_strict_lease_timeout_us,
            "phase_started_at_epoch": now,
            "phase_deadline_epoch": now + phase_budget_us / 1_000_000.0,
            "effective_timeout_us": timing_plan.effective_timeout_us,
        }

        assert asyncio.run(reporter(progress)) is True
        observed = pipeline_state.read_pipeline_native_match_progress(
            checkpoint,
            max_age=30,
            provider_dispatch_nonce=nonce,
        )
        assert observed is not None
        assert observed["owner_tool"] == "run_quality_gates"
        assert observed["provider_dispatch_nonce"] == nonce
        assert observed["event_seq"] == 1
        assert observed["event_type"] == "hand_start"

        finalizing_started = now + 1.0
        finalizing = {
            **progress,
            "event_type": "finalizing",
            "hand": 70,
            "liveness_phase": "finalizing",
            "phase_budget_us": timing_plan.finalization_timeout_us,
            "phase_started_at_epoch": finalizing_started,
            "phase_deadline_epoch": (
                finalizing_started
                + timing_plan.finalization_timeout_us / 1_000_000.0
            ),
        }
        assert asyncio.run(reporter(finalizing)) is True
        finalizing_live = pipeline_state.read_pipeline_native_match_progress(
            checkpoint,
            max_age=30,
            provider_dispatch_nonce=nonce,
        )
        assert finalizing_live["event_type"] == "finalizing"
        assert finalizing_live["hand"] == 70
        assert finalizing_live["phase_budget_us"] == (
            timing_plan.finalization_timeout_us
        )
        assert asyncio.run(reporter({**finalizing, "hand": 69})) is False
        assert asyncio.run(reporter({
            **finalizing,
            "phase_budget_us": timing_plan.cleanup_timeout_us,
            "phase_deadline_epoch": (
                finalizing_started
                + timing_plan.cleanup_timeout_us / 1_000_000.0
            ),
        })) is False
        assert pipeline_state.validate_native_match_progress(
            checkpoint,
            {**finalizing_live, "schema_version": 3},
            provider_dispatch_nonce=nonce,
        ) is None

        # A merely well-formed digest cannot borrow the frozen checkpoint's
        # extension authority.
        forged = dict(progress, timing_plan_digest="c" * 64)
        assert asyncio.run(reporter(forged)) is False

        # The direct runner's terminal callback removes only its own exact
        # dispatch/match sidecar instead of lending a later stall an extension.
        assert asyncio.run(reporter({
            "event_type": "terminal",
            "terminal": True,
            "terminal_outcome": "runner_returned",
            "match_identity_digest": "b" * 64,
            "timing_plan_digest": timing_plan.digest(),
        }))
        assert pipeline_state.read_pipeline_native_match_progress(
            checkpoint,
            max_age=30,
            provider_dispatch_nonce=nonce,
        ) is None
        receipt = pipeline_state.consume_native_match_terminal_handoff(
            checkpoint,
            finalizing_live,
        )
        assert receipt is not None
        assert receipt["terminal_outcome"] == "runner_returned"
        assert receipt["last_live_event_seq"] == finalizing_live["event_seq"]
        assert receipt["terminal_event_seq"] == finalizing_live["event_seq"] + 1
        # The handoff is process-local and one-shot; it is never another live
        # progress record and cannot be replayed by the same dispatch.
        assert pipeline_state.consume_native_match_terminal_handoff(
            checkpoint,
            finalizing_live,
        ) is None

        malformed = dict(progress, phase_deadline_epoch=now + 99_999.0)
        assert pipeline_state.write_pipeline_runtime_heartbeat(
            checkpoint,
            phase="native_match_progress",
            native_match_progress=malformed,
        ) is False
        assert pipeline_state.make_native_match_heartbeat_reporter(
            checkpoint,
            owner_tool="run_precommit_eval",
        ) is None
    finally:
        pipeline_state.reset_native_match_dispatch_nonce(token)
        pipeline_state.revoke_native_match_dispatch_nonce(nonce)


def test_native_match_terminal_and_revocation_cannot_delete_new_dispatch(
    tmp_path,
    monkeypatch,
):
    """An old tool completion must not clear a newer provider's heartbeat."""

    import pipeline_state
    from national_native import build_native_match_timing_plan

    heartbeat_file = tmp_path / "pipeline_runtime_heartbeat.json"
    checkpoint = _checkpoint(62, 61, "workers_done")
    timing_plan = build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=600.0,
    )
    checkpoint["audit_context"] = {
        "quality_native_match_timing_plan": timing_plan.snapshot(),
        "quality_native_match_timing_plan_digest": timing_plan.digest(),
    }
    monkeypatch.setattr(
        pipeline_state,
        "PIPELINE_RUNTIME_HEARTBEAT_FILE",
        heartbeat_file,
    )
    # There is deliberately no ambient test/manual heartbeat authority.
    assert pipeline_state.make_native_match_heartbeat_reporter(
        checkpoint,
        owner_tool="run_quality_gates",
    ) is None

    nonce_a = "a" * 32
    nonce_b = "b" * 32
    token_a = pipeline_state.activate_native_match_dispatch_nonce(nonce_a)
    try:
        reporter_a = pipeline_state.make_native_match_heartbeat_reporter(
            checkpoint,
            owner_tool="run_quality_gates",
        )
    finally:
        pipeline_state.reset_native_match_dispatch_nonce(token_a)
    token_b = pipeline_state.activate_native_match_dispatch_nonce(nonce_b)
    try:
        reporter_b = pipeline_state.make_native_match_heartbeat_reporter(
            checkpoint,
            owner_tool="run_quality_gates",
        )
    finally:
        pipeline_state.reset_native_match_dispatch_nonce(token_b)
    assert reporter_a is not None and reporter_b is not None

    now = __import__("time").time()
    phase_budget_us = timing_plan.launch_timeout_us

    def launching(identity):
        return {
            "event_type": "launching",
            "hand": None,
            "hands": 70,
            "timing_plan_digest": timing_plan.digest(),
            "match_identity_digest": identity,
            "liveness_phase": "launching",
            "phase_budget_us": phase_budget_us,
            "operation_started_at_epoch": now,
            "operation_deadline_epoch": (
                now + timing_plan.first_strict_lease_timeout_us / 1_000_000.0
            ),
            "operation_budget_us": timing_plan.first_strict_lease_timeout_us,
            "phase_started_at_epoch": now,
            "phase_deadline_epoch": now + phase_budget_us / 1_000_000.0,
            "effective_timeout_us": timing_plan.effective_timeout_us,
        }

    try:
        assert asyncio.run(reporter_a(launching("c" * 64))) is True
        assert asyncio.run(reporter_b(launching("d" * 64))) is True

        # A terminal callback from the first match has a valid checkpoint but
        # not the currently persisted dispatch/match identity, so it fails
        # closed instead of unlinking the second sidecar.
        assert asyncio.run(reporter_a({
            "event_type": "terminal",
            "terminal": True,
            "terminal_outcome": "runner_returned",
            "match_identity_digest": "c" * 64,
            "timing_plan_digest": timing_plan.digest(),
        })) is False
        assert not pipeline_state.native_match_dispatch_nonce_is_active(nonce_a)
        assert pipeline_state.native_match_dispatch_nonce_is_active(nonce_b)
        live = pipeline_state.read_pipeline_native_match_progress(
            checkpoint,
            max_age=30,
            provider_dispatch_nonce=nonce_b,
        )
        assert live is not None
        assert live["match_identity_digest"] == "d" * 64
        assert live["hand"] is None

        import orchestrator

        monkeypatch.setattr(
            orchestrator,
            "_read_active_pipeline_checkpoint",
            lambda: checkpoint,
        )
        monkeypatch.setattr(
            orchestrator,
            "ORCH_NATIVE_MATCH_MAX_EXTENSION_SEC",
            timing_plan.first_strict_lease_timeout_us / 1_000_000.0,
        )
        extension = orchestrator._bounded_native_match_extension(
            stream_started_epoch=now - 10.0,
            original_deadline_epoch=now + 0.1,
            provider_dispatch_nonce=nonce_b,
        )
        assert extension is not None
        assert extension["progress"]["event_type"] == "launching"
        assert extension["deadline_epoch"] == pytest.approx(
            live["phase_deadline_epoch"]
        )

        # Launching is the sole phase without a real hand.  Old schema-3
        # launch payloads, a fabricated hand, and handless engine progress all
        # fail closed rather than borrowing the launch budget.
        assert pipeline_state.validate_native_match_progress(
            checkpoint,
            {**live, "schema_version": 3},
            provider_dispatch_nonce=nonce_b,
        ) is None
        assert pipeline_state.validate_native_match_progress(
            checkpoint,
            {**live, "hand": 1},
            provider_dispatch_nonce=nonce_b,
        ) is None
        assert pipeline_state.validate_native_match_progress(
            checkpoint,
            {
                **live,
                "event_type": "engine_started",
                "liveness_phase": "engine_running",
                "phase_budget_us": timing_plan.effective_timeout_us,
            },
            provider_dispatch_nonce=nonce_b,
        ) is None

        # Cancellation/revocation of the old SDK dispatch similarly cannot
        # remove the newer dispatch's sidecar.
        assert pipeline_state.revoke_native_match_dispatch_nonce(nonce_a) is False
        assert pipeline_state.read_pipeline_native_match_progress(
            checkpoint,
            max_age=30,
            provider_dispatch_nonce=nonce_b,
        ) is not None
    finally:
        pipeline_state.revoke_native_match_dispatch_nonce(nonce_a)
        pipeline_state.revoke_native_match_dispatch_nonce(nonce_b)
