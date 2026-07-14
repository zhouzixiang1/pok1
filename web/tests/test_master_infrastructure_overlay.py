import asyncio
import json
import shutil
from types import SimpleNamespace

import evolution_infra
import tool_planning


def test_master_source_probe_retries_same_tool_then_abandons(tmp_path, monkeypatch):
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
