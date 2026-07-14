"""The Orchestrator MCP registry is never exposed as an HTTP executor."""

import pytest


@pytest.mark.parametrize(
    "name",
    [
        "get_status",
        "get_bot_info",
        "get_match_history",
        "get_h2h",
        "get_bot_stats",
        "prepare_next_gen",
        "run_crossover",
        "execute_workers",
        "run_quality_gates",
        "run_precommit_eval",
        "commit_bot",
        "abandon_generation",
        "cleanup_incomplete",
        "reap_incomplete",
        "start_daemon",
        "stop_daemon",
    ],
)
def test_mcp_tool_cannot_be_called_over_control_http(client, name):
    response = client.post(
        f"/api/control/tool/{name}",
        json={"args": {"version": 143, "force": True}},
    )

    assert response.status_code == 410
    assert response.json()["detail"] == {
        "code": "control_tool_executor_retired",
        "tool": name,
        "message": "Use explicit read-only APIs or operator control endpoints.",
    }


def test_control_catalog_contains_no_mcp_registry_names(client):
    catalog = client.get("/api/control/tools").json()
    ids = set(catalog["tools"])

    assert "get_status" not in ids
    assert "prepare_next_gen" not in ids
    assert "commit_bot" not in ids
    assert all(item["path"].startswith("/api/control/") for item in catalog["capabilities"])
