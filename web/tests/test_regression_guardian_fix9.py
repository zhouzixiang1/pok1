"""Tests for fix-9: regression_guardian diagnosis surfaced to experience_pool + context.

Covers:
  - tool_gates: guardian diagnosis written to regression_guardian.jsonl on score<4
  - orchestrator_context: guardian insights injected into Master context
"""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))


# ──────────────────────────────────────────────
# Test 1: Guardian diagnosis writes to JSONL
# ──────────────────────────────────────────────

class TestRegressionGuardianWritesJsonl:
    """When critic score < 4 triggers _run_regression_guardian and the guardian
    returns a diagnosis, the diagnosis must be written to
    regression_guardian.jsonl (fix-9).
    """

    def _patch_critic_for_guardian(self, monkeypatch, score, guardian_return):
        import importlib
        tool_gates = sys.modules.get("core.tool_gates") or importlib.import_module("tool_gates")
        audit_agents = sys.modules.get("core.audit_agents") or importlib.import_module("audit_agents")

        fake_ui = MagicMock()
        fake_ui.get_output.return_value = ""

        monkeypatch.setattr(tool_gates, "_run_critic", AsyncMock(return_value={
            "score": score,
            "approved": score >= 6,
            "feedback": "regression detected",
            "strategic_assessment": "poor",
            "evidence": None,
        }))
        monkeypatch.setattr(tool_gates, "_matching_checkpoint", MagicMock(return_value={
            "master_plan": {"tasks": []}, "gate_results": {}, "generation_attempt": 0,
        }))
        monkeypatch.setattr(tool_gates, "_quality_gate_ok", MagicMock(return_value=True))
        monkeypatch.setattr(tool_gates, "_review_gate_ok", MagicMock(return_value=True))
        monkeypatch.setattr(tool_gates, "_idempotency_check", MagicMock(return_value=None))
        monkeypatch.setattr(tool_gates, "_set_pipeline_status", MagicMock())
        monkeypatch.setattr(tool_gates, "_record_gate", MagicMock(return_value=True))
        monkeypatch.setattr(tool_gates, "_record_quality_failure", MagicMock())
        monkeypatch.setattr(tool_gates, "_get_ui", MagicMock(return_value=fake_ui))
        monkeypatch.setattr(audit_agents, "_run_regression_guardian",
                            AsyncMock(return_value=guardian_return))
        return tool_gates

    def test_jsonl_written_on_low_score_with_diagnosis(self, monkeypatch, tmp_path):
        """Score < 4 + guardian returns diagnosis -> JSONL file should be created."""
        import evolution_infra
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        guardian_fake = {
            "diagnosis": "Preflop range too wide against tight opponents",
            "failure_stage": "workers",
            "root_cause": "over-aggression",
            "severity": "major",
            "recovery_recommendation": "tighten opening ranges",
            "confidence": "medium",
        }
        tool_gates = self._patch_critic_for_guardian(monkeypatch, score=3, guardian_return=guardian_fake)

        import asyncio
        args = {"version": 99, "source_v": 98, "plan": [], "reviewer_feedback": ""}
        raw = asyncio.run(tool_gates.run_critic.handler(args))
        res = json.loads(raw["content"][0]["text"])

        # Guardian diagnosis should be in result (existing behavior)
        assert "regression_guardian" in res
        assert res["regression_guardian"]["diagnosis"] == "Preflop range too wide against tight opponents"

        # NEW: JSONL file should have been written
        guardian_file = tmp_path / "regression_guardian.jsonl"
        assert guardian_file.exists(), "regression_guardian.jsonl should be created"
        entries = [json.loads(line) for line in guardian_file.read_text().strip().split("\n")]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["version"] == 99
        assert entry["source_v"] == 98
        assert entry["score"] == 3.0
        assert entry["diagnosis"] == "Preflop range too wide against tight opponents"
        assert entry["root_cause"] == "over-aggression"
        assert entry["severity"] == "major"
        assert entry["recovery_recommendation"] == "tighten opening ranges"
        assert "timestamp" in entry

    def test_jsonl_not_written_when_no_diagnosis_text(self, monkeypatch, tmp_path):
        """Guardian returns empty diagnosis -> no JSONL entry."""
        import evolution_infra
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        guardian_fake = {
            "diagnosis": "",  # empty diagnosis
            "failure_stage": "workers",
            "root_cause": "unknown",
            "severity": "minor",
        }
        tool_gates = self._patch_critic_for_guardian(monkeypatch, score=2, guardian_return=guardian_fake)

        import asyncio
        args = {"version": 99, "source_v": 98, "plan": [], "reviewer_feedback": ""}
        asyncio.run(tool_gates.run_critic.handler(args))

        guardian_file = tmp_path / "regression_guardian.jsonl"
        assert not guardian_file.exists(), "No JSONL should be written when diagnosis is empty"

    def test_jsonl_not_written_on_approved_score(self, monkeypatch, tmp_path):
        """Score >= 6 -> guardian not called, no JSONL entry."""
        import evolution_infra
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        tool_gates = self._patch_critic_for_guardian(
            monkeypatch, score=7,
            guardian_return={"diagnosis": "should not be called"})

        import asyncio
        args = {"version": 99, "source_v": 98, "plan": [], "reviewer_feedback": ""}
        asyncio.run(tool_gates.run_critic.handler(args))

        guardian_file = tmp_path / "regression_guardian.jsonl"
        assert not guardian_file.exists()

    def test_jsonl_append_multiple_entries(self, monkeypatch, tmp_path):
        """Multiple low-score runs should append entries, not overwrite."""
        import evolution_infra
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        tool_gates = self._patch_critic_for_guardian(
            monkeypatch, score=3,
            guardian_return={
                "diagnosis": "First regression",
                "severity": "major",
                "root_cause": "cause1",
                "recovery_recommendation": "fix1",
            })

        import asyncio
        args1 = {"version": 99, "source_v": 98, "plan": [], "reviewer_feedback": ""}
        asyncio.run(tool_gates.run_critic.handler(args1))

        # Second run with different version
        args2 = {"version": 101, "source_v": 100, "plan": [], "reviewer_feedback": ""}
        tool_gates2 = self._patch_critic_for_guardian(
            monkeypatch, score=2,
            guardian_return={
                "diagnosis": "Second regression",
                "severity": "critical",
                "root_cause": "cause2",
                "recovery_recommendation": "fix2",
            })
        asyncio.run(tool_gates2.run_critic.handler(args2))

        guardian_file = tmp_path / "regression_guardian.jsonl"
        entries = [json.loads(line) for line in guardian_file.read_text().strip().split("\n")]
        assert len(entries) == 2
        assert entries[0]["version"] == 99
        assert entries[0]["diagnosis"] == "First regression"
        assert entries[1]["version"] == 101
        assert entries[1]["diagnosis"] == "Second regression"


# ──────────────────────────────────────────────
# Test 2: Guardian insights in orchestrator context
# ──────────────────────────────────────────────

class TestRegressionGuardianInContext:
    """orchestrator_context._build_context should inject regression_guardian
    insights when regression_guardian.jsonl exists (fix-9).
    """

    def test_context_includes_guardian_insights_gen_ctx(self, monkeypatch, tmp_path):
        """gen_ctx path should include guardian insights in context output."""
        import evolution_infra
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        # Write a guardian JSONL entry
        guardian_file = tmp_path / "regression_guardian.jsonl"
        entry = json.dumps({
            "version": 99, "source_v": 98, "score": 2.5,
            "diagnosis": "River play too passive against aggressive opponents",
            "root_cause": "fear of bluffing",
            "severity": "major",
            "recovery_recommendation": "increase river bluff frequency",
            "timestamp": "2026-06-21T10:00:00",
        }, ensure_ascii=False)
        guardian_file.write_text(entry + "\n")

        # Ensure get_active_bots returns something (needed by gen_ctx path)
        monkeypatch.setattr(evolution_infra, "get_active_bots", lambda: ["claude_v98"])

        from orchestrator_context import _build_context
        ctx = MagicMock()
        ctx.current_v = 98
        ctx.next_v = 99
        ctx.strategy = "master"
        ctx.source_v = 98
        ctx.stagnation_info = ""
        ctx.match_analysis = ""
        ctx.replay_spotlight = ""
        ctx.performance_verification = ""
        ctx.crossover_parents = None

        result = _build_context(one_gen=False, gen_ctx=ctx)
        assert "REGRESSION GUARDIAN INSIGHTS" in result
        assert "River play too passive" in result
        assert "fear of bluffing" in result
        assert "score=2.5" in result

    def test_context_includes_guardian_insights_non_gen_ctx(self, monkeypatch, tmp_path):
        """Non-gen_ctx path should include guardian insights."""
        import evolution_infra
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        guardian_file = tmp_path / "regression_guardian.jsonl"
        entry = json.dumps({
            "version": 99, "source_v": 98, "score": 1.5,
            "diagnosis": "Complete failure in position play",
            "root_cause": "no positional awareness",
            "severity": "critical",
            "recovery_recommendation": "add position-based adjustments",
            "timestamp": "2026-06-21T11:00:00",
        }, ensure_ascii=False)
        guardian_file.write_text(entry + "\n")

        # Mock evolution_core / evolution_infra dependencies for non-gen_ctx path
        monkeypatch.setattr(evolution_infra, "get_active_bots", lambda: ["claude_v98"])
        monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 98)

        from glicko2 import Glicko2Player
        mock_ratings = {"claude_v98": Glicko2Player(r=1500, rd=100, sigma=0.06)}
        monkeypatch.setattr(evolution_infra, "load_ratings", lambda: mock_ratings)

        from orchestrator_context import _build_context
        result = _build_context(one_gen=False, dry_run=False, gen_ctx=None)
        assert "REGRESSION GUARDIAN INSIGHTS" in result
        assert "Complete failure in position play" in result

    def test_context_no_guardian_section_when_file_missing(self, monkeypatch, tmp_path):
        """When regression_guardian.jsonl doesn't exist, no guardian section in context."""
        import evolution_infra
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(evolution_infra, "get_active_bots", lambda: ["claude_v98"])

        from orchestrator_context import _build_context
        ctx = MagicMock()
        ctx.current_v = 98
        ctx.next_v = 99
        ctx.strategy = "master"
        ctx.source_v = 98
        ctx.stagnation_info = ""
        ctx.match_analysis = ""
        ctx.replay_spotlight = ""
        ctx.performance_verification = ""
        ctx.crossover_parents = None

        result = _build_context(one_gen=False, gen_ctx=ctx)
        assert "REGRESSION GUARDIAN INSIGHTS" not in result

    def test_context_limits_guardian_to_3_entries(self, monkeypatch, tmp_path):
        """Context should show at most 3 recent guardian entries."""
        import evolution_infra
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(evolution_infra, "get_active_bots", lambda: ["claude_v98"])

        guardian_file = tmp_path / "regression_guardian.jsonl"
        lines = []
        for i in range(5):
            entry = json.dumps({
                "version": 90 + i, "source_v": 89 + i, "score": 2.0 + i * 0.1,
                "diagnosis": f"Diagnosis {i}",
                "root_cause": f"cause_{i}",
                "severity": "minor",
                "recovery_recommendation": f"fix_{i}",
                "timestamp": f"2026-06-21T{10+i}:00:00",
            }, ensure_ascii=False)
            lines.append(entry)
        guardian_file.write_text("\n".join(lines) + "\n")

        from orchestrator_context import _build_context
        ctx = MagicMock()
        ctx.current_v = 98
        ctx.next_v = 99
        ctx.strategy = "master"
        ctx.source_v = 98
        ctx.stagnation_info = ""
        ctx.match_analysis = ""
        ctx.replay_spotlight = ""
        ctx.performance_verification = ""
        ctx.crossover_parents = None

        result = _build_context(one_gen=False, gen_ctx=ctx)
        # Should contain last 3 entries (v92, v93, v94) but not v90, v91
        assert "Diagnosis 2" in result
        assert "Diagnosis 3" in result
        assert "Diagnosis 4" in result
        assert "Diagnosis 0" not in result
        assert "Diagnosis 1" not in result
