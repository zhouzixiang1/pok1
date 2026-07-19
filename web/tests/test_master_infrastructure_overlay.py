import asyncio
import json
import shutil
from types import SimpleNamespace

import evolution_infra
import pytest
import tool_planning

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")


def test_strict_authority_failure_uses_control_plane_abandon(monkeypatch):
    from strict_authority_workflow import StrictAuthorityError

    captured = {}

    async def fake_abandon(next_v, source_v, **kwargs):
        captured.update({"next_v": next_v, "source_v": source_v, **kwargs})
        return {"classified": "control_plane"}

    monkeypatch.setattr(
        tool_planning,
        "_abandon_master_generation",
        fake_abandon,
    )
    result = asyncio.run(tool_planning._abandon_strict_master_authority(
        143,
        142,
        error=StrictAuthorityError([
            "strict_authority_phase_slot_context_drift:master:proposal:mechanism"
        ]),
        ui=None,
    ))

    assert result == {"classified": "control_plane"}
    assert captured["error"] == "SYSTEM_STRICT_AUTHORITY_INVALID"
    assert captured["fail_count"] == 0
    assert captured["payload"]["failure_class"] == "control_plane"
    assert captured["payload"]["validation_errors"] == [
        "strict_authority_phase_slot_context_drift:master:proposal:mechanism"
    ]


def test_master_checkpoint_authority_failure_blocks_without_abandon_or_llm_retry(
    monkeypatch,
):
    from agent_master import MasterAuthorityError

    async def forbidden_abandon(*_args, **_kwargs):
        raise AssertionError("deterministic authority failure must not spend a label")

    monkeypatch.setattr(
        tool_planning,
        "_abandon_master_generation",
        forbidden_abandon,
    )
    result = asyncio.run(tool_planning._block_master_authority(
        147,
        143,
        error=MasterAuthorityError(
            143,
            147,
            "a" * 64,
            ["protocol_bootstrap_live_allocation:checkpoint_abandoned_receipt_head_changed"],
        ),
        ui=None,
    ))
    payload = json.loads(result["content"][0]["text"])

    assert payload["error"] == "MASTER_AUTHORITY_RECOVERY_BLOCKED"
    assert payload["failure_class"] == "control_plane"
    assert payload["recovery_blocked"] is True
    assert payload["retryable"] is False
    assert payload["action"] == "repair_master_authority_contract"


def test_orchestrator_preserves_checkpoint_for_typed_authority_block():
    import orchestrator

    checkpoint = {
        "next_v": 147,
        "source_v": 143,
        "stage": "direction_audited",
    }
    classified = orchestrator._classify_recovery_after_deterministic_route(
        {"action": "resume", "checkpoint": checkpoint},
        {
            "result": {
                "error": "MASTER_AUTHORITY_RECOVERY_BLOCKED",
                "failure_class": "control_plane",
                "recovery_blocked": True,
                "action": "repair_master_authority_contract",
                "validation_errors": ["live_allocation_drift"],
            },
        },
        {"action": "resume", "checkpoint": checkpoint},
    )

    assert classified["action"] == "blocked"
    assert classified["checkpoint"] is checkpoint
    assert classified["diagnostics"]["issues"] == ["live_allocation_drift"]
    assert classified["diagnostics"]["operator_action"] == (
        "repair_master_authority_contract"
    )


def test_master_llm_transport_uses_bounded_six_attempt_recovery_budget(
    monkeypatch,
):
    captured = {}

    monkeypatch.setattr(
        tool_planning,
        "_matching_checkpoint",
        lambda *_args: {"runtime_contract_ledger": {"ledger_digest": "d" * 64}},
    )
    monkeypatch.setattr(
        tool_planning,
        "_complete_artifact_fingerprint",
        lambda *_args: "candidate-fingerprint",
    )
    monkeypatch.setattr(
        tool_planning,
        "_master_source_fingerprint",
        lambda *_args: "source-fingerprint",
    )

    async def fake_record(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "action": "retry_same_tool",
            "abandoned": False,
            "infra_failure": {"attempt": 3, "max_attempts": kwargs["max_attempts"]},
        }

    monkeypatch.setattr(
        tool_planning,
        "_record_infrastructure_failure",
        fake_record,
    )

    payload = json.loads(asyncio.run(
        tool_planning._handle_master_llm_infrastructure(
            147,
            143,
            None,
            component="master_llm",
            issue="proposal_scout:counterfactual:stall",
            prompt_digest="a" * 64,
        )
    )["content"][0]["text"])

    assert captured["max_attempts"] == 6
    assert captured["owner_tool"] == "run_master"
    assert captured["resume_stage"] == "direction_audited"
    assert payload["action"] == "retry_same_tool"
    assert payload["abandoned"] is False


def test_journaled_master_role_park_is_attempt_neutral(monkeypatch):
    from agent_master import MasterEnsembleInfrastructureParked

    cleared = []
    record_calls = []
    monkeypatch.setattr(
        tool_planning,
        "_clear_master_runtime_heartbeat",
        lambda *args: cleared.append(args),
    )
    monkeypatch.setattr(
        tool_planning,
        "_record_infrastructure_failure",
        lambda *_args, **_kwargs: record_calls.append(True),
    )
    monkeypatch.setattr(tool_planning, "log_system_event", lambda *_a, **_k: None)
    error = MasterEnsembleInfrastructureParked(
        143,
        147,
        "a" * 64,
        "proposal_scout:counterfactual:stall",
        slot="proposal:counterfactual",
        retry_state={
            "run_id": "generation:147:test:strict-authority-v3",
            "role_attempt": 2,
            "accepted_slots": [
                "proposal:mechanism",
                "proposal:compute_memory",
            ],
            "pending_slots": [
                "proposal:counterfactual",
                "ballot:falsification",
                "ballot:scope",
            ],
        },
    )

    payload = json.loads(
        tool_planning._handle_master_ensemble_provider_parked(
            147,
            143,
            None,
            error,
        )["content"][0]["text"]
    )

    assert cleared == [(147, 143)]
    assert record_calls == []
    assert payload["error"] == "MASTER_ENSEMBLE_PROVIDER_PARKED"
    assert payload["pending"] is True
    assert payload["action"] == "retry_same_tool"
    assert payload["checkpoint_preserved"] is True
    assert payload["abandoned"] is False
    assert payload["role_attempt"] == 2
    assert payload["retry_after_sec"] == 10.0

    exhausted_role = MasterEnsembleInfrastructureParked(
        143,
        147,
        "a" * 64,
        "proposal_scout:counterfactual:stall",
        slot="proposal:counterfactual",
        retry_state={
            "run_id": "generation:147:test:strict-authority-v3",
            "role_attempt": 3,
            "accepted_slots": [
                "proposal:mechanism",
                "proposal:compute_memory",
            ],
            "pending_slots": [
                "proposal:counterfactual",
                "ballot:falsification",
                "ballot:scope",
            ],
        },
    )
    attention = json.loads(
        tool_planning._handle_master_ensemble_provider_parked(
            147,
            143,
            None,
            exhausted_role,
        )["content"][0]["text"]
    )
    assert attention["error"] == (
        "MASTER_ENSEMBLE_PROVIDER_ATTENTION_REQUIRED"
    )
    assert attention["pending"] is False
    assert attention["action"] == "operator_attention_required"
    assert attention["checkpoint_preserved"] is True
    assert attention["abandoned"] is False
    assert attention["needs_attention"] is True
    assert attention["recovery_blocked"] is True
    assert attention["validation_errors"] == [
        "master_ensemble_provider_role_retry_exhausted:"
        "proposal:counterfactual:attempt_3"
    ]

    classified = __import__("orchestrator")._classify_recovery_after_deterministic_route(
        {
            "action": "resume",
            "checkpoint": {
                "workflow_run_id": "generation:147:test",
                "next_v": 147,
                "source_v": 143,
                "stage": "direction_audited",
            },
        },
        {"result": attention},
        {
            "action": "resume",
            "checkpoint": {
                "workflow_run_id": "generation:147:test",
                "next_v": 147,
                "source_v": 143,
                "stage": "direction_audited",
            },
        },
    )
    assert classified["action"] == "blocked"
    assert classified["checkpoint"]["next_v"] == 147
    assert classified["diagnostics"]["operator_action"] == (
        "operator_attention_required"
    )


def test_fresh_v143_architecture_policy_uses_live_prepared_baseline(
    tmp_path,
    monkeypatch,
):
    import runtime_architecture_policy as architecture
    from runtime_architecture_policy import (
        build_lineage_only_prepared_capability_snapshot,
    )
    from system_strict_bootstrap import materialize_fresh_candidate

    bots = tmp_path / "bots"
    source = bots / "national_v142"
    candidate = bots / "national_v143"
    bots.mkdir(parents=True)
    materialize_fresh_candidate(candidate, version=143, final_policy=True)
    source.mkdir()
    (source / "policy.py").write_text(
        "raise RuntimeError('poison stale v142 must never be opened')\n",
        encoding="utf-8",
    )

    real_evaluate = architecture.evaluate_national_capabilities

    def candidate_only_evaluate(path):
        if getattr(path, "name", "") == "national_v142":
            raise AssertionError("stale v142 capability probe was invoked")
        return real_evaluate(path)

    monkeypatch.setattr(
        architecture,
        "evaluate_national_capabilities",
        candidate_only_evaluate,
    )
    snapshot = build_lineage_only_prepared_capability_snapshot(
        "national_v142",
        candidate,
    )

    def target_only_bot_dir(version):
        if int(version) == 142:
            raise AssertionError("fresh architecture must not resolve v142")
        return bots / f"national_v{version}"

    monkeypatch.setattr(
        tool_planning,
        "get_bot_dir",
        target_only_bot_dir,
    )

    assessment = tool_planning._build_generation_architecture_policy(
        142,
        prepared_capability_snapshot=snapshot,
        prepared_dir=candidate,
        allow_lineage_only_source=True,
    )

    assert assessment["outcome"] == "passed"
    assert assessment["capabilities"]["lineage_only"] is True
    assert assessment["policy"]["source_bot"] == "national_v142"
    assert assessment["policy"]["source_epoch_compatible"] is False
    assert assessment["policy"]["effective_baseline_bot"] == "national_v143"


def test_missing_normal_parent_cannot_claim_lineage_only_exception(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        tool_planning,
        "get_bot_dir",
        lambda version: tmp_path / f"national_v{version}",
    )

    assessment = tool_planning._build_generation_architecture_policy(143)

    assert assessment["outcome"] == "source_invalid"


def test_fresh_quality_transition_never_resolves_or_probes_stale_v142(
    tmp_path,
    monkeypatch,
):
    import runtime_architecture_policy as architecture
    import tool_gates
    from system_strict_bootstrap import materialize_fresh_candidate

    bots = tmp_path / "bots"
    stale = bots / "national_v142"
    candidate = bots / "national_v143"
    bots.mkdir(parents=True)
    stale.mkdir()
    (stale / "policy.py").write_text(
        "raise RuntimeError('poison stale v142 must never be opened')\n",
        encoding="utf-8",
    )
    materialize_fresh_candidate(candidate, version=143, final_policy=True)

    real_evaluate = architecture.evaluate_national_capabilities

    def candidate_only_evaluate(path):
        if getattr(path, "name", "") == "national_v142":
            raise AssertionError("quality probed stale v142")
        return real_evaluate(path)

    monkeypatch.setattr(
        architecture,
        "evaluate_national_capabilities",
        candidate_only_evaluate,
    )
    snapshot = architecture.build_lineage_only_prepared_capability_snapshot(
        "national_v142",
        candidate,
    )
    expected_policy = architecture.build_lineage_only_architecture_policy(
        "national_v142",
        prepared_capability_snapshot=snapshot,
    )

    def source_resolution_forbidden(version):
        if int(version) == 142:
            raise AssertionError("quality resolved stale v142")
        return bots / f"national_v{version}"

    monkeypatch.setattr(tool_gates, "get_bot_dir", source_resolution_forbidden)
    assert tool_gates._quality_source_dir(
        142,
        numeric_lineage_only=True,
    ) is None

    transition = architecture.evaluate_architecture_transition(
        None,
        candidate,
        expected_policy=expected_policy,
        lineage_source_bot="national_v142",
    )

    assert transition["conclusive"] is True
    assert transition["source_capabilities"]["lineage_only"] is True
    assert transition["policy"]["source_bot"] == "national_v142"


def test_master_source_probe_retries_same_tool_then_abandons(
    tmp_path,
    monkeypatch,
    synthetic_checkpoint_authority,
):
    from prepared_baseline_contract import build_prepared_artifact_contract
    from system_strict_bootstrap import (
        materialize_fresh_candidate,
        refresh_policy_identity,
    )

    state_file = tmp_path / "pipeline_state.json"
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", state_file)
    bots = tmp_path / "bots"
    source = bots / "national_v143"
    candidate = bots / "national_v144"
    bots.mkdir(parents=True)
    materialize_fresh_candidate(source, version=143, final_policy=True)
    shutil.copytree(source, candidate)
    refresh_policy_identity(candidate, version=144, parent_versions=(143,))
    prepared_contract = build_prepared_artifact_contract(
        candidate,
        source_v=143,
        next_v=144,
    )
    import checkpoint_schema
    from bot_namespace import EVALUATION_EPOCH

    published_source = SimpleNamespace(
        eligible=True,
        version=143,
        issues=(),
        runtime_manifest={"epoch": EVALUATION_EPOCH, "version": 143},
        epoch_receipt={"epoch": EVALUATION_EPOCH, "version": 143},
        publication_identity={
            "published": True,
            "tag": "national-bot-v143",
            "version": 143,
        },
        certificate_digest="b" * 64,
    )
    audit_context = {"prepared_artifact_contract": prepared_contract}
    epoch_binding = checkpoint_schema.build_checkpoint_epoch_binding(
        next_v=144,
        source_v=143,
        audit_context=audit_context,
        published_high_water=143,
        abandoned_receipt_floor=0,
        abandoned_receipt_head_digest=None,
        parent_resolver=lambda *_args, **_kwargs: published_source,
    )
    state_file.write_text(json.dumps({
        "checkpoint_schema_version": checkpoint_schema.CHECKPOINT_SCHEMA_VERSION,
        "evaluation_epoch": EVALUATION_EPOCH,
        "epoch_binding": epoch_binding,
        "next_v": 144,
        "source_v": 143,
        "parent2_v": None,
        "run_id": "144#0",
        "workflow_run_id": "test-master-probe-144-143",
        "checkpoint_revision": 1,
        "stage": "direction_audited",
        "master_plan": None,
        "reviewer_feedback": "",
        "generation_attempt": 0,
        "audit_attempt": 0,
        "gate_results": {},
        "audit_context": audit_context,
    }))
    monkeypatch.setattr(
        tool_planning,
        "get_bot_dir",
        lambda version: bots / f"national_v{version}",
    )
    # This unit test owns the Master infrastructure overlay, not the repository
    # worktree guard.  Keep the guard covered by its dedicated adversarial suite.
    import tool_runtime_guard
    monkeypatch.setattr(
        tool_runtime_guard,
        "ensure_runtime_git_guard",
        lambda *_args, **_kwargs: (True, {"guard": "unit_test"}),
    )
    monkeypatch.setattr(
        tool_planning,
        "_build_generation_architecture_policy",
        lambda _source_v: {
            "outcome": "infrastructure_failure",
            "policy": None,
            "capabilities": None,
            "infrastructure_failures": [{
                "component": "national_runtime_probe",
                "failure_class": "probe_infra",
                "issues": ["sandbox launch failed"],
            }],
        },
    )
    calls = {"master": 0, "abandon": 0}

    async def should_not_run_master(*_a, **_k):
        calls["master"] += 1
        raise AssertionError("Master LLM must not run without source capability evidence")

    monkeypatch.setattr(tool_planning, "_run_master_analysis", should_not_run_master)
    import national_runtime_probe
    monkeypatch.setattr(national_runtime_probe, "_bot_code_fingerprint", lambda _path: "source-fp")
    import tool_bot_management

    async def fake_abandon(*_a, **_k):
        calls["abandon"] += 1
        return {"abandoned": True, "reason": "test probe exhaustion"}

    monkeypatch.setattr(tool_bot_management, "_do_abandon_generation", fake_abandon)

    actions = []
    for _ in range(3):
        result = asyncio.run(
            tool_planning.run_master.handler({"source_v": 143, "next_v": 144})
        )
        payload = json.loads(result["content"][0]["text"])
        actions.append(payload["action"])

    checkpoint = evolution_infra.read_pipeline_checkpoint()
    assert actions == ["retry_same_tool", "retry_same_tool", "abandon_generation"]
    assert calls == {"master": 0, "abandon": 1}
    assert checkpoint["stage"] == "direction_audited"
    assert checkpoint["master_plan"] is None
    assert checkpoint["infra_failure"]["owner_tool"] == "run_master"
    assert checkpoint["infra_failure"]["attempt"] == 3
    assert checkpoint["infra_failure"]["exhausted"] is True
