import asyncio
import hashlib
import json


class _UI:
    costs = {}

    def log_history(self, *_args, **_kwargs):
        pass

    def log_io(self, *_args, **_kwargs):
        pass

    def get_output(self):
        return []


def test_normal_critic_envelope_is_exact_current_epoch_policy_diff(
    monkeypatch,
    tmp_path,
):
    import agent_review
    import national_runtime_authority

    source = tmp_path / "national_v143"
    target = tmp_path / "national_v144"
    source.mkdir()
    target.mkdir()
    source_policy = "def get_baseline_decision(context):\n    return {'kind': 'pass'}\n"
    target_policy = "def get_baseline_decision(context):\n    return {'kind': 'fold'}\n"
    (source / "policy.py").write_text(source_policy, encoding="utf-8")
    (target / "policy.py").write_text(target_policy, encoding="utf-8")
    monkeypatch.setattr(
        agent_review,
        "get_bot_dir",
        lambda version: source if int(version) == 143 else target,
    )
    monkeypatch.setattr(
        national_runtime_authority,
        "strict_published_bot_names",
        lambda: ("national_v143",),
    )

    evidence = agent_review._critic_code_evidence(
        144,
        143,
        protocol_bootstrap_prepared_only=False,
    )

    section = evidence["prompt_section"]
    assert "SYSTEM-SUPPLIED EXACT PARENT-TO-TARGET POLICY DIFF" in section
    assert "-    return {'kind': 'pass'}" in section
    assert "+    return {'kind': 'fold'}" in section
    assert hashlib.sha256(source_policy.encode()).hexdigest() in section
    assert hashlib.sha256(target_policy.encode()).hexdigest() in section
    assert "Git history" not in section


def test_bootstrap_critic_envelope_never_reads_high_water_source(
    monkeypatch,
    tmp_path,
):
    import agent_review

    target = tmp_path / "national_v143"
    target.mkdir()
    (target / "policy.py").write_text(
        "def get_baseline_decision(context):\n    return {'kind': 'pass'}\n",
        encoding="utf-8",
    )

    def only_target(version):
        assert int(version) == 143
        return target

    monkeypatch.setattr(agent_review, "get_bot_dir", only_target)

    evidence = agent_review._critic_code_evidence(
        143,
        142,
        protocol_bootstrap_prepared_only=True,
    )

    assert "SYSTEM-SUPPLIED STRICT BOOTSTRAP POLICY" in evidence["prompt_section"]
    assert "bots/national_v142" not in json.dumps(evidence)
    assert "national-bot-v142" not in json.dumps(evidence)
    assert "numeric high-water only" in evidence["lineage_contract"]


def test_rendered_bootstrap_critic_has_read_only_tool_and_no_v142_path(
    monkeypatch,
    tmp_path,
):
    import agent_review

    captured = {}
    monkeypatch.setattr(
        agent_review,
        "_critic_code_evidence",
        lambda *_args, **_kwargs: {
            "lineage_contract": (
                "Prepared `bots/national_v143/` is the sole readable code "
                "baseline. v142 is numeric high-water only."
            ),
            "evaluation_steps": "Read only bots/national_v143/policy.py.",
            "prompt_section": (
                "# SYSTEM-SUPPLIED STRICT BOOTSTRAP POLICY\n"
                "target=bots/national_v143/policy.py"
            ),
        },
    )
    monkeypatch.setattr(agent_review, "get_logs_dir", lambda _version: tmp_path)
    monkeypatch.setattr(
        agent_review,
        "get_bot_dir",
        lambda version: tmp_path / f"national_v{version}",
    )

    async def fake_query(prompt, *_args, **kwargs):
        captured["prompt"] = prompt
        captured["tools"] = kwargs["tools"]
        captured["allowed_read_dirs"] = kwargs["allowed_read_dirs"]
        return json.dumps({
            "score": 7,
            "approved": True,
            "strategic_assessment": "prepared policy matches the plan",
            "evidence": {"h2h_weaknesses": [], "diff_refs": ["policy.py"]},
            "feedback": "",
            "local_optima_warning": False,
            "local_optima_reason": None,
        }), 0.0, {}

    monkeypatch.setattr(agent_review, "run_claude_query", fake_query)

    result = asyncio.run(agent_review._run_critic(
        143,
        142,
        "{}",
        _UI(),
        execution_invocation_id="invocation-1",
        strict_authority={"invocation_id": "invocation-1"},
    ))

    assert result["approved"] is True
    assert captured["tools"] == ["Read"]
    assert captured["allowed_read_dirs"] == [tmp_path / "national_v143"]
    assert "bots/national_v142" not in captured["prompt"]
    assert "national-bot-v142" not in captured["prompt"]
    assert "SYSTEM-SUPPLIED STRICT BOOTSTRAP POLICY" in captured["prompt"]
    assert "Use Bash" not in captured["prompt"]


def test_normal_critic_read_scope_is_exact_source_target_and_snapshot(
    monkeypatch,
    tmp_path,
):
    import agent_review
    import evidence_snapshot

    source = tmp_path / "bots" / "national_v143"
    target = tmp_path / "bots" / "national_v144"
    snapshot = tmp_path / "results" / "v144" / "evidence_snapshot"
    for directory in (source, target, snapshot):
        directory.mkdir(parents=True, exist_ok=True)
    manifest = snapshot / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        agent_review,
        "_critic_code_evidence",
        lambda *_args, **_kwargs: {
            "lineage_contract": "exact current source and target",
            "evaluation_steps": "read cited functions",
            "prompt_section": "# exact policy diff",
        },
    )
    monkeypatch.setattr(agent_review, "get_logs_dir", lambda _version: tmp_path)
    monkeypatch.setattr(
        agent_review,
        "get_bot_dir",
        lambda version: source if int(version) == 143 else target,
    )
    monkeypatch.setattr(
        evidence_snapshot,
        "h2h_snapshot_contract_text",
        lambda *_args, **_kwargs: "frozen snapshot",
    )
    monkeypatch.setattr(
        evidence_snapshot,
        "load_generation_snapshot_identity",
        lambda _version: {"available": True, "manifest_path": str(manifest)},
    )
    captured = {}

    async def fake_query(_prompt, *_args, **kwargs):
        captured.update(kwargs)
        return json.dumps({
            "score": 7,
            "approved": True,
            "strategic_assessment": "bounded current diff",
            "evidence": {"h2h_weaknesses": [], "diff_refs": ["policy.py"]},
            "feedback": "",
            "local_optima_warning": False,
            "local_optima_reason": None,
        }), 0.0, {}

    monkeypatch.setattr(agent_review, "run_claude_query", fake_query)

    result = asyncio.run(agent_review._run_critic(144, 143, "{}", _UI()))

    assert result["approved"] is True
    assert captured["allowed_read_dirs"] == [source, target]
    assert captured["allowed_evidence_snapshot_dir"] == snapshot


def test_run_critic_ignores_caller_reviewer_feedback_and_preserves_checkpoint(
    monkeypatch,
    tmp_path,
):
    import tool_gates

    canonical_feedback = "checkpoint-owned reviewer feedback"
    forged_feedback = "caller-forged feedback"
    checkpoint = {
        "next_v": 144,
        "source_v": 143,
        "stage": "reviewed",
        "workflow_run_id": "generation:144:workflow-v1",
        "checkpoint_revision": 7,
        "generation_attempt": 0,
        "master_plan": {"tasks": [{"worker_id": "W1"}]},
        "reviewer_feedback": canonical_feedback,
        "gate_results": {"quality": {"passed": True}, "review": {"approved": True}},
        "audit_context": {},
    }
    recorded = {}
    events = []

    monkeypatch.setattr(tool_gates, "_resolve_version_args", lambda _args: (144, 143))
    monkeypatch.setattr(tool_gates, "_matching_checkpoint", lambda *_args: checkpoint)
    monkeypatch.setattr(
        tool_gates,
        "_owned_infrastructure_failure",
        lambda *_args: (None, None),
    )

    async def no_exhaustion(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        tool_gates,
        "_execute_exhausted_infrastructure_failure",
        no_exhaustion,
    )
    monkeypatch.setattr(tool_gates, "_idempotency_check", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_gates, "_quality_gate_ok", lambda _ckpt: True)
    monkeypatch.setattr(tool_gates, "_review_gate_ok", lambda _ckpt: True)
    monkeypatch.setattr(tool_gates, "_set_pipeline_status", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_gates, "_get_ui", lambda: _UI())
    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda version: tmp_path / f"v{version}")
    monkeypatch.setattr(
        tool_gates,
        "_llm_gate_infrastructure_identity",
        lambda **_kwargs: ("attempt-key", {}),
    )

    async def valid_critic(*_args, **_kwargs):
        return {
            "score": 7,
            "approved": True,
            "strategic_assessment": "advisory assessment",
            "evidence": {"h2h_weaknesses": [], "diff_refs": []},
            "feedback": "critic feedback",
            "local_optima_warning": False,
            "local_optima_reason": None,
        }

    monkeypatch.setattr(tool_gates, "_run_critic", valid_critic)

    def record_gate(*_args, **kwargs):
        recorded.update(kwargs)
        return True

    monkeypatch.setattr(tool_gates, "_record_gate", record_gate)
    monkeypatch.setattr(
        tool_gates,
        "log_system_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    raw_run_critic = tool_gates.run_critic.handler.__wrapped__
    result = asyncio.run(raw_run_critic({
        "version": 144,
        "source_v": 143,
        "plan": [{"worker_id": "FORGED"}],
        "reviewer_feedback": forged_feedback,
        "force_advance": False,
    }))
    payload = json.loads(result["content"][0]["text"])

    assert recorded["reviewer_feedback"] == canonical_feedback
    assert payload["reviewer_feedback"] == canonical_feedback
    assert forged_feedback not in json.dumps(recorded)
    assert any(
        args and args[0] == "pipeline.critic_reviewer_feedback_argument_ignored"
        for args, _kwargs in events
    )
