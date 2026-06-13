"""Tests for audit agent Pydantic schemas and safe defaults."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from output_schema import (
    MasterPlanAuditResult, WorkerCoTCheckResult, DynamicTestScenario,
    DynamicTestSuite, PrecommitSemanticResult, DegenerationDiagnosis,
    CrossoverCompatibilityResult, ExperiencePoolAuditResult,
    AGENT_SCHEMAS, validate_agent_output,
)


class TestMasterPlanAuditResult:
    def test_valid_pass(self):
        data = {"plan_coherent": True, "overall_pass": True, "feedback": ""}
        result, errors = validate_agent_output("master_plan_auditor", data)
        assert not errors
        assert result["overall_pass"] is True

    def test_valid_fail_with_contradictions(self):
        data = {
            "plan_coherent": False,
            "contradiction_found": True,
            "contradictions": ["Task 1 and Task 2 contradict"],
            "experience_alignment": "misaligned",
            "direction_novelty": "repetitive",
            "overall_pass": False,
            "feedback": "Tasks contradict each other",
            "retry_recommended": True,
        }
        result, errors = validate_agent_output("master_plan_auditor", data)
        assert not errors
        assert result["overall_pass"] is False
        assert len(result["contradictions"]) == 1

    def test_defaults(self):
        result, errors = validate_agent_output("master_plan_auditor", {})
        assert not errors
        assert result["plan_coherent"] is True
        assert result["overall_pass"] is True
        assert result["retry_recommended"] is False

    def test_in_schema_registry(self):
        assert "master_plan_auditor" in AGENT_SCHEMAS


class TestWorkerCoTCheckResult:
    def test_consistent(self):
        data = {"worker_id": 1, "cot_consistent": True}
        result, errors = validate_agent_output("worker_cot_checker", data)
        assert not errors
        assert result["cot_consistent"] is True

    def test_inconsistent_with_focus_areas(self):
        data = {
            "worker_id": 2,
            "cot_consistent": False,
            "discrepancies": ["Claimed bluff but no bluff code"],
            "logical_contradictions": ["Said increase aggression but added fold"],
            "boundary_violations": [],
            "focus_areas": ["Check fold frequency in strategy.py"],
        }
        result, errors = validate_agent_output("worker_cot_checker", data)
        assert not errors
        assert result["cot_consistent"] is False
        assert len(result["focus_areas"]) == 1

    def test_defaults(self):
        result, errors = validate_agent_output("worker_cot_checker", {})
        assert not errors
        assert result["cot_consistent"] is True


class TestDynamicTestSuite:
    def test_valid_scenarios(self):
        data = {
            "scenarios": [
                {
                    "id": "dynamic_001",
                    "description": "Test bluff doesn't fold nuts",
                    "input": {"requests": [{"public_cards": [0, 4, 8], "my_cards": [0, 1]}], "responses": []},
                    "expected_actions": ["call", "raise"],
                    "forbidden_actions": ["fold"],
                    "rationale": "Worker added bluff logic",
                },
            ]
        }
        result, errors = validate_agent_output("dynamic_test_generator", data)
        assert not errors
        assert len(result["scenarios"]) == 1

    def test_max_10_scenarios(self):
        scenarios = [
            {"id": f"dynamic_{i:03d}", "description": f"Test {i}", "input": {}}
            for i in range(11)
        ]
        data = {"scenarios": scenarios}
        result, errors = validate_agent_output("dynamic_test_generator", data)
        assert errors  # Should fail with >10 scenarios


class TestPrecommitSemanticResult:
    def test_proceed(self):
        data = {
            "win_pattern_analysis": "Wins distributed evenly",
            "regression_semantics": "safe",
            "recommended_action": "proceed",
            "confidence": "high",
        }
        result, errors = validate_agent_output("precommit_semantic", data)
        assert not errors
        assert result["recommended_action"] == "proceed"

    def test_block(self):
        data = {"recommended_action": "block", "regression_semantics": "clear_regression"}
        result, errors = validate_agent_output("precommit_semantic", data)
        assert not errors
        assert result["recommended_action"] == "block"

    def test_defaults(self):
        result, errors = validate_agent_output("precommit_semantic", {})
        assert not errors
        assert result["recommended_action"] == "proceed"


class TestDegenerationDiagnosis:
    def test_not_degenerating(self):
        data = {"is_degenerating": False, "recommendation": "continue"}
        result, errors = validate_agent_output("degeneration_diagnosis", data)
        assert not errors
        assert result["is_degenerating"] is False

    def test_urgent_intervention(self):
        data = {
            "is_degenerating": True,
            "root_causes": ["Strategy decay in postflop"],
            "recommendation": "crossover",
            "urgent_intervention": True,
        }
        result, errors = validate_agent_output("degeneration_diagnosis", data)
        assert not errors
        assert result["urgent_intervention"] is True


class TestCrossoverCompatibilityResult:
    def test_compatible(self):
        data = {
            "compatible": True,
            "compatibility_score": 8,
            "conflict_areas": [],
            "suggested_merge_approach": "Take strategy.py from A",
            "files_to_take_from_a": ["strategy.py"],
            "files_to_take_from_b": ["constants.py"],
        }
        result, errors = validate_agent_output("crossover_compatibility", data)
        assert not errors
        assert result["compatibility_score"] == 8

    def test_incompatible(self):
        data = {"compatible": False, "compatibility_score": 2, "conflict_areas": ["Card encoding mismatch"]}
        result, errors = validate_agent_output("crossover_compatibility", data)
        assert not errors
        assert result["compatible"] is False

    def test_score_range(self):
        # Score must be 1-10
        data = {"compatible": True, "compatibility_score": 0}
        result, errors = validate_agent_output("crossover_compatibility", data)
        assert errors  # Should fail with score < 1


class TestExperiencePoolAuditResult:
    def test_healthy(self):
        data = {"overall_health": "healthy"}
        result, errors = validate_agent_output("experience_pool_audit", data)
        assert not errors

    def test_needs_cleanup(self):
        data = {
            "stale_entries": ["v8 preflop strategy is outdated"],
            "contradictions": ["Increase vs decrease aggression"],
            "overall_health": "needs_cleanup",
        }
        result, errors = validate_agent_output("experience_pool_audit", data)
        assert not errors
        assert len(result["stale_entries"]) == 1


class TestAuditAgentsSafeDefaults:
    """Test that audit agent functions return safe defaults on import."""

    def test_audit_agents_importable(self):
        from audit_agents import (
            _run_master_plan_audit,
            _run_worker_cot_check,
            _generate_dynamic_tests,
            _run_precommit_semantic,
            _run_degeneration_diagnosis,
            _run_crossover_compatibility_audit,
            _run_experience_pool_audit,
            _run_regression_guardian,
        )
        # All functions should be importable
        assert callable(_run_master_plan_audit)
        assert callable(_run_worker_cot_check)
        assert callable(_generate_dynamic_tests)
        assert callable(_run_precommit_semantic)
        assert callable(_run_degeneration_diagnosis)
        assert callable(_run_crossover_compatibility_audit)
        assert callable(_run_experience_pool_audit)
        assert callable(_run_regression_guardian)


class TestRunCriticRegressionGuardianInline:
    """P2-7: run_critic awaits the Regression Guardian synchronously and merges
    its diagnosis into the tool result (no longer fire-and-forget).
    """

    def _patch_critic_dependencies(self, monkeypatch, score, guardian_return,
                                   guardian_side_effect=None):
        import asyncio
        import json
        from unittest.mock import AsyncMock, MagicMock

        # conftest imports server.app which loads the module as
        # ``core.tool_gates``; this file's sys.path entry also exposes the bare
        # ``tool_gates`` name. Patch the SAME module object that
        # ``run_critic.handler`` resolves its globals from, so prefer the
        # ``core.*`` form when present.
        import importlib
        tool_gates = sys.modules.get("core.tool_gates") or importlib.import_module("tool_gates")
        audit_agents = sys.modules.get("core.audit_agents") or importlib.import_module("audit_agents")

        fake_ui = MagicMock()
        fake_ui.get_output.return_value = ""

        monkeypatch.setattr(tool_gates, "_run_critic", AsyncMock(return_value={
            "score": score,
            "approved": score >= 6,
            "feedback": "weak strategy" if score < 6 else "good",
            "strategic_assessment": "poor" if score < 6 else "solid",
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

        if guardian_side_effect is not None:
            mock_guardian = AsyncMock(side_effect=guardian_side_effect)
        else:
            mock_guardian = AsyncMock(return_value=guardian_return)
        monkeypatch.setattr(audit_agents, "_run_regression_guardian", mock_guardian)
        return tool_gates, mock_guardian

    def _call(self, tool_gates, args):
        import asyncio
        import json
        raw = asyncio.run(tool_gates.run_critic.handler(args))
        return json.loads(raw["content"][0]["text"])

    def test_low_score_merges_guardian_diagnosis(self, monkeypatch):
        guardian_fake = {
            "diagnosis": "Preflop range too wide",
            "failure_stage": "workers",
            "root_cause": "over-aggression",
            "severity": "major",
            "recovery_recommendation": "tighten ranges",
            "confidence": "medium",
            "systematic_issue": "yes",
        }
        tool_gates, mock_guardian = self._patch_critic_dependencies(
            monkeypatch, score=3, guardian_return=guardian_fake)

        args = {"version": 99, "source_v": 98, "plan": [],
                "reviewer_feedback": ""}
        res = self._call(tool_gates, args)

        # Guardian was awaited (synchronous, not fire-and-forget)
        mock_guardian.assert_awaited_once()
        # Critic still forces retry_workers — guardian is NOT a hard second gate
        assert res["approved"] is False
        assert res["score"] == 3.0
        assert res["action"] == "retry_workers"
        # Diagnosis is visible to the Orchestrator in the result
        assert "regression_guardian" in res
        rg = res["regression_guardian"]
        assert rg["severity"] == "major"
        assert rg["failure_stage"] == "workers"
        assert rg["recovery_recommendation"] == "tighten ranges"
        assert rg["diagnosis"] == "Preflop range too wide"
        assert rg["root_cause"] == "over-aggression"
        assert rg["confidence"] == "medium"

    def test_guardian_exception_does_not_crash(self, monkeypatch):
        tool_gates, mock_guardian = self._patch_critic_dependencies(
            monkeypatch, score=2,
            guardian_return=None,
            guardian_side_effect=RuntimeError("guardian boom"))

        args = {"version": 99, "source_v": 98, "plan": [],
                "reviewer_feedback": ""}
        # Must not raise
        res = self._call(tool_gates, args)

        assert res["approved"] is False
        assert res["action"] == "retry_workers"
        # No diagnosis merged when the guardian threw
        assert "regression_guardian" not in res

    def test_approved_score_skips_guardian(self, monkeypatch):
        guardian_fake = {
            "diagnosis": "should not run", "severity": "major",
            "failure_stage": "workers", "recovery_recommendation": "x",
        }
        tool_gates, mock_guardian = self._patch_critic_dependencies(
            monkeypatch, score=7, guardian_return=guardian_fake)

        args = {"version": 99, "source_v": 98, "plan": [],
                "reviewer_feedback": ""}
        res = self._call(tool_gates, args)

        assert res["approved"] is True
        assert res["action"] == "approve"
        mock_guardian.assert_not_awaited()
        assert "regression_guardian" not in res

