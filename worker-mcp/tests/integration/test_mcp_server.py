import pytest

from worker_mcp.server import StaticTokenVerifier, build_server


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
