"""Tests for pipeline MCP tools (non-LLM parts) via POST /api/control/tool/{name}."""

import json
import shutil
from pathlib import Path

import pytest


class TestPrepareNextGen:
    def test_creates_directory(self, client, tmp_path, monkeypatch):
        import evolution_infra
        import tool_pipeline

        fake_bots = tmp_path / "bots"
        fake_bots.mkdir()
        src = fake_bots / "claude_v99"
        src.mkdir()
        (src / "main.py").write_text("x = 1\n")
        (src / ".completed").touch()
        monkeypatch.setattr(evolution_infra, "BOTS_DIR", fake_bots)
        monkeypatch.setattr(tool_pipeline, "get_bot_dir", evolution_infra.get_bot_dir)

        fake_results = tmp_path / "results"
        fake_results.mkdir()
        fake_ckpt = fake_results / "pipeline_state.json"
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", fake_results)
        monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", fake_ckpt)

        monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 99)
        monkeypatch.setattr(tool_pipeline, "find_current_v", lambda: 99)

        resp = client.post("/api/control/tool/prepare_next_gen",
                           json={"args": {"source_v": 99, "next_v": 100}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert result["prepared"] is True
        assert result["source_v"] == 99
        assert result["next_v"] == 100
        assert (fake_bots / "claude_v100").exists()

    def test_missing_source(self, client):
        resp = client.post("/api/control/tool/prepare_next_gen",
                           json={"args": {"source_v": 9999, "next_v": 10000}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert "error" in result


class TestRunQualityGates:
    @pytest.mark.timeout(120)
    def test_on_existing_bot(self, client):
        resp = client.post("/api/control/tool/run_quality_gates",
                           json={"args": {"version": 30}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert "version" in result
        assert "compile_ok" in result
        assert "all_passed" in result
        assert "decision_pass_rate" in result

    def test_on_nonexistent(self, client):
        resp = client.post("/api/control/tool/run_quality_gates",
                           json={"args": {"version": 9999}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        # Empty dir passes compile (no files to check) but fails decision tests
        assert result["all_passed"] is False
