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
class TestRunMasterIdempotent:
    """run_master idempotency guard (fix-4): returns cached plan when checkpoint
    already has a master_plan at a stage >= master_planned."""

    def test_run_master_idempotent_returns_cache(self, client, monkeypatch):
        """run_master with existing plan in checkpoint returns cached result
        without calling _run_master_analysis."""
        import tool_planning
        import tool_helpers

        cached_plan = {
            "tasks": [
                {"worker_id": "W1", "role": "Algorithmic Logic Architect",
                 "target_files": ["strategy.py"], "worker_prompt": "Add feature X."}
            ],
            "analysis": "cached analysis",
        }
        fake_checkpoint = {
            "next_v": 200,
            "source_v": 199,
            "stage": "master_planned",
            "master_plan": cached_plan,
        }

        # Stub _matching_checkpoint so the idempotency guard sees a cached plan
        monkeypatch.setattr(tool_planning, "_matching_checkpoint",
                            lambda nv, sv: fake_checkpoint)

        # Track whether _run_master_analysis is called — it should NOT be
        call_log = []
        async def _fake_master(*a, **kw):
            call_log.append("called")
            return cached_plan
        monkeypatch.setattr(tool_planning, "_run_master_analysis", _fake_master)

        resp = client.post("/api/control/tool/run_master",
                           json={"args": {"source_v": 199, "next_v": 200}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        # Cached result returned
        assert result.get("idempotent_cache") is True
        assert result.get("plan") == cached_plan
        # _run_master_analysis was NOT called
        assert call_log == []

    def test_run_master_non_idempotent_calls_analysis(self, client, monkeypatch):
        """run_master without a cached plan proceeds to call _run_master_analysis
        (idempotency guard does NOT block fresh calls)."""
        import tool_planning

        # No matching checkpoint — guard should NOT intercept
        monkeypatch.setattr(tool_planning, "_matching_checkpoint",
                            lambda nv, sv: None)

        call_log = []
        fresh_plan = {"tasks": [], "analysis": "fresh"}

        async def _fake_master(*a, **kw):
            call_log.append("called")
            return fresh_plan
        monkeypatch.setattr(tool_planning, "_run_master_analysis", _fake_master)

        # Stub the audit agent to avoid a real LLM call (which hangs in tests)
        import audit_agents
        async def _fake_audit(*a, **kw):
            return {"overall_pass": True, "feedback": "", "contradictions": [],
                    "direction_novelty": "novel"}
        monkeypatch.setattr(audit_agents, "_run_master_plan_audit", _fake_audit)

        resp = client.post("/api/control/tool/run_master",
                           json={"args": {"source_v": 199, "next_v": 200}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        # No idempotent_cache key means guard did not fire
        assert result.get("idempotent_cache") is not True
        # _run_master_analysis WAS called (audit retry may call it multiple times)
        assert len(call_log) >= 1

    def test_run_master_stage_direction_audited_not_cached(self, client, monkeypatch):
        """run_master at stage='direction_audited' does NOT return cached
        (no master_plan yet — the audit completed but master hasn't run)."""
        import tool_planning

        fake_checkpoint = {
            "next_v": 200,
            "source_v": 199,
            "stage": "direction_audited",
            "master_plan": None,
            "direction_audit": {"repetition_detected": False},
        }
        monkeypatch.setattr(tool_planning, "_matching_checkpoint",
                            lambda nv, sv: fake_checkpoint)

        call_log = []
        fresh_plan = {"tasks": [], "analysis": "fresh"}

        async def _fake_master(*a, **kw):
            call_log.append("called")
            return fresh_plan
        monkeypatch.setattr(tool_planning, "_run_master_analysis", _fake_master)

        # Stub the audit agent to avoid a real LLM call (which hangs in tests)
        import audit_agents
        async def _fake_audit(*a, **kw):
            return {"overall_pass": True, "feedback": "", "contradictions": [],
                    "direction_novelty": "novel"}
        monkeypatch.setattr(audit_agents, "_run_master_plan_audit", _fake_audit)

        resp = client.post("/api/control/tool/run_master",
                           json={"args": {"source_v": 199, "next_v": 200}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert result.get("idempotent_cache") is not True
        # _run_master_analysis WAS called (audit retry may call it multiple times)
        assert len(call_log) >= 1

    def test_run_master_validation_failure_bumps_master_budget(self, client, monkeypatch):
        """A schema/audit-clean plan can still fail hard validation. That path
        must count against the Master retry budget and emit a structured event,
        otherwise the orchestrator can repeatedly re-run Master with no
        checkpoint progress."""
        import audit_agents
        import evolution_infra
        import tool_planning

        checkpoint = {
            "next_v": 246,
            "source_v": 245,
            "stage": "direction_audited",
            "master_plan": None,
            "audit_attempt": 1,
            "direction_audit": {"repetition_detected": False, "llm_failed": False},
        }
        plan = {
            "analysis": "plan analysis",
            "targeted_failure": "bad citation",
            "expected_behavior_change": "fold bad spots",
            "do_not_touch": [],
            "measurement_plan": "run gates",
            "tasks": [
                {
                    "worker_id": 1,
                    "role": "Algorithmic Logic Architect",
                    "target_files": ["strategy.py"],
                    "worker_prompt": "Fix G6H28 by changing strategy.py.",
                }
            ],
        }
        writes = []
        events = []

        class _UI:
            def clear_io(self):
                pass

            def log_history(self, *_args, **_kwargs):
                pass

            def get_output(self):
                return ""

        async def _fake_master(*_args, **_kwargs):
            return dict(plan)

        async def _fake_audit(*_args, **_kwargs):
            return {"overall_pass": True, "feedback": "", "contradictions": []}

        def _fake_write(next_v, source_v, stage, **kwargs):
            writes.append((next_v, source_v, stage, kwargs))
            checkpoint["next_v"] = next_v
            checkpoint["source_v"] = source_v
            checkpoint["stage"] = stage
            if "audit_attempt" in kwargs:
                checkpoint["audit_attempt"] = kwargs["audit_attempt"]
            if kwargs.get("audit_context"):
                checkpoint.setdefault("audit_context", {}).update(kwargs["audit_context"])
            return True

        monkeypatch.setattr(tool_planning, "_matching_checkpoint", lambda *_a, **_k: checkpoint)
        monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint)
        monkeypatch.setattr(tool_planning, "write_pipeline_checkpoint", _fake_write)
        monkeypatch.setattr(tool_planning, "_run_master_analysis", _fake_master)
        monkeypatch.setattr(audit_agents, "_run_master_plan_audit", _fake_audit)
        monkeypatch.setattr(tool_planning, "_get_ui", lambda: _UI())
        monkeypatch.setattr(tool_planning, "_extract_exhausted_keywords", lambda: [])
        monkeypatch.setattr(tool_planning, "_build_cross_gen_constraint_block", lambda _v: "")
        monkeypatch.setattr(
            tool_planning,
            "_validate_master_plan",
            lambda *_a, **_k: (
                ["FABRICATED_EVIDENCE: cited hand G6H28 is NOT in the spotlight manifest"],
                ["advisory warning"],
            ),
        )
        monkeypatch.setattr(
            tool_planning,
            "log_system_event",
            lambda event_type, severity, message, data=None: events.append(
                (event_type, severity, message, data or {})
            ),
        )

        resp = client.post("/api/control/tool/run_master",
                           json={"args": {"source_v": 245, "next_v": 246}})

        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert result["error"] == "MASTER_VALIDATION_FAILED"
        assert result["fail_count"] == 2
        assert "validation_errors" in result
        assert any(e[0] == "pipeline.master_validation_failed" for e in events)
        assert writes[-1][3]["audit_attempt"] == 2
        assert writes[-1][3]["audit_context"]["master_validation"]["errors"] == result["validation_errors"]
