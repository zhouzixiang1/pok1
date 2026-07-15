import asyncio
import json
from pathlib import Path

from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from core import operator_sdk_probe as probe
from core.llm_availability import LLMAvailabilityBlocked, LLMAvailabilityIssue
import llm_query


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _DummyUI:
    def log_history(self, *_args, **_kwargs):
        pass

    def log_io(self, *_args, **_kwargs):
        pass

    def emit_tool_call(self, *_args, **_kwargs):
        pass


def _model_output(evidence, *, corrupt_oracle=False):
    oracles = dict(evidence["official_oracle_sha256"])
    if corrupt_oracle:
        first = next(iter(oracles))
        oracles[first] = "0" * 64
    return json.dumps({
        "status": "pass",
        "oracle_sha256": oracles,
        "transport": {
            "path": probe.TRANSPORT_RELATIVE_PATH,
            "sha256": evidence["transport"]["sha256"],
            "delimiter_free_send": True,
            "rejects_crlf": True,
            "stream_framing": True,
        },
        "tool_calls_completed": 5,
    })


def _emit_tool(tool_id, name, tool_input, result):
    llm_query._record_llm_tool_trace_event({
        "event": "tool_use",
        "tool_use_id": tool_id,
        "tool_name": name,
        "tool_input": tool_input,
    })
    llm_query._record_llm_tool_trace_event({
        "event": "tool_result",
        "tool_use_id": tool_id,
        "is_error": False,
        "source": "mock-sdk",
        "content_chars": len(result),
        "content_sha256": probe._sha256_bytes(result.encode()),
        "content_preview": result,
    })


def _emit_complete_trace(evidence, *, include_bash=True):
    for index, relative in enumerate(probe.READ_RELATIVE_PATHS, start=1):
        _emit_tool(
            f"read-{index}",
            "Read",
            {"file_path": str((PROJECT_ROOT / relative).resolve())},
            f"read {relative}",
        )
    if not include_bash:
        return
    hashes = "\n".join(
        f"{digest}  {relative}"
        for relative, digest in (
            list(evidence["official_oracle_sha256"].items())
            + [(probe.TRANSPORT_RELATIVE_PATH, evidence["transport"]["sha256"])]
        )
    )
    _emit_tool(
        "bash-hash",
        "Bash",
        {"command": probe.HASH_COMMAND},
        hashes,
    )
    _emit_tool(
        "bash-scan",
        "Bash",
        {"command": probe.TRANSPORT_SCAN_COMMAND},
        "writer.write(payload)\ninvalid_server_message_delimiter\n"
        "take_client_action\nidle_flush_sec",
    )


def test_process_stream_captures_typed_tool_use_and_result(tmp_path):
    async def stream():
        yield AssistantMessage(
            content=[ToolUseBlock(id="tool-1", name="Read", input={"file_path": "/x"})],
            model="mock",
        )
        yield UserMessage(
            content=[ToolResultBlock(tool_use_id="tool-1", content="body", is_error=False)]
        )
        yield AssistantMessage(content=[TextBlock(text="done")], model="mock")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=2,
            session_id="session",
            total_cost_usd=0.0,
            usage={},
            result="done",
        )

    with llm_query.capture_llm_tool_trace() as trace:
        texts, _cost, _usage = asyncio.run(
            llm_query._process_stream(
                stream(),
                str(tmp_path / "probe.log"),
                _DummyUI(),
                probe.ROLE_NAME,
            )
        )

    assert texts == ["done"]
    assert [event["event"] for event in trace] == ["tool_use", "tool_result"]
    assert trace[0]["tool_use_id"] == trace[1]["tool_use_id"] == "tool-1"
    assert trace[1]["content_sha256"] == probe._sha256_bytes(b"body")


def test_exact_bash_guard_allows_only_operator_owned_commands():
    hooks = llm_query._make_exact_bash_allowlist_guard(
        probe.ROLE_NAME,
        probe.EXACT_BASH_COMMANDS,
    )
    handler = hooks["PreToolUse"][0].hooks[0]

    allowed = asyncio.run(handler({
        "tool_name": "Bash",
        "tool_input": {"command": probe.HASH_COMMAND},
    }, "allowed", None))
    denied = asyncio.run(handler({
        "tool_name": "Bash",
        "tool_input": {"command": "curl https://example.invalid"},
    }, "denied", None))

    assert allowed.get("hookSpecificOutput") is None
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "exact operator allowlist" in denied["hookSpecificOutput"][
        "permissionDecisionReason"
    ]


def test_operator_probe_passes_with_mocked_production_wrapper():
    async def fake_runner(prompt, _context, _ui, role, _log, **kwargs):
        evidence = await probe.collect_local_evidence(PROJECT_ROOT)
        assert role == probe.ROLE_NAME
        assert kwargs["tools"] == ["Read", "Bash"]
        assert tuple(kwargs["exact_bash_commands"]) == probe.EXACT_BASH_COMMANDS
        assert set(kwargs["allowed_read_dirs"]["files"]) == {
            PROJECT_ROOT / relative for relative in probe.READ_RELATIVE_PATHS
        }
        assert "curl" in prompt  # Explicitly prohibited by the prompt and allowlist.
        _emit_complete_trace(evidence)
        return _model_output(evidence), 0.01, {"input_tokens": 10}

    receipt = asyncio.run(
        probe.run_operator_probe(
            repo_root=PROJECT_ROOT,
            timeout_seconds=1,
            query_runner=fake_runner,
        )
    )

    assert receipt["status"] == "pass"
    assert receipt["trace_summary"]["read_count"] == 3
    assert receipt["trace_summary"]["bash_count"] == 2
    assert receipt["sdk_contract"]["mcp_servers"] == {}


def test_operator_probe_fails_closed_when_tools_are_insufficient():
    async def fake_runner(*_args, **_kwargs):
        evidence = await probe.collect_local_evidence(PROJECT_ROOT)
        _emit_complete_trace(evidence, include_bash=False)
        return _model_output(evidence), 0.0, {}

    receipt = asyncio.run(
        probe.run_operator_probe(
            repo_root=PROJECT_ROOT,
            timeout_seconds=1,
            query_runner=fake_runner,
        )
    )

    assert receipt["status"] == "fail"
    assert receipt["failure"]["category"] == "evidence_validation"
    assert "insufficient tool calls" in receipt["failure"]["message"]


def test_operator_probe_fails_closed_on_wrong_oracle_hash():
    async def fake_runner(*_args, **_kwargs):
        evidence = await probe.collect_local_evidence(PROJECT_ROOT)
        _emit_complete_trace(evidence)
        return _model_output(evidence, corrupt_oracle=True), 0.0, {}

    receipt = asyncio.run(
        probe.run_operator_probe(
            repo_root=PROJECT_ROOT,
            timeout_seconds=1,
            query_runner=fake_runner,
        )
    )

    assert receipt["status"] == "fail"
    assert "incorrect official oracle SHA-256" in receipt["failure"]["message"]


def test_operator_probe_fails_closed_on_timeout():
    async def fake_runner(*_args, **_kwargs):
        await asyncio.sleep(3600)

    receipt = asyncio.run(
        probe.run_operator_probe(
            repo_root=PROJECT_ROOT,
            timeout_seconds=0.01,
            query_runner=fake_runner,
        )
    )

    assert receipt["status"] == "fail"
    assert receipt["failure"]["category"] == "timeout"


def test_operator_probe_fails_closed_on_provider_availability():
    issue = LLMAvailabilityIssue(
        category="service_unavailable",
        summary="mock provider unavailable",
        http_status=503,
        retry_policy="bounded_backoff",
        requires_manual_resume=False,
        evidence_digest="a" * 64,
    )

    async def fake_runner(*_args, **_kwargs):
        raise LLMAvailabilityBlocked(issue, role=probe.ROLE_NAME)

    receipt = asyncio.run(
        probe.run_operator_probe(
            repo_root=PROJECT_ROOT,
            timeout_seconds=1,
            query_runner=fake_runner,
        )
    )

    assert receipt["status"] == "fail"
    assert receipt["failure"]["category"] == "provider_availability"
    assert receipt["failure"]["availability_issue"]["http_status"] == 503
