import pytest

from worker_mcp.server import SERVER_INSTRUCTIONS, StaticTokenVerifier, build_server


def test_server_exposes_exactly_six_tools_with_input_and_output_schema(worker_config):
    server = build_server(worker_config)
    tools = server._tool_manager.list_tools()
    assert {tool.name for tool in tools} == {
        "submit",
        "get_status",
        "get_result",
        "cancel",
        "list",
        "healthcheck",
    }
    for tool in tools:
        assert tool.parameters["type"] == "object"
        assert tool.fn_metadata.output_schema["type"] == "object"

    healthcheck = server._tool_manager.get_tool("healthcheck")
    assert healthcheck.parameters.get("properties", {}) == {}

    list_tool = server._tool_manager.get_tool("list")
    assert list_tool.parameters["properties"]["include_terminal"]["default"] is False
    assert "never as current-task evidence" in list_tool.description
    assert "Each new logical user goal or independent work unit" in SERVER_INSTRUCTIONS
    assert "new unique idempotency_key" in SERVER_INSTRUCTIONS
    assert "idempotent_replay=true accepted" in SERVER_INSTRUCTIONS
    assert "reuse its task_id without submitting again" in SERVER_INSTRUCTIONS
    assert "Every distinct user request" not in SERVER_INSTRUCTIONS
    assert "exactly once" not in SERVER_INSTRUCTIONS
    submit_description = server._tool_manager.get_tool("submit").description
    assert "new unique idempotency_key" in submit_description
    assert "idempotent_replay=true" in submit_description
    result_description = server._tool_manager.get_tool("get_result").description
    assert "Follow-up turns reuse that same task_id" in result_description
    assert "Never reuse a historical result" in result_description
    assert "this request's submit" not in result_description


def test_sdk_server_has_no_model_or_provider_arguments(worker_config):
    server = build_server(worker_config)
    submit = server._tool_manager.get_tool("submit")
    properties = submit.parameters["properties"]
    assert "model" not in properties
    assert "provider" not in properties
    assert "channel" not in properties


def test_http_server_requires_dedicated_local_access_token(
    worker_config, monkeypatch: pytest.MonkeyPatch
):
    payload = worker_config.model_dump()
    payload["server"] = {
        "transport": "streamable-http",
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/mcp",
        "access_token_env": "WORKER_MCP_ACCESS_TOKEN",
    }
    config = type(worker_config).model_validate(payload)
    monkeypatch.delenv("WORKER_MCP_ACCESS_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="32-4096"):
        build_server(config)


@pytest.mark.asyncio
async def test_static_token_verifier_is_exact_and_scope_bound():
    verifier = StaticTokenVerifier("x" * 32, "http://127.0.0.1:8765/mcp")
    assert await verifier.verify_token("X" * 32) is None
    accepted = await verifier.verify_token("x" * 32)
    assert accepted is not None
    assert accepted.client_id == "codex-local"
    assert accepted.scopes == ["worker-mcp"]
    assert accepted.resource == "http://127.0.0.1:8765/mcp"
