from worker_mcp.server import build_server


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
