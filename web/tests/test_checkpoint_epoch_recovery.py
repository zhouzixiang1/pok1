from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import checkpoint_schema
import evolution_infra
import pipeline_recovery
from evaluation_contract import ALWAYS_CRITICAL_EXACT
from evolution_scope import classify_path
from pipeline_state import generic_abandon_block, route_policy
from system_strict_bootstrap import build_fresh_bootstrap_receipt


def test_checkpoint_schema_is_always_evaluation_contract_critical():
    assert "web/core/checkpoint_schema.py" in ALWAYS_CRITICAL_EXACT
    assert classify_path("web/core/checkpoint_schema.py", candidate_v=144) == "critical"


def _canonical_digest(payload):
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _policy_epoch_reset_receipt():
    archive_root = (
        "archive/evolution_epochs/national_native_v1/"
        "runtime_legacy_untrusted/20260714_000000_000000"
    )
    claim_payload = {
        "schema_version": 1,
        "kind": "national_tcp_policy_epoch_reset_claim",
        "epoch": "national_tcp_policy_v1",
        "created_at": "2026-07-14T00:00:00.000000",
        "git_head": "a" * 40,
        "archive_root": archive_root,
        "first_target_version": 143,
        "checkout_role": "autonomous_evolution_runtime",
        "one_time": True,
    }
    claim = {
        **claim_payload,
        "claim_digest": _canonical_digest(claim_payload),
    }
    payload = {
        "schema_version": 2,
        "kind": "national_tcp_policy_epoch_reset",
        "epoch": "national_tcp_policy_v1",
        "created_at": "2026-07-14T00:00:00",
        "mode": "execute",
        "git_head": "a" * 40,
        "archive_root": archive_root,
        "execution_scope": {
            "checkout_role": "autonomous_evolution_runtime",
            "one_time": True,
            "prior_reset_evidence_required_empty": True,
            "claim_digest": claim["claim_digest"],
        },
        "archived_version_high_water": 142,
        "version_authority_high_water": 142,
        "first_target_version": 143,
        "source_code_inherited": False,
        "seed_bot": None,
        "active_namespace": {
            "bot": "national_v143",
            "protocol": "official-national-raw-tcp-v1",
            "policy_abi": "national-tcp-policy-runtime-v1",
        },
        "archived_runtime": [],
        "archived_bot_debris": [],
    }
    return {
        **payload,
        "receipt_digest": _canonical_digest(payload),
        "_test_claim": claim,
    }


def _write_reset_authority(root, receipt):
    receipt = dict(receipt)
    claim = receipt.pop("_test_claim")
    live = root / checkpoint_schema.POLICY_EPOCH_RESET_RECEIPT_RELATIVE_PATH
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(json.dumps(receipt, ensure_ascii=False) + "\n", encoding="utf-8")
    archive = root / receipt["archive_root"]
    archive.mkdir(parents=True)
    (archive / "reset_claim.json").write_text(
        json.dumps(claim, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (archive / "reset_receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return receipt


def _checkpoint(binding, audit_context, *, next_v, source_v):
    return {
        "checkpoint_schema_version": checkpoint_schema.CHECKPOINT_SCHEMA_VERSION,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": binding,
        "next_v": next_v,
        "source_v": source_v,
        "parent2_v": None,
        "stage": "direction_audited",
        "workflow_run_id": f"generation:{next_v}:test",
        "checkpoint_revision": 4,
        "audit_context": audit_context,
        "repo_baseline": {"branch": "main", "head": "same123"},
    }


def _published_parent(version):
    return SimpleNamespace(
        eligible=True,
        version=version,
        issues=(),
        runtime_manifest={"epoch": "national_tcp_policy_v1", "version": version},
        epoch_receipt={"epoch": "national_tcp_policy_v1", "version": version},
        publication_identity={
            "published": True,
            "tag": f"national-bot-v{version}",
            "version": version,
        },
        certificate_digest="b" * 64,
    )


def _published_parent_tags(*, version, **_kwargs):
    return {
        "completion_tag": f"national-bot-v{version}",
        "completion_tag_object_oid": "c" * 40,
        "high_water_tag": f"national-high-water-v{version}",
        "high_water_tag_object_oid": "d" * 40,
        "publication_commit_oid": "e" * 40,
        "completion_tree_oid": "f" * 40,
        "tag_artifact_hash": "a" * 64,
    }


def test_old_v155_migration_checkpoint_cannot_route_or_recover(tmp_path):
    # This matches the stale runtime shape observed at the epoch cut: it has
    # modern workflow fencing but no strict checkpoint schema/epoch identity,
    # and its only origin declaration is the retired v142 migration mode.
    checkpoint = {
        "next_v": 155,
        "source_v": 142,
        "stage": "direction_audited",
        "workflow_run_id": "generation:155:legacy",
        "checkpoint_revision": 9,
        "audit_context": {
            "protocol_bootstrap": {"mode": "legacy_strategy_migration"},
            "selection": {"strategy": "legacy_strategy_migration"},
        },
        "repo_baseline": {"branch": "main", "head": "same123"},
    }

    route = route_policy(checkpoint)
    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot={
            "ok": True,
            "branch": "main",
            "head": "same123",
            "entries": [],
        },
        project_root=tmp_path,
    )

    assert route["next_tool"] is None
    assert route["allowed_tools"] == []
    assert route["intent"] == "operator_reconcile_checkpoint"
    assert "checkpoint_legacy_strategy_migration_forbidden" in route["epoch_issues"]
    assert "run_master" in route["directive"]
    assert diag["active"] is True
    assert diag["recoverable"] is False
    assert diag["operator_action"] == "operator_reconcile_checkpoint"
    assert "reconcile_national_policy_epoch.py --execute" in diag["operator_command"]
    assert "--quarantine-legacy-ledger-and-abandon-checkpoint" in (
        diag["operator_command"]
    )
    assert "repo" not in diag
    abandon = generic_abandon_block(checkpoint)
    assert abandon["blocked"] is True
    assert abandon["reason"] == "checkpoint_epoch_requires_operator_reconciliation"


def test_fresh_v143_resume_requires_and_accepts_live_reset_receipt(tmp_path):
    reset_receipt = _write_reset_authority(
        tmp_path,
        _policy_epoch_reset_receipt(),
    )
    bootstrap = build_fresh_bootstrap_receipt(
        active_bots=(),
        epoch_reset_receipt_digest=reset_receipt["receipt_digest"],
    )
    audit_context = {
        "protocol_bootstrap": bootstrap,
        "selection": {"strategy": "fresh_policy_bootstrap"},
    }
    binding = checkpoint_schema.build_checkpoint_epoch_binding(
        next_v=143,
        source_v=142,
        audit_context=audit_context,
        published_high_water=142,
        abandoned_receipt_floor=0,
        abandoned_receipt_head_digest=None,
        repo_root=tmp_path,
    )
    checkpoint = _checkpoint(
        binding,
        audit_context,
        next_v=143,
        source_v=142,
    )
    (tmp_path / "bots" / "national_v143").mkdir(parents=True)

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot={
            "ok": True,
            "branch": "main",
            "head": "same123",
            "entries": [],
        },
        project_root=tmp_path,
    )

    assert checkpoint_schema.checkpoint_epoch_errors(checkpoint) == []
    assert diag["recoverable"] is True
    assert diag["epoch"]["mode"] == "fresh_bootstrap"
    assert route_policy(checkpoint)["next_tool"] == "run_master"


def test_fresh_v143_resume_rejects_missing_live_reset_receipt(tmp_path):
    reset_receipt = _policy_epoch_reset_receipt()
    reset_receipt.pop("_test_claim")
    bootstrap = build_fresh_bootstrap_receipt(
        active_bots=(),
        epoch_reset_receipt_digest=reset_receipt["receipt_digest"],
    )
    audit_context = {
        "protocol_bootstrap": bootstrap,
        "selection": {"strategy": "fresh_policy_bootstrap"},
    }
    checkpoint = _checkpoint(
        checkpoint_schema.build_checkpoint_epoch_binding(
            next_v=143,
            source_v=142,
            audit_context=audit_context,
            published_high_water=142,
            abandoned_receipt_floor=0,
            abandoned_receipt_head_digest=None,
            repo_root=tmp_path,
        ),
        audit_context,
        next_v=143,
        source_v=142,
    )

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot={"ok": True, "branch": "main", "head": "same123", "entries": []},
        project_root=tmp_path,
    )

    assert diag["recoverable"] is False
    assert "policy_epoch_reset_receipt_missing_or_unsafe" in diag["issues"]


def test_normal_strict_v144_resume_accepts_published_parent_binding(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        checkpoint_schema,
        "resolve_national_bot_spec",
        lambda *_args, **_kwargs: _published_parent(143),
    )
    monkeypatch.setattr(
        checkpoint_schema,
        "resolve_published_parent_tag_authority",
        _published_parent_tags,
    )
    audit_context = {
        "selection": {
            "strategy": "master",
            "parent_a": 143,
            "parent_b": None,
        }
    }
    binding = checkpoint_schema.build_checkpoint_epoch_binding(
        next_v=144,
        source_v=143,
        audit_context=audit_context,
        published_high_water=143,
        abandoned_receipt_floor=0,
        abandoned_receipt_head_digest=None,
        repo_root=tmp_path,
        parent_resolver=lambda *_args, **_kwargs: _published_parent(143),
        parent_tag_authority_resolver=_published_parent_tags,
    )
    checkpoint = _checkpoint(
        binding,
        audit_context,
        next_v=144,
        source_v=143,
    )
    (tmp_path / "bots" / "national_v144").mkdir(parents=True)

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot={
            "ok": True,
            "branch": "main",
            "head": "same123",
            "entries": [],
        },
        project_root=tmp_path,
    )

    assert checkpoint_schema.checkpoint_epoch_errors(checkpoint) == []
    assert diag["recoverable"] is True
    assert diag["epoch"]["mode"] == "published_strict_parent"
    assert route_policy(checkpoint)["next_tool"] == "run_master"


def test_live_parent_authority_ignores_later_main_head_observations(tmp_path):
    def parent_with_main(main_oid):
        parent = _published_parent(143)
        parent.publication_identity.update({
            "tag_type": "tag",
            "tag_object": "c" * 40,
            "commit_oid": "e" * 40,
            "completion_tree_oid": "f" * 40,
            "tag_artifact_hash": "a" * 64,
            "main_commit_oid": main_oid,
            "main_tree_oid": "f" * 40,
            "current_tree_oid": "f" * 40,
        })
        return parent

    binding = checkpoint_schema.build_checkpoint_epoch_binding(
        next_v=144,
        source_v=143,
        audit_context={"selection": {"strategy": "master"}},
        published_high_water=143,
        abandoned_receipt_floor=0,
        abandoned_receipt_head_digest=None,
        repo_root=tmp_path,
        parent_resolver=lambda *_args, **_kwargs: parent_with_main("1" * 40),
        parent_tag_authority_resolver=_published_parent_tags,
    )
    checkpoint = _checkpoint(
        binding,
        {"selection": {"strategy": "master"}},
        next_v=144,
        source_v=143,
    )

    assert checkpoint_schema.live_checkpoint_parent_authority_errors(
        checkpoint,
        repo_root=tmp_path,
        parent_resolver=lambda *_args, **_kwargs: parent_with_main("2" * 40),
        parent_tag_authority_resolver=_published_parent_tags,
    ) == []


def test_resigned_nonexistent_parent_is_rejected_by_recovery_and_checkpoint_cas(
    tmp_path,
    monkeypatch,
):
    binding = checkpoint_schema.build_checkpoint_epoch_binding(
        next_v=144,
        source_v=143,
        audit_context={"selection": {"strategy": "master"}},
        published_high_water=143,
        abandoned_receipt_floor=0,
        abandoned_receipt_head_digest=None,
        repo_root=tmp_path,
        parent_resolver=lambda *_args, **_kwargs: _published_parent(143),
        parent_tag_authority_resolver=_published_parent_tags,
    )
    forged = json.loads(json.dumps(binding))
    forged["published_parent_identities"][0].update({
        "runtime_manifest_digest": "1" * 64,
        "epoch_receipt_digest": "2" * 64,
        "publication_identity_digest": "3" * 64,
        "certificate_digest": "4" * 64,
        "completion_tag_object_oid": "5" * 40,
        "high_water_tag_object_oid": "6" * 40,
        "publication_commit_oid": "7" * 40,
        "completion_tree_oid": "8" * 40,
        "tag_artifact_hash": "9" * 64,
    })
    unsigned = {
        key: value for key, value in forged.items() if key != "binding_digest"
    }
    forged["binding_digest"] = _canonical_digest(unsigned)
    checkpoint = _checkpoint(
        forged,
        {"selection": {"strategy": "master"}},
        next_v=144,
        source_v=143,
    )
    checkpoint["workflow_run_id"] = "generation:144:workflow-v1"
    checkpoint["checkpoint_revision"] = 4
    (tmp_path / "bots" / "national_v144").mkdir(parents=True)

    def missing_parent(*_args, **_kwargs):
        raise FileNotFoundError("published parent is absent")

    monkeypatch.setattr(
        checkpoint_schema,
        "resolve_national_bot_spec",
        missing_parent,
    )
    monkeypatch.setattr(
        checkpoint_schema,
        "resolve_published_parent_tag_authority",
        _published_parent_tags,
    )

    # A canonical self-digest is structural evidence only; live authority must
    # still reject the invented parent at every resume/write boundary.
    assert checkpoint_schema.checkpoint_epoch_errors(checkpoint) == []
    live_errors = checkpoint_schema.live_checkpoint_parent_authority_errors(
        checkpoint,
        repo_root=tmp_path,
    )
    assert any("resolution_error:v143:FileNotFoundError" in item for item in live_errors)

    diagnostic = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot={
            "ok": True,
            "branch": "main",
            "head": "same123",
            "entries": [],
        },
        project_root=tmp_path,
    )
    assert diagnostic["recoverable"] is False
    assert any("parent_authority" in item for item in diagnostic["issues"])

    state_path = tmp_path / "pipeline_state.json"
    state_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", state_path)
    monkeypatch.setattr(
        evolution_infra,
        "checkpoint_allocation_authority",
        lambda **_kwargs: {
            "published_high_water": 143,
            "abandoned_receipt_floor": 0,
            "abandoned_receipt_head_digest": None,
            "allocation_floor": 143,
        },
    )
    assert evolution_infra.write_pipeline_checkpoint(
        144,
        143,
        "preparing",
        expected_checkpoint_revision=4,
        expected_checkpoint_stage="direction_audited",
        expected_workflow_run_id="generation:144:workflow-v1",
    ) is False
    assert json.loads(state_path.read_text(encoding="utf-8")) == checkpoint


def test_checkpoint_writer_refuses_implicit_upgrade_of_legacy_active_state(
    tmp_path,
    monkeypatch,
):
    state_path = tmp_path / "pipeline_state.json"
    legacy = {
        "next_v": 155,
        "source_v": 142,
        "stage": "direction_audited",
        "workflow_run_id": "generation:155:legacy",
        "checkpoint_revision": 9,
        "audit_context": {
            "protocol_bootstrap": {"mode": "legacy_strategy_migration"}
        },
    }
    encoded = json.dumps(legacy, sort_keys=True)
    state_path.write_text(encoded, encoding="utf-8")
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", state_path)

    assert evolution_infra.write_pipeline_checkpoint(
        155,
        142,
        "direction_audited",
        expected_checkpoint_revision=9,
    ) is False
    assert json.loads(state_path.read_text(encoding="utf-8")) == legacy


def test_checkpoint_writer_creates_once_and_preserves_strict_epoch_binding(
    tmp_path,
    monkeypatch,
):
    state_path = tmp_path / "pipeline_state.json"
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", state_path)
    monkeypatch.setattr(
        evolution_infra,
        "_capture_repo_baseline",
        lambda stage, **_kwargs: {
            "branch": "main",
            "head": "same123",
            "captured_stage": stage,
        },
    )
    monkeypatch.setattr(
        checkpoint_schema,
        "resolve_national_bot_spec",
        lambda *_args, **_kwargs: _published_parent(143),
    )
    monkeypatch.setattr(
        checkpoint_schema,
        "resolve_published_parent_tag_authority",
        _published_parent_tags,
    )
    monkeypatch.setattr(
        evolution_infra,
        "checkpoint_allocation_authority",
        lambda **_kwargs: {
            "published_high_water": 143,
            "abandoned_receipt_floor": 0,
            "abandoned_receipt_head_digest": None,
            "allocation_floor": 143,
        },
    )
    audit_context = {
        "selection": {
            "strategy": "master",
            "parent_a": 143,
            "parent_b": None,
        }
    }

    assert evolution_infra.write_pipeline_checkpoint(
        144,
        143,
        "selected",
        audit_context=audit_context,
        workflow_run_id="generation:144:test",
    ) is True
    selected = json.loads(state_path.read_text(encoding="utf-8"))
    original_binding = selected["epoch_binding"]
    assert (
        selected["checkpoint_schema_version"]
        == checkpoint_schema.CHECKPOINT_SCHEMA_VERSION
    )
    assert selected["evaluation_epoch"] == "national_tcp_policy_v1"
    assert checkpoint_schema.checkpoint_epoch_errors(selected) == []

    assert evolution_infra.write_pipeline_checkpoint(
        144,
        143,
        "preparing",
        expected_checkpoint_revision=selected["checkpoint_revision"],
        expected_checkpoint_stage="selected",
        expected_workflow_run_id="generation:144:test",
    ) is True
    preparing = json.loads(state_path.read_text(encoding="utf-8"))
    assert preparing["checkpoint_revision"] == selected["checkpoint_revision"] + 1
    assert preparing["epoch_binding"] == original_binding
    assert checkpoint_schema.checkpoint_epoch_errors(preparing) == []


def test_checkpoint_writer_rejects_target_jump_before_data_creation(
    tmp_path,
    monkeypatch,
):
    state_path = tmp_path / "pipeline_state.json"
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", state_path)
    monkeypatch.setattr(
        evolution_infra,
        "checkpoint_allocation_authority",
        lambda **_kwargs: {
            "published_high_water": 143,
            "abandoned_receipt_floor": 144,
            "abandoned_receipt_head_digest": "a" * 64,
            "allocation_floor": 144,
        },
    )
    monkeypatch.setattr(
        checkpoint_schema,
        "resolve_national_bot_spec",
        lambda *_args, **_kwargs: _published_parent(143),
    )

    assert evolution_infra.write_pipeline_checkpoint(
        150,
        143,
        "selected",
        audit_context={"selection": {"strategy": "master"}},
        workflow_run_id="generation:150:workflow-v1",
    ) is False
    assert not state_path.exists()


def test_checkpoint_data_and_sidecar_symlinks_fail_closed(
    tmp_path,
    monkeypatch,
):
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    state_path = tmp_path / "pipeline_state.json"
    state_path.symlink_to(outside)
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", state_path)

    assert evolution_infra.read_pipeline_checkpoint() is None
    assert evolution_infra.write_pipeline_checkpoint(
        144,
        143,
        "selected",
        workflow_run_id="generation:144:workflow-v1",
    ) is False
    assert evolution_infra.clear_pipeline_checkpoint() is False
    assert outside.read_text(encoding="utf-8") == "{}"

    state_path.unlink()
    lock_path = state_path.with_suffix(".json.lock")
    lock_path.unlink(missing_ok=True)
    lock_path.symlink_to(outside)
    assert evolution_infra.read_pipeline_checkpoint() is None
    assert evolution_infra.write_pipeline_checkpoint(
        144,
        143,
        "selected",
        workflow_run_id="generation:144:workflow-v1",
    ) is False
    assert evolution_infra.clear_pipeline_checkpoint() is False
    assert outside.read_text(encoding="utf-8") == "{}"
