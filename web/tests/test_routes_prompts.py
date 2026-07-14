"""Tests for /api/prompts/* endpoints."""


class TestListPrompts:
    def test_returns_list(self, client):
        resp = client.get("/api/prompts")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 17
        for p in data:
            assert "name" in p
            assert "exists" in p
            assert "role" in p
            assert p["editable"] is False
            assert p["mutation_authority"] == "source_control_only"

    def test_core_prompt_names_present(self, client):
        resp = client.get("/api/prompts")
        data = resp.json()
        names = {p["name"] for p in data}
        assert "initial" not in names
        assert names == {
            "orchestrator", "master", "master_plan_audit", "worker",
            "worker_profile_national_native", "worker_cot_check", "debug_worker",
            "reviewer", "critic", "crossover", "crossover_compatibility",
            "direction_auditor", "literature_probe", "combined_analyst",
            "degeneration_diagnosis", "cycle_archivist",
            "official_platform_analysis",
        }


class TestGetPrompt:
    def test_found(self, client):
        resp = client.get("/api/prompts/master")
        assert resp.status_code == 200
        assert len(resp.text) > 0
        assert "# Role" in resp.text or "Role" in resp.text

    def test_unknown(self, client):
        resp = client.get("/api/prompts/nonexistent")
        assert resp.status_code == 404


class TestPromptMutationRetired:
    def test_update_route_is_not_exposed(self, client, temp_prompt_dir, monkeypatch):
        from server.routes import prompts
        monkeypatch.setattr(prompts, "PROMPTS_DIR", temp_prompt_dir)
        before = (temp_prompt_dir / "master_prompt.md").read_bytes()
        resp = client.put("/api/prompts/master", json={"content": "# Updated prompt\nTest content\n"})
        assert resp.status_code == 405
        assert (temp_prompt_dir / "master_prompt.md").read_bytes() == before

    def test_unknown_update_is_not_exposed(self, client):
        resp = client.put("/api/prompts/nonexistent", json={"content": "test"})
        assert resp.status_code == 405

    def test_git_reset_route_is_not_exposed(self, client, monkeypatch):
        import subprocess

        def forbidden(*_args, **_kwargs):
            raise AssertionError("HTTP prompt catalog must never invoke git")

        monkeypatch.setattr(subprocess, "run", forbidden)
        resp = client.post("/api/prompts/master/reset")
        assert resp.status_code == 404

    def test_unknown_reset_is_not_exposed(self, client):
        resp = client.post("/api/prompts/nonexistent/reset")
        assert resp.status_code == 404
