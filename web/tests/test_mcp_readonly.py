"""Tests for read-only MCP tools via POST /api/control/tool/{name}."""

import json

import pytest


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
    @pytest.mark.requires_active_bot
    def test_found(self, client, active_bot_version):
        resp = client.post("/api/control/tool/get_bot_info", json={"args": {"version": active_bot_version}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert result["exists"] is True
        assert result["version"] == active_bot_version
        assert "files" in result

    def test_missing(self, client):
        resp = client.post("/api/control/tool/get_bot_info", json={"args": {"version": 9999}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert "error" in result

    @pytest.mark.requires_graveyard_bot
    def test_graveyard_bot(self, client, graveyard_bot_version):
        resp = client.post("/api/control/tool/get_bot_info", json={"args": {"version": graveyard_bot_version}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert result["exists"] is True


class TestGetMatchHistory:
    @pytest.mark.requires_active_bot
    def test_basic(self, client, active_bot_version):
        resp = client.post("/api/control/tool/get_match_history",
                           json={"args": {"version": active_bot_version, "n": 3}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert "matches" in result

    def test_no_matches(self, client):
        resp = client.post("/api/control/tool/get_match_history",
                           json={"args": {"version": 9999, "n": 5}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert result["matches"] == []


@pytest.mark.requires_active_bot
class TestGetH2H:
    def test_with_opponent(self, client, active_bot_version):
        bot_name = f"claude_v{active_bot_version}"
        resp = client.post("/api/control/tool/get_h2h",
                           json={"args": {"bot_name": bot_name}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert "opponents" in result

    def test_all_opponents(self, client, active_bot_version):
        resp = client.post("/api/control/tool/get_h2h",
                           json={"args": {"bot_name": f"claude_v{active_bot_version}"}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert "opponents" in result


class TestGetBotStats:
    @pytest.mark.requires_active_bot
    def test_found(self, client, active_bot_version):
        bot_name = f"claude_v{active_bot_version}"
        resp = client.post("/api/control/tool/get_bot_stats",
                           json={"args": {"bot_name": bot_name}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert result["bot_name"] == bot_name
        assert "games" in result

    def test_missing(self, client):
        resp = client.post("/api/control/tool/get_bot_stats",
                           json={"args": {"bot_name": "nonexistent"}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert "error" in result
