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
