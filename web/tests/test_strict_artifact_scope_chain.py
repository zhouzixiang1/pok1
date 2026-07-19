"""Prepared→Worker→Quality scope authority for the strict five-file ABI."""

from __future__ import annotations

import json
import py_compile
import sys

from bot_artifact import canonical_digest, hash_path
from bot_namespace import (
    SYSTEM_DERIVED_IDENTITY_FILES,
    policy_identity_document_errors,
    refresh_policy_identity_documents,
)
from prepared_baseline_contract import build_prepared_artifact_contract
from tool_gates import _prepared_artifact_delta_files
from worker_boundary import (
    audit_strict_policy_artifact_delta_against_plan,
    audit_worker_boundary,
    snapshot_python_files,
)


def _strict_bot(root, *, version=144, parents=(143,)):
    root.mkdir()
    (root / "national_bot.py").write_text(
        "def run():\n    return None\n", encoding="utf-8"
    )
    (root / "precompute.py").write_text(
        "TABLE = (1, 2, 3)\n", encoding="utf-8"
    )
    (root / "policy.py").write_text(
        "def decide(context):\n    return {'kind': 'pass'}\n",
        encoding="utf-8",
    )
    (root / "national_runtime_manifest.json").write_text("{}\n", encoding="utf-8")
    (root / "policy_epoch_receipt.json").write_text("{}\n", encoding="utf-8")
    refresh_policy_identity_documents(
        root,
        version,
        parent_versions=parents,
    )
    assert policy_identity_document_errors(
        root,
        version,
        parent_versions=parents,
    ) == []
    return root


def _task():
    return {
        "worker_id": "policy-owner",
        "role": "Algorithmic Logic Architect",
        "target_files": ["policy.py"],
        "must_change_files": ["policy.py"],
    }


def _worker_policy_edit(bot):
    (bot / "policy.py").write_text(
        "def decide(context):\n    return {'kind': 'fold'}\n",
        encoding="utf-8",
    )


def test_prepared_worker_quality_chain_accepts_only_verified_identity_derivation(
    tmp_path,
):
    bot = _strict_bot(tmp_path / "national_v144")
    prepared = build_prepared_artifact_contract(
        bot,
        source_v=143,
        next_v=144,
    )
    checkpoint = {
        "source_v": 143,
        "next_v": 144,
        "audit_context": {"prepared_artifact_contract": prepared},
    }
    worker_before = snapshot_python_files(bot)

    _worker_policy_edit(bot)
    worker_audit = audit_worker_boundary(
        bot,
        _task(),
        worker_before,
        next_v=144,
    )
    assert worker_audit.passed
    assert worker_audit.changed_files == ["policy.py"]

    refresh_policy_identity_documents(bot, 144, parent_versions=(143,))
    changed, errors = _prepared_artifact_delta_files(checkpoint, bot)
    assert errors == []
    assert set(changed) == {"policy.py", *SYSTEM_DERIVED_IDENTITY_FILES}

    quality_scope = audit_strict_policy_artifact_delta_against_plan(
        changed,
        [_task()],
        candidate_dir=bot,
        version=144,
        parent_versions=(143,),
    )
    assert quality_scope.passed
    assert quality_scope.allowed_files == ["policy.py"]
    assert quality_scope.system_derived_files == sorted(
        SYSTEM_DERIVED_IDENTITY_FILES
    )
    assert quality_scope.violation_files == []


def test_worker_pycompile_cache_is_host_cleaned_before_identity_refresh(tmp_path):
    """The mandatory Worker compile probe must not consume a retry attempt."""
    import tool_planning

    bot = _strict_bot(tmp_path / "national_v144")
    task_context = bot / ".task_context"
    task_context.mkdir()
    (task_context / "worker.md").write_text(
        "compiler-owned input\n", encoding="utf-8"
    )
    _worker_policy_edit(bot)

    py_compile.compile(str(bot / "policy.py"), doraise=True)
    cache = bot / "__pycache__"
    assert cache.is_dir()
    assert any(cache.glob(f"policy.{sys.implementation.cache_tag}.pyc"))

    removed = tool_planning._cleanup_worker_transients_before_identity_refresh(bot)

    assert "__pycache__" in removed
    assert not cache.exists()
    assert task_context.is_dir()
    refresh_policy_identity_documents(bot, 144, parent_versions=(143,))
    assert policy_identity_document_errors(
        bot,
        144,
        parent_versions=(143,),
        allow_working_task_context=True,
    ) == []


def test_worker_pre_identity_cleanup_does_not_hide_extra_artifact(tmp_path):
    """Only known transient caches are cleaned; other layout drift stays fatal."""
    import pytest
    import tool_planning

    bot = _strict_bot(tmp_path / "national_v144")
    _worker_policy_edit(bot)
    py_compile.compile(str(bot / "policy.py"), doraise=True)
    extra = bot / "candidate-owned-table"
    extra.mkdir()
    (extra / "payload.bin").write_bytes(b"unbound")

    tool_planning._cleanup_worker_transients_before_identity_refresh(bot)

    assert extra.is_dir()
    with pytest.raises(
        ValueError,
        match="artifact_extra_directory_forbidden:candidate-owned-table",
    ):
        refresh_policy_identity_documents(bot, 144, parent_versions=(143,))


def test_first_strict_blueprint_three_file_delta_is_not_scope_rejected(tmp_path):
    from system_strict_bootstrap import BLUEPRINT_DIR, materialize_fresh_candidate

    bot = tmp_path / "national_v143"
    materialize_fresh_candidate(bot, version=143)
    prepared = build_prepared_artifact_contract(
        bot,
        source_v=142,
        next_v=143,
    )
    checkpoint = {
        "source_v": 142,
        "next_v": 143,
        "audit_context": {"prepared_artifact_contract": prepared},
    }
    (bot / "policy.py").write_bytes((BLUEPRINT_DIR / "policy.py").read_bytes())
    refresh_policy_identity_documents(bot, 143, parent_versions=())

    changed, errors = _prepared_artifact_delta_files(checkpoint, bot)
    assert errors == []
    assert set(changed) == {"policy.py", *SYSTEM_DERIVED_IDENTITY_FILES}
    result = audit_strict_policy_artifact_delta_against_plan(
        changed,
        [_task()],
        candidate_dir=bot,
        version=143,
        parent_versions=(),
    )
    assert result.passed
    assert result.system_derived_files == sorted(SYSTEM_DERIVED_IDENTITY_FILES)


def test_worker_cannot_claim_identity_refresh_as_declared_write(tmp_path):
    bot = _strict_bot(tmp_path / "national_v144")
    before = snapshot_python_files(bot)
    _worker_policy_edit(bot)
    (bot / "national_runtime_manifest.json").write_text(
        '{"worker":"forged"}\n', encoding="utf-8"
    )

    result = audit_worker_boundary(bot, _task(), before, next_v=144)

    assert not result.passed
    assert result.violation_files == ["national_runtime_manifest.json"]


def test_quality_requires_refresh_receipt_bound_to_durable_worker_effect(tmp_path):
    bot = _strict_bot(tmp_path / "national_v144")
    before = snapshot_python_files(bot)
    _worker_policy_edit(bot)
    identity = refresh_policy_identity_documents(
        bot, 144, parent_versions=(143,)
    )
    from worker_boundary import diff_snapshot

    changed = diff_snapshot(bot, before)
    missing = audit_strict_policy_artifact_delta_against_plan(
        changed,
        [_task()],
        candidate_dir=bot,
        version=144,
        parent_versions=(143,),
        require_identity_refresh_receipt=True,
    )
    assert not missing.passed
    assert any("refresh_receipt_missing" in item for item in missing.violations)

    artifact_hash = hash_path(bot)
    durable = {
        "artifact_hash": artifact_hash,
        "envelope_digest": "a" * 64,
        "effect_id": "effect-1",
        "lease_epoch": 3,
    }
    subject = {
        "schema_version": 1,
        "kind": "strict-policy-identity-refresh-v1",
        "version": 144,
        "parent_versions": [143],
        "candidate_changed_files": ["policy.py"],
        "system_derived_files": sorted(SYSTEM_DERIVED_IDENTITY_FILES),
        "final_changed_files": sorted(
            {"policy.py", *SYSTEM_DERIVED_IDENTITY_FILES}
        ),
        "runtime_manifest_digest": identity["runtime_manifest_digest"],
        "epoch_receipt_digest": identity["epoch_receipt_digest"],
        "envelope_digest": durable["envelope_digest"],
        "effect_id": durable["effect_id"],
        "lease_epoch": durable["lease_epoch"],
        "output_artifact_hash": artifact_hash,
    }
    receipt = {**subject, "receipt_digest": canonical_digest(subject)}
    accepted = audit_strict_policy_artifact_delta_against_plan(
        changed,
        [_task()],
        candidate_dir=bot,
        version=144,
        parent_versions=(143,),
        identity_refresh_receipt=receipt,
        durable_worker_output=durable,
        require_identity_refresh_receipt=True,
    )
    assert accepted.passed
    assert accepted.system_derived_files == sorted(SYSTEM_DERIVED_IDENTITY_FILES)


def test_quality_scope_rejects_helper_and_binary_even_with_valid_identities(
    tmp_path,
):
    for rogue_name, payload in (
        ("helper.py", b"def hidden():\n    return 1\n"),
        ("equity.bin", b"\x00\xffpolicy-table"),
    ):
        bot = _strict_bot(tmp_path / rogue_name.replace(".", "_"))
        before = snapshot_python_files(bot)
        _worker_policy_edit(bot)
        refresh_policy_identity_documents(bot, 144, parent_versions=(143,))
        (bot / rogue_name).write_bytes(payload)
        from worker_boundary import diff_snapshot

        changed = diff_snapshot(bot, before)
        result = audit_strict_policy_artifact_delta_against_plan(
            changed,
            [_task()],
            candidate_dir=bot,
            version=144,
            parent_versions=(143,),
        )

        assert not result.passed
        assert rogue_name in result.violation_files
        assert result.system_derived_files == []


def test_quality_scope_rejects_semantically_equal_identity_json_rewrite(tmp_path):
    bot = _strict_bot(tmp_path / "national_v144")
    before = snapshot_python_files(bot)
    _worker_policy_edit(bot)
    refresh_policy_identity_documents(bot, 144, parent_versions=(143,))
    manifest_path = bot / "national_runtime_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    from worker_boundary import diff_snapshot

    changed = diff_snapshot(bot, before)
    result = audit_strict_policy_artifact_delta_against_plan(
        changed,
        [_task()],
        candidate_dir=bot,
        version=144,
        parent_versions=(143,),
    )

    assert not result.passed
    assert "national_runtime_manifest.json" in result.violation_files
    assert any("noncanonical_bytes" in item for item in result.violations)


def test_preparation_rebinds_parent_receipt_to_new_version_and_lineage(tmp_path):
    bot = _strict_bot(
        tmp_path / "copied_parent",
        version=143,
        parents=(),
    )
    refresh_policy_identity_documents(bot, 144, parent_versions=(143,))

    receipt = json.loads(
        (bot / "policy_epoch_receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["version"] == 144
    assert receipt["lineage"]["parent_versions"] == [143]
    assert policy_identity_document_errors(
        bot,
        144,
        parent_versions=(143,),
    ) == []
