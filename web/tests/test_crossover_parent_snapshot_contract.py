import asyncio
import copy
import json
from types import SimpleNamespace

import pytest


def _parent_checkpoint(parent_a, parent_b, *, stage="selected"):
    from bot_artifact import hash_path

    parent_a_v = int(parent_a.name.rsplit("national_v", 1)[1])
    parent_b_v = int(parent_b.name.rsplit("national_v", 1)[1])
    target_v = max(parent_a_v, parent_b_v) + 10
    identities = [
        {
            "version": version,
            "bot": f"national_v{version}",
            "tag_artifact_hash": hash_path(path),
            "certificate_digest": character * 64,
        }
        for version, path, character in (
            (parent_a_v, parent_a, "a"),
            (parent_b_v, parent_b, "b"),
        )
    ]
    return {
        "checkpoint_schema_version": 2,
        "evaluation_epoch": "national_tcp_policy_v1",
        "next_v": target_v,
        "source_v": parent_a_v,
        "parent2_v": parent_b_v,
        "stage": stage,
        "checkpoint_revision": 7,
        "workflow_run_id": f"generation:{target_v}:parent-snapshot-test",
        "audit_context": {"selection": {"strategy": "crossover"}},
        "epoch_binding": {
            "binding_digest": "e" * 64,
            "published_parent_identities": identities,
        },
    }


def _parents(tmp_path):
    parent_a = tmp_path / "bots" / "national_v149"
    parent_b = tmp_path / "bots" / "national_v143"
    for path, marker in ((parent_a, "PARENT_A"), (parent_b, "PARENT_B")):
        path.mkdir(parents=True)
        (path / "policy.py").write_text(f"# {marker}\n", encoding="utf-8")
    return parent_a, parent_b


def _bind_test_authority(monkeypatch, checkpoint, parent_a, parent_b, results):
    import audit_agents
    import checkpoint_schema
    import evolution_infra

    monkeypatch.setattr(checkpoint_schema, "checkpoint_epoch_errors", lambda _c: [])
    monkeypatch.setattr(
        checkpoint_schema,
        "live_checkpoint_parent_authority_errors",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(audit_agents, "RESULTS_DIR", results)
    monkeypatch.setattr(
        audit_agents,
        "get_bot_dir",
        lambda version: parent_a if int(version) == 149 else parent_b,
    )


def test_compatibility_reads_only_exact_published_parent_snapshots(
    tmp_path, monkeypatch
):
    import audit_agents

    parent_a, parent_b = _parents(tmp_path)
    checkpoint = _parent_checkpoint(parent_a, parent_b)
    results = tmp_path / "results"
    _bind_test_authority(
        monkeypatch, checkpoint, parent_a, parent_b, results
    )
    monkeypatch.setattr(audit_agents, "get_logs_dir", lambda _v: tmp_path)
    monkeypatch.setattr(
        audit_agents,
        "frozen_crossover_parent_architecture",
        lambda _bundle: {
            "architecture_policy": {"policy_digest": "p" * 64},
            "capability_context": {
                "national_v149": {"detector_version": "frozen"},
                "national_v143": {"detector_version": "frozen"},
            },
        },
    )
    captured = []

    async def query(prompt, *_args, **_kwargs):
        captured.append(str(prompt))
        return json.dumps({
            "compatible": True,
            "compatibility_score": 8,
            "conflict_areas": [],
            "suggested_merge_approach": "merge policy components",
            "files_to_take_from_a": ["policy.py"],
            "files_to_take_from_b": ["policy.py"],
        }), 0.0, {}

    monkeypatch.setattr(audit_agents, "run_claude_query", query)
    result = asyncio.run(audit_agents._run_crossover_compatibility_audit(
        149,
        143,
        SimpleNamespace(),
        target_v=checkpoint["next_v"],
        authoritative_checkpoint=checkpoint,
    ))

    assert len(captured) == 1
    assert "PARENT_A" in captured[0]
    assert "PARENT_B" in captured[0]
    receipt = result["parent_snapshot_receipt"]
    assert receipt["workflow_run_id"] == checkpoint["workflow_run_id"]
    assert receipt["checkpoint_digest"]
    assert [item["snapshot_artifact_hash"] for item in receipt["parents"]] == [
        item["tag_artifact_hash"]
        for item in checkpoint["epoch_binding"]["published_parent_identities"]
    ]


def test_parent_drift_after_capture_blocks_compatibility_provider(
    tmp_path, monkeypatch
):
    import audit_agents

    parent_a, parent_b = _parents(tmp_path)
    checkpoint = _parent_checkpoint(parent_a, parent_b)
    _bind_test_authority(
        monkeypatch, checkpoint, parent_a, parent_b, tmp_path / "results"
    )
    original = audit_agents.capture_crossover_parent_snapshots

    def capture_then_drift(*args, **kwargs):
        bundle = original(*args, **kwargs)
        (parent_a / "policy.py").write_text("# DRIFTED\n", encoding="utf-8")
        return bundle

    monkeypatch.setattr(
        audit_agents, "capture_crossover_parent_snapshots", capture_then_drift
    )
    monkeypatch.setattr(
        audit_agents,
        "frozen_crossover_parent_architecture",
        lambda _bundle: {
            "architecture_policy": {},
            "capability_context": {},
        },
    )
    provider_calls = []

    async def must_not_run(*_args, **_kwargs):
        provider_calls.append(True)
        raise AssertionError("drifted parent reached compatibility provider")

    monkeypatch.setattr(audit_agents, "run_claude_query", must_not_run)
    with pytest.raises(
        audit_agents.CrossoverParentSnapshotError,
        match="pre_dispatch_live_drift",
    ):
        asyncio.run(audit_agents._run_crossover_compatibility_audit(
            149,
            143,
            SimpleNamespace(),
            target_v=checkpoint["next_v"],
            authoritative_checkpoint=checkpoint,
        ))
    assert provider_calls == []


@pytest.mark.parametrize(
    "authority_issue",
    [
        "checkpoint_parent_completion_tree_drift",
        "checkpoint_parent_completion_tag_object_drift",
        "checkpoint_parent_official_certificate_digest_drift",
    ],
)
def test_publication_tag_tree_or_certificate_mismatch_calls_no_provider(
    tmp_path, monkeypatch, authority_issue
):
    import audit_agents
    import checkpoint_schema

    parent_a, parent_b = _parents(tmp_path)
    checkpoint = _parent_checkpoint(parent_a, parent_b)
    _bind_test_authority(
        monkeypatch, checkpoint, parent_a, parent_b, tmp_path / "results"
    )
    monkeypatch.setattr(
        checkpoint_schema,
        "live_checkpoint_parent_authority_errors",
        lambda *_a, **_k: [authority_issue],
    )
    provider_calls = []

    async def provider(*_args, **_kwargs):
        provider_calls.append(True)

    monkeypatch.setattr(audit_agents, "run_claude_query", provider)
    with pytest.raises(
        audit_agents.CrossoverParentSnapshotError,
        match=authority_issue,
    ):
        asyncio.run(audit_agents._run_crossover_compatibility_audit(
            149,
            143,
            SimpleNamespace(),
            target_v=checkpoint["next_v"],
            authoritative_checkpoint=checkpoint,
        ))
    assert provider_calls == []


def test_capture_rejects_before_drift_symlink_and_after_capture_drift(
    tmp_path, monkeypatch
):
    import audit_agents
    from worker_workflow import WorkerArtifactStore

    parent_a, parent_b = _parents(tmp_path)
    checkpoint = _parent_checkpoint(parent_a, parent_b)
    results = tmp_path / "results"
    _bind_test_authority(
        monkeypatch, checkpoint, parent_a, parent_b, results
    )

    (parent_a / "policy.py").write_text("# BEFORE_DRIFT\n", encoding="utf-8")
    with pytest.raises(
        audit_agents.CrossoverParentSnapshotError,
        match="pre_capture_drift",
    ):
        audit_agents.capture_crossover_parent_snapshots(
            149, 143, checkpoint["next_v"], checkpoint=checkpoint
        )

    (parent_a / "policy.py").write_text("# PARENT_A\n", encoding="utf-8")
    (parent_a / "escape").symlink_to(parent_b / "policy.py")
    with pytest.raises(
        audit_agents.CrossoverParentSnapshotError,
        match="pre_capture_invalid",
    ):
        audit_agents.capture_crossover_parent_snapshots(
            149, 143, checkpoint["next_v"], checkpoint=checkpoint
        )
    (parent_a / "escape").unlink()

    delegate = WorkerArtifactStore(results / "workflow" / "artifacts")

    class DriftAfterFirstCapture:
        def __init__(self):
            self.calls = 0

        def capture(self, path):
            digest = delegate.capture(path)
            self.calls += 1
            if self.calls == 1:
                (parent_a / "policy.py").write_text(
                    "# AFTER_CAPTURE_DRIFT\n", encoding="utf-8"
                )
            return digest

        def path_for(self, digest):
            return delegate.path_for(digest)

    with pytest.raises(
        audit_agents.CrossoverParentSnapshotError,
        match="post_capture_drift",
    ):
        audit_agents.capture_crossover_parent_snapshots(
            149,
            143,
            checkpoint["next_v"],
            checkpoint=checkpoint,
            artifact_store=DriftAfterFirstCapture(),
        )


def test_self_resigned_receipt_cannot_substitute_snapshot(tmp_path, monkeypatch):
    import audit_agents

    parent_a, parent_b = _parents(tmp_path)
    checkpoint = _parent_checkpoint(parent_a, parent_b)
    _bind_test_authority(
        monkeypatch, checkpoint, parent_a, parent_b, tmp_path / "results"
    )
    bundle = audit_agents.capture_crossover_parent_snapshots(
        149, 143, checkpoint["next_v"], checkpoint=checkpoint
    )
    forged = copy.deepcopy(bundle["receipt"])
    forged["parents"][0]["snapshot_artifact_hash"] = (
        forged["parents"][1]["snapshot_artifact_hash"]
    )
    forged["receipt_digest"] = audit_agents._crossover_snapshot_digest({
        key: value for key, value in forged.items() if key != "receipt_digest"
    })

    with pytest.raises(
        audit_agents.CrossoverParentSnapshotError,
        match="parent_a_identity_mismatch",
    ):
        audit_agents.resolve_crossover_parent_snapshots(
            forged,
            checkpoint=checkpoint,
            parent_a_v=149,
            parent_b_v=143,
            target_v=checkpoint["next_v"],
        )


def test_projection_bound_stage_transition_reuses_original_parent_snapshots(
    tmp_path, monkeypatch
):
    import audit_agents
    from workflow_kernel import content_digest

    parent_a, parent_b = _parents(tmp_path)
    selected = _parent_checkpoint(parent_a, parent_b)
    _bind_test_authority(
        monkeypatch, selected, parent_a, parent_b, tmp_path / "results"
    )
    bundle = audit_agents.capture_crossover_parent_snapshots(
        149, 143, selected["next_v"], checkpoint=selected
    )
    receipt = bundle["receipt"]
    projection_body = {
        "schema_version": 1,
        "workflow_run_id": selected["workflow_run_id"],
        "parent_a_v": 149,
        "parent_b_v": 143,
        "target_v": selected["next_v"],
        "expected_checkpoint_digest": receipt["checkpoint_digest"],
        "expected_checkpoint_revision": selected["checkpoint_revision"],
        "expected_checkpoint_stage": "selected",
        "committed_revision": selected["checkpoint_revision"] + 1,
        "crossover_semantics": {
            "compatibility": {"parent_snapshot_receipt": receipt},
        },
    }
    projected = copy.deepcopy(selected)
    projected["stage"] = "crossover_running"
    projected["checkpoint_revision"] += 1
    projected["audit_context"]["crossover"] = {
        "compatibility": {"parent_snapshot_receipt": receipt},
        "projection": {
            **projection_body,
            "projection_id": content_digest(projection_body),
        },
    }

    resolved = audit_agents.resolve_crossover_parent_snapshots(
        receipt,
        checkpoint=projected,
        parent_a_v=149,
        parent_b_v=143,
        target_v=selected["next_v"],
    )
    assert resolved["parent_a_artifact_hash"] == receipt["parents"][0][
        "snapshot_artifact_hash"
    ]
    forged_projection = copy.deepcopy(projected)
    forged_projection["audit_context"]["crossover"]["projection"][
        "expected_checkpoint_digest"
    ] = "0" * 64
    with pytest.raises(
        audit_agents.CrossoverParentSnapshotError,
        match="receipt_subject_mismatch",
    ):
        audit_agents.resolve_crossover_parent_snapshots(
            receipt,
            checkpoint=forged_projection,
            parent_a_v=149,
            parent_b_v=143,
            target_v=selected["next_v"],
        )


def test_synthesis_rejects_receipt_snapshot_mismatch_before_provider(
    tmp_path, monkeypatch
):
    import agent_review
    import audit_agents
    import evolution_infra

    parent_a, parent_b = _parents(tmp_path)
    checkpoint = _parent_checkpoint(parent_a, parent_b)
    results = tmp_path / "results"
    _bind_test_authority(
        monkeypatch, checkpoint, parent_a, parent_b, results
    )
    bundle = audit_agents.capture_crossover_parent_snapshots(
        149, 143, checkpoint["next_v"], checkpoint=checkpoint
    )
    forged = copy.deepcopy(bundle["receipt"])
    forged["parents"][0]["snapshot_artifact_hash"] = "f" * 64
    forged["receipt_digest"] = audit_agents._crossover_snapshot_digest({
        key: value for key, value in forged.items() if key != "receipt_digest"
    })
    target = tmp_path / "bots" / f"national_v{checkpoint['next_v']}"
    monkeypatch.setattr(agent_review, "RESULTS_DIR", results)
    monkeypatch.setattr(
        agent_review,
        "get_bot_dir",
        lambda version: {
            149: parent_a,
            143: parent_b,
            checkpoint["next_v"]: target,
        }[int(version)],
    )
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint)
    provider_calls = []

    async def provider(*_args, **_kwargs):
        provider_calls.append(True)

    monkeypatch.setattr(agent_review, "run_claude_query", provider)
    result = asyncio.run(agent_review._run_crossover(
        149,
        143,
        checkpoint["next_v"],
        SimpleNamespace(log_history=lambda *_a, **_k: None),
        compatibility={"parent_snapshot_receipt": forged},
    ))

    assert result["success"] is False
    assert result["component"] == "crossover_parent_snapshot_contract"
    assert provider_calls == []
