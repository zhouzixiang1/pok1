import hashlib
import json

import pytest

from core.pipeline_state import (
    TIMEOUT_ABANDONABLE_STAGES,
    generic_abandon_block as _generic_abandon_block,
    head_drift_allowed_tools,
    head_drift_resume_policy,
    literature_probe_receipt_binding,
    next_tool_for_checkpoint as _next_tool_for_checkpoint,
    route_policy as _route_policy,
    session_recoverable_stages,
    validate_stage_transition,
)
from core.tool_helpers import (
    _critic_gate_ok,
    _prepare_official_profile_refresh as _prepare_official_profile_refresh_impl,
)
from core.pipeline_infrastructure import (
    build_infrastructure_failure,
    infrastructure_attempt_key,
)
from core.national_runtime_probe import runtime_probe_native_template_evidence
from core.tool_planning import (
    _critic_advisory_rework_refusal,
    _has_legacy_critic_repair_contract,
    _synthesize_rework_tasks_from_checkpoint,
)


def _digest(payload):
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _strict_checkpoint(checkpoint):
    """Add a complete schema-2 allocation/parent envelope to route fixtures.

    Route tests should exercise the active state machine, not accidentally
    assert against the fail-closed legacy-checkpoint route.  The immutable Git
    identities below are synthetic, but they have the exact shape persisted by
    the schema-2 allocator.  Tests that cover legacy recovery call the raw
    production router instead of this helper.
    """

    if not isinstance(checkpoint, dict) or checkpoint.get("epoch_binding"):
        return checkpoint
    target = checkpoint.get("next_v")
    source = checkpoint.get("source_v")
    if type(target) is not int or type(source) is not int or target < 144 or source < 143:
        return checkpoint
    parent2 = checkpoint.get("parent2_v")
    parents = [source] + ([parent2] if type(parent2) is int else [])
    identities = [
        {
            "version": version,
            "bot": f"national_v{version}",
            "role": "parent_source",
            "epoch": "national_tcp_policy_v1",
            "runtime_manifest_digest": "1" * 64,
            "epoch_receipt_digest": "2" * 64,
            "publication_identity_digest": "3" * 64,
            "certificate_digest": "4" * 64,
            "completion_tag": f"national-bot-v{version}",
            "completion_tag_object_oid": "5" * 40,
            "high_water_tag": f"national-high-water-v{version}",
            "high_water_tag_object_oid": "6" * 40,
            "publication_commit_oid": "7" * 40,
            "completion_tree_oid": "8" * 40,
            "tag_artifact_hash": "9" * 64,
        }
        for version in parents
    ]
    payload = {
        "schema_version": 2,
        "epoch": "national_tcp_policy_v1",
        "mode": "published_strict_parent",
        "next_v": target,
        "source_v": source,
        "parent2_v": parent2 if type(parent2) is int else None,
        "parent_versions": parents,
        "source_artifact_inherited": True,
        "parent_authority": "strict_published_parent_resolution",
        "published_parent_identities": identities,
        "protocol_bootstrap_receipt_digest": None,
        "policy_epoch_reset_receipt_digest": None,
        "published_high_water": target - 1,
        "abandoned_receipt_floor": 0,
        "abandoned_receipt_head_digest": None,
        "allocation_floor": target - 1,
    }
    return {
        **checkpoint,
        "checkpoint_schema_version": 2,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": {**payload, "binding_digest": _digest(payload)},
        "workflow_run_id": checkpoint.get(
            "workflow_run_id", f"generation:{target}:pipeline-state-test"
        ),
        "checkpoint_revision": checkpoint.get("checkpoint_revision", 1),
    }


def route_policy(checkpoint):
    return _route_policy(_strict_checkpoint(checkpoint))


def next_tool_for_checkpoint(checkpoint):
    return _next_tool_for_checkpoint(_strict_checkpoint(checkpoint))


def generic_abandon_block(checkpoint, **kwargs):
    return _generic_abandon_block(_strict_checkpoint(checkpoint), **kwargs)


def _prepare_official_profile_refresh(checkpoint, next_tool):
    return _prepare_official_profile_refresh_impl(
        _strict_checkpoint(checkpoint),
        next_tool,
    )


def test_legacy_checkpoint_routes_only_to_controlled_epoch_reconciliation():
    route = _route_policy({
        "stage": "direction_audited",
        "next_v": 155,
        "source_v": 142,
    })

    assert route["next_tool"] is None
    assert route["allowed_tools"] == []
    assert route["intent"] == "operator_reconcile_checkpoint"
    assert route["failure_class"] == "checkpoint_epoch_incompatible"
    assert "checkpoint_schema_version_missing_or_mismatch" in route["epoch_issues"]


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

    route = route_policy(checkpoint)
    assert route["next_tool"] == "execute_workers"
    assert route["allowed_tools"] == ["execute_workers"]
    assert route["intent"] == "precommit_rework"
    blocked = generic_abandon_block(checkpoint, max_precommit_retries=3)
    assert blocked["blocked"] is True
    assert blocked["next_tool"] == "execute_workers"
    assert blocked["failure_class"] == "regression"


def test_critic_advice_cannot_transition_directly_back_to_workers():
    ok, reason = validate_stage_transition("critic_checked", "workers_done")

    assert ok is False
    assert "backward_transition" in reason


@pytest.mark.parametrize(
    ("stage", "next_tool"),
    [
        ("quality_passed", "run_review"),
        ("reviewed", "run_critic"),
    ],
)
def test_completed_gate_stage_exposes_only_its_canonical_next_tool(
    stage,
    next_tool,
):
    checkpoint = {
        "stage": stage,
        "next_v": 264,
        "source_v": 244,
    }

    route = route_policy(checkpoint)
    assert route["next_tool"] == next_tool
    assert route["allowed_tools"] == [next_tool]
    ok, reason = validate_stage_transition(stage, "workers_done")
    assert ok is False
    assert "backward_transition" in reason


def test_legacy_critic_repair_contract_fails_closed_without_worker_synthesis():
    checkpoint = {
        "stage": "repair_planned",
        "next_v": 264,
        "source_v": 244,
        "reviewer_feedback": "Retired critic advice",
        "master_plan": {
            "work_item": {
                "kind": "critic_repair",
                "route": {"intent": "critic_rework"},
            },
            "tasks": [{
                "task_kind": "critic_repair",
                "repair_blocker": "critic_rejection",
                "target_files": ["policy.py"],
            }],
        },
    }

    assert _has_legacy_critic_repair_contract(
        checkpoint,
        checkpoint["master_plan"]["tasks"],
    ) is True
    assert _synthesize_rework_tasks_from_checkpoint(checkpoint) == []
    refusal = _critic_advisory_rework_refusal(
        checkpoint,
        checkpoint["master_plan"]["tasks"],
        264,
        244,
    )
    assert refusal["error"] == "LEGACY_CRITIC_REPAIR_FORBIDDEN"
    assert refusal["safe_to_auto_execute"] is False
    assert refusal["next_tool"] == "abandon_generation"


def test_schema_valid_critic_advice_routes_to_precommit_not_workers():
    checkpoint = {
        "stage": "critic_checked",
        "next_v": 264,
        "source_v": 244,
        "gate_results": {
            "critic": {
                "critic_llm_executed": True,
                "schema_valid": True,
                "advisory_approved": False,
            }
        },
    }

    refusal = _critic_advisory_rework_refusal(
        checkpoint,
        [],
        264,
        244,
    )
    assert refusal["error"] == "CRITIC_ADVISORY_REWORK_FORBIDDEN"
    assert refusal["safe_to_auto_execute"] is True
    assert refusal["next_tool"] == "run_precommit_eval"


def test_incomplete_legacy_critic_record_refreshes_stale_quality_first(monkeypatch):
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
    assert route["next_tool"] == "run_quality_gates"
    assert route["allowed_tools"] == ["run_quality_gates"]
    assert route["intent"] == "quality_profile_refresh"


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
                "llm_invoked": True,
                "critic_llm_executed": True,
                "schema_valid": True,
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
    assert route["allowed_tools"] == ["run_precommit_eval"]
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


@pytest.mark.parametrize(
    ("stage", "expected_tool"),
    [
        ("preparing", "prepare_next_gen"),
        ("timed_out", "abandon_generation"),
        ("infra_timed_out", "run_precommit_eval"),
    ],
)
def test_recovery_only_stages_expose_their_single_canonical_tool(
    stage,
    expected_tool,
):
    route = route_policy({
        "stage": stage,
        "next_v": 265,
        "source_v": 254,
    })

    assert route["next_tool"] == expected_tool
    assert route["allowed_tools"] == [expected_tool]


def test_timeout_stages_are_fresh_session_recoverable():
    stages = session_recoverable_stages()

    assert "timed_out" in stages
    assert "infra_timed_out" in stages


def test_plain_timeout_allowlist_is_exactly_the_disposable_stage_set():
    assert TIMEOUT_ABANDONABLE_STAGES == frozenset({
        "selected",
        "preparing",
        "prepared",
        "crossover_running",
        "direction_audited",
        "master_planned",
        "workers_done",
        "quality_failed",
    })


def test_timed_out_cannot_restart_preparation_without_canonical_abandon():
    ok, reason = validate_stage_transition("timed_out", "preparing")

    assert ok is False
    assert reason == "timed_out_requires_canonical_abandon: preparing"


@pytest.mark.parametrize(
    "stage",
    [
        "quality_passed",
        "reviewed",
        "critic_checked",
        "precommit_failed",
        "repair_planned",
        "rework_running",
        "verified",
        "official_certifying",
        "official_failed",
    ],
)
def test_timeout_overlay_cannot_erase_non_disposable_stage_authority(stage):
    ok, reason = validate_stage_transition(stage, "timed_out")

    assert ok is False
    assert reason == f"timeout_cannot_erase_stage_authority: {stage}"


def test_infra_timeout_overlay_is_precommit_only():
    assert validate_stage_transition(
        "critic_checked",
        "infra_timed_out",
    ) == (True, "infra_timeout_override")
    ok, reason = validate_stage_transition("reviewed", "infra_timed_out")
    assert ok is False
    assert reason == "infra_timeout_requires_critic_checked: reviewed"


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


def test_route_policy_does_not_expose_unrequired_literature_probe():
    route = route_policy({
        "stage": "direction_audited",
        "next_v": 300,
        "source_v": 299,
    })

    assert route["next_tool"] == "run_master"
    assert route["allowed_tools"] == ["run_master"]


def test_route_policy_requires_literature_probe_receipt_when_stagnant():
    from core.master_context_contract import build_master_context

    checkpoint = {
        "stage": "direction_audited",
        "next_v": 300,
        "source_v": 299,
        "audit_context": {
            "master_context": build_master_context(
                next_v=300,
                source_v=299,
                stagnation_info="STAGNATION_DETECTED (is_stagnant=true)",
            ),
        },
        "direction_audit": {"repetition_detected": False},
    }

    route = route_policy(checkpoint)

    assert route["next_tool"] == "run_literature_probe"
    assert route["intent"] == "mandatory_literature_probe"
    assert route["allowed_tools"] == ["run_literature_probe"]


def test_crossover_infrastructure_overlay_routes_same_tool_without_replanning():
    failure = build_infrastructure_failure(
        None,
        component="national_runtime_probe",
        code="crossover_preplan_probe_inconclusive",
        owner_tool="run_crossover",
        resume_stage="crossover_running",
        attempt_key="preserved-child",
        issues=["probe unavailable"],
    )

    route = route_policy({
        "stage": "crossover_running",
        "next_v": 300,
        "source_v": 299,
        "parent2_v": 250,
        "infra_failure": failure,
    })

    assert route["next_tool"] == "run_crossover"
    assert route["allowed_tools"] == ["run_crossover"]
    assert route["failure_class"] == "infrastructure"


@pytest.mark.parametrize("reason", ["governed_skip", "literature_probe_timeout", "literature_probe_failed"])
def test_route_policy_accepts_identity_bound_literature_attempt_receipts(reason):
    from core.master_context_contract import build_master_context

    checkpoint = {
        "stage": "direction_audited",
        "next_v": 300,
        "source_v": 299,
        "direction_audit": {"repetition_detected": True},
        "audit_context": {
            "master_context": build_master_context(
                next_v=300,
                source_v=299,
                stagnation_info="STAGNATION_DETECTED (is_stagnant=true)",
            ),
        },
    }
    binding, errors = literature_probe_receipt_binding(checkpoint)
    assert not errors
    checkpoint["literature_probe"] = {
        "next_v": 300,
        "source_v": 299,
        "reason": reason,
        **binding,
    }

    route = route_policy(checkpoint)

    assert route["next_tool"] == "run_master"
    assert "run_master" in route["allowed_tools"]


def test_route_policy_rejects_old_literature_receipt_after_context_change():
    from core.master_context_contract import build_master_context

    checkpoint = {
        "stage": "direction_audited",
        "next_v": 300,
        "source_v": 299,
        "direction_audit": {
            "repetition_detected": True,
            "mandatory_constraints": "avoid static threshold tuning",
        },
        "audit_context": {
            "master_context": build_master_context(
                next_v=300,
                source_v=299,
                stagnation_info="STAGNATION_DETECTED (is_stagnant=true)",
                match_analysis="old H2H weakness",
            ),
        },
    }
    binding, errors = literature_probe_receipt_binding(checkpoint)
    assert not errors
    checkpoint["literature_probe"] = {
        "next_v": 300,
        "source_v": 299,
        "reason": "governed_skip",
        **binding,
    }
    assert route_policy(checkpoint)["next_tool"] == "run_master"

    checkpoint["audit_context"]["master_context"] = build_master_context(
        next_v=300,
        source_v=299,
        stagnation_info="STAGNATION_DETECTED (is_stagnant=true)",
        match_analysis="new H2H weakness",
    )
    assert route_policy(checkpoint)["next_tool"] == "run_literature_probe"

    replacement_binding, errors = literature_probe_receipt_binding(checkpoint)
    assert not errors
    checkpoint["literature_probe"] = {
        "next_v": 300,
        "source_v": 299,
        "reason": "literature_probe_timeout",
        **replacement_binding,
    }
    assert route_policy(checkpoint)["next_tool"] == "run_master"

    checkpoint["direction_audit"]["mandatory_constraints"] = "use a range posterior"
    assert route_policy(checkpoint)["next_tool"] == "run_literature_probe"


@pytest.mark.parametrize(
    ("current_stage", "proposed_stage"),
    [
        ("selected", "preparing"),
        ("selected", "crossover_running"),
        ("preparing", "prepared"),
        ("crossover_running", "prepared"),
        ("prepared", "direction_audited"),
    ],
)
def test_early_generation_stage_edges_are_explicit(current_stage, proposed_stage):
    ok, reason = validate_stage_transition(current_stage, proposed_stage)
    assert ok, reason


@pytest.mark.parametrize(
    ("current_stage", "proposed_stage"),
    [
        ("selected", "prepared"),
        ("preparing", "direction_audited"),
        ("prepared", "crossover_running"),
        ("prepared", "master_planned"),
    ],
)
def test_early_generation_stage_edges_reject_weak_model_reordering(
    current_stage,
    proposed_stage,
):
    ok, reason = validate_stage_transition(current_stage, proposed_stage)
    assert not ok
    assert "early_generation_transition_not_allowed" in reason


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
                **runtime_probe_native_template_evidence(),
            },
            "review": {"approved": True},
            "critic": {"approved": True},
            "precommit_eval": {
                "passed": True,
                "workflow_profile_id": "national_native",
                "national_execution_mode": "native_tcp",
                "evaluation_protocol": "national_native_tcp",
                **runtime_probe_native_template_evidence(),
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
                **runtime_probe_native_template_evidence(),
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


def test_quality_admission_drift_routes_to_fresh_quality_without_exe_or_workers(
    monkeypatch,
):
    """Formal-harness drift is system evidence refresh, not a worker repair."""

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")
    checkpoint = {
        "stage": "official_certifying",
        "next_v": 300,
        "source_v": 299,
        "gate_results": {
            "official_full": {
                "passed": False,
                "outcome": "quality_admission_blocked",
                "failure_class": "quality",
                "quality_admission_refresh": True,
                "repairable_by_workers": False,
            },
        },
    }

    route = route_policy(checkpoint)

    assert route["next_tool"] == "run_quality_gates"
    assert route["allowed_tools"] == ["run_quality_gates"]
    assert route["intent"] == "quality_admission_refresh"
    assert route["failure_class"] == "quality"
    assert "run_quality_gates first" in route["directive"]

    # The commit path clears any completed/failed formal job before recording
    # this marker.  The quality tool accepts the route even with no attachment,
    # rather than polling/retrying the old job.
    refresh = _prepare_official_profile_refresh(checkpoint, "run_quality_gates")
    assert refresh == {
        "ok": True,
        "needed": True,
        "job_state": "missing_attachment",
    }
    refused = _prepare_official_profile_refresh(checkpoint, "commit_bot")
    assert refused["ok"] is False
    assert refused["needed"] is True
    assert refused["route"]["next_tool"] == "run_quality_gates"


def test_quality_admission_refresh_has_a_checkpoint_scoped_head_drift_policy():
    """Only the exact terminal marker may replace an EXE poll after HEAD drift."""

    checkpoint = {
        "stage": "official_certifying",
        "gate_results": {
            "official_full": {
                "outcome": "quality_admission_blocked",
                "failure_class": "quality",
                "quality_admission_refresh": True,
            },
        },
    }

    dynamic = head_drift_resume_policy(
        "official_certifying",
        checkpoint=checkpoint,
    )
    assert dynamic is not None
    assert dynamic["allowed_tools"] == ("run_quality_gates",)
    assert dynamic["resume_kind"] == "quality_admission_refresh"
    assert dynamic["requires_contract_unchanged"] is True
    assert head_drift_allowed_tools(
        "official_certifying",
        checkpoint=checkpoint,
    ) == {"run_quality_gates"}

    # The ordinary official-certifying stage still has an attached durable job
    # and must not gain a broad quality-gate retry permission.
    ordinary = head_drift_resume_policy("official_certifying", checkpoint={
        "stage": "official_certifying",
        "gate_results": {"official_full": {"outcome": "pending"}},
    })
    assert ordinary is not None
    assert ordinary["allowed_tools"] == ("commit_bot",)
    assert head_drift_allowed_tools("official_certifying") == {"commit_bot"}


def test_official_certifying_profile_refresh_transitions_are_legal():
    for target in ("quality_failed", "quality_passed", "precommit_failed", "verified"):
        ok, reason = validate_stage_transition("official_certifying", target)
        assert ok, (target, reason)
        assert reason == "official_profile_refresh"


def test_session_recovery_classification_covers_official_active_stages():
    stages = session_recoverable_stages()

    assert "official_certifying" in stages
    assert "official_failed" in stages
    assert "official_bootstrap_required" not in stages
    assert "official_inconclusive" not in stages
    assert "archived" not in stages


def test_publishing_is_forward_only_recoverable_commit_route():
    checkpoint = {
        "stage": "publishing",
        "next_v": 300,
        "source_v": 299,
        "gate_results": {},
    }

    route = route_policy(checkpoint)

    assert route["next_tool"] == "commit_bot"
    assert route["intent"] == "publication_resume"
    assert route["allowed_tools"] == ["commit_bot"]
    assert "publishing" in session_recoverable_stages()
    ok, reason = validate_stage_transition("publishing", "timed_out")
    assert not ok
    assert reason == "publication_transaction_is_durable"
    blocked = generic_abandon_block(checkpoint)
    assert blocked["blocked"] is True
    assert blocked["next_tool"] == "commit_bot"


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
