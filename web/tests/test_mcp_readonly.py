"""Tests for read-only MCP tools via POST /api/control/tool/{name}."""

import json


class TestGetStatus:
    def test_returns_status(self, client):
        resp = client.post("/api/control/tool/get_status", json={"args": {}})
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        result = json.loads(data["result"])
        assert "current_v" in result
        assert "next_v" in result
        assert "active_bots_count" in result
        assert isinstance(result["active_bots_count"], int)
        assert result["active_bots_count"] > 0


class TestGetBotInfo:
    def test_found(self, client):
        resp = client.post("/api/control/tool/get_bot_info", json={"args": {"version": 30}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert result["exists"] is True
        assert result["version"] == 30
        assert "files" in result

    def test_missing(self, client):
        resp = client.post("/api/control/tool/get_bot_info", json={"args": {"version": 9999}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert "error" in result

    def test_graveyard_bot(self, client):
        resp = client.post("/api/control/tool/get_bot_info", json={"args": {"version": 31}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert result["exists"] is True


class TestGetMatchHistory:
    def test_basic(self, client):
        resp = client.post("/api/control/tool/get_match_history",
                           json={"args": {"version": 30, "n": 3}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert "matches" in result

    def test_no_matches(self, client):
        resp = client.post("/api/control/tool/get_match_history",
                           json={"args": {"version": 9999, "n": 5}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert result["matches"] == []


class TestGetH2H:
    def test_with_opponent(self, client):
        resp = client.post("/api/control/tool/get_h2h",
                           json={"args": {"bot_name": "claude_v30", "opponent": "claude_v29"}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert "opponents" in result

    def test_all_opponents(self, client):
        resp = client.post("/api/control/tool/get_h2h",
                           json={"args": {"bot_name": "claude_v30"}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert "opponents" in result


class TestGetBotStats:
    def test_found(self, client):
        resp = client.post("/api/control/tool/get_bot_stats",
                           json={"args": {"bot_name": "claude_v30"}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert result["bot_name"] == "claude_v30"
        assert "games" in result

    def test_missing(self, client):
        resp = client.post("/api/control/tool/get_bot_stats",
                           json={"args": {"bot_name": "nonexistent"}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert "error" in result
