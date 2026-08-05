"""Tests for the radically-simplified abandoned-versions ledger.

Covers the headline behavioral guarantees of the 2026-08-05 refactor
(commit 3fc5ec64):

1. Historical ledger rows are immutable records that survive a parent
   re-publish (re-certification / tag rewrite) -- they are never re-resolved
   against live git on load.
2. The holistic ``head_digest`` fingerprint changes when a row is appended or
   any field of any row is mutated (the allocation CAS property).
3. The one-time migration script strips the legacy chain fields correctly.
"""

import json
import sys
from pathlib import Path

import pytest

import evolution_infra
from abandoned_version_ledger import (
    _abandoned_ledger_head_digest,
    _abandoned_version_receipt_identity_digest,
    load_abandoned_version_receipts,
)


# ---------------------------------------------------------------------------
# Shared synthetic helpers (mirror the conftest synthetic_checkpoint_authority
# pattern, kept local so this module is self-contained).
# ---------------------------------------------------------------------------

def _published_parent(version, *, tree="9" * 40, tag_object="b" * 40):
    return {
        "version": version,
        "bot": f"national_cloud_v{version}",
        "role": "parent_source",
        "epoch": "national_tcp_policy_v1",
        "runtime_manifest_digest": "1" * 64,
        "epoch_receipt_digest": "2" * 64,
        "publication_identity_digest": "3" * 64,
        "certificate_digest": "4" * 64,
        "completion_tag": f"national-cloud-bot-v{version}",
        "completion_tag_object_oid": tag_object,
        "high_water_tag": f"national-cloud-high-water-v{version}",
        "high_water_tag_object_oid": "5" * 40,
        "publication_commit_oid": "6" * 40,
        "completion_tree_oid": tree,
        "tag_artifact_hash": "7" * 64,
    }


def _binding(next_v, source_v, *, published_high_water, abandoned_floor,
             abandoned_head, parent_versions, parent_identities, parent2_v=None):
    payload = {
        "schema_version": 2,
        "epoch": "national_tcp_policy_v1",
        "mode": "published_strict_parent",
        "next_v": next_v,
        "source_v": source_v,
        "parent2_v": parent2_v,
        "parent_versions": parent_versions,
        "source_artifact_inherited": True,
        "parent_authority": "strict_published_parent_resolution",
        "published_parent_identities": parent_identities,
        "protocol_bootstrap_receipt_digest": None,
        "policy_epoch_reset_receipt_digest": None,
        "published_high_water": published_high_water,
        "abandoned_receipt_floor": abandoned_floor,
        "abandoned_receipt_head_digest": abandoned_head,
        "allocation_floor": max(published_high_water, abandoned_floor),
    }
    from checkpoint_schema import _canonical_digest

    payload["binding_digest"] = _canonical_digest(
        {k: v for k, v in payload.items() if k != "binding_digest"}
    )
    return payload


def _envelope(next_v, source_v, *, binding):
    return {
        "checkpoint_schema_version": 2,
        "evaluation_epoch": "national_tcp_policy_v1",
        "next_v": next_v,
        "source_v": source_v,
        "parent2_v": binding.get("parent2_v"),
        "generation_mode": None,
        "epoch_binding": binding,
        "audit_context": {},
    }


def _receipt_row(version, source_v, *, reason, parent_versions, parent_identities,
                 published_high_water, abandoned_floor, abandoned_head, timestamp=1700000000.0,
                 stage="direction_audited", workflow_run_id=None, checkpoint_revision=1):
    if workflow_run_id is None:
        workflow_run_id = f"generation:{version}:workflow-v1"
    return {
        "schema_version": 1,
        "kind": "national-policy-abandon-receipt",
        "evaluation_epoch": "national_tcp_policy_v1",
        "version": version,
        "source_v": source_v,
        "checkpoint_stage": stage,
        "workflow_run_id": workflow_run_id,
        "checkpoint_revision": checkpoint_revision,
        "checkpoint_envelope": _envelope(
            version, source_v,
            binding=_binding(
                version, source_v,
                published_high_water=published_high_water,
                abandoned_floor=abandoned_floor,
                abandoned_head=abandoned_head,
                parent_versions=parent_versions,
                parent_identities=parent_identities,
            ),
        ),
        "reason": reason,
        "timestamp": timestamp,
        "infra_failure": None,
    }


def _write_ledger(tmp_path: Path, rows: list[dict]) -> Path:
    ledger = tmp_path / "abandoned_versions.jsonl"
    payload = "".join(
        json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for r in rows
    )
    ledger.write_text(payload, encoding="utf-8")
    return ledger


# ---------------------------------------------------------------------------
# 1. Historical row survives parent re-publish (the root-cause fix)
# ---------------------------------------------------------------------------

def test_historical_row_loads_after_parent_tree_oid_drift(tmp_path, monkeypatch):
    """A historical abandon row whose parent's tree OID has since changed
    (legitimate re-certification) must still decode.  Before the refactor this
    raised ``checkpoint_parent_authority_identity_drift`` and wedged the whole
    ledger."""
    # A historical row recorded parent v27 with tree OID "9"*40.
    old_parent = _published_parent(27, tree="9" * 40)
    row = _receipt_row(
        30, 27, reason="worker_terminal_abandon",
        parent_versions=[27], parent_identities=[old_parent],
        published_high_water=29, abandoned_floor=29, abandoned_head=None,
    )
    ledger = _write_ledger(tmp_path, [row])

    # The live git now has a DIFFERENT tree OID for v27 (re-published).
    live_parent = _published_parent(27, tree="d" * 40)
    monkeypatch.setattr(
        "checkpoint_schema.resolve_national_bot_spec",
        lambda *a, **k: type("S", (), {"eligible": True})(),
    )
    monkeypatch.setattr(
        "checkpoint_schema.resolve_published_parent_tag_authority",
        lambda *a, **k: {},
    )

    rows = load_abandoned_version_receipts(path=ledger, project_root=tmp_path)
    assert len(rows) == 1
    assert rows[0]["version"] == 30


def test_live_checkpoint_still_drift_checked(tmp_path, monkeypatch):
    """The live checkpoint being abandoned still receives the full drift
    check -- historical_receipt=False must NOT skip it."""
    from abandoned_version_ledger import _validate_abandoned_checkpoint
    from evolution_infra import AbandonedVersionLedgerError

    live_parent = _published_parent(27, tree="9" * 40)
    binding = _binding(
        31, 27, published_high_water=29, abandoned_floor=30,
        abandoned_head=_abandoned_ledger_head_digest([]),
        parent_versions=[27], parent_identities=[live_parent],
    )
    checkpoint = {
        **_envelope(31, 27, binding=binding),
        "stage": "direction_audited",
        "workflow_run_id": "generation:31:workflow-v1",
        "checkpoint_revision": 1,
    }
    # Live git returns a DIFFERENT tree -> drift must be caught.
    monkeypatch.setattr(
        "checkpoint_schema.resolve_national_bot_spec",
        lambda *a, **k: type("S", (), {"eligible": True})(),
    )
    monkeypatch.setattr(
        "checkpoint_schema.resolve_published_parent_tag_authority",
        lambda *a, **k: {},
    )
    with pytest.raises(AbandonedVersionLedgerError) as exc:
        _validate_abandoned_checkpoint(checkpoint, project_root=tmp_path)
    # The live checkpoint must fail the parent-authority check (drift or
    # version/identity mismatch -- either way the live re-resolution ran and
    # caught the stale binding, which is the property under test).
    msg = str(exc.value)
    assert (
        "checkpoint_parent_authority_identity_drift" in msg
        or "checkpoint_parent_authority:" in msg
    ), msg


# ---------------------------------------------------------------------------
# 2. Holistic head digest changes on append / mutation (CAS property)
# ---------------------------------------------------------------------------

def test_head_digest_is_none_for_empty_ledger():
    assert _abandoned_ledger_head_digest([]) is None


def test_head_digest_changes_on_append():
    parent = _published_parent(27)
    base = dict(
        source_v=27, reason="worker_terminal_abandon",
        parent_versions=[27], parent_identities=[parent],
        published_high_water=29, abandoned_floor=29, abandoned_head=None,
    )
    r1 = _receipt_row(30, **base)
    r2 = _receipt_row(31, **base)
    head0 = _abandoned_ledger_head_digest([r1])
    head1 = _abandoned_ledger_head_digest([r1, r2])
    assert head0 is not None and head1 is not None
    assert head0 != head1


def test_head_digest_changes_on_any_row_mutation():
    parent = _published_parent(27)
    base = dict(
        source_v=27, reason="worker_terminal_abandon",
        parent_versions=[27], parent_identities=[parent],
        published_high_water=29, abandoned_floor=29, abandoned_head=None,
    )
    r1 = _receipt_row(30, **base)
    r2 = _receipt_row(31, **base)
    rows = [r1, r2]
    original_head = _abandoned_ledger_head_digest(rows)
    # Mutate a field in the first row.
    rows[0] = {**rows[0], "reason": "tampered_reason"}
    mutated_head = _abandoned_ledger_head_digest(rows)
    assert original_head != mutated_head


def test_identity_digest_is_stable_and_excludes_legacy_fields():
    parent = _published_parent(27)
    base = dict(
        source_v=27, reason="worker_terminal_abandon",
        parent_versions=[27], parent_identities=[parent],
        published_high_water=29, abandoned_floor=29, abandoned_head=None,
    )
    row = _receipt_row(30, **base)
    clean_digest = _abandoned_version_receipt_identity_digest(row)
    # Adding the legacy chain fields must NOT change the identity digest.
    legacy_row = {**row, "receipt_digest": "a" * 64, "previous_receipt_digest": None}
    assert _abandoned_version_receipt_identity_digest(legacy_row) == clean_digest


# ---------------------------------------------------------------------------
# 3. Migration script strips legacy fields correctly
# ---------------------------------------------------------------------------

def test_migrate_dry_run_reports_but_does_not_write(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    try:
        import migrate_abandon_ledger_drop_chain_digests as mig
    finally:
        sys.path.pop(0)
    parent = _published_parent(27)
    base = dict(
        source_v=27, reason="worker_terminal_abandon",
        parent_versions=[27], parent_identities=[parent],
        published_high_water=29, abandoned_floor=29, abandoned_head=None,
    )
    legacy_row = {
        **_receipt_row(30, **base),
        "receipt_digest": "a" * 64,
        "previous_receipt_digest": None,
    }
    ledger = _write_ledger(tmp_path, [legacy_row])
    before = ledger.read_text(encoding="utf-8")
    rc = mig.migrate(ledger, execute=False)
    assert rc == 0
    assert ledger.read_text(encoding="utf-8") == before  # unchanged


def test_migrate_strips_legacy_fields(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    try:
        import migrate_abandon_ledger_drop_chain_digests as mig
    finally:
        sys.path.pop(0)
    parent = _published_parent(27)
    base = dict(
        source_v=27, reason="worker_terminal_abandon",
        parent_versions=[27], parent_identities=[parent],
        published_high_water=29, abandoned_floor=29, abandoned_head=None,
    )
    legacy_row = {
        **_receipt_row(30, **base),
        "receipt_digest": "a" * 64,
        "previous_receipt_digest": None,
    }
    ledger = _write_ledger(tmp_path, [legacy_row])
    rc = mig.migrate(ledger, execute=True)
    assert rc == 0
    migrated = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert "receipt_digest" not in migrated
    assert "previous_receipt_digest" not in migrated
    # All schema fields preserved.
    assert migrated["version"] == 30
    assert migrated["reason"] == "worker_terminal_abandon"
    assert migrated["checkpoint_envelope"]["epoch_binding"]["parent_versions"] == [27]


def test_migrate_is_idempotent(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    try:
        import migrate_abandon_ledger_drop_chain_digests as mig
    finally:
        sys.path.pop(0)
    parent = _published_parent(27)
    base = dict(
        source_v=27, reason="worker_terminal_abandon",
        parent_versions=[27], parent_identities=[parent],
        published_high_water=29, abandoned_floor=29, abandoned_head=None,
    )
    ledger = _write_ledger(tmp_path, [{**_receipt_row(30, **base), "receipt_digest": "a" * 64}])
    assert mig.migrate(ledger, execute=True) == 0
    after_first = ledger.read_text(encoding="utf-8")
    # Second run finds nothing to strip.
    rc = mig.migrate(ledger, execute=True)
    assert rc == 0
    assert ledger.read_text(encoding="utf-8") == after_first
