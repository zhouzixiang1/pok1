from core.pipeline_state import (
    generic_abandon_block,
    next_tool_for_checkpoint,
    route_policy,
    validate_stage_transition,
)
from core.tool_helpers import _critic_gate_ok


def test_precommit_failed_is_forward_and_reworkable():
    ok, reason = validate_stage_transition("critic_checked", "precommit_failed")
    assert ok, reason

    ok, reason = validate_stage_transition("precommit_failed", "master_planned")
    assert ok, reason
    assert "retry" in reason

    ok, reason = validate_stage_transition("precommit_failed", "repair_planned")
    assert ok, reason
    assert "rework" in reason

    ok, reason = validate_stage_transition("repair_planned", "rework_running")
    assert ok, reason

    ok, reason = validate_stage_transition("rework_running", "workers_done")
    assert ok, reason


def test_old_critic_checked_failed_precommit_routes_to_workers():
    checkpoint = {
        "stage": "critic_checked",
        "next_v": 263,
        "source_v": 244,
        "precommit_attempt": 1,
        "gate_results": {
            "precommit_eval": {
                "passed": False,
                "blockers": [{"reason": "semantic_regression"}],
            }
        },
    }

    assert next_tool_for_checkpoint(checkpoint) == "execute_workers"
    blocked = generic_abandon_block(checkpoint, max_precommit_retries=3)
    assert blocked["blocked"] is True
    assert blocked["next_tool"] == "execute_workers"
    assert blocked["failure_class"] == "regression"


def test_rejected_critic_routes_to_workers_before_precommit(monkeypatch):
    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")
    checkpoint = {
        "stage": "reviewed",
        "next_v": 263,
        "source_v": 244,
        "gate_results": {
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
                "workflow_profile_id": "national_native",
                "national_execution_mode": "native_tcp",
                "national_native_contract_ok": True,
            },
            "review": {"approved": True},
            "critic": {
                "approved": False,
                "raw_approved": False,
                "advisory_approved": False,
                "score": 4,
                "feedback": "bad direction",
            },
        },
    }

    route = route_policy(checkpoint)
    assert route["next_tool"] == "execute_workers"
    assert route["intent"] == "critic_rework"
    assert "Critic rejected" in route["directive"]


def test_critic_force_advanced_no_longer_bypasses_gate():
    checkpoint = {
        "gate_results": {
            "critic": {
                "approved": False,
                "force_advanced": True,
                "raw_approved": False,
                "advisory_approved": False,
                "score": 4,
            }
        }
    }

    assert _critic_gate_ok(checkpoint) is False


def test_precommit_failed_blocks_abandon_until_hard_limit():
    checkpoint = {
        "stage": "precommit_failed",
        "next_v": 263,
        "source_v": 244,
        "precommit_attempt": 1,
        "gate_results": {
            "precommit_eval": {
                "passed": False,
                "blockers": [{"reason": "aggregate_precommit_regression"}],
            }
        },
    }

    blocked = generic_abandon_block(checkpoint, max_precommit_retries=3)
    assert blocked["next_tool"] == "execute_workers"

    checkpoint["precommit_attempt"] = 3
    assert generic_abandon_block(checkpoint, max_precommit_retries=3) is None


def test_precommit_infra_stays_on_precommit_retry():
    checkpoint = {
        "stage": "critic_checked",
        "next_v": 263,
        "source_v": 244,
        "precommit_attempt": 1,
        "gate_results": {
            "precommit_eval": {
                "passed": False,
                "blockers": [{"reason": "match_timeout"}],
            }
        },
    }

    assert next_tool_for_checkpoint(checkpoint) == "run_precommit_eval"
    blocked = generic_abandon_block(checkpoint, max_precommit_retries=3)
    assert blocked["next_tool"] == "run_precommit_eval"


def test_selected_next_tool_distinguishes_master_and_crossover():
    assert next_tool_for_checkpoint({"stage": "selected", "next_v": 265, "source_v": 254}) == "prepare_next_gen"
    assert (
        next_tool_for_checkpoint(
            {"stage": "selected", "next_v": 266, "source_v": 254, "parent2_v": 240}
        )
        == "run_crossover"
    )


def test_route_policy_allows_crossover_quality_repair_workers():
    checkpoint = {
        "stage": "master_planned",
        "next_v": 265,
        "source_v": 243,
        "parent2_v": 249,
        "master_plan": {
            "strategy": "crossover",
            "tasks": [
                {"worker_id": "w1", "target_files": ["state.py"], "worker_prompt": "fix position_semantics"},
            ],
        },
    }

    route = route_policy(checkpoint)
    assert route["next_tool"] == "execute_workers"
    assert route["intent"] == "initial_workers"
    assert "execute_workers" in route["directive"]


def test_route_policy_for_explicit_rework_stage():
    checkpoint = {
        "stage": "repair_planned",
        "next_v": 265,
        "source_v": 243,
        "parent2_v": 249,
        "master_plan": {"strategy": "crossover", "work_item": {"kind": "crossover_quality_repair"}},
    }

    route = route_policy(checkpoint)
    assert route["next_tool"] == "execute_workers"
    assert route["intent"] == "quality_rework"
    assert "Rework" in route["directive"]


def test_route_policy_exposes_literature_probe_as_allowed_pre_master_tool():
    route = route_policy({
        "stage": "direction_audited",
        "next_v": 300,
        "source_v": 299,
    })

    assert route["next_tool"] == "run_master"
    assert "run_master" in route["allowed_tools"]
    assert "run_literature_probe" in route["allowed_tools"]


def test_verified_native_precommit_routes_to_commit_without_quality_contract_flag(monkeypatch):
    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")

    route = route_policy({
        "stage": "verified",
        "next_v": 300,
        "source_v": 299,
        "gate_results": {
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
                "workflow_profile_id": "national_native",
                "national_execution_mode": "native_tcp",
                "national_native_contract_ok": True,
            },
            "review": {"approved": True},
            "critic": {"approved": True},
            "precommit_eval": {
                "passed": True,
                "workflow_profile_id": "national_native",
                "national_execution_mode": "native_tcp",
                "evaluation_protocol": "national_native_tcp",
            },
        },
    })

    assert route["next_tool"] == "commit_bot"
    assert route["intent"] == "pipeline"


def test_verified_old_adapter_precommit_revalidates_under_native_profile(monkeypatch):
    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")

    route = route_policy({
        "stage": "verified",
        "next_v": 300,
        "source_v": 299,
        "gate_results": {
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
                "workflow_profile_id": "national_native",
                "national_execution_mode": "native_tcp",
                "national_native_contract_ok": True,
            },
            "review": {"approved": True},
            "critic": {"approved": True},
            "precommit_eval": {
                "passed": True,
                "workflow_profile_id": "national_primary",
                "national_execution_mode": "adapter",
            },
        },
    })

    assert route["next_tool"] == "run_precommit_eval"
    assert route["intent"] == "precommit_profile_refresh"
