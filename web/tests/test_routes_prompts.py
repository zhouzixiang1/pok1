"""Tests for /api/prompts/* endpoints."""


class TestListPrompts:
    def test_returns_list(self, client):
        resp = client.get("/api/prompts")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 7
        for p in data:
            assert "name" in p
            assert "exists" in p
            assert "role" in p

    def test_core_prompt_names_present(self, client):
        resp = client.get("/api/prompts")
        data = resp.json()
        names = {p["name"] for p in data}
        expected_min = {"orchestrator", "master", "worker", "reviewer", "critic", "crossover", "initial"}
        assert expected_min.issubset(names)


class TestGetPrompt:
    def test_found(self, client):
        resp = client.get("/api/prompts/master")
        assert resp.status_code == 200
        assert len(resp.text) > 0
        assert "# Role" in resp.text or "Role" in resp.text

    def test_unknown(self, client):
        resp = client.get("/api/prompts/nonexistent")
        assert resp.status_code == 404


class TestUpdatePrompt:
    def test_update(self, client, temp_prompt_dir, monkeypatch):
        from server.routes import prompts
        monkeypatch.setattr(prompts, "PROMPTS_DIR", temp_prompt_dir)
        resp = client.put("/api/prompts/master", json={"content": "# Updated prompt\nTest content\n"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["saved"] is True
        assert data["name"] == "master"

    def test_unknown(self, client):
        resp = client.put("/api/prompts/nonexistent", json={"content": "test"})
        assert resp.status_code == 404


class TestResetPrompt:
    def test_reset(self, client, monkeypatch):
        import subprocess
        calls = []
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            class R:
                returncode = 0
                stderr = ""
            return R()
        monkeypatch.setattr(subprocess, "run", fake_run)
        resp = client.post("/api/prompts/master/reset")
        assert resp.status_code == 200
        data = resp.json()
        assert data["reset"] is True
        assert data["name"] == "master"
        assert any("checkout" in str(c) for c in calls)

    def test_unknown(self, client):
        resp = client.post("/api/prompts/nonexistent/reset")
        assert resp.status_code == 404
