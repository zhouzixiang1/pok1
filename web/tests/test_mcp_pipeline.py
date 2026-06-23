"""Tests for pipeline MCP tools (non-LLM parts) via POST /api/control/tool/{name}."""

import json
import shutil
from pathlib import Path

import pytest


class TestPrepareNextGen:
    def test_creates_directory(self, client, tmp_path, monkeypatch):
        import evolution_infra
        import tool_gates

        fake_bots = tmp_path / "bots"
        fake_bots.mkdir()
        src = fake_bots / "claude_v99"
        src.mkdir()
        (src / "main.py").write_text("x = 1\n")
        (src / ".completed").touch()
        monkeypatch.setattr(evolution_infra, "BOTS_DIR", fake_bots)
        # Keep GRAVEYARD_DIR consistent with the overridden BOTS_DIR: get_bot_dir()
        # falls back to GRAVEYARD_DIR/<v> when the primary path is missing. The
        # autouse isolate_state fixture symlinks real bots/graveyard (which holds
        # reaped bots like v100) into its isolation tree; without this override
        # get_bot_dir(100) resolves to that graveyard copy, prepare_next_gen sees
        # a completed v100 and refuses to overwrite — KeyError 'prepared'.
        monkeypatch.setattr(evolution_infra, "GRAVEYARD_DIR", fake_bots / "graveyard")
        monkeypatch.setattr(tool_gates, "get_bot_dir", evolution_infra.get_bot_dir)

        fake_results = tmp_path / "results"
        fake_results.mkdir()
        fake_ckpt = fake_results / "pipeline_state.json"
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", fake_results)
        monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", fake_ckpt)

        monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 99)
        monkeypatch.setattr(tool_gates, "find_current_v", lambda: 99)
        # git_has_tag is checked by prepare_next_gen to verify source bot commit
        monkeypatch.setattr(evolution_infra, "git_has_tag", lambda v: True)

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


class TestCheckCitations:
    """A4/evidence_gate: _check_citations distinguishes None (skip) from {} (fabricate)."""

    def test_none_skips(self):
        from tool_planning import _check_citations
        # None = no manifest loaded, always skip
        assert _check_citations(["G1H1#abc", "G3H42"], None) == []

    def test_empty_dict_all_fabricated(self):
        from tool_planning import _check_citations
        # Empty dict = manifest loaded but empty = ALL citations are fabricated
        errors = _check_citations(["G1H1#abc"], {})
        assert len(errors) == 1
        assert "FABRICATED_EVIDENCE" in errors[0]

    def test_empty_text_no_errors(self):
        from tool_planning import _check_citations
        errors = _check_citations(["no citations here"], {"G1H1": "abcd1234"})
        assert errors == []

    def test_valid_anchor_passes(self):
        from tool_planning import _check_citations
        anchor_map = {"G1H1": "abcd1234", "G3H42": "deadbeef"}
        # Valid base ID with correct anchor
        errors = _check_citations(["See G1H1#abcd1234"], anchor_map)
        assert errors == []
        # Valid base ID without anchor (no tamper check)
        errors = _check_citations(["See G1H1"], anchor_map)
        assert errors == []

    def test_wrong_anchor_fails(self):
        from tool_planning import _check_citations
        anchor_map = {"G1H1": "abcd1234"}
        errors = _check_citations(["See G1H1#00000000"], anchor_map)
        assert len(errors) == 1
        assert "anchor mismatch" in errors[0]

    def test_invalid_base_fails(self):
        from tool_planning import _check_citations
        anchor_map = {"G1H1": "abcd1234"}
        errors = _check_citations(["See G9H9#abcd1234"], anchor_map)
        assert len(errors) == 1
        assert "NOT in the spotlight manifest" in errors[0]
