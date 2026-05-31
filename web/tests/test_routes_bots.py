"""Tests for /api/bots/* endpoints."""


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
        # Graveyard may or may not be populated
        for bot in data["active"]:
            assert "name" in bot
            assert "version" in bot
            assert "completed" in bot
            assert "files" in bot


class TestBotDetail:
    def test_found(self, client):
        resp = client.get("/api/bots/30")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "claude_v30"
        assert data["version"] == 30
        assert "files" in data
        assert "total_lines" in data

    def test_404(self, client):
        resp = client.get("/api/bots/9999")
        assert resp.status_code == 404


class TestBotCode:
    def test_read_main(self, client):
        resp = client.get("/api/bots/30/code/main.py")
        assert resp.status_code == 200
        assert "def " in resp.text or "import " in resp.text

    def test_invalid_filename(self, client):
        # Path traversal: framework resolves ../ before route matching → 404
        resp = client.get("/api/bots/30/code/../etc/passwd")
        assert resp.status_code == 404

    def test_non_py_file(self, client):
        resp = client.get("/api/bots/30/code/main.txt")
        assert resp.status_code == 400

    def test_404(self, client):
        resp = client.get("/api/bots/30/code/nonexistent.py")
        assert resp.status_code == 404

    def test_backslash_blocked(self, client):
        resp = client.get("/api/bots/30/code/..\\etc")
        assert resp.status_code == 400
