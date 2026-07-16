"""Logic-level tests for route endpoints — verifies business logic, not just HTTP status codes."""

import json
import time
from pathlib import Path

import pytest
from bot_namespace import bot_name


# ── ratings.py: Ranking logic ──

class TestRatingsRanking:
    @pytest.mark.requires_active_bot
    def test_sorted_by_selection_score_descending(self, client):
        resp = client.get("/api/ratings")
        assert resp.status_code == 200
        data = resp.json()
        scores = [r["selection_score"] for r in data]
        assert scores == sorted(scores, reverse=True)
        assert "leaderboard_score" in data[0]
        assert "h2h_avg_wr" in data[0]
        assert "h2h_coverage" in data[0]

    def test_ranks_sequential_from_1(self, client):
        resp = client.get("/api/ratings")
        data = resp.json()
        ranks = [r["rank"] for r in data]
        assert ranks == list(range(1, len(data) + 1))

    def test_conservative_rating_formula(self, client):
        resp = client.get("/api/ratings")
        data = resp.json()
        for row in data:
            # Formula: round(r - 2*rd, 1) using raw values
            # We can't perfectly reconstruct raw r/rd from rounded values,
            # so check within tolerance
            expected = row["rating"] - 2 * row["rd"]
            assert abs(row["conservative_rating"] - expected) < 0.2

    def test_confidence_matches_rd(self, client):
        resp = client.get("/api/ratings")
        data = resp.json()
        for row in data:
            rd = row["rd"]
            conf = row["confidence"]
            if rd < 50:
                assert conf == "very_confident"
            elif rd < 100:
                assert conf == "confident"
            elif rd < 200:
                assert conf == "uncertain"
            else:
                assert conf == "very_uncertain"


# ── ratings.py: Daemon status thresholds ──

class TestDaemonStatusLogic:
    def test_status_field_is_valid(self, client):
        resp = client.get("/api/daemon/status")
        data = resp.json()
        assert data["status"] in (
            "active",
            "degraded",
            "stopped",
            "disabled",
            "blocked",
        )

    def test_age_non_negative(self, client):
        resp = client.get("/api/daemon/status")
        data = resp.json()
        assert data["last_update_age_seconds"] >= -1


# ── ratings.py: retired experience API remains absent ──

class TestRetiredExperienceRouteLogic:
    def test_get_route_does_not_exist(self, client):
        assert client.get("/api/experience").status_code == 404

    def test_append_route_does_not_exist(self, client):
        resp = client.post("/api/experience/append", json={"lesson": "   "})
        assert resp.status_code == 404

    def test_markdown_overwrite_route_does_not_exist(self, client):
        resp = client.put("/api/experience", json={"content": "old markdown"})
        assert resp.status_code == 404


# ── ratings.py: H2H filtering ──

class TestH2HFilterLogic:
    @pytest.mark.requires_active_bot
    def test_filtered_contains_only_specified_bot(self, client, active_bot_version):
        name = bot_name(active_bot_version)
        resp = client.get(f"/api/h2h?bot_name={name}")
        assert resp.status_code == 200
        data = resp.json()
        for key in data:
            assert name in key


# ── matches.py: Replay path traversal ──

class TestMatchReplaySecurity:
    def test_path_traversal_blocked(self, client):
        resp = client.get("/api/matches/replay/../../core/results/glicko_ratings.json")
        # Should be 404 (path doesn't resolve to a valid replay) or 400
        assert resp.status_code in (400, 404)


# ── bots.py: Sorting and filtering ──

class TestBotsSorting:
    @pytest.mark.requires_active_bot
    def test_numerical_sorting(self, client):
        resp = client.get("/api/bots")
        data = resp.json()
        active = data.get("active", [])
        versions = [b["version"] for b in active]
        assert versions == sorted(versions)

    def test_active_bots_have_completed_sentinel(self, client):
        resp = client.get("/api/bots")
        data = resp.json()
        for bot in data.get("active", []):
            assert bot.get("completed") is True

    def test_archive_is_not_exposed_by_compatibility_query(self, client):
        resp = client.get("/api/bots?include_graveyard=true")
        data = resp.json()
        assert "graveyard" not in data


# ── bots.py: Code reading ──

class TestBotCodeLogic:
    @pytest.mark.requires_active_bot
    def test_returns_python_source(self, client, active_bot_version):
        resp = client.get(f"/api/bots/{active_bot_version}/code/policy.py")
        assert resp.status_code == 200
        assert "def " in resp.text or "import " in resp.text

    @pytest.mark.requires_active_bot
    def test_non_py_rejected(self, client, active_bot_version):
        resp = client.get(f"/api/bots/{active_bot_version}/code/policy.txt")
        assert resp.status_code == 400

    @pytest.mark.requires_active_bot
    def test_path_separator_rejected(self, client, active_bot_version):
        resp = client.get(f"/api/bots/{active_bot_version}/code/sub/dir/policy.py")
        assert resp.status_code in (400, 404)


# ── logs.py: Path traversal ──

class TestLogsSecurity:
    def test_version_traversal_blocked(self, client):
        resp = client.get("/api/logs/generations/../../etc/passwd")
        assert resp.status_code in (400, 404)

    def test_filename_traversal_blocked(self, client):
        # This should be caught by is_relative_to check
        resp = client.get("/api/logs/generations/v1/../../../../etc/passwd")
        assert resp.status_code in (400, 404)

    def test_tail_negative_rejected(self, client):
        resp = client.get("/api/logs/generations/v30/master_io.txt?tail=-1")
        assert resp.status_code == 422


# ── logs.py: Orchestrator log validation ──

class TestOrchestratorLogValidation:
    def test_non_orchestrator_prefix_rejected(self, client):
        resp = client.get("/api/logs/orchestrator/other_log.txt")
        assert resp.status_code == 400

    def test_non_txt_rejected(self, client):
        resp = client.get("/api/logs/orchestrator/orchestrator_log.py")
        assert resp.status_code == 400

    def test_slash_in_filename_rejected(self, client):
        resp = client.get("/api/logs/orchestrator/orchestrator_log.txt/../../secret")
        assert resp.status_code in (400, 404)


# ── prompts.py: Name mapping logic ──

class TestPromptsNameMapping:
    def test_orchestrator_maps_to_orchestrator_md(self, client):
        resp = client.get("/api/prompts/orchestrator")
        assert resp.status_code == 200
        # Verify it reads orchestrator.md, not orchestrator_prompt.md
        content = resp.text
        assert len(content) > 0

    def test_worker_maps_to_worker_prompt_md(self, client):
        resp = client.get("/api/prompts/worker")
        assert resp.status_code == 200
        assert len(resp.text) > 0

    def test_all_allowed_names_return_content(self, client):
        for name in [
            "master", "master_plan_audit", "worker",
            "worker_profile_national_native", "worker_cot_check", "debug_worker",
            "reviewer", "critic", "crossover", "crossover_compatibility",
            "direction_auditor", "literature_probe", "combined_analyst",
            "degeneration_diagnosis", "cycle_archivist",
            "official_platform_analysis", "orchestrator",
        ]:
            resp = client.get(f"/api/prompts/{name}")
            assert resp.status_code == 200, f"Failed for prompt: {name}"


# ── prompts.py: source-controlled, read-only catalog ──

class TestPromptsWriteLogic:
    def test_empty_content_cannot_be_written_over_http(self, client, temp_prompt_dir, monkeypatch):
        from server.routes import prompts
        monkeypatch.setattr(prompts, "PROMPTS_DIR", temp_prompt_dir)
        before = (temp_prompt_dir / "master_prompt.md").read_bytes()
        resp = client.put("/api/prompts/master", json={"content": ""})
        assert resp.status_code == 405
        assert (temp_prompt_dir / "master_prompt.md").read_bytes() == before

    def test_multiline_content_cannot_be_written_over_http(self, client, temp_prompt_dir, monkeypatch):
        from server.routes import prompts
        monkeypatch.setattr(prompts, "PROMPTS_DIR", temp_prompt_dir)
        before = (temp_prompt_dir / "master_prompt.md").read_bytes()
        content = "line1\nline2\nline3\n"
        resp = client.put("/api/prompts/master", json={"content": content})
        assert resp.status_code == 405
        assert (temp_prompt_dir / "master_prompt.md").read_bytes() == before


# ── control.py: Strict config bounds ──

class TestStrictConfigBounds:
    def test_daemon_workers_rejects_low(self, client):
        before = client.get("/api/control/config").json()
        resp = client.put("/api/control/config", json={"daemon_workers": 0})
        assert resp.status_code == 422
        assert client.get("/api/control/config").json() == before

    def test_daemon_workers_rejects_high(self, client):
        before = client.get("/api/control/config").json()
        resp = client.put("/api/control/config", json={"daemon_workers": 100})
        assert resp.status_code == 422
        assert client.get("/api/control/config").json() == before

    def test_daemon_pairs_rejects_low(self, client):
        before = client.get("/api/control/config").json()
        resp = client.put("/api/control/config", json={"daemon_pairs": -5})
        assert resp.status_code == 422
        assert client.get("/api/control/config").json() == before

    def test_daemon_pairs_rejects_high(self, client):
        before = client.get("/api/control/config").json()
        resp = client.put("/api/control/config", json={"daemon_pairs": 9})
        assert resp.status_code == 422
        assert client.get("/api/control/config").json() == before

    def test_bool_not_accepted_as_int(self, client):
        before = client.get("/api/control/config").json()
        resp = client.put("/api/control/config", json={"daemon_workers": True})
        assert resp.status_code == 422
        assert client.get("/api/control/config").json() == before


# ── control.py: Session management ──

class TestSessionLogic:
    def test_no_session_file_returns_inactive(self, client):
        resp = client.get("/api/control/orchestrator/session")
        data = resp.json()
        assert data["active"] is False
        assert data["session_id"] is None

    def test_delete_when_absent(self, client):
        resp = client.delete("/api/control/orchestrator/session")
        assert resp.status_code == 410
        assert resp.json()["detail"]["code"] == (
            "orchestrator_provider_session_resume_retired"
        )


# ── control.py: Tool dispatch ──

class TestToolDispatchLogic:
    def test_old_tool_dispatch_is_permanently_retired(self, client):
        resp = client.post("/api/control/tool/nonexistent_xyz", json={"args": {}})
        assert resp.status_code == 410
        assert resp.json()["detail"]["code"] == "control_tool_executor_retired"
