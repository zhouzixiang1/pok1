"""Tests for audit agent Pydantic schemas and safe defaults."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from output_schema import (
    MasterPlanAuditResult, WorkerCoTCheckResult,
    DegenerationDiagnosis,
    CrossoverCompatibilityResult,
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
            "evidence_alignment": "misaligned",
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
            "focus_areas": ["Check fold frequency in policy.py"],
        }
        result, errors = validate_agent_output("worker_cot_checker", data)
        assert not errors
        assert result["cot_consistent"] is False
        assert len(result["focus_areas"]) == 1

    def test_defaults(self):
        result, errors = validate_agent_output("worker_cot_checker", {})
        assert not errors
        assert result["cot_consistent"] is True


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
            "suggested_merge_approach": "Merge the compatible policy branches",
            "files_to_take_from_a": ["policy.py"],
            "files_to_take_from_b": ["policy.py"],
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


class TestAuditAgentsSafeDefaults:
    """Test that audit agent functions return safe defaults on import."""

    def test_audit_agents_importable(self):
        from audit_agents import (
            _run_master_plan_audit,
            _run_worker_cot_check,
            _run_degeneration_diagnosis,
            _run_crossover_compatibility_audit,
        )
        # All functions should be importable
        assert callable(_run_master_plan_audit)
        assert callable(_run_worker_cot_check)
        assert callable(_run_degeneration_diagnosis)
        assert callable(_run_crossover_compatibility_audit)

    def test_retired_roles_have_no_schema_or_callable_surface(self):
        import audit_agents
        import output_schema

        retired_schemas = {
            "dynamic_test_generator",
            "precommit_semantic",
            "experience_pool_audit",
            "regression_guardian",
        }
        assert retired_schemas.isdisjoint(AGENT_SCHEMAS)
        for class_name in (
            "DynamicTestScenario",
            "DynamicTestSuite",
            "PrecommitSemanticResult",
            "ExperiencePoolAuditResult",
        ):
            assert not hasattr(output_schema, class_name)
        for function_name in (
            "_generate_dynamic_tests",
            "_run_precommit_semantic",
            "_run_experience_pool_audit",
            "_run_regression_guardian",
        ):
            assert not hasattr(audit_agents, function_name)
