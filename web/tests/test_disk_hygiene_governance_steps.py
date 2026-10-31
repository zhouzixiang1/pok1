"""Governance step tests: identity keep knob, retired purge, abandon-txn
reap, events auto-rotation (frozen-signature mock), and run_disk_hygiene
wiring of the janitor safe window.

Design (2026-10-21, post-review R1-R5):
- identity keep mirrors the candidates G1 knob (POK_EVALUATION_IDENTITY_KEEP_
  SNAPSHOTS; clamp >= 1, unparsable -> library default 20; critical tier caps
  at 4);
- retired purge is fail-closed on ANY live-reference-check exception (R2) and
  receipt-first;
- abandon-txn reap skips on an active reconciliation claim (R3 ii), uses the
  ledger floor find_abandoned_version_floor() (R3 iii), reaps only receipt-
  complete quarantine transactions strictly below the floor, older than the
  window, receipt-first, and never touches the ledger itself;
- events auto-compression stays default-off and tier-gated, and drives the
  real A-track entry ``autonomous_rotate_append_only_sources(*, actor, ...)``;
  an ImportError still degrades to skipped:"unavailable" as a fail-safe.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
import types
from pathlib import Path

import abandoned_version_ledger
import disk_hygiene
import evaluation_identity_retention
import post_publication_handoff
import system_log

DAY = 24.0 * 3600.0
HEX64 = "a" * 64


def _window(
    *,
    open_: bool = True,
    tier: str = "tight",
    retired_days=7.0,
    txn_days=30.0,
    keep_cap=None,
    rotation_tier=False,
) -> dict:
    return {
        "open": open_,
        "closed_reasons": [] if open_ else ["stage_master_planned"],
        "tier": tier,
        "free_bytes": 5 * 1024 ** 3,
        "retired_purge_days": retired_days if open_ and tier in ("tight", "critical") else None,
        "abandon_txn_days": txn_days if open_ and tier in ("tight", "critical") else None,
        "identity_keep_cap": keep_cap,
        "events_rotation_tier_allowed": rotation_tier,
    }


def _no_handoff(monkeypatch):
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "none", "records": [], "issues": []},
    )


# --- identity keep knob (G1 pattern) ---------------------------------------


def test_identity_keep_env_clamps_and_falls_back(monkeypatch):
    env = disk_hygiene.IDENTITY_KEEP_ENV
    monkeypatch.delenv(env, raising=False)
    assert disk_hygiene._identity_keep_snapshots() == 20
    monkeypatch.setenv(env, "4")
    assert disk_hygiene._identity_keep_snapshots() == 4
    monkeypatch.setenv(env, "0")
    assert disk_hygiene._identity_keep_snapshots() == 1
    monkeypatch.setenv(env, "-9")
    assert disk_hygiene._identity_keep_snapshots() == 1
    monkeypatch.setenv(env, "abc")
    assert disk_hygiene._identity_keep_snapshots() == 20
    monkeypatch.setenv(env, " 6 ")
    assert disk_hygiene._identity_keep_snapshots() == 6


def _stage_identity_snapshots(results: Path, count: int) -> None:
    archive = results / "archive" / "evaluation_identity"
    archive.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        name = time.strftime(
            "%Y%m%d_%H%M%S", time.gmtime(1_700_000_000 + index * 100)
        )
        (archive / name).mkdir()


def test_identity_retention_step_passes_env_keep(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(disk_hygiene.IDENTITY_KEEP_ENV, "4")
    _stage_identity_snapshots(tmp_path, 6)
    report = disk_hygiene._evaluation_identity_retention_step(
        tmp_path, window=_window(tier="normal", retired_days=None)
    )
    assert report is not None
    assert len(report["moved"]) == 2
    retired = tmp_path / "archive" / "evaluation_identity_retired"
    staged = [
        p
        for p in retired.iterdir()
        if p.is_dir() and p.name != "retention_receipts"
    ]
    assert len(staged) == 2


def test_identity_retention_step_critical_caps_keep(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(disk_hygiene.IDENTITY_KEEP_ENV, raising=False)
    _stage_identity_snapshots(tmp_path, 6)
    report = disk_hygiene._evaluation_identity_retention_step(
        tmp_path,
        window=_window(tier="critical", retired_days=1.0, keep_cap=4),
    )
    assert report is not None
    assert len(report["moved"]) == 2


# --- retired identity purge (R2 fail-closed + receipt-first) ----------------


def _stage_retired(tmp_path: Path, name: str, digest: str) -> Path:
    retired = tmp_path / "archive" / "evaluation_identity_retired"
    snapshot = retired / name
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "evaluation_data_manifest.json").write_text(
        json.dumps({"manifest_digest": digest}), encoding="utf-8"
    )
    (snapshot / "match_history.jsonl").write_text("{}\n", encoding="utf-8")
    receipts = retired / "retention_receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    return snapshot


def _write_retention_receipt(
    tmp_path: Path, *, created_at: float, moved: list[dict]
) -> None:
    receipts = tmp_path / "archive" / "evaluation_identity_retired" / "retention_receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    body = {
        "schema_version": 1,
        "kind": "evaluation_identity_retention",
        "created_at": created_at,
        "moved": moved,
        "receipt_digest": "b" * 64,
    }
    (receipts / f"receipt-{int(created_at)}.json").write_text(
        json.dumps(body), encoding="utf-8"
    )


def test_purge_removes_aged_moved_unreferenced_snapshot(tmp_path: Path):
    name = "20260101_000000"
    snapshot = _stage_retired(tmp_path, name, HEX64)
    _write_retention_receipt(
        tmp_path,
        created_at=time.time() - 8 * DAY,
        moved=[{"name": name, "manifest_digest": HEX64, "bytes": 10}],
    )
    report = disk_hygiene._purge_retired_identity_snapshots(
        tmp_path, window=_window()
    )
    assert report is not None
    assert report.get("purged") == [name]
    assert not snapshot.exists()
    receipts = tmp_path / "archive" / "evaluation_identity_retired" / "retention_receipts"
    assert receipts.is_dir()
    kinds = []
    for path in receipts.glob("*.json"):
        body = json.loads(path.read_text(encoding="utf-8"))
        kinds.append(body.get("kind"))
    assert "evaluation_identity_retired_purged" in kinds


def test_purge_keeps_recent_unmoved_and_receipts_dir(tmp_path: Path):
    fresh = _stage_retired(tmp_path, "20260102_000000", "c" * 64)
    unmoved = _stage_retired(tmp_path, "20260103_000000", "d" * 64)
    _write_retention_receipt(
        tmp_path,
        created_at=time.time() - 1 * DAY,
        moved=[{"name": "20260102_000000", "manifest_digest": "c" * 64}],
    )
    report = disk_hygiene._purge_retired_identity_snapshots(
        tmp_path, window=_window()
    )
    assert report is not None
    assert report.get("purged") == []
    assert fresh.exists()
    assert unmoved.exists()
    receipts = tmp_path / "archive" / "evaluation_identity_retired" / "retention_receipts"
    assert receipts.is_dir()
    assert any(receipts.glob("*.json"))


def test_purge_fail_closed_on_unreadable_live_surface(tmp_path: Path):
    name = "20260101_000000"
    snapshot = _stage_retired(tmp_path, name, HEX64)
    _write_retention_receipt(
        tmp_path,
        created_at=time.time() - 8 * DAY,
        moved=[{"name": name, "manifest_digest": HEX64, "bytes": 10}],
    )
    # A present-but-unreadable live reference surface must abort the whole
    # round (R2) — never continue with an empty reference set.
    (tmp_path / "evaluation_cycle_manifest.json").write_text(
        "{not json", encoding="utf-8"
    )
    report = disk_hygiene._purge_retired_identity_snapshots(
        tmp_path, window=_window()
    )
    assert report is not None
    assert str(report.get("skipped", "")).startswith(
        "live_reference_check_failed"
    )
    assert snapshot.exists()


def test_purge_receipt_first_keeps_snapshots_when_receipt_fails(
    monkeypatch, tmp_path: Path
):
    name = "20260101_000000"
    snapshot = _stage_retired(tmp_path, name, HEX64)
    _write_retention_receipt(
        tmp_path,
        created_at=time.time() - 8 * DAY,
        moved=[{"name": name, "manifest_digest": HEX64, "bytes": 10}],
    )

    def _boom(_dir, _body):
        raise OSError("disk full")

    monkeypatch.setattr(evaluation_identity_retention, "_write_receipt", _boom)
    report = disk_hygiene._purge_retired_identity_snapshots(
        tmp_path, window=_window()
    )
    assert report is not None
    assert str(report.get("skipped", "")).startswith("receipt_failed")
    assert snapshot.exists()


def test_purge_skips_when_window_closed_or_tier_normal(tmp_path: Path):
    assert disk_hygiene._purge_retired_identity_snapshots(
        tmp_path, window=_window(open_=False)
    ) == {"skipped": "window_closed"}
    assert disk_hygiene._purge_retired_identity_snapshots(
        tmp_path, window=_window(tier="normal", retired_days=None)
    ) == {"skipped": "tier_not_eligible"}


# --- abandon transaction reap (R3) ------------------------------------------


def _stage_txn(
    root: Path,
    tx: str,
    *,
    next_v: int,
    state: str = "quarantine",
    cleared: bool = True,
    receipt: bool = True,
    age_days: float = 40.0,
) -> Path:
    tx_dir = root / tx
    tx_dir.mkdir(parents=True, exist_ok=True)
    (tx_dir / "claim.json").write_text(
        json.dumps(
            {
                "kind": "national-policy-abandon",
                "claim_digest": "e" * 64,
                "checkpoint": {"next_v": next_v, "stage": "selected"},
                "candidate": {"path": f"bots/national_cloud_v{next_v}"},
            }
        ),
        encoding="utf-8",
    )
    if receipt:
        (tx_dir / "receipt.json").write_text(
            json.dumps(
                {
                    "kind": "national-policy-recorded-abandon-finalize",
                    "checkpoint_cleared": cleared,
                    "candidate_state": state,
                    "claim_digest": "e" * 64,
                    "receipt_digest": "f" * 64,
                }
            ),
            encoding="utf-8",
        )
    (tx_dir / "candidate").mkdir(exist_ok=True)
    (tx_dir / "candidate" / "policy.py").write_text("# x\n", encoding="utf-8")
    stamp = time.time() - age_days * DAY
    os.utime(tx_dir, (stamp, stamp))
    if receipt:
        os.utime(tx_dir / "receipt.json", (stamp, stamp))
    return tx_dir


def test_reap_removes_only_eligible_transactions(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        abandoned_version_ledger, "find_abandoned_version_floor", lambda: 20
    )
    (tmp_path / "pipeline_state.json").write_text(
        json.dumps({"next_v": 30, "stage": "direction_audited"}),
        encoding="utf-8",
    )
    root = tmp_path / "policy_epoch_abandon_transactions"
    eligible = _stage_txn(root, "1" * 64, next_v=10)
    absent_state = _stage_txn(root, "2" * 64, next_v=11, state="absent")
    no_receipt = _stage_txn(root, "3" * 64, next_v=12, receipt=False)
    uncleared = _stage_txn(root, "4" * 64, next_v=13, cleared=False)
    at_floor = _stage_txn(root, "5" * 64, next_v=30)
    fresh = _stage_txn(root, "6" * 64, next_v=14, age_days=1.0)

    report = disk_hygiene._reap_aged_abandon_transactions(
        tmp_path, window=_window(), published_high=25
    )
    assert report is not None
    assert report.get("reaped") == 1
    assert not eligible.exists()
    assert absent_state.exists()
    assert no_receipt.exists()
    assert uncleared.exists()
    assert at_floor.exists()
    assert fresh.exists()

    ledger = tmp_path / disk_hygiene.REAPED_ABANDON_TXN_LEDGER_NAME
    rows = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [row["transaction_id"] for row in rows] == ["1" * 64]
    assert rows[0]["next_v"] == 10
    assert rows[0]["candidate_state"] == "quarantine"
    assert rows[0]["floor"] == 30


def test_reap_skips_when_reconciliation_claim_active(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        abandoned_version_ledger, "find_abandoned_version_floor", lambda: 20
    )
    root = tmp_path / "policy_epoch_abandon_transactions"
    eligible = _stage_txn(root, "1" * 64, next_v=10)
    (tmp_path / "policy_epoch_reconciliation_claim.json").write_text(
        "{}", encoding="utf-8"
    )
    report = disk_hygiene._reap_aged_abandon_transactions(
        tmp_path, window=_window(), published_high=25
    )
    assert report == {"skipped": "reconciliation_claim_active"}
    assert eligible.exists()


def test_reap_fail_closed_on_unreadable_floor(monkeypatch, tmp_path: Path):
    def _boom():
        raise abandoned_version_ledger.AbandonedVersionLedgerError(
            "ledger unreadable"
        )

    monkeypatch.setattr(
        abandoned_version_ledger, "find_abandoned_version_floor", _boom
    )
    root = tmp_path / "policy_epoch_abandon_transactions"
    eligible = _stage_txn(root, "1" * 64, next_v=10)
    report = disk_hygiene._reap_aged_abandon_transactions(
        tmp_path, window=_window(), published_high=25
    )
    assert report is not None
    assert str(report.get("skipped", "")).startswith("abandon_floor_unreadable")
    assert eligible.exists()


def test_reap_fail_closed_on_unparseable_primary(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        abandoned_version_ledger, "find_abandoned_version_floor", lambda: 20
    )
    (tmp_path / "pipeline_state.json").write_text("{nope", encoding="utf-8")
    root = tmp_path / "policy_epoch_abandon_transactions"
    eligible = _stage_txn(root, "1" * 64, next_v=10)
    report = disk_hygiene._reap_aged_abandon_transactions(
        tmp_path, window=_window(), published_high=25
    )
    assert report is not None
    assert str(report.get("skipped", "")).startswith(
        "primary_checkpoint_unreadable"
    )
    assert eligible.exists()


def test_reap_receipt_first_keeps_dir_when_ledger_append_fails(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(
        abandoned_version_ledger, "find_abandoned_version_floor", lambda: 20
    )
    (tmp_path / "pipeline_state.json").write_text(
        json.dumps({"next_v": 30, "stage": "direction_audited"}),
        encoding="utf-8",
    )
    root = tmp_path / "policy_epoch_abandon_transactions"
    eligible = _stage_txn(root, "1" * 64, next_v=10)

    def _boom(_results, _name, _row):
        raise OSError("no space")

    monkeypatch.setattr(disk_hygiene, "_append_locked_jsonl_receipt", _boom)
    report = disk_hygiene._reap_aged_abandon_transactions(
        tmp_path, window=_window(), published_high=25
    )
    assert report is not None
    assert report.get("reaped") == 0
    assert eligible.exists()


def test_reap_skips_when_window_closed_or_tier_normal(tmp_path: Path):
    assert disk_hygiene._reap_aged_abandon_transactions(
        tmp_path, window=_window(open_=False), published_high=25
    ) == {"skipped": "window_closed"}
    assert disk_hygiene._reap_aged_abandon_transactions(
        tmp_path, window=_window(tier="normal", txn_days=None), published_high=25
    ) == {"skipped": "tier_not_eligible"}


# --- events auto-rotation (frozen signature; default inert) -----------------


def test_rotation_disabled_by_default(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(disk_hygiene.EVENTS_ROTATION_ENV, raising=False)
    assert (
        disk_hygiene._events_auto_rotation_step(
            tmp_path, window=_window(tier="critical", rotation_tier=True)
        )
        is None
    )


def test_rotation_gated_on_window_and_tier(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(disk_hygiene.EVENTS_ROTATION_ENV, "1")
    assert disk_hygiene._events_auto_rotation_step(
        tmp_path, window=_window(open_=False, tier="critical", rotation_tier=True)
    ) == {"skipped": "window_closed"}
    assert disk_hygiene._events_auto_rotation_step(
        tmp_path, window=_window(tier="normal", rotation_tier=False)
    ) == {"skipped": "tier_not_critical"}


def test_rotation_unavailable_emits_skipped_event(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(disk_hygiene.EVENTS_ROTATION_ENV, "1")
    events = []
    monkeypatch.setattr(
        system_log,
        "log_system_event",
        lambda name, sev, msg, data=None: events.append((name, sev, msg, data)),
    )
    # Simulate a deployment where the A-track entry is absent: the step must
    # degrade to skipped:"unavailable" and say so via the canonical event.
    monkeypatch.setitem(
        sys.modules,
        "evolution_infra_archive_rotation",
        types.ModuleType("evolution_infra_archive_rotation"),
    )
    report = disk_hygiene._events_auto_rotation_step(
        tmp_path, window=_window(tier="critical", rotation_tier=True)
    )
    assert report == {"skipped": "unavailable"}
    assert events and events[0][0] == "pipeline.events_auto_rotation_skipped"


def test_rotation_success_uses_frozen_signature_and_emits_done(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv(disk_hygiene.EVENTS_ROTATION_ENV, "1")
    calls = {}

    def _entry(*, actor: str, sources=None, max_batch_bytes=None,
               skips_out=None):
        calls["actor"] = actor
        calls["sources"] = sources
        calls["max_batch_bytes"] = max_batch_bytes
        # 2026-10-31 queue #4 extended the frozen signature with skips_out
        # (per-source rebase skip reasons); this test is the canary that
        # guards the CALL SHAPE, so the extension is asserted, not drifted.
        calls["skips_out_passed_as_list"] = isinstance(skips_out, list)
        return [
            {
                "source": str(tmp_path / "events.jsonl"),
                "rotation_id": "r1",
                "start_offset": 0,
                "end_offset": 128,
                "archive": str(tmp_path / "archives" / "x.jsonl"),
                "source_preserved_append_only": True,
                "actor": actor,
            }
        ]

    fake = types.ModuleType("evolution_infra_archive_rotation")
    fake.autonomous_rotate_append_only_sources = _entry
    monkeypatch.setitem(
        sys.modules, "evolution_infra_archive_rotation", fake
    )
    events = []
    monkeypatch.setattr(
        system_log,
        "log_system_event",
        lambda name, sev, msg, data=None: events.append((name, sev, msg, data)),
    )
    report = disk_hygiene._events_auto_rotation_step(
        tmp_path, window=_window(tier="critical", rotation_tier=True)
    )
    assert calls["actor"] == "disk_hygiene_janitor"
    assert calls["sources"] is None
    assert calls["max_batch_bytes"] is None
    assert calls["skips_out_passed_as_list"] is True
    assert report is not None
    assert report.get("rotated") == 1
    # the skip-reason channel exists and is empty on an all-rotated pass
    assert report.get("skips") == []
    assert report["receipts"][0]["source_preserved_append_only"] is True
    done = [item for item in events if item[0] == "pipeline.events_auto_rotation_done"]
    assert done and done[0][3]["sources"][0]["end_offset"] == 128


def test_rotation_entry_really_compresses_events(monkeypatch, tmp_path: Path):
    """The real A-track entry must gzip and re-head, not just report a row."""

    import evolution_infra
    import evolution_infra_rotation_rebase as rebase

    results = tmp_path / "results"
    archive = results / "archive"
    results.mkdir()
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "ARCHIVE_DIR", archive)
    _no_handoff(monkeypatch)
    monkeypatch.setenv(disk_hygiene.EVENTS_ROTATION_ENV, "1")
    monkeypatch.setenv(rebase.EVENTS_REBASE_ENABLED_ENV, "1")
    monkeypatch.setenv(rebase.EVENTS_REBASE_MIN_COLD_BYTES_ENV, "16")
    monkeypatch.setenv(rebase.EVENTS_REBASE_MAX_SPAN_BYTES_ENV, "2048")
    monkeypatch.setenv(rebase.EVENTS_REBASE_MIN_INTERVAL_SEC_ENV, "0")
    events = []
    monkeypatch.setattr(
        system_log,
        "log_system_event",
        lambda name, sev, msg, data=None: events.append((name, sev, msg, data)),
    )
    source = results / "events.jsonl"
    original = b"".join(
        json.dumps({"row": index, "pad": "x" * 60}).encode("utf-8") + b"\n"
        for index in range(400)
    )
    source.write_bytes(original)

    report = disk_hygiene._events_auto_rotation_step(
        results, window=_window(tier="critical", rotation_tier=True)
    )

    assert report is not None and report["rotated"] == 1
    receipt = report["receipts"][0]
    assert receipt["source_preserved_append_only"] is False
    assert receipt["reheaded"] is True
    assert receipt["start_offset"] == 0
    assert 0 < receipt["end_offset"] < len(original)
    live = source.read_bytes()
    assert live == original[receipt["end_offset"]:]
    with gzip.open(receipt["archive"], "rb") as handle:
        assert handle.read() == original[: receipt["end_offset"]]
    state = rebase.load_rebase_state(source)
    assert state is not None and state["state"] == "completed"
    done = [item for item in events if item[0] == "pipeline.events_auto_rotation_done"]
    assert done and done[0][3]["sources"][0]["reheaded"] is True


# --- run_disk_hygiene wiring -------------------------------------------------


def test_run_disk_hygiene_reports_safe_window(monkeypatch, tmp_path: Path):
    _no_handoff(monkeypatch)
    results = tmp_path / "results"
    results.mkdir()
    (results / "pipeline_state.json").write_text(
        json.dumps({"next_v": 5, "stage": "direction_audited"}),
        encoding="utf-8",
    )
    report = disk_hygiene.run_disk_hygiene(results, min_free_bytes=1)
    assert report["ok"] is True
    window = report["janitor_window"]
    assert window["open"] is True
    assert window["tier"] in ("normal", "tight", "critical", "unknown")
    # No retired area / txn root in this fixture -> no purge rows.
    assert "evaluation_identity_retired_purge" not in report
    assert "events_auto_rotation" not in report


def test_new_reaped_ledgers_are_protected_names():
    assert "reaped_replays.jsonl" in disk_hygiene._PROTECTED_RESULT_NAMES
    assert (
        "reaped_abandon_transactions.jsonl"
        in disk_hygiene._PROTECTED_RESULT_NAMES
    )


def test_rotation_skip_reason_is_reported_per_source(monkeypatch, tmp_path: Path):
    """Queue #4 (2026-10-31): ``rotated: 0`` must be attributable.

    The block-33/block-34 diagnoses each had to re-derive the watermark
    arithmetic by hand because the adapter dropped the rebase layer's skip
    token.  Drive the REAL entry with a cold backlog below the min-cold floor
    and assert the reported reason names the gate that fired.
    """

    import evolution_infra
    import evolution_infra_rotation_rebase as rebase

    results = tmp_path / "results"
    archive = results / "archive"
    results.mkdir()
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "ARCHIVE_DIR", archive)
    _no_handoff(monkeypatch)
    monkeypatch.setenv(disk_hygiene.EVENTS_ROTATION_ENV, "1")
    monkeypatch.setenv(rebase.EVENTS_REBASE_ENABLED_ENV, "1")
    monkeypatch.setenv(rebase.EVENTS_REBASE_MIN_INTERVAL_SEC_ENV, "0")
    monkeypatch.setenv(rebase.EVENTS_REBASE_MIN_COLD_BYTES_ENV, "100000")
    source = results / "events.jsonl"
    # 490 bytes total, NO watermark file: the whole file is the cold prefix
    # and still sits below the 100 KB floor. (A hand-written watermark that
    # omits its required fields is intentionally rejected upstream as
    # watermark_invalid — an ERROR, not a skip — verified 2026-10-31.)
    source.write_bytes(
        b"".join(
            json.dumps({"row": i, "pad": "y" * 40}).encode() + b"\n"
            for i in range(10)
        )
    )
    report = disk_hygiene._events_auto_rotation_step(
        results, window=_window(tier="critical", rotation_tier=True)
    )
    assert report is not None
    assert report["rotated"] == 0
    assert report["receipts"] == []
    skips = report.get("skips") or []
    assert len(skips) == 1, skips
    assert skips[0]["source"] == "events.jsonl"
    assert skips[0]["status"] == "skipped"
    assert skips[0]["reason"] == "below_threshold", skips
