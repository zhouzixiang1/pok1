"""Retired slice2b lifecycle-store archive (T5, 2026-10-15).

``POK_SLICE2B_ENABLED=0`` retired the one-ahead feature on 2026-08-15; the
persisted lifecycle store (``results/workflow/slice2b_lifecycle.sqlite3``,
~126MB on the cloud host) stopped receiving writes at the same moment.  These
tests pin the controlled archive path contract:

* DISABLED + idle beyond the threshold -> the store is MOVED (never
  truncated) into the archive dir under its original name plus an archive
  timestamp, and a content-bound receipt is left at the original location.
* DISABLED + missing store -> silently tolerated (no error, no archive dir).
* ENABLED -> never archives, no matter how idle the store looks.
* Not idle enough -> no-op.
* WAL/SHM sidecars travel with the main file (byte-preserving move).
* ``recover_at_boot`` tolerates the archived-away store: no recovery
  attempt, no error, empty result.
* The receipt shape is exact and its digest is content-bound.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

import pytest

from producer_consumer_slice2b import (
    SLICE2B_LIFECYCLE_ARCHIVE_RECEIPT_NAME,
    SLICE2B_LIFECYCLE_ARCHIVE_RECEIPT_SCHEMA,
    Slice2bError,
    archive_idle_slice2b_lifecycle,
)
from producer_consumer_slice2b_activation import (
    SLICE2B_ENV_VAR,
    Slice2bActivation,
    maybe_archive_retired_slice2b_lifecycle,
)
from producer_consumer_workflow_store import ProducerConsumerWorkflowAdapter
from workflow_kernel import WorkflowStore


NOW = 1_800_000_000.0
STAMP = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(NOW))
IDLE_SECONDS = 40 * 24 * 3600.0


def _make_idle_store(tmp_path: Path, *, age: float = IDLE_SECONDS) -> Path:
    """Create a lifecycle-looking sqlite store with an old mtime."""
    db = tmp_path / "workflow" / "slice2b_lifecycle.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"sqlite-dead-store-bytes")
    os.utime(db, (NOW - age, NOW - age))
    return db


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(value: dict) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Disabled-state archive + missing-store tolerance
# ---------------------------------------------------------------------------


def test_disabled_archives_idle_store_and_writes_receipt(tmp_path):
    db = _make_idle_store(tmp_path)
    archive_dir = tmp_path / "results" / "archive"
    before_bytes = db.stat().st_size
    before_sha = _sha256_file(db)
    before_mtime = db.stat().st_mtime

    receipt = archive_idle_slice2b_lifecycle(
        enabled=False,
        db_path=db,
        archive_dir=archive_dir,
        idle_threshold_seconds=30 * 24 * 3600.0,
        now=NOW,
    )

    assert isinstance(receipt, dict)
    # The store was MOVED, not copied/truncated: original gone, archived copy
    # byte-identical under the original name + archive timestamp.
    assert not db.exists()
    archived = archive_dir / f"slice2b_lifecycle.sqlite3.{STAMP}"
    assert archived.is_file()
    assert _sha256_file(archived) == before_sha
    assert archived.stat().st_size == before_bytes
    assert archived.stat().st_mtime == before_mtime
    # The receipt is left at the ORIGINAL location and records the destination.
    receipt_path = db.parent / SLICE2B_LIFECYCLE_ARCHIVE_RECEIPT_NAME
    assert receipt_path.is_file()
    on_disk = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert on_disk == receipt


def test_disabled_missing_store_is_tolerated(tmp_path):
    """Disabled boot with an archived-away (missing) store: no error, no side
    effects -- not even the archive directory is created."""
    archive_dir = tmp_path / "results" / "archive"
    receipt = archive_idle_slice2b_lifecycle(
        enabled=False,
        db_path=tmp_path / "workflow" / "slice2b_lifecycle.sqlite3",
        archive_dir=archive_dir,
        idle_threshold_seconds=IDLE_SECONDS,
        now=NOW,
    )
    assert receipt is None
    assert not archive_dir.exists()
    assert not (tmp_path / "workflow").exists()


def test_disabled_not_idle_enough_is_a_noop(tmp_path):
    db = _make_idle_store(tmp_path, age=3600.0)
    archive_dir = tmp_path / "results" / "archive"
    receipt = archive_idle_slice2b_lifecycle(
        enabled=False,
        db_path=db,
        archive_dir=archive_dir,
        idle_threshold_seconds=IDLE_SECONDS,
        now=NOW,
    )
    assert receipt is None
    assert db.is_file()  # store untouched
    assert not archive_dir.exists()
    assert not (db.parent / SLICE2B_LIFECYCLE_ARCHIVE_RECEIPT_NAME).exists()


def test_disabled_moves_wal_and_shm_sidecars_alongside(tmp_path):
    """A leftover WAL holds committed pages not yet in the main db; the move
    must carry it (and the -shm index) so the archived store is complete."""
    db = _make_idle_store(tmp_path)
    wal = db.with_name(db.name + "-wal")
    shm = db.with_name(db.name + "-shm")
    wal.write_bytes(b"wal-pages")
    shm.write_bytes(b"shm-index")
    for side in (wal, shm):
        os.utime(side, (NOW - IDLE_SECONDS, NOW - IDLE_SECONDS))
    archive_dir = tmp_path / "results" / "archive"

    receipt = archive_idle_slice2b_lifecycle(
        enabled=False,
        db_path=db,
        archive_dir=archive_dir,
        idle_threshold_seconds=30 * 24 * 3600.0,
        now=NOW,
    )

    assert receipt is not None
    assert not db.exists() and not wal.exists() and not shm.exists()
    assert (archive_dir / f"slice2b_lifecycle.sqlite3.{STAMP}").is_file()
    assert (archive_dir / f"slice2b_lifecycle.sqlite3.{STAMP}-wal").read_bytes() == b"wal-pages"
    assert (archive_dir / f"slice2b_lifecycle.sqlite3.{STAMP}-shm").read_bytes() == b"shm-index"
    names = {entry["original_name"] for entry in receipt["archived_files"]}
    assert names == {
        "slice2b_lifecycle.sqlite3",
        "slice2b_lifecycle.sqlite3-wal",
        "slice2b_lifecycle.sqlite3-shm",
    }


def test_destination_collision_fails_closed(tmp_path):
    """A same-timestamp destination collision raises instead of clobbering an
    existing archive (os.replace would silently overwrite)."""
    db = _make_idle_store(tmp_path)
    archive_dir = tmp_path / "results" / "archive"
    first = archive_idle_slice2b_lifecycle(
        enabled=False,
        db_path=db,
        archive_dir=archive_dir,
        idle_threshold_seconds=30 * 24 * 3600.0,
        now=NOW,
    )
    assert first is not None
    # A second dead store appears and is archived with the SAME clock instant.
    db2 = _make_idle_store(tmp_path)
    with pytest.raises(Slice2bError, match="destination_exists"):
        archive_idle_slice2b_lifecycle(
            enabled=False,
            db_path=db2,
            archive_dir=archive_dir,
            idle_threshold_seconds=30 * 24 * 3600.0,
            now=NOW,
        )
    assert db2.is_file()  # refused before touching the source


# ---------------------------------------------------------------------------
# Enabled-state refusal
# ---------------------------------------------------------------------------


def test_enabled_never_archives_no_matter_how_idle(tmp_path):
    db = _make_idle_store(tmp_path)
    archive_dir = tmp_path / "results" / "archive"
    receipt = archive_idle_slice2b_lifecycle(
        enabled=True,
        db_path=db,
        archive_dir=archive_dir,
        idle_threshold_seconds=0.0,  # infinitely idle; must still refuse
        now=NOW,
    )
    assert receipt is None
    assert db.is_file()
    assert not archive_dir.exists()
    assert not (db.parent / SLICE2B_LIFECYCLE_ARCHIVE_RECEIPT_NAME).exists()


def test_maybe_archive_assembly_respects_live_feature_flag(tmp_path, monkeypatch):
    """The assembly wrapper resolves POK_SLICE2B_ENABLED: disabled archives,
    enabled refuses -- same store, same clock."""
    db = _make_idle_store(tmp_path)
    archive_dir = tmp_path / "results" / "archive"

    monkeypatch.setenv(SLICE2B_ENV_VAR, "1")
    assert maybe_archive_retired_slice2b_lifecycle(
        db_path=db,
        archive_dir=archive_dir,
        idle_threshold_seconds=30 * 24 * 3600.0,
        now=NOW,
    ) is None
    assert db.is_file() and not archive_dir.exists()

    monkeypatch.delenv(SLICE2B_ENV_VAR, raising=False)
    receipt = maybe_archive_retired_slice2b_lifecycle(
        db_path=db,
        archive_dir=archive_dir,
        idle_threshold_seconds=30 * 24 * 3600.0,
        now=NOW,
    )
    assert receipt is not None
    assert not db.exists()
    assert (archive_dir / f"slice2b_lifecycle.sqlite3.{STAMP}").is_file()


# ---------------------------------------------------------------------------
# Receipt shape
# ---------------------------------------------------------------------------


def test_receipt_shape_is_exact_and_content_bound(tmp_path):
    db = _make_idle_store(tmp_path)
    size = db.stat().st_size
    sha = _sha256_file(db)
    receipt = archive_idle_slice2b_lifecycle(
        enabled=False,
        db_path=db,
        archive_dir=tmp_path / "archive",
        idle_threshold_seconds=30 * 24 * 3600.0,
        now=NOW,
    )
    assert receipt is not None
    assert set(receipt) == {
        "schema",
        "schema_version",
        "archived_at_epoch",
        "archived_at_utc",
        "feature_env_var",
        "feature_enabled_at_archive",
        "reason",
        "source_dir",
        "source_file_name",
        "archive_dir",
        "archived_files",
        "idle_seconds_observed",
        "idle_threshold_seconds",
        "restore_policy",
        "receipt_digest",
    }
    assert receipt["schema"] == SLICE2B_LIFECYCLE_ARCHIVE_RECEIPT_SCHEMA
    assert receipt["schema_version"] == 1
    assert receipt["archived_at_epoch"] == NOW
    assert receipt["archived_at_utc"] == time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW)
    )
    assert receipt["feature_env_var"] == "POK_SLICE2B_ENABLED"
    assert receipt["feature_enabled_at_archive"] is False
    assert receipt["reason"] == "slice2b_disabled_and_lifecycle_idle"
    assert receipt["source_file_name"] == "slice2b_lifecycle.sqlite3"
    assert receipt["source_dir"] == str(db.parent)
    assert receipt["archive_dir"] == str(tmp_path / "archive")
    assert receipt["archived_files"] == [
        {
            "original_name": "slice2b_lifecycle.sqlite3",
            "archived_name": f"slice2b_lifecycle.sqlite3.{STAMP}",
            "bytes": size,
            "sha256": sha,
        }
    ]
    assert receipt["idle_seconds_observed"] == round(IDLE_SECONDS, 3)
    assert receipt["idle_threshold_seconds"] == 30 * 24 * 3600.0
    # No-auto-restore policy: re-enabling is a manual decision.
    assert receipt["restore_policy"]["auto_restore"] is False
    assert receipt["restore_policy"]["manual_decision_required"] is True
    # Content-bound digest over the receipt minus the digest itself.
    unsigned = {k: v for k, v in receipt.items() if k != "receipt_digest"}
    assert re.fullmatch(r"[0-9a-f]{64}", receipt["receipt_digest"])
    assert receipt["receipt_digest"] == _canonical_digest(unsigned)


# ---------------------------------------------------------------------------
# recover_at_boot tolerance for the archived-away store
# ---------------------------------------------------------------------------


def _build_activation(tmp_path: Path) -> Slice2bActivation:
    store = WorkflowStore(tmp_path / "events.sqlite3")
    adapter = ProducerConsumerWorkflowAdapter(store)
    return Slice2bActivation(adapter=adapter)


def test_recover_at_boot_tolerates_missing_store_when_disabled(tmp_path, monkeypatch):
    """Feature disabled + store archived away mid-process: recover_at_boot
    must not attempt recovery and must not raise."""
    monkeypatch.delenv(SLICE2B_ENV_VAR, raising=False)
    activation = _build_activation(tmp_path)
    db = Path(activation.ledger.db_path)
    assert db.is_file()
    # Seed one non-terminal candidate, then archive the store away (the
    # controlled path moves the file out from under the live activation).
    activation.ledger.start(
        candidate_id="candidate-v101",
        sealed_artifact_hash="a" * 64,
        envelope_effect_id="effect-1",
        envelope_digest="b" * 64,
    )
    archived = archive_idle_slice2b_lifecycle(
        enabled=False,
        db_path=db,
        archive_dir=tmp_path / "archive",
        idle_threshold_seconds=0.0,
        now=NOW,
    )
    assert archived is not None
    assert not db.exists()

    recovered = activation.recover_at_boot()
    # No recovery attempted, no error: the canonical empty result.
    assert recovered == {"rescheduled": [], "terminal": [], "rejected": []}


def test_recover_at_boot_on_fresh_post_archive_store_is_empty(tmp_path, monkeypatch):
    """Operator re-enables slice2b after the archive: a fresh activation
    starts from an empty store and recover_at_boot is a clean no-op."""
    monkeypatch.setenv(SLICE2B_ENV_VAR, "1")
    db = _make_idle_store(tmp_path)
    workflow_dir = db.parent
    receipt = archive_idle_slice2b_lifecycle(
        enabled=False,
        db_path=db,
        archive_dir=tmp_path / "archive",
        idle_threshold_seconds=30 * 24 * 3600.0,
        now=NOW,
    )
    assert receipt is not None
    assert not db.exists()
    # The archived bytes are NOT auto-restored by re-enabling.
    assert not db.exists()

    activation = _build_activation(tmp_path)
    recovered = activation.recover_at_boot()
    assert recovered == {"rescheduled": [], "terminal": [], "rejected": []}
    # A fresh empty store was created; the archive stays where the receipt
    # points (manual restoration only).
    assert Path(activation.ledger.db_path).is_file()
    assert (tmp_path / "archive" / f"slice2b_lifecycle.sqlite3.{STAMP}").is_file()
    assert (workflow_dir / SLICE2B_LIFECYCLE_ARCHIVE_RECEIPT_NAME).is_file()


def test_recover_at_boot_still_recovers_when_store_present(tmp_path, monkeypatch):
    """Regression guard: the missing-store fast path must not swallow real
    recovery when the store exists."""
    monkeypatch.delenv(SLICE2B_ENV_VAR, raising=False)
    activation = _build_activation(tmp_path)
    db = Path(activation.ledger.db_path)
    activation.ledger.start(
        candidate_id="candidate-v102",
        sealed_artifact_hash="c" * 64,
        envelope_effect_id="effect-2",
        envelope_digest="d" * 64,
        sealed_snapshot={
            "schema": "producer-consumer-slice2b-sealed-candidate-v1",
            "candidate_id": "candidate-v102",
            "next_v": 102,
            "source_v": 101,
            "artifact_hash": "c" * 64,
            "quality_native_match_timing_plan": {"submitted_at_epoch": NOW},
        },
    )
    assert db.is_file()
    recovered = activation.recover_at_boot()
    assert [e["candidate_id"] for e in recovered["rescheduled"]] == ["candidate-v102"]


# ---------------------------------------------------------------------------
# Pipeline-boot wiring (orchestrator_loop_phases phase-A setup -> assembly
# wrapper); the lazy import there keeps the dormant slice modules inert
# ---------------------------------------------------------------------------


def _seed_isolated_idle_store(age: float = IDLE_SECONDS) -> Path:
    """Seed an idle lifecycle store inside the autouse-isolated RESULTS_DIR."""
    import evolution_infra

    workflow_dir = Path(evolution_infra.RESULTS_DIR) / "workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    db = workflow_dir / "slice2b_lifecycle.sqlite3"
    db.write_bytes(b"sqlite-dead-store-bytes")
    old = time.time() - age
    os.utime(db, (old, old))
    return db


def _run_pipeline_boot(monkeypatch):
    """Drive the REAL phase-A setup body once.

    The epoch guard is mocked to raise so the body takes its documented
    early-exit right after the boot-time janitors -- which is exactly the
    window the retired-store archive hook occupies (it deliberately runs
    BEFORE the epoch guard).  Returns the phase-A result (None == the
    canonical early-exit; -1.0 would mean a crash-path escape).
    """
    import epoch_authority
    import orchestrator_loop_phases as olp

    def _raise(_operation):
        raise RuntimeError("epoch_unavailable_probe")

    monkeypatch.setattr(
        epoch_authority, "require_policy_epoch_initialized", _raise
    )
    return asyncio.run(
        olp._loop_phase_a_setup(
            None,
            None,
            True,
            None,
            None,
            olp._orch._STARTUP_RECOVERY_UNSET,
        )
    )


def test_pipeline_boot_archives_idle_store_when_disabled(monkeypatch):
    monkeypatch.delenv(SLICE2B_ENV_VAR, raising=False)
    db = _seed_isolated_idle_store()
    import evolution_infra

    archive_dir = Path(evolution_infra.ARCHIVE_DIR)

    result = _run_pipeline_boot(monkeypatch)

    assert result is None  # canonical early-exit, not the -1.0 crash sentinel
    assert not db.exists()
    archived = sorted(p.name for p in archive_dir.glob("slice2b_lifecycle.sqlite3.*"))
    assert len(archived) == 1
    assert re.fullmatch(r"slice2b_lifecycle\.sqlite3\.\d{8}T\d{6}Z", archived[0])
    receipt_path = db.parent / SLICE2B_LIFECYCLE_ARCHIVE_RECEIPT_NAME
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["feature_enabled_at_archive"] is False
    assert receipt["restore_policy"]["auto_restore"] is False


def test_pipeline_boot_never_archives_when_enabled(monkeypatch):
    monkeypatch.setenv(SLICE2B_ENV_VAR, "1")
    db = _seed_isolated_idle_store()
    import evolution_infra

    archive_dir = Path(evolution_infra.ARCHIVE_DIR)

    result = _run_pipeline_boot(monkeypatch)

    assert result is None
    assert db.is_file()
    assert not list(archive_dir.glob("slice2b_lifecycle.sqlite3.*"))
    assert not (db.parent / SLICE2B_LIFECYCLE_ARCHIVE_RECEIPT_NAME).exists()
