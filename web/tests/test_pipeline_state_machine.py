from core.pipeline_state import (
    generic_abandon_block,
    next_tool_for_checkpoint,
    route_policy,
    session_recoverable_stages,
    validate_stage_transition,
)
from core.tool_helpers import _critic_gate_ok, _prepare_official_profile_refresh
from core.pipeline_infrastructure import (
    build_infrastructure_failure,
    infrastructure_attempt_key,
)


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


def test_completed_critic_advice_does_not_replace_native_precommit_gate():
    checkpoint = {
        "gate_results": {
            "critic": {
                "approved": True,
                "raw_approved": False,
                "advisory_approved": False,
                "score": 2,
                "action": "proceed_to_precommit",
            }
        }
    }

    assert _critic_gate_ok(checkpoint) is True

    routed = {
        "stage": "critic_checked",
        "next_v": 264,
        "source_v": 244,
        **checkpoint,
    }
    route = route_policy(routed)
    assert route["next_tool"] == "run_precommit_eval"
    assert route["intent"] == "precommit_eval"


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


def test_quality_probe_infra_retries_are_identity_bound():
    key = infrastructure_attempt_key(
        component="national_runtime_probe",
        candidate_fingerprint="candidate-a",
        source_fingerprint="source-a",
        harness_identity="probe-v1",
    )
    kwargs = {
        "component": "national_runtime_probe",
        "code": "probe_unavailable",
        "owner_tool": "run_quality_gates",
        "resume_stage": "workers_done",
        "attempt_key": key,
        "issues": ["candidate: bwrap unavailable"],
        "max_attempts": 3,
    }

    first = build_infrastructure_failure(None, now=1, **kwargs)
    second = build_infrastructure_failure(first, now=2, **kwargs)
    terminal = build_infrastructure_failure(second, now=3, **kwargs)

    assert first["attempt"] == 1
    assert first["action"] == "retry_same_tool"
    assert second["attempt"] == 2
    assert terminal["attempt"] == 3
    assert terminal["action"] == "abandon_generation"
    assert terminal["retryable"] is False

    changed = build_infrastructure_failure(
        terminal,
        now=4,
        **{**kwargs, "attempt_key": infrastructure_attempt_key(
            component="national_runtime_probe",
            candidate_fingerprint="candidate-b",
            source_fingerprint="source-a",
            harness_identity="probe-v1",
        )},
    )
    assert changed["attempt"] == 1


def test_quality_probe_infra_state_machine_never_requests_bot_repair():
    key = infrastructure_attempt_key(component="national_runtime_probe")
    common = {
        "component": "national_runtime_probe",
        "code": "probe_unavailable",
        "owner_tool": "run_quality_gates",
        "resume_stage": "workers_done",
        "attempt_key": key,
        "issues": ["bwrap unavailable"],
        "max_attempts": 2,
    }
    first = build_infrastructure_failure(None, now=1, **common)
    exhausted = build_infrastructure_failure(first, now=2, **common)
    retry = {
        "stage": "workers_done",
        "next_v": 301,
        "source_v": 300,
        "gate_results": {},
        "infra_failure": first,
    }
    terminal = {**retry, "infra_failure": exhausted}

    retry_route = route_policy(retry)
    assert retry_route["next_tool"] == "run_quality_gates"
    assert retry_route["intent"] == "infra_retry"
    assert "execute_workers" not in retry_route["allowed_tools"]

    terminal_route = route_policy(terminal)
    assert terminal_route["next_tool"] == "run_quality_gates"
    assert terminal_route["allowed_tools"] == ["run_quality_gates"]
    assert terminal_route["intent"] == "infra_abandon"
    assert "do not edit bot code" in terminal_route["directive"].lower()


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


def test_official_certifying_profile_refresh_transitions_are_legal():
    for target in ("quality_failed", "quality_passed", "precommit_failed", "verified"):
        ok, reason = validate_stage_transition("official_certifying", target)
        assert ok, (target, reason)
        assert reason == "official_profile_refresh"


def test_session_recovery_classification_covers_official_active_stages():
    stages = session_recoverable_stages()

    assert "official_certifying" in stages
    assert "official_failed" in stages
    assert "official_inconclusive" not in stages
    assert "archived" not in stages


def test_profile_refresh_cancels_attached_official_job(monkeypatch):
    import official_certification_job

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")
    calls = []
    monkeypatch.setattr(
        official_certification_job,
        "cancel_job",
        lambda job_id, **kwargs: calls.append((job_id, kwargs)) or {
            "job_id": job_id,
            "state": "cancelled",
        },
    )
    checkpoint = {
        "stage": "official_certifying",
        "next_v": 300,
        "source_v": 299,
        "official_job": {"job_id": "official-job-1"},
        "gate_results": {
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
                "workflow_profile_id": "national_primary",
                "national_execution_mode": "adapter",
            },
        },
    }

    result = _prepare_official_profile_refresh(checkpoint, "run_quality_gates")

    assert result["ok"] is True
    assert result["needed"] is True
    assert result["job_state"] == "cancelled"
    assert calls[0][0] == "official-job-1"
