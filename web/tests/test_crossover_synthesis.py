import asyncio
from pathlib import Path

import pytest


def _payload(module, *, prompt="complete prompt"):
    return module.build_synthesis_input(
        run_id="generation:3:crossover-effect-test",
        prompt=prompt,
        parent_a_v=1,
        parent_b_v=2,
        target_v=3,
        attempt=1,
        checkpoint={
            "next_v": 3,
            "source_v": 1,
            "parent2_v": 2,
            "stage": "selected",
            "checkpoint_revision": 4,
            "workflow_run_id": "generation:3:crossover-effect-test",
        },
        checkpoint_digest="c" * 64,
        parent_a_artifact_hash="a" * 64,
        parent_b_artifact_hash="b" * 64,
        input_snapshot_hash="i" * 64,
        compatibility_receipt={"compatible": True, "advisory_only": True},
        capability_context={"parent_a": {"runtime": "current"}},
        architecture_policy={"policy_digest": "p" * 64},
    )


def test_synthesis_effect_freezes_full_input_and_replays_accepted_snapshot(tmp_path):
    import crossover_synthesis as synthesis
    from worker_workflow import WorkerArtifactStore
    from workflow_kernel import WorkflowStore

    root = tmp_path / "workflow"
    store = WorkflowStore(root / "events.sqlite3")
    artifacts = WorkerArtifactStore(root / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "policy.py").write_text("value = 'baseline'\n", encoding="utf-8")

    effect_id, invocation_id, payload = _payload(synthesis)
    effect = synthesis.ensure_synthesis_effect(
        store=store,
        run_id=payload["workflow_run_id"],
        effect_id=effect_id,
        input_payload=payload,
        definition_version=2,
    )
    assert effect["status"] == "requested"
    assert effect["input_payload"]["prompt"] == "complete prompt"
    assert effect["input_payload"]["parents"] == {
        "parent_a_v": 1,
        "parent_b_v": 2,
        "parent_a_artifact_hash": "a" * 64,
        "parent_b_artifact_hash": "b" * 64,
    }
    assert effect["input_payload"]["checkpoint"]["checkpoint_revision"] == 4
    assert effect["input_payload"]["attempt"] == 1
    assert effect["input_payload"]["input_snapshot_hash"] == "i" * 64

    lease = synthesis.claim_synthesis_effect(
        store=store,
        effect_id=effect_id,
        invocation_id=invocation_id,
    )
    (workspace / "policy.py").write_text("value = 'model output'\n", encoding="utf-8")
    accepted = synthesis.complete_synthesis_effect(
        store=store,
        artifact_store=artifacts,
        lease=lease,
        invocation_id=invocation_id,
        workspace=workspace,
    )
    assert accepted["replayed"] is False
    completed_effect = store.effect(effect_id)
    assert completed_effect["status"] == "completed"
    durable_receipt = synthesis.synthesis_receipt(completed_effect, artifacts)
    assert durable_receipt["checkpoint_digest"] == "c" * 64
    assert durable_receipt["parent_a_artifact_hash"] == "a" * 64
    assert durable_receipt["parent_b_artifact_hash"] == "b" * 64

    replay = tmp_path / "replay"
    replay.mkdir()
    (replay / "policy.py").write_text("value = 'baseline'\n", encoding="utf-8")
    restored = synthesis.materialize_completed_effect(
        effect=completed_effect,
        workspace=replay,
        artifact_store=artifacts,
    )
    assert restored["replayed"] is True
    assert (replay / "policy.py").read_text(encoding="utf-8") == (
        "value = 'model output'\n"
    )


def test_synthesis_effect_fences_concurrent_and_expired_provider_results(tmp_path):
    import crossover_synthesis as synthesis
    from worker_workflow import WorkerArtifactStore
    from workflow_kernel import WorkflowBusy, WorkflowStore

    root = tmp_path / "workflow"
    store = WorkflowStore(root / "events.sqlite3")
    artifacts = WorkerArtifactStore(root / "artifacts")
    effect_id, invocation_id, payload = _payload(synthesis)
    synthesis.ensure_synthesis_effect(
        store=store,
        run_id=payload["workflow_run_id"],
        effect_id=effect_id,
        input_payload=payload,
        definition_version=2,
    )
    first = store.claim_effect(effect_id, owner="first", lease_seconds=10, now=10)
    with pytest.raises(WorkflowBusy):
        store.claim_effect(effect_id, owner="concurrent", lease_seconds=10, now=11)
    second = store.claim_effect(effect_id, owner="second", lease_seconds=10, now=21)

    stale_workspace = tmp_path / "stale"
    stale_workspace.mkdir()
    (stale_workspace / "policy.py").write_text("stale\n", encoding="utf-8")
    with pytest.raises(WorkflowBusy):
        synthesis.complete_synthesis_effect(
            store=store,
            artifact_store=artifacts,
            lease=first,
            invocation_id=invocation_id,
            workspace=stale_workspace,
        )

    winner_workspace = tmp_path / "winner"
    winner_workspace.mkdir()
    (winner_workspace / "policy.py").write_text("winner\n", encoding="utf-8")
    winner = synthesis.complete_synthesis_effect(
        store=store,
        artifact_store=artifacts,
        lease=second,
        invocation_id=invocation_id,
        workspace=winner_workspace,
    )
    assert winner["replayed"] is False
    assert (
        store.effect(effect_id)["result_payload"]["output_artifact_hash"]
        == winner["output_artifact_hash"]
    )


def test_synthesis_effect_immediately_fences_provably_dead_process_owner(
    tmp_path, monkeypatch
):
    import crossover_synthesis as synthesis
    from workflow_kernel import WorkflowStore

    store = WorkflowStore(tmp_path / "events.sqlite3")
    effect_id, invocation_id, payload = _payload(synthesis)
    synthesis.ensure_synthesis_effect(
        store=store,
        run_id=payload["workflow_run_id"],
        effect_id=effect_id,
        input_payload=payload,
        definition_version=2,
    )
    first = synthesis.claim_synthesis_effect(
        store=store,
        effect_id=effect_id,
        invocation_id=invocation_id,
    )
    assert first.lease_epoch == 1
    monkeypatch.setattr(
        synthesis,
        "_recognized_owner_is_dead",
        lambda _owner: True,
    )
    replacement = synthesis.claim_synthesis_effect(
        store=store,
        effect_id=effect_id,
        invocation_id=invocation_id,
    )
    assert replacement.lease_epoch == 2
    assert replacement.attempt == 2


def test_same_synthesis_effect_id_rejects_changed_prompt(tmp_path):
    import crossover_synthesis as synthesis
    from workflow_kernel import WorkflowConflict, WorkflowStore

    store = WorkflowStore(tmp_path / "events.sqlite3")
    effect_id, _invocation_id, payload = _payload(synthesis, prompt="prompt one")
    synthesis.ensure_synthesis_effect(
        store=store,
        run_id=payload["workflow_run_id"],
        effect_id=effect_id,
        input_payload=payload,
        definition_version=2,
    )
    same_effect_id, _other_invocation, changed = _payload(
        synthesis, prompt="prompt two"
    )
    assert same_effect_id == effect_id
    with pytest.raises(WorkflowConflict):
        synthesis.ensure_synthesis_effect(
            store=store,
            run_id=changed["workflow_run_id"],
            effect_id=same_effect_id,
            input_payload=changed,
            definition_version=2,
        )


def test_run_crossover_reuses_completed_synthesis_and_reruns_gates(
    tmp_path, monkeypatch
):
    import shutil

    import agent_review
    import crossover_provenance
    import evolution_infra
    import national_native
    import national_position_contract
    import runtime_architecture_policy
    import workflow_profiles
    from workflow_kernel import WorkflowStore

    bots = tmp_path / "bots"
    parent_a = bots / "national_v143"
    parent_b = bots / "national_v146"
    target = bots / "national_v147"
    from bot_namespace import refresh_policy_identity_documents
    from system_strict_bootstrap import materialize_fresh_candidate

    materialize_fresh_candidate(parent_a, version=143, final_policy=True)
    shutil.copytree(parent_a, parent_b)
    refresh_policy_identity_documents(parent_b, 146, parent_versions=(143,))
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "crossover_prompt.md").write_text(
        "merge {{parent_a_version}} {{parent_b_version}} into {{version}}",
        encoding="utf-8",
    )
    logs = tmp_path / "logs"
    logs.mkdir()
    results = tmp_path / "results"
    evidence_snapshot_dir = results / "v147" / "evidence_snapshot"
    evidence_snapshot_dir.mkdir(parents=True)
    checkpoint = {
        "next_v": 147,
        "source_v": 143,
        "parent2_v": 146,
        "stage": "selected",
        "checkpoint_revision": 4,
        "workflow_run_id": "generation:147:crossover-replay-test",
        "audit_context": {"selection": {"strategy": "crossover"}},
        "checkpoint_schema_version": 2,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": {
            "binding_digest": "e" * 64,
            "published_parent_identities": [
                {"tag_artifact_hash": ""},
                {"tag_artifact_hash": ""},
            ],
        },
    }

    def bot_dir(version):
        return {143: parent_a, 146: parent_b, 147: target}[int(version)]

    from bot_artifact import hash_path

    checkpoint["epoch_binding"]["published_parent_identities"][0][
        "tag_artifact_hash"
    ] = hash_path(parent_a)
    checkpoint["epoch_binding"]["published_parent_identities"][1][
        "tag_artifact_hash"
    ] = hash_path(parent_b)

    query_calls = []
    query_kwargs = []

    async def query(prompt, *_args, **kwargs):
        store = WorkflowStore(results / "workflow" / "events.sqlite3")
        effect = store.effect(
            "crossover-synthesis:generation:147:crossover-replay-test:attempt-1"
        )
        # Persistence and lease acquisition must both precede the provider call.
        assert effect["status"] == "running"
        assert effect["input_payload"]["prompt"] == prompt
        assert effect["input_payload"]["checkpoint"] == checkpoint
        query_calls.append(prompt)
        query_kwargs.append(kwargs)
        # The durable provider result must remain a valid strict typed policy;
        # this test exercises effect replay/projection recovery, not rejection
        # of a malformed policy ABI.
        write_scope = kwargs["allowed_write_dir"]
        write_target = Path(kwargs["allowed_read_dirs"][-1]) / "policy.py"
        assert write_scope == {"files": [write_target]}
        write_target.write_bytes((parent_b / "policy.py").read_bytes())

    gate_calls = []

    def verify(path):
        gate_calls.append(Path(path))
        return []

    projection_calls = []

    def crash_projection(**kwargs):
        projection_calls.append(kwargs)
        raise KeyboardInterrupt("crash after deterministic gates")

    monkeypatch.setattr(agent_review, "PROMPTS_DIR", prompts)
    monkeypatch.setattr(agent_review, "RESULTS_DIR", results)
    monkeypatch.setattr(agent_review, "get_bot_dir", bot_dir)
    monkeypatch.setattr(agent_review, "get_logs_dir", lambda _v: logs)
    monkeypatch.setattr(agent_review, "run_claude_query", query)
    import evidence_snapshot
    monkeypatch.setattr(
        evidence_snapshot,
        "h2h_snapshot_contract_text",
        lambda *_args, **_kwargs: "# Frozen generation evidence\n",
    )
    monkeypatch.setattr(
        evidence_snapshot,
        "load_generation_snapshot_identity",
        lambda _v: {
            "available": True,
            "manifest_path": str(evidence_snapshot_dir / "manifest.json"),
        },
    )
    monkeypatch.setattr(agent_review, "verify_code", verify)
    monkeypatch.setattr(agent_review, "run_import_contract_test", lambda _p: [])
    async def smoke_ok(*_args, **_kwargs):
        return {"passed": True, "issues": []}

    monkeypatch.setattr(national_native, "run_native_tcp_smoke", smoke_ok)
    monkeypatch.setattr(agent_review, "_project_crossover_candidate", crash_projection)
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(
        workflow_profiles,
        "get_workflow_profile",
        lambda: type("Profile", (), {"national_execution_mode": "native_tcp"})(),
    )
    monkeypatch.setattr(
        crossover_provenance,
        "validate_crossover_recombination_provenance",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        national_position_contract,
        "detect_position_semantics_errors",
        lambda _p: [],
    )
    monkeypatch.setattr(
        runtime_architecture_policy,
        "evaluate_architecture_transition",
        lambda *_a, **_k: {
            "ok": True,
            "outcome": "passed",
            "source_capabilities": {},
            "candidate_capabilities": {},
            "runtime_floor_failures": [],
            "regressions": [],
        },
    )

    import audit_agents
    import checkpoint_schema
    from worker_workflow import WorkerArtifactStore

    monkeypatch.setattr(checkpoint_schema, "checkpoint_epoch_errors", lambda _c: [])
    monkeypatch.setattr(
        checkpoint_schema,
        "live_checkpoint_parent_authority_errors",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(audit_agents, "RESULTS_DIR", results)
    monkeypatch.setattr(audit_agents, "get_bot_dir", bot_dir)
    snapshot_bundle = audit_agents.capture_crossover_parent_snapshots(
        143,
        146,
        147,
        checkpoint=checkpoint,
        checkpoint_reader=lambda: checkpoint,
        artifact_store=WorkerArtifactStore(results / "workflow" / "artifacts"),
    )

    compatibility = {
        "compatible": False,
        "compatibility_score": 3,
        "conflict_areas": ["untrusted prose"],
        "suggested_merge_approach": "untrusted prose",
        "files_to_take_from_b": ["policy.py"],
        "parent_snapshot_receipt": snapshot_bundle["receipt"],
    }
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(agent_review._run_crossover(
            143, 146, 147, type("UI", (), {
                "log_history": lambda self, *_a, **_k: None,
                "clear_io": lambda self: None,
                "set_status": lambda self, *_a, **_k: None,
            })(), compatibility=compatibility,
        ))

    effect_id = (
        "crossover-synthesis:generation:147:crossover-replay-test:attempt-1"
    )
    store = WorkflowStore(results / "workflow" / "events.sqlite3")
    completed = store.effect(effect_id)
    assert completed["status"] == "completed"
    assert len(query_calls) == 1
    assert query_kwargs[0]["allowed_evidence_snapshot_dir"] == evidence_snapshot_dir
    lease_target = Path(query_kwargs[0]["allowed_read_dirs"][-1])
    assert query_kwargs[0]["allowed_write_dir"] == {
        "files": [lease_target / "policy.py"]
    }
    assert "provider-dispatch Runtime Path Contract" in query_calls[0]
    assert "system quality gate owns compilation" in query_calls[0]
    assert "py_compile" not in query_calls[0]
    assert str(lease_target) not in query_calls[0]
    assert len(gate_calls) == 1

    async def must_not_query(*_args, **_kwargs):
        raise AssertionError("completed crossover synthesis called the LLM again")

    monkeypatch.setattr(agent_review, "run_claude_query", must_not_query)
    monkeypatch.setattr(
        agent_review,
        "_project_crossover_candidate",
        lambda **kwargs: projection_calls.append(kwargs) or True,
    )
    result = asyncio.run(agent_review._run_crossover(
        143, 146, 147, type("UI", (), {
            "log_history": lambda self, *_a, **_k: None,
            "clear_io": lambda self: None,
            "set_status": lambda self, *_a, **_k: None,
        })(), compatibility=compatibility,
    ))

    assert result is True
    assert len(query_calls) == 1
    assert len(gate_calls) == 2
    final_call = projection_calls[-1]
    assert final_call["synthesis_receipt"]["effect_id"] == effect_id
    assert final_call["compatibility"] == {
        "compatible": False,
        "compatibility_score": 3,
        "conflict_area_count": 1,
        "files_to_take_from_a": [],
        "files_to_take_from_b": ["policy.py"],
        "advisory_only": True,
        "parent_snapshot_receipt": snapshot_bundle["receipt"],
    }
    assert (
        final_call["synthesis_receipt"]["compatibility_receipt"]
        == final_call["compatibility"]
    )
