from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import checkpoint_schema
import evolution_infra
import pytest
from bot_artifact import canonical_digest
from system_strict_bootstrap import build_fresh_bootstrap_receipt

from scripts import reconcile_national_policy_epoch as reconcile
from tests.test_checkpoint_epoch_recovery import (
    _policy_epoch_reset_receipt,
    _write_reset_authority,
)
from tests.test_epoch_authority import _strict_checkpoint

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")


def _published_parent(name, **_kwargs):
    version = int(str(name).rsplit("national_v", 1)[1])
    return SimpleNamespace(
        eligible=True,
        version=version,
        issues=(),
        runtime_manifest={"epoch": "national_tcp_policy_v1", "version": version},
        epoch_receipt={"epoch": "national_tcp_policy_v1", "version": version},
        publication_identity={"published": True, "version": version},
        certificate_digest="b" * 64,
    )


def _legacy_v1_binding(binding):
    unsigned = {
        key: value
        for key, value in binding.items()
        if key not in {
            "binding_digest",
            "published_high_water",
            "abandoned_receipt_floor",
            "abandoned_receipt_head_digest",
            "allocation_floor",
        }
    }
    unsigned["schema_version"] = 1
    return {**unsigned, "binding_digest": canonical_digest(unsigned)}


def _candidate(path: Path):
    path.mkdir(parents=True)
    for name, content in {
        "national_bot.py": "def run():\n    return None\n",
        "policy.py": "def decide(_context):\n    return {'kind': 'pass'}\n",
        "precompute.py": "TABLE = ()\n",
        "national_runtime_manifest.json": "{}\n",
        "policy_epoch_receipt.json": "{}\n",
    }.items():
        (path / name).write_text(content, encoding="utf-8")


def _legacy_rows(count=17):
    return "".join(
        json.dumps({
            "v": 143,
            "reason": f"legacy failure {attempt}",
            "timestamp": float(attempt),
            "workflow_run_id": f"generation:143:workflow-v{attempt}",
            "infra_failure": None,
        }, sort_keys=True) + "\n"
        for attempt in range(1, count + 1)
    )


def _configure_paths(tmp_path, monkeypatch):
    root = tmp_path / ".evolution_pok"
    core = root / "web" / "core"
    results = core / "results"
    bots = root / "bots"
    archive = root / (
        "archive/evolution_epochs/national_tcp_policy_v1/runtime_reconciliation"
    )
    results.mkdir(parents=True)
    bots.mkdir(parents=True)
    monkeypatch.setattr(reconcile, "ROOT", root)
    monkeypatch.setattr(reconcile, "CORE", core)
    monkeypatch.setattr(reconcile, "RESULTS", results)
    monkeypatch.setattr(reconcile, "BOTS", bots)
    monkeypatch.setattr(reconcile, "ARCHIVE_BASE", archive)
    monkeypatch.setattr(
        reconcile,
        "RESET_RECEIPT",
        results / "policy_epoch_reset_receipt.json",
    )
    monkeypatch.setattr(reconcile, "LEDGER", results / "abandoned_versions.jsonl")
    monkeypatch.setattr(reconcile, "CHECKPOINT", results / "pipeline_state.json")
    monkeypatch.setattr(
        reconcile,
        "LIVE_CLAIM",
        results / "policy_epoch_reconciliation_claim.json",
    )
    monkeypatch.setattr(
        reconcile,
        "LIVE_RECEIPT",
        results / "policy_epoch_reconciliation_receipt.json",
    )
    monkeypatch.setattr(
        reconcile,
        "RECORDED_FINALIZE_RECEIPT",
        results / "policy_epoch_recorded_abandon_finalize_receipt.json",
    )
    monkeypatch.setattr(reconcile, "_runtime_checkout_identity_errors", lambda: [])
    monkeypatch.setattr(reconcile, "_runtime_process_errors", lambda: [])
    monkeypatch.setattr(reconcile, "_version_authority_high_water", lambda: 142)
    monkeypatch.setattr(
        reconcile,
        "_git",
        lambda *args: "a" * 40 if args == ("rev-parse", "HEAD") else "",
    )
    monkeypatch.setattr(
        reconcile,
        "_git_explicit_presence",
        lambda *_args: False,
    )
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 142)
    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", root)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(
        evolution_infra,
        "ABANDONED_VERSIONS_FILE",
        results / "abandoned_versions.jsonl",
    )
    return root, results, bots


def _fresh_v143_checkpoint(root, reset_receipt, *, workflow_attempt=18):
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
        repo_root=root,
    )
    return {
        "checkpoint_schema_version": 1,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": _legacy_v1_binding(binding),
        "next_v": 143,
        "source_v": 142,
        "parent2_v": None,
        "stage": "direction_audited",
        "workflow_run_id": f"generation:143:workflow-v{workflow_attempt}",
        "checkpoint_revision": 4,
        "audit_context": audit_context,
    }


def _prepared_legacy_claim(tmp_path, monkeypatch):
    root, results, bots = _configure_paths(tmp_path, monkeypatch)
    reset = _write_reset_authority(root, _policy_epoch_reset_receipt())
    checkpoint = _fresh_v143_checkpoint(root, reset)
    reconcile.LEDGER.write_text(_legacy_rows(), encoding="utf-8")
    reconcile.CHECKPOINT.write_text(json.dumps(checkpoint), encoding="utf-8")
    _candidate(bots / "national_v143")
    claim = reconcile._build_plan()["claim"]
    return root, results, bots, claim


def _resign_legacy_claim(claim):
    claim = json.loads(json.dumps(claim))
    claim["terminal_abandon"]["infra_failure"][
        "reconciliation_input_digest"
    ] = canonical_digest(claim["inputs"])
    unsigned = {key: value for key, value in claim.items() if key != "claim_digest"}
    claim["claim_digest"] = canonical_digest(unsigned)
    return claim


def _completed_receipt_for_archive(archive_root):
    payload = {
        "schema_version": 1,
        "kind": "national-policy-runtime-reconciliation",
        "evaluation_epoch": "national_tcp_policy_v1",
        "mode": "execute",
        "claim_digest": "a" * 64,
        "archive_root": archive_root,
        "legacy_rows_authority_weight": 0,
        "allocation_receipt_digest": None,
        "abandoned_workflow_run_id": "generation:143:workflow-v18",
        "workflow_fence": {},
        "next_target_version": 143,
        "next_workflow_attempt": 19,
    }
    return {**payload, "receipt_digest": canonical_digest(payload)}


def test_reconcile_quarantines_legacy_rows_and_preserves_v18_attempt(
    tmp_path,
    monkeypatch,
):
    root, results, bots = _configure_paths(tmp_path, monkeypatch)
    reset = _write_reset_authority(root, _policy_epoch_reset_receipt())
    checkpoint = _fresh_v143_checkpoint(root, reset)
    legacy = _legacy_rows()
    reconcile.LEDGER.write_text(legacy, encoding="utf-8")
    reconcile.CHECKPOINT.write_text(json.dumps(checkpoint), encoding="utf-8")
    reconcile.CHECKPOINT.with_suffix(".json.lock").write_text("", encoding="utf-8")
    (results / "pipeline_state.json.v143_stale_backup").write_text(
        "stale backup",
        encoding="utf-8",
    )
    _candidate(bots / "national_v143")

    dry_run = reconcile.run(
        execute=False,
        quarantine_legacy_ledger_and_abandon_checkpoint=True,
    )
    assert dry_run["mutates"] is False
    assert reconcile.CHECKPOINT.exists()
    assert reconcile.LEDGER.read_text(encoding="utf-8") == legacy

    receipt = reconcile.run(
        execute=True,
        acknowledge_runtime_checkout=True,
        quarantine_legacy_ledger_and_abandon_checkpoint=True,
    )

    assert receipt["legacy_rows_authority_weight"] == 0
    assert receipt["abandoned_workflow_run_id"] == "generation:143:workflow-v18"
    assert receipt["next_target_version"] == 143
    assert receipt["next_workflow_attempt"] == 19
    assert receipt["allocation_receipt_digest"]
    assert not reconcile.CHECKPOINT.exists()
    assert not (bots / "national_v143").exists()
    strict_rows = evolution_infra.load_abandoned_version_receipts(
        path=reconcile.LEDGER,
        project_root=root,
    )
    assert len(strict_rows) == 1
    assert strict_rows[0]["workflow_run_id"] == "generation:143:workflow-v18"
    assert evolution_infra.abandoned_version_attempt_count(143) == 18
    stale_v18 = checkpoint_schema.upgrade_legacy_checkpoint_for_controlled_abandon(
        checkpoint,
        published_high_water=142,
        abandoned_receipt_floor=0,
        abandoned_receipt_head_digest=None,
    )
    stale_errors = checkpoint_schema.live_checkpoint_allocation_authority_errors(
        stale_v18,
        published_high_water=142,
        abandoned_receipt_floor=0,
        abandoned_receipt_head_digest=strict_rows[0]["receipt_digest"],
    )
    assert "checkpoint_abandoned_receipt_head_changed" in stale_errors
    new_binding = checkpoint_schema.build_checkpoint_epoch_binding(
        next_v=143,
        source_v=142,
        audit_context=checkpoint["audit_context"],
        published_high_water=142,
        abandoned_receipt_floor=0,
        abandoned_receipt_head_digest=strict_rows[0]["receipt_digest"],
        repo_root=root,
    )
    v19 = {
        **checkpoint,
        "checkpoint_schema_version": checkpoint_schema.CHECKPOINT_SCHEMA_VERSION,
        "epoch_binding": new_binding,
        "workflow_run_id": "generation:143:workflow-v19",
    }
    assert checkpoint_schema.live_checkpoint_allocation_authority_errors(
        v19,
        published_high_water=142,
        abandoned_receipt_floor=0,
        abandoned_receipt_head_digest=strict_rows[0]["receipt_digest"],
    ) == []
    archive_root = root / receipt["archive_root"]
    archived_legacy = archive_root / "legacy_abandoned_versions.jsonl"
    assert archived_legacy.read_text(encoding="utf-8") == legacy
    assert hashlib.sha256(archived_legacy.read_bytes()).hexdigest() == (
        dry_run["inputs"]["legacy_ledger"]["sha256"]
    )
    assert (archive_root / "candidate" / "national_v143").is_dir()
    assert reconcile.CHECKPOINT.with_suffix(".json.lock").is_file()
    assert not (
        archive_root / "checkpoint_auxiliary" / "pipeline_state.json.lock"
    ).exists()
    assert (
        archive_root
        / "checkpoint_auxiliary"
        / "pipeline_state.json.v143_stale_backup"
    ).is_file()

    # Completed command and terminal append are idempotent.
    (results / "generation_cost_ledger.jsonl").write_text(
        json.dumps({
            "generation_id": "generation:143:workflow-v19",
            "kind": "notice",
        }) + "\n",
        encoding="utf-8",
    )
    reconcile.LIVE_CLAIM.write_bytes(
        (archive_root / "reconciliation_claim.json").read_bytes()
    )
    replay = reconcile.run(
        execute=True,
        acknowledge_runtime_checkout=True,
        quarantine_legacy_ledger_and_abandon_checkpoint=True,
    )
    assert replay == receipt
    assert not reconcile.LIVE_CLAIM.exists()
    assert len(evolution_infra.load_abandoned_version_receipts(
        path=reconcile.LEDGER,
        project_root=root,
    )) == 1


def test_reconcile_large_legacy_jump_quarantines_without_burning_labels(
    tmp_path,
    monkeypatch,
):
    root, _results, bots = _configure_paths(tmp_path, monkeypatch)
    _write_reset_authority(root, _policy_epoch_reset_receipt())
    legacy = _legacy_rows()
    reconcile.LEDGER.write_text(legacy, encoding="utf-8")
    binding = checkpoint_schema.build_checkpoint_epoch_binding(
        next_v=155,
        source_v=143,
        audit_context={},
        published_high_water=154,
        abandoned_receipt_floor=0,
        abandoned_receipt_head_digest=None,
        parent_resolver=_published_parent,
    )
    checkpoint = {
        "checkpoint_schema_version": 1,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": _legacy_v1_binding(binding),
        "next_v": 155,
        "source_v": 143,
        "parent2_v": None,
        "stage": "direction_audited",
        "workflow_run_id": "generation:155:workflow-v18",
        "checkpoint_revision": 9,
        "audit_context": {},
    }
    reconcile.CHECKPOINT.write_text(json.dumps(checkpoint), encoding="utf-8")
    _candidate(bots / "national_v155")

    receipt = reconcile.run(
        execute=True,
        acknowledge_runtime_checkout=True,
        quarantine_legacy_ledger_and_abandon_checkpoint=True,
    )

    assert receipt["allocation_receipt_digest"] is None
    assert receipt["next_target_version"] == 143
    assert not reconcile.LEDGER.exists()
    assert not reconcile.CHECKPOINT.exists()
    assert not (bots / "national_v155").exists()


def test_recorded_v2_abandon_finalize_recovers_after_clear_failure(
    tmp_path,
    monkeypatch,
):
    import evolution_core
    import tool_bot_management as tbm

    root, results, bots = _configure_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 143)
    checkpoint = _strict_checkpoint(144, 143, revision=6)
    reconcile.CHECKPOINT.write_text(json.dumps(checkpoint), encoding="utf-8")
    candidate = bots / "national_v144"
    _candidate(candidate)
    terminal = evolution_infra.append_abandoned_version_receipt(
        checkpoint,
        reason="clear-cas-failed",
        timestamp=10.0,
        path=reconcile.LEDGER,
        project_root=root,
    )
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", reconcile.CHECKPOINT)
    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", reconcile.CHECKPOINT)
    monkeypatch.setattr(tbm, "RESULTS_DIR", results)
    monkeypatch.setattr(tbm, "PROJECT_ROOT", root)
    monkeypatch.setattr(tbm, "get_bot_dir", lambda version: bots / f"national_v{version}")
    monkeypatch.setattr(tbm, "git_dir_is_committed", lambda _version: False)
    monkeypatch.setattr(tbm, "git_has_publication_ref", lambda _version: False)
    monkeypatch.setattr(tbm, "log_system_event", lambda *_a, **_k: None)

    dry = reconcile.run(
        execute=False,
        finalize_recorded_abandon_checkpoint=True,
    )
    assert dry["mutates"] is False
    assert dry["abandon_receipt_digest"] == terminal["receipt_digest"]

    receipt = reconcile.run(
        execute=True,
        acknowledge_runtime_checkout=True,
        finalize_recorded_abandon_checkpoint=True,
    )
    assert receipt["checkpoint_cleared"] is True
    assert receipt["candidate_removed"] is True
    assert receipt["abandon_receipt_digest"] == terminal["receipt_digest"]
    assert not reconcile.CHECKPOINT.exists()
    assert not candidate.exists()
    assert len(evolution_infra.load_abandoned_version_receipts(
        path=reconcile.LEDGER,
        project_root=root,
    )) == 1
    replay = reconcile.run(
        execute=True,
        acknowledge_runtime_checkout=True,
        finalize_recorded_abandon_checkpoint=True,
    )
    assert replay == receipt


def test_schema2_live_claim_dry_run_and_execute_resume_quarantined_candidate(
    tmp_path,
    monkeypatch,
):
    import evolution_core
    import tool_bot_management as tbm

    root, results, bots = _configure_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 143)
    checkpoint = _strict_checkpoint(144, 143, revision=10)
    reconcile.CHECKPOINT.write_text(json.dumps(checkpoint), encoding="utf-8")
    candidate = bots / "national_v144"
    _candidate(candidate)

    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", reconcile.CHECKPOINT)
    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", reconcile.CHECKPOINT)
    monkeypatch.setattr(tbm, "RESULTS_DIR", results)
    monkeypatch.setattr(tbm, "PROJECT_ROOT", root)
    monkeypatch.setattr(tbm, "get_bot_dir", lambda version: bots / f"national_v{version}")
    monkeypatch.setattr(
        tbm,
        "read_pipeline_checkpoint",
        lambda: (
            json.loads(reconcile.CHECKPOINT.read_text(encoding="utf-8"))
            if reconcile.CHECKPOINT.exists()
            else None
        ),
    )
    monkeypatch.setattr(tbm, "git_dir_is_committed", lambda _version: False)
    monkeypatch.setattr(tbm, "git_has_publication_ref", lambda _version: False)
    monkeypatch.setattr(
        tbm,
        "_evolution_git",
        lambda *args, **_kwargs: (
            "a" * 40 if args == ("rev-parse", "HEAD") else ""
        ),
    )
    monkeypatch.setattr(tbm, "log_system_event", lambda *_a, **_k: None)
    monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", lambda **_kwargs: False)
    tbm._LAST_ABANDON_TS[:] = [0.0, ""]

    first = asyncio.run(tbm._do_abandon_generation(
        reason="abandon_generation",
        _bypass_rate_limit=True,
        **tbm.expected_abandon_identity(checkpoint),
    ))

    assert first["abandoned"] is False
    assert first["reason"] == "checkpoint_identity_conflict"
    assert not candidate.exists()
    claim = json.loads(reconcile.LIVE_CLAIM.read_text(encoding="utf-8"))
    assert claim["schema_version"] == 2
    quarantine = (
        results
        / "policy_epoch_abandon_transactions"
        / claim["transaction_id"]
        / "candidate"
    )
    assert quarantine.is_dir()

    dry = reconcile.run(
        execute=False,
        finalize_recorded_abandon_checkpoint=True,
    )
    assert dry["schema_version"] == 2
    assert dry["claim_digest"] == claim["claim_digest"]
    assert dry["mutates"] is False

    def clear_checkpoint(**_kwargs):
        reconcile.CHECKPOINT.unlink()
        tbm._fsync_parent_directory(reconcile.CHECKPOINT)
        return True

    monkeypatch.setattr(tbm, "clear_pipeline_checkpoint", clear_checkpoint)
    receipt = reconcile.run(
        execute=True,
        acknowledge_runtime_checkout=True,
        finalize_recorded_abandon_checkpoint=True,
    )

    assert receipt["schema_version"] == 2
    assert receipt["claim_digest"] == claim["claim_digest"]
    assert receipt["checkpoint_cleared"] is True
    assert receipt["candidate_state"] == "quarantine"
    assert not reconcile.CHECKPOINT.exists()
    assert not reconcile.LIVE_CLAIM.exists()
    assert quarantine.is_dir()


def test_recorded_finalize_recovers_old_clear_before_candidate_delete_window(
    tmp_path,
    monkeypatch,
):
    root, _results, bots = _configure_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 143)
    checkpoint = _strict_checkpoint(144, 143, revision=8)
    reconcile.CHECKPOINT.write_text(json.dumps(checkpoint), encoding="utf-8")
    candidate = bots / "national_v144"
    _candidate(candidate)
    terminal = evolution_infra.append_abandoned_version_receipt(
        checkpoint,
        reason="old-clear-before-rmtree-window",
        timestamp=11.0,
        path=reconcile.LEDGER,
        project_root=root,
    )
    claim = reconcile._recorded_finalize_plan()["claim"]
    reconcile.LIVE_CLAIM.write_text(
        json.dumps(claim, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Simulate the former ordering: checkpoint CAS succeeded, process crashed
    # before the still-present exact candidate could be removed.
    reconcile.CHECKPOINT.unlink()

    receipt = reconcile.run(
        execute=True,
        acknowledge_runtime_checkout=True,
        finalize_recorded_abandon_checkpoint=True,
    )

    assert receipt["abandon_receipt_digest"] == terminal["receipt_digest"]
    assert receipt["checkpoint_cleared"] is True
    assert receipt["candidate_removed"] is True
    assert not candidate.exists()


def test_recorded_finalize_preserves_drifted_candidate_after_checkpoint_clear(
    tmp_path,
    monkeypatch,
):
    root, _results, bots = _configure_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 143)
    checkpoint = _strict_checkpoint(144, 143, revision=9)
    reconcile.CHECKPOINT.write_text(json.dumps(checkpoint), encoding="utf-8")
    candidate = bots / "national_v144"
    _candidate(candidate)
    evolution_infra.append_abandoned_version_receipt(
        checkpoint,
        reason="old-clear-before-rmtree-drift",
        timestamp=12.0,
        path=reconcile.LEDGER,
        project_root=root,
    )
    claim = reconcile._recorded_finalize_plan()["claim"]
    reconcile.LIVE_CLAIM.write_text(
        json.dumps(claim, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    reconcile.CHECKPOINT.unlink()
    (candidate / "policy.py").write_text("# drifted after claim\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="candidate changed after claim"):
        reconcile.run(
            execute=True,
            acknowledge_runtime_checkout=True,
            finalize_recorded_abandon_checkpoint=True,
        )

    assert candidate.exists()
    assert (candidate / "policy.py").read_text(encoding="utf-8") == (
        "# drifted after claim\n"
    )
    assert reconcile.LIVE_CLAIM.exists()


def test_legacy_claim_rejects_candidate_path_traversal_even_when_resigned(
    tmp_path,
    monkeypatch,
):
    _root, _results, _bots, claim = _prepared_legacy_claim(tmp_path, monkeypatch)
    claim["inputs"]["candidate"]["path"] = "../../outside"
    claim = _resign_legacy_claim(claim)
    reconcile.LIVE_CLAIM.write_text(json.dumps(claim) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="candidate identity is invalid"):
        reconcile.run(
            execute=False,
            quarantine_legacy_ledger_and_abandon_checkpoint=True,
        )


def test_legacy_claim_rejects_forged_candidate_counts_on_resume(
    tmp_path,
    monkeypatch,
):
    _root, _results, _bots, claim = _prepared_legacy_claim(tmp_path, monkeypatch)
    claim["inputs"]["candidate"]["entries"] += 1
    claim = _resign_legacy_claim(claim)
    reconcile.LIVE_CLAIM.write_text(json.dumps(claim) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="candidate differs"):
        reconcile.run(
            execute=False,
            quarantine_legacy_ledger_and_abandon_checkpoint=True,
        )


@pytest.mark.parametrize(
    "names, error",
    (
        (["../../outside"], "runtime control file is invalid"),
        (["unknown.json"], "runtime control file is invalid"),
        ([".daemon_pid", ".daemon_pid"], "runtime control file is duplicated"),
    ),
)
def test_legacy_claim_rejects_unsafe_runtime_control_names(
    tmp_path,
    monkeypatch,
    names,
    error,
):
    _root, _results, _bots, claim = _prepared_legacy_claim(tmp_path, monkeypatch)
    claim["inputs"]["runtime_control"]["files"] = [
        {"name": name, "sha256": "a" * 64, "size": 0}
        for name in names
    ]
    claim = _resign_legacy_claim(claim)
    reconcile.LIVE_CLAIM.write_text(json.dumps(claim) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=error):
        reconcile.run(
            execute=False,
            quarantine_legacy_ledger_and_abandon_checkpoint=True,
        )


def test_legacy_claim_runtime_control_symlink_is_never_followed(
    tmp_path,
    monkeypatch,
):
    _root, results, _bots, claim = _prepared_legacy_claim(tmp_path, monkeypatch)
    outside = tmp_path / "outside-pid"
    outside.write_text("123\n", encoding="utf-8")
    (results / ".daemon_pid").symlink_to(outside)
    raw = outside.read_bytes()
    claim["inputs"]["runtime_control"]["files"] = [{
        "name": ".daemon_pid",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }]
    claim = _resign_legacy_claim(claim)
    reconcile.LIVE_CLAIM.write_text(json.dumps(claim) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unsafe reconciliation input path"):
        reconcile.run(
            execute=False,
            quarantine_legacy_ledger_and_abandon_checkpoint=True,
        )
    assert outside.read_text(encoding="utf-8") == "123\n"


@pytest.mark.parametrize("archive_root", ("../../outside", "/tmp/outside"))
def test_completed_receipt_rejects_noncanonical_archive_path_before_read(
    tmp_path,
    monkeypatch,
    archive_root,
):
    _root, results, _bots = _configure_paths(tmp_path, monkeypatch)
    receipt_path = results / "forged-completed-receipt.json"
    receipt_path.write_text(
        json.dumps(_completed_receipt_for_archive(archive_root)) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="archive path"):
        reconcile._validated_completed_receipt(receipt_path)


def test_completed_receipt_rejects_symlinked_archive_root_before_claim_read(
    tmp_path,
    monkeypatch,
):
    root, results, _bots = _configure_paths(tmp_path, monkeypatch)
    reconcile.ARCHIVE_BASE.mkdir(parents=True)
    outside = tmp_path / "outside-archive"
    outside.mkdir()
    archive_root = reconcile.ARCHIVE_BASE / "legacy-aaaaaaaaaaaa-bbbbbbbbbbbb"
    archive_root.symlink_to(outside, target_is_directory=True)
    archive_relative = str(archive_root.relative_to(root))
    receipt_path = results / "forged-completed-receipt.json"
    receipt_path.write_text(
        json.dumps(_completed_receipt_for_archive(archive_relative)) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="directory ancestor is unsafe"):
        reconcile._validated_completed_receipt(receipt_path)


@pytest.mark.parametrize("symlink_location", ("base", "root", "runtime_control"))
def test_execute_rejects_symlinked_archive_destination_before_claim_or_move(
    tmp_path,
    monkeypatch,
    symlink_location,
):
    root, _results, _bots, claim = _prepared_legacy_claim(tmp_path, monkeypatch)
    archive_root = root / claim["archive_root"]
    outside = tmp_path / f"outside-{symlink_location}"
    outside.mkdir()
    if symlink_location == "base":
        reconcile.ARCHIVE_BASE.parent.mkdir(parents=True, exist_ok=True)
        reconcile.ARCHIVE_BASE.symlink_to(outside, target_is_directory=True)
    elif symlink_location == "root":
        reconcile.ARCHIVE_BASE.mkdir(parents=True)
        archive_root.symlink_to(outside, target_is_directory=True)
    else:
        archive_root.mkdir(parents=True)
        (archive_root / "runtime_control").symlink_to(
            outside,
            target_is_directory=True,
        )

    with pytest.raises(RuntimeError, match="directory .*unsafe"):
        reconcile.run(
            execute=True,
            acknowledge_runtime_checkout=True,
            quarantine_legacy_ledger_and_abandon_checkpoint=True,
        )
    assert not reconcile.LIVE_CLAIM.exists()
    assert reconcile.CHECKPOINT.exists()
    assert reconcile.LEDGER.exists()


@pytest.mark.parametrize("corrupt_binding", (False, True))
def test_unknown_legacy_bytes_are_zero_weight_and_bad_binding_cannot_mint(
    tmp_path,
    monkeypatch,
    corrupt_binding,
):
    root, _results, bots = _configure_paths(tmp_path, monkeypatch)
    reset = _write_reset_authority(root, _policy_epoch_reset_receipt())
    checkpoint = _fresh_v143_checkpoint(root, reset)
    if corrupt_binding:
        checkpoint["epoch_binding"]["binding_digest"] = "0" * 64
    raw_legacy = b"{malformed-without-final-newline"
    reconcile.LEDGER.write_bytes(raw_legacy)
    reconcile.CHECKPOINT.write_text(json.dumps(checkpoint), encoding="utf-8")
    _candidate(bots / "national_v143")

    plan = reconcile.run(
        execute=False,
        quarantine_legacy_ledger_and_abandon_checkpoint=True,
    )
    legacy = plan["inputs"]["legacy_ledger"]
    assert legacy["authority_weight"] == 0
    assert legacy["recognized_legacy_shape"] is False
    assert "partial_final_row" in legacy["issues"]
    assert plan["inputs"]["target_successor"] is True
    assert plan["inputs"]["allocation_receipt_eligible"] is (not corrupt_binding)

    receipt = reconcile.run(
        execute=True,
        acknowledge_runtime_checkout=True,
        quarantine_legacy_ledger_and_abandon_checkpoint=True,
    )
    assert bool(receipt["allocation_receipt_digest"]) is (not corrupt_binding)
    if corrupt_binding:
        assert not reconcile.LEDGER.exists()
    else:
        rows = evolution_infra.load_abandoned_version_receipts(
            path=reconcile.LEDGER,
            project_root=root,
        )
        assert [row["workflow_run_id"] for row in rows] == [
            "generation:143:workflow-v18"
        ]
