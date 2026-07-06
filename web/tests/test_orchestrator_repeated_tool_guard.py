import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "web" / "core"))


def _patch_checkpoint(monkeypatch, checkpoint):
    import orchestrator

    monkeypatch.setattr(
        orchestrator,
        "_read_checkpoint_for_repeated_tool_guard",
        lambda: dict(checkpoint),
    )
    return orchestrator


def test_repeated_run_master_after_validation_failure_is_corrective(monkeypatch):
    orchestrator = _patch_checkpoint(
        monkeypatch,
        {
            "next_v": 64,
            "source_v": 63,
            "stage": "direction_audited",
            "master_plan": None,
            "audit_attempt": 1,
            "audit_context": {
                "master_validation": {
                    "errors": ["EXHAUSTED_DIRECTION_REPEATED"],
                },
            },
        },
    )

    result = orchestrator._classify_allowed_repeated_pipeline_tool("run_master", {})

    assert result["reason"] == "corrective_master_replan"
    assert result["audit_attempt"] == 1


def test_repeated_run_master_without_failure_context_stays_redundant(monkeypatch):
    orchestrator = _patch_checkpoint(
        monkeypatch,
        {
            "next_v": 64,
            "source_v": 63,
            "stage": "direction_audited",
            "master_plan": None,
            "audit_attempt": 0,
        },
    )

    assert orchestrator._classify_allowed_repeated_pipeline_tool("run_master", {}) is None


def test_repeated_execute_workers_on_quality_failed_route_is_corrective(monkeypatch):
    orchestrator = _patch_checkpoint(
        monkeypatch,
        {
            "next_v": 65,
            "source_v": 64,
            "stage": "quality_failed",
            "master_plan": {"tasks": []},
            "gate_results": {"quality": {"passed": False}},
        },
    )

    result = orchestrator._classify_allowed_repeated_pipeline_tool("execute_workers", {})

    assert result["reason"] == "corrective_gate_reentry"
    assert result["stage"] == "quality_failed"


def test_second_quality_gate_without_repair_history_stays_redundant(monkeypatch):
    orchestrator = _patch_checkpoint(
        monkeypatch,
        {
            "next_v": 65,
            "source_v": 64,
            "stage": "workers_done",
            "master_plan": {"tasks": []},
            "gate_results": {},
        },
    )

    assert orchestrator._classify_allowed_repeated_pipeline_tool("run_quality_gates", {}) is None


def test_second_quality_gate_after_repair_is_corrective(monkeypatch):
    orchestrator = _patch_checkpoint(
        monkeypatch,
        {
            "next_v": 65,
            "source_v": 64,
            "stage": "workers_done",
            "master_plan": {"tasks": []},
            "reviewer_feedback": "quality failed: fix exact blocker",
            "gate_results": {"quality": {"passed": False}},
        },
    )

    result = orchestrator._classify_allowed_repeated_pipeline_tool("run_quality_gates", {})

    assert result["reason"] == "corrective_gate_reentry"
    assert result["stage"] == "workers_done"
