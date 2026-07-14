from copy import deepcopy

import pytest


def _fixture(tmp_path, monkeypatch):
    import crossover_projection as projection
    import evolution_infra
    from worker_workflow import WorkerArtifactStore
    from workflow_kernel import WorkflowStore

    checkpoint = {
        "next_v": 3,
        "source_v": 1,
        "parent2_v": 2,
        "stage": "selected",
        "checkpoint_revision": 7,
        "workflow_run_id": "generation:3:crossover-test",
        "audit_context": {"selection": {"strategy": "crossover"}},
    }
    state = {"checkpoint": deepcopy(checkpoint)}
    target = tmp_path / "bots" / "national_v3"
    target.mkdir(parents=True)
    (target / "policy.py").write_text("value = 'preimage'\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "policy.py").write_text("value = 'output'\n")
    workflow_root = tmp_path / "results" / "workflow"
    artifacts = WorkerArtifactStore(workflow_root / "artifacts")
    store = WorkflowStore(workflow_root / "events.sqlite3")
    entry_identity = projection.target_identity(target)
    preimage_hash = artifacts.capture(target)

    monkeypatch.setattr(
        evolution_infra,
        "read_pipeline_checkpoint",
        lambda: deepcopy(state["checkpoint"]),
    )

    def commit(next_v, source_v, stage, **kwargs):
        current = state["checkpoint"]
        assert next_v == 3
        assert source_v == 1
        assert stage == "crossover_running"
        assert kwargs["expected_checkpoint_revision"] == 7
        assert kwargs["expected_checkpoint_stage"] == "selected"
        assert kwargs["expected_workflow_run_id"] == checkpoint["workflow_run_id"]
        state["checkpoint"] = {
            **current,
            "stage": "crossover_running",
            "checkpoint_revision": 8,
            "audit_context": kwargs["audit_context"],
        }
        return True

    monkeypatch.setattr(evolution_infra, "write_pipeline_checkpoint", commit)
    args = {
        "workspace": workspace,
        "target_dir": target,
        "parent_a_v": 1,
        "parent_b_v": 2,
        "target_v": 3,
        "attempt": 0,
        "compatibility": {"compatible": True},
        "architecture_policy": {"policy_digest": "p" * 64},
        "entry_checkpoint": checkpoint,
        "entry_target_identity": entry_identity,
        "preimage_artifact_hash": preimage_hash,
        "workflow_store": store,
        "artifact_store": artifacts,
    }
    return projection, evolution_infra, state, target, artifacts, store, args


def _pending_journals(tmp_path):
    return list(
        (tmp_path / "results" / "workflow" / "crossover_projections").glob(
            "*.json"
        )
    )


def test_projection_recovers_crash_after_intent_before_materialize(
    tmp_path, monkeypatch
):
    projection, _infra, state, target, artifacts, store, args = _fixture(
        tmp_path, monkeypatch
    )
    real_materialize = artifacts.materialize
    calls = 0

    def crash_first(*call_args, **call_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt("kill after durable intent")
        return real_materialize(*call_args, **call_kwargs)

    monkeypatch.setattr(artifacts, "materialize", crash_first)
    with pytest.raises(KeyboardInterrupt):
        projection.project_crossover_candidate(**args)

    assert len(_pending_journals(tmp_path)) == 1
    assert (target / "policy.py").read_text() == "value = 'preimage'\n"
    recovered = projection.recover_crossover_projection(
        entry_checkpoint=state["checkpoint"],
        target_dir=target,
        parent_a_v=1,
        parent_b_v=2,
        target_v=3,
        workflow_store=store,
        artifact_store=artifacts,
    )
    assert recovered is True
    assert state["checkpoint"]["stage"] == "crossover_running"
    assert (target / "policy.py").read_text() == "value = 'output'\n"
    assert _pending_journals(tmp_path) == []


def test_projection_recovers_crash_after_materialize_before_checkpoint_cas(
    tmp_path, monkeypatch
):
    projection, infra, state, target, artifacts, store, args = _fixture(
        tmp_path, monkeypatch
    )
    real_commit = infra.write_pipeline_checkpoint
    calls = 0

    def crash_first(*call_args, **call_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt("kill before checkpoint CAS")
        return real_commit(*call_args, **call_kwargs)

    monkeypatch.setattr(infra, "write_pipeline_checkpoint", crash_first)
    with pytest.raises(KeyboardInterrupt):
        projection.project_crossover_candidate(**args)

    assert len(_pending_journals(tmp_path)) == 1
    assert state["checkpoint"]["stage"] == "selected"
    assert (target / "policy.py").read_text() == "value = 'output'\n"
    recovered = projection.recover_crossover_projection(
        entry_checkpoint=state["checkpoint"],
        target_dir=target,
        parent_a_v=1,
        parent_b_v=2,
        target_v=3,
        workflow_store=store,
        artifact_store=artifacts,
    )
    assert state["checkpoint"]["stage"] == "crossover_running"
    assert recovered is True
    assert _pending_journals(tmp_path) == []


def test_projection_recovers_crash_after_atomic_directory_exchange(
    tmp_path, monkeypatch
):
    projection, _infra, state, target, artifacts, store, args = _fixture(
        tmp_path, monkeypatch
    )
    real_remove = artifacts._remove_journal
    crashed = False

    def crash_after_exchange(_journal):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise KeyboardInterrupt("kill after atomic materialization exchange")
        return real_remove(_journal)

    monkeypatch.setattr(artifacts, "_remove_journal", crash_after_exchange)
    with pytest.raises(KeyboardInterrupt):
        projection.project_crossover_candidate(**args)

    assert crashed is True
    assert target.exists()
    assert (target / "policy.py").read_text() == "value = 'output'\n"
    assert len(_pending_journals(tmp_path)) == 1
    monkeypatch.setattr(artifacts, "_remove_journal", real_remove)
    recovered = projection.recover_crossover_projection(
        entry_checkpoint=state["checkpoint"],
        target_dir=target,
        parent_a_v=1,
        parent_b_v=2,
        target_v=3,
        workflow_store=store,
        artifact_store=artifacts,
    )
    assert recovered is True
    assert state["checkpoint"]["stage"] == "crossover_running"
    assert (target / "policy.py").read_text() == "value = 'output'\n"
    assert _pending_journals(tmp_path) == []


def test_projection_recovers_crash_after_checkpoint_cas_before_receipt_cleanup(
    tmp_path, monkeypatch
):
    projection, _infra, state, target, artifacts, store, args = _fixture(
        tmp_path, monkeypatch
    )
    real_remove = projection._remove_intent
    calls = 0

    def crash_first(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt("kill after checkpoint CAS")
        return real_remove(path)

    monkeypatch.setattr(projection, "_remove_intent", crash_first)
    with pytest.raises(KeyboardInterrupt):
        projection.project_crossover_candidate(**args)

    assert state["checkpoint"]["stage"] == "crossover_running"
    assert len(_pending_journals(tmp_path)) == 1
    recovered = projection.recover_crossover_projection(
        entry_checkpoint=state["checkpoint"],
        target_dir=target,
        parent_a_v=1,
        parent_b_v=2,
        target_v=3,
        workflow_store=store,
        artifact_store=artifacts,
    )
    assert recovered is True
    assert (target / "policy.py").read_text() == "value = 'output'\n"
    assert _pending_journals(tmp_path) == []


def test_projection_receipt_survives_downstream_checkpoint_before_cleanup(
    tmp_path, monkeypatch
):
    projection, _infra, state, target, artifacts, store, args = _fixture(
        tmp_path, monkeypatch
    )
    real_remove = projection._remove_intent
    monkeypatch.setattr(
        projection,
        "_remove_intent",
        lambda _path: (_ for _ in ()).throw(
            KeyboardInterrupt("kill after checkpoint CAS")
        ),
    )
    with pytest.raises(KeyboardInterrupt):
        projection.project_crossover_candidate(**args)
    state["checkpoint"] = {
        **state["checkpoint"],
        "stage": "prepared",
        "checkpoint_revision": 9,
    }
    monkeypatch.setattr(projection, "_remove_intent", real_remove)

    recovered = projection.recover_crossover_projection(
        entry_checkpoint=state["checkpoint"],
        target_dir=target,
        parent_a_v=1,
        parent_b_v=2,
        target_v=3,
        workflow_store=store,
        artifact_store=artifacts,
    )

    assert state["checkpoint"]["stage"] == "prepared"
    assert recovered["absorbed"] is True
    assert recovered["action"] == "follow_checkpoint"
    assert recovered["checkpoint_stage"] == "prepared"
    assert (target / "policy.py").read_text() == "value = 'output'\n"
    assert _pending_journals(tmp_path) == []


def test_projection_subreceipt_binds_stage_revision_and_pre_dispatch_absorption(
    tmp_path, monkeypatch
):
    projection, _infra, state, target, artifacts, store, args = _fixture(
        tmp_path, monkeypatch
    )
    real_remove = projection._remove_intent
    monkeypatch.setattr(
        projection,
        "_remove_intent",
        lambda _path: (_ for _ in ()).throw(
            KeyboardInterrupt("kill after checkpoint CAS")
        ),
    )
    with pytest.raises(KeyboardInterrupt):
        projection.project_crossover_candidate(**args)
    journal = _pending_journals(tmp_path)[0]
    intent = projection._load_intent(journal)
    committed = deepcopy(state["checkpoint"])

    assert projection._checkpoint_is_committed(committed, intent) is True
    selected = {**committed, "stage": "selected", "checkpoint_revision": 7}
    assert projection._checkpoint_is_committed(selected, intent) is False
    abandoned = {**committed, "stage": "abandoned", "checkpoint_revision": 9}
    assert projection._checkpoint_is_committed(abandoned, intent) is False
    enriched = deepcopy(committed)
    enriched["stage"] = "prepared"
    enriched["checkpoint_revision"] = 9
    enriched["audit_context"]["crossover"]["baseline_prepared"] = True
    assert projection._checkpoint_is_committed(enriched, intent) is True
    tampered = deepcopy(enriched)
    tampered["audit_context"]["crossover"]["projection"][
        "projection_id"
    ] = "0" * 64
    assert projection._checkpoint_is_committed(tampered, intent) is False
    tampered_semantics = deepcopy(enriched)
    tampered_semantics["audit_context"]["crossover"]["compatibility"] = {
        "compatible": False,
    }
    assert (
        projection._checkpoint_is_committed(tampered_semantics, intent)
        is False
    )

    downstream = deepcopy(enriched)
    downstream["stage"] = "workers_done"
    downstream["checkpoint_revision"] = 10
    (target / "policy.py").write_text("value = 'worker output'\n")
    monkeypatch.setattr(projection, "_remove_intent", real_remove)
    absorbed = projection.absorb_committed_crossover_projection(
        checkpoint=downstream,
        target_dir=target,
        workflow_store=store,
    )

    assert absorbed["success"] is True
    assert absorbed["absorbed"] is True
    assert (target / "policy.py").read_text() == "value = 'worker output'\n"
    assert _pending_journals(tmp_path) == []


def test_projection_cas_refusal_restores_exact_preimage(tmp_path, monkeypatch):
    projection, infra, state, target, _artifacts, _store, args = _fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(infra, "write_pipeline_checkpoint", lambda *_a, **_k: False)

    result = projection.project_crossover_candidate(**args)

    assert result["component"] == "crossover_projection_conflict"
    assert state["checkpoint"]["stage"] == "selected"
    assert (target / "policy.py").read_text() == "value = 'preimage'\n"
    assert _pending_journals(tmp_path) == []
