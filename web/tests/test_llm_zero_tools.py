import ast
import asyncio
from pathlib import Path

from bot_namespace import bot_name, bot_tag


class _UI:
    def log_history(self, *_args, **_kwargs):
        return None

    def log_io(self, *_args, **_kwargs):
        return None

    def emit_tool_call(self, *_args, **_kwargs):
        return None

    def update_cost(self, *_args, **_kwargs):
        return None


def test_tools_none_reaches_options_and_cli_as_explicit_zero_tools(
    monkeypatch,
    tmp_path,
):
    import llm_availability_store
    import llm_query
    import orchestrator_cost_policy
    import rate_limiter
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    captured = {}
    events = []

    async def fake_stream(full_prompt, options, log_file_path, ui, role_name):
        captured["prompt"] = full_prompt
        captured["options"] = options
        return ["ok"], 0.0, {}

    monkeypatch.setattr(
        orchestrator_cost_policy,
        "assert_operator_cost_limit_available",
        lambda: None,
    )
    monkeypatch.setattr(
        llm_availability_store,
        "raise_if_llm_paused",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)
    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", fake_stream)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )
    import cycle_archivist

    snapshot = cycle_archivist._cycle_archivist_prompt_projection(
        {
            "evaluation_epoch": "national_tcp_policy_v1",
            "bot_name": bot_name(149),
            "git_tag": bot_tag(149),
            "publication_identity": {
                "publication_id": "1" * 64,
                "commit_oid": "2" * 40,
                "candidate_artifact_hash": "3" * 64,
            },
            "strength_evidence_identity": {"marker": "zero tool audit"},
            "review_score": 9,
            "critic_score": 8,
            "precommit_passed": True,
            "post_publication_handoff": {
                "identity_digest": "4" * 64,
                "publication_id": "1" * 64,
            },
        },
        version=149,
        source_v=143,
    )
    rendered_prompt = llm_query.render_llm_prompt(
        "CYCLE ARCHIVIST",
        producer=cycle_archivist._render_cycle_archivist_provider_prompt,
        renderer_inputs={
            "snapshot": snapshot,
            "version": 149,
            "source_v": 143,
        },
    )

    output, _cost, _usage = asyncio.run(
        llm_query.run_claude_query(
            rendered_prompt,
            [],
            _UI(),
            "CYCLE ARCHIVIST",
            tmp_path / "zero_tool_io.txt",
        )
    )

    assert output == "ok"
    options = captured["options"]
    assert options.permission_mode == "bypassPermissions"
    assert options.tools == []
    assert options.mcp_servers == {}
    assert options.strict_mcp_config is True
    start = next(fields for category, _severity, _message, fields in events
                 if category == "pipeline.llm_role_start")
    assert start["tools"] == []

    transport = SubprocessCLITransport(
        prompt=captured["prompt"],
        options=options,
    )
    transport._cli_path = "/synthetic/claude"
    command = transport._build_command()
    tools_index = command.index("--tools")
    assert command[tools_index + 1] == ""
    assert command.count("--tools") == 1
    assert "--allowedTools" not in command
    assert "--mcp-config" not in command
    assert "--strict-mcp-config" in command
    permission_index = command.index("--permission-mode")
    assert command[permission_index + 1] == "bypassPermissions"


def test_six_audit_and_analyst_calls_declare_empty_tools():
    core_dir = Path(__file__).resolve().parents[1] / "core"
    expected_counts = {
        "direction_auditor.py": 1,
        "audit_agents.py": 4,
        "combined_analyst.py": 1,
    }
    total = 0

    for filename, expected_count in expected_counts.items():
        tree = ast.parse((core_dir / filename).read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_claude_query"
        ]
        assert len(calls) == expected_count
        total += len(calls)
        for call in calls:
            tools_keyword = next(
                (keyword for keyword in call.keywords if keyword.arg == "tools"),
                None,
            )
            assert tools_keyword is not None
            assert isinstance(tools_keyword.value, ast.List)
            assert tools_keyword.value.elts == []

    assert total == 6
