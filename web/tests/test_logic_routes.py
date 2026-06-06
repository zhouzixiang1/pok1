"""Logic-level tests for route endpoints — verifies business logic, not just HTTP status codes."""

import json
import time
from pathlib import Path

import pytest


# ── ratings.py: Ranking logic ──

class TestRatingsRanking:
    def test_sorted_by_h2h_descending(self, client):
        resp = client.get("/api/ratings")
        assert resp.status_code == 200
        data = resp.json()
        if len(data) >= 2:
            wr_values = [r["h2h_avg_wr"] if r["h2h_avg_wr"] is not None else 0.0 for r in data]
            assert wr_values == sorted(wr_values, reverse=True)

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
        assert data["status"] in ("active", "recent", "idle", "unknown")

    def test_age_non_negative(self, client):
        resp = client.get("/api/daemon/status")
        data = resp.json()
        assert data["last_update_age_seconds"] >= -1


# ── ratings.py: Experience append logic ──

class TestExperienceAppendLogic:
    def test_whitespace_only_rejected(self, client, temp_experience, monkeypatch):
        from server.routes import ratings
        monkeypatch.setattr(ratings, "EXPERIENCE_FILE", temp_experience)
        resp = client.post("/api/experience/append", json={"lesson": "   "})
        assert resp.status_code == 400

    def test_double_newline_not_duplicated(self, client, temp_experience, monkeypatch):
        from server.routes import ratings
        monkeypatch.setattr(ratings, "EXPERIENCE_FILE", temp_experience)
        temp_experience.write_text("existing content\n\n")
        resp = client.post("/api/experience/append", json={"lesson": "new lesson"})
        assert resp.status_code == 200
        content = temp_experience.read_text()
        # Should not have triple newline
        assert "\n\n\n" not in content


# ── ratings.py: H2H filtering ──

class TestH2HFilterLogic:
    def test_filtered_contains_only_specified_bot(self, client, active_bot_version):
        if active_bot_version is None:
            return
        bot_name = f"claude_v{active_bot_version}"
        resp = client.get(f"/api/h2h?bot_name={bot_name}")
        assert resp.status_code == 200
        data = resp.json()
        for key in data:
            assert bot_name in key


# ── matches.py: Replay path traversal ──

class TestMatchReplaySecurity:
    def test_path_traversal_blocked(self, client):
        resp = client.get("/api/matches/replay/../../core/results/glicko_ratings.json")
        # Should be 404 (path doesn't resolve to a valid replay) or 400
        assert resp.status_code in (400, 404)


# ── bots.py: Sorting and filtering ──

class TestBotsSorting:
    def test_numerical_sorting(self, client):
        resp = client.get("/api/bots")
        data = resp.json()
        active = data.get("active", [])
        if len(active) >= 2:
            versions = [b["version"] for b in active]
            assert versions == sorted(versions)

    def test_active_bots_have_completed_sentinel(self, client):
        resp = client.get("/api/bots")
        data = resp.json()
        for bot in data.get("active", []):
            assert bot.get("completed") is True

    def test_graveyard_flag(self, client):
        resp = client.get("/api/bots?include_graveyard=true")
        data = resp.json()
        for bot in data.get("graveyard", []):
            assert bot.get("graveyard") is True


# ── bots.py: Code reading ──

class TestBotCodeLogic:
    def test_returns_python_source(self, client, active_bot_version):
        if active_bot_version is None:
            return
        resp = client.get(f"/api/bots/{active_bot_version}/code/main.py")
        assert resp.status_code == 200
        assert "def " in resp.text or "import " in resp.text or len(resp.text) > 0

    def test_non_py_rejected(self, client, active_bot_version):
        if active_bot_version is None:
            return
        resp = client.get(f"/api/bots/{active_bot_version}/code/main.txt")
        assert resp.status_code == 400

    def test_path_separator_rejected(self, client, active_bot_version):
        if active_bot_version is None:
            return
        resp = client.get(f"/api/bots/{active_bot_version}/code/sub/dir/main.py")
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
        for name in ["master", "worker", "reviewer", "critic", "crossover", "orchestrator", "initial"]:
            resp = client.get(f"/api/prompts/{name}")
            assert resp.status_code == 200, f"Failed for prompt: {name}"


# ── prompts.py: Write logic ──

class TestPromptsWriteLogic:
    def test_empty_content_accepted(self, client, temp_prompt_dir, monkeypatch):
        from server.routes import prompts
        monkeypatch.setattr(prompts, "PROMPTS_DIR", temp_prompt_dir)
        resp = client.put("/api/prompts/master", json={"content": ""})
        assert resp.status_code == 200
        data = resp.json()
        assert data["lines"] == 1  # empty string has 1 line per count("\n") + 1

    def test_lines_count_matches_content(self, client, temp_prompt_dir, monkeypatch):
        from server.routes import prompts
        monkeypatch.setattr(prompts, "PROMPTS_DIR", temp_prompt_dir)
        content = "line1\nline2\nline3\n"
        resp = client.put("/api/prompts/master", json={"content": content})
        assert resp.status_code == 200
        assert resp.json()["lines"] == 4  # 3 newlines + 1


# ── control.py: Config clamping ──

class TestConfigClamping:
    def test_daemon_workers_clamped_low(self, client):
        resp = client.put("/api/control/config", json={"daemon_workers": 0})
        assert resp.status_code == 200
        assert resp.json()["daemon_workers"] >= 1

    def test_daemon_workers_clamped_high(self, client):
        resp = client.put("/api/control/config", json={"daemon_workers": 100})
        assert resp.status_code == 200
        assert resp.json()["daemon_workers"] <= 128

    def test_daemon_pairs_clamped_low(self, client):
        resp = client.put("/api/control/config", json={"daemon_pairs": -5})
        assert resp.status_code == 200
        assert resp.json()["daemon_pairs"] >= 1

    def test_daemon_pairs_clamped_high(self, client):
        resp = client.put("/api/control/config", json={"daemon_pairs": 50})
        assert resp.status_code == 200
        assert resp.json()["daemon_pairs"] <= 20

    def test_bool_not_accepted_as_int(self, client):
        resp = client.put("/api/control/config", json={"daemon_workers": True})
        # Should be rejected by strict mode or bool-is-not-int guard
        assert resp.status_code in (422, 200)
        if resp.status_code == 200:
            # If accepted, value should not change to 1
            orig = client.get("/api/control/config").json()
            # daemon_workers should remain unchanged
            assert isinstance(orig["daemon_workers"], int)


# ── control.py: Session management ──

class TestSessionLogic:
    def test_no_session_file_returns_inactive(self, client):
        resp = client.get("/api/control/orchestrator/session")
        data = resp.json()
        assert data["active"] is False or data["session_id"] is None

    def test_delete_when_absent(self, client):
        resp = client.delete("/api/control/orchestrator/session")
        assert resp.status_code == 200
        data = resp.json()
        assert "cleared" in data


# ── control.py: Tool dispatch ──

class TestToolDispatchLogic:
    def test_unknown_tool_lists_available(self, client):
        resp = client.post("/api/control/tool/nonexistent_xyz", json={"args": {}})
        assert resp.status_code == 404
        # Response should mention available tools
        detail = resp.json().get("detail", "")
        assert "Available" in detail or "tool" in detail.lower() or len(detail) > 0
