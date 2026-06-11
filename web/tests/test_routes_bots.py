"""Tests for /api/bots/* endpoints."""

import pytest


class TestListBots:
    def test_default(self, client):
        resp = client.get("/api/bots")
        assert resp.status_code == 200
        data = resp.json()
        assert "active" in data
        assert "graveyard" in data
        assert isinstance(data["active"], list)
        assert data["graveyard"] == []

    def test_with_graveyard(self, client):
        resp = client.get("/api/bots?include_graveyard=true")
        assert resp.status_code == 200
        data = resp.json()
        assert "graveyard" in data
        for bot in data["active"]:
            assert "name" in bot
            assert "version" in bot
            assert "completed" in bot
            assert "files" in bot


class TestBotDetail:
    @pytest.mark.requires_active_bot
    def test_found(self, client, active_bot_version):
        resp = client.get(f"/api/bots/{active_bot_version}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == f"claude_v{active_bot_version}"
        assert data["version"] == active_bot_version
        assert "files" in data
        assert "total_lines" in data

    def test_404(self, client):
        resp = client.get("/api/bots/9999")
        assert resp.status_code == 404


@pytest.mark.requires_active_bot
class TestBotCode:
    def test_read_main(self, client, active_bot_version):
        resp = client.get(f"/api/bots/{active_bot_version}/code/main.py")
        assert resp.status_code == 200
        assert "def " in resp.text or "import " in resp.text

    def test_invalid_filename(self, client, active_bot_version):
        resp = client.get(f"/api/bots/{active_bot_version}/code/../etc/passwd")
        assert resp.status_code == 404

    def test_non_py_file(self, client, active_bot_version):
        resp = client.get(f"/api/bots/{active_bot_version}/code/main.txt")
        assert resp.status_code == 400

    def test_404(self, client, active_bot_version):
        resp = client.get(f"/api/bots/{active_bot_version}/code/nonexistent.py")
        assert resp.status_code == 404

    def test_backslash_blocked(self, client, active_bot_version):
        resp = client.get(f"/api/bots/{active_bot_version}/code/..\\etc")
        assert resp.status_code == 400
