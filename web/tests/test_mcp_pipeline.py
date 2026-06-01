"""Tests for pipeline MCP tools (non-LLM parts) via POST /api/control/tool/{name}."""

import json
import shutil
from pathlib import Path

import pytest


class TestPrepareNextGen:
    def test_creates_directory(self, client, tmp_path, monkeypatch):
        # Set up temp bots dir with a source bot
        src = tmp_path / "bots" / "claude_v99"
        src.mkdir(parents=True)
        (src / "main.py").write_text("x = 1\n")
        (src / ".completed").touch()

        from evolution_infra import BOTS_DIR
        resp = client.post("/api/control/tool/prepare_next_gen",
                           json={"args": {"source_v": 30, "next_v": 41}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert result["prepared"] is True
        assert result["source_v"] == 30
        assert result["next_v"] == 41

        # Clean up created dir
        next_dir = Path(__file__).resolve().parents[2] / "bots" / "claude_v41"
        if next_dir.exists():
            shutil.rmtree(next_dir)

        # Clean up checkpoint
        ckpt_file = Path(__file__).resolve().parents[2] / "web" / "core" / "results" / "pipeline_state.json"
        if ckpt_file.exists():
            ckpt_file.unlink()

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
