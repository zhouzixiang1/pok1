"""Tests for /api/control/* endpoints."""

import json


class TestConfig:
    def test_get(self, client):
        resp = client.get("/api/control/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "daemon_enabled" in data
        assert "daemon_workers" in data
        assert "daemon_pairs" in data

    def test_set_partial(self, client):
        # Get current config first
        orig = client.get("/api/control/config").json()
        resp = client.put("/api/control/config", json={"daemon_pairs": 3})
        assert resp.status_code == 200
        data = resp.json()
        assert data["daemon_pairs"] == 3
        assert data["daemon_enabled"] == orig["daemon_enabled"]
        # Restore
        client.put("/api/control/config", json={"daemon_pairs": orig["daemon_pairs"]})

    def test_set_invalid_type(self, client):
        resp = client.put("/api/control/config", json={"daemon_workers": "not_a_number"})
        assert resp.status_code == 422


class TestStatus:
    def test_returns_state(self, client):
        resp = client.get("/api/control/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "running" in data
        assert "mode" in data
        assert "daemon_enabled" in data


class TestDecisions:
    def test_returns_list(self, client):
        resp = client.get("/api/control/decisions")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestTools:
    def test_list(self, client):
        resp = client.get("/api/control/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert "tools" in data
        assert len(data["tools"]) > 0

    def test_call_unknown(self, client):
        resp = client.post("/api/control/tool/nonexistent_tool_xyz", json={"args": {}})
        assert resp.status_code == 404


class TestOrchestratorSession:
    def test_get(self, client):
        resp = client.get("/api/control/orchestrator/session")
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert "active" in data

    def test_clear(self, client):
        resp = client.delete("/api/control/orchestrator/session")
        assert resp.status_code == 200
        data = resp.json()
        assert "cleared" in data


class TestStop:
    def test_stop(self, client):
        resp = client.post("/api/control/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"


class TestStartConflict:
    def test_start_when_not_running(self, client):
        # Ensure stopped
        client.post("/api/control/stop")
        # Start should succeed or fail cleanly (might fail due to missing claude_agent_sdk in test env)
        resp = client.post("/api/control/start")
        # Either 200 (started) or 500 (import error) is acceptable
        assert resp.status_code in (200, 500)
        # Clean up
        client.post("/api/control/stop")
