"""Governance safe-window (janitor gate) unit tests.

The window is the single testable predicate that governs the destructive
governance steps (retired-identity purge, abandon-transaction reap, events
auto-rotation) and the identity-retention tightening tier:

    A. no active/blocked post-publication handoff claim
       (``pending_handoff_route()["status"] == "none"``; "pending" and
       "blocked" both close, fail-closed);
    B. stage gate: primary checkpoint absent (generation gap) opens the
       window; a parseable checkpoint opens it only at
       {selected, prepared, crossover_running, direction_audited}
       (STAGE_ORDER, pipeline_state.py); an unparseable checkpoint closes;
       any live-ahead draft slot (or unparseable draft checkpoint) closes;
    C. disk-pressure tier (normal >= tight >= critical by free bytes):
       tight enables the purge/reap steps at their default windows, critical
       tightens them (retired 1d / txn 7d, identity keep capped at 4, events
       rotation tier allowed); A/B always dominate — closed means closed.

Designed 2026-10-21 (disk-governance track); see AGENTS.md janitor notes.
"""

from __future__ import annotations

import json
from pathlib import Path

import disk_hygiene
import post_publication_handoff

GIB = 1024 * 1024 * 1024


def _no_handoff(monkeypatch):
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "none", "records": [], "issues": []},
    )


def _handoff(monkeypatch, status: str):
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": status, "records": [], "issues": []},
    )


def _write_checkpoint(results: Path, payload: dict) -> None:
    (results / "pipeline_state.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_window_open_at_generation_gap(monkeypatch, tmp_path: Path):
    _no_handoff(monkeypatch)
    window = disk_hygiene.janitor_safe_window(
        tmp_path, free_bytes=10 * GIB
    )
    assert window["open"] is True
    assert window["closed_reasons"] == []
    assert window["tier"] == "normal"
    # Normal tier: aggressive steps are not eligible.
    assert window["retired_purge_days"] is None
    assert window["abandon_txn_days"] is None
    assert window["identity_keep_cap"] is None
    assert window["events_rotation_tier_allowed"] is False


def test_window_closed_when_handoff_pending(monkeypatch, tmp_path: Path):
    _handoff(monkeypatch, "pending")
    _write_checkpoint(
        tmp_path, {"next_v": 5, "stage": "direction_audited"}
    )
    window = disk_hygiene.janitor_safe_window(
        tmp_path, free_bytes=10 * GIB
    )
    assert window["open"] is False
    assert any(r.startswith("handoff_") for r in window["closed_reasons"])


def test_window_closed_when_handoff_blocked(monkeypatch, tmp_path: Path):
    _handoff(monkeypatch, "blocked")
    window = disk_hygiene.janitor_safe_window(
        tmp_path, free_bytes=10 * GIB
    )
    assert window["open"] is False
    assert "handoff_blocked" in window["closed_reasons"]


def test_window_closed_when_handoff_check_raises(monkeypatch, tmp_path: Path):
    def _boom():
        raise RuntimeError("handoff store unreadable")

    monkeypatch.setattr(
        post_publication_handoff, "pending_handoff_route", _boom
    )
    window = disk_hygiene.janitor_safe_window(
        tmp_path, free_bytes=10 * GIB
    )
    assert window["open"] is False
    assert "handoff_blocked" in window["closed_reasons"]


def test_window_open_only_at_pre_master_stages(monkeypatch, tmp_path: Path):
    _no_handoff(monkeypatch)
    for stage in (
        "selected",
        "prepared",
        "crossover_running",
        "direction_audited",
    ):
        results = tmp_path / f"case-{stage}"
        results.mkdir()
        _write_checkpoint(results, {"next_v": 5, "stage": stage})
        window = disk_hygiene.janitor_safe_window(
            results, free_bytes=10 * GIB
        )
        assert window["open"] is True, stage


def test_window_closed_at_materializing_and_active_stages(
    monkeypatch, tmp_path: Path
):
    _no_handoff(monkeypatch)
    for stage in ("preparing", "master_planned", "workers_done", "verified"):
        results = tmp_path / f"case-{stage}"
        results.mkdir()
        _write_checkpoint(results, {"next_v": 5, "stage": stage})
        window = disk_hygiene.janitor_safe_window(
            results, free_bytes=10 * GIB
        )
        assert window["open"] is False, stage
        (results / "pipeline_state.json").unlink()
        results.rmdir()


def test_window_closed_on_unparseable_primary(monkeypatch, tmp_path: Path):
    _no_handoff(monkeypatch)
    (tmp_path / "pipeline_state.json").write_text("{not json", encoding="utf-8")
    window = disk_hygiene.janitor_safe_window(
        tmp_path, free_bytes=10 * GIB
    )
    assert window["open"] is False
    assert "primary_checkpoint_unreadable" in window["closed_reasons"]


def test_live_ahead_draft_closes_window(monkeypatch, tmp_path: Path):
    _no_handoff(monkeypatch)
    _write_checkpoint(
        tmp_path, {"next_v": 5, "stage": "direction_audited"}
    )
    (tmp_path / "pipeline_state_draft1.json").write_text(
        json.dumps({"next_v": 99, "stage": "selected"}), encoding="utf-8"
    )
    window = disk_hygiene.janitor_safe_window(
        tmp_path, published=set(), free_bytes=10 * GIB
    )
    assert window["open"] is False
    assert "live_ahead_draft" in window["closed_reasons"]


def test_draft_behind_high_water_keeps_window_open(
    monkeypatch, tmp_path: Path
):
    _no_handoff(monkeypatch)
    _write_checkpoint(
        tmp_path,
        {
            "next_v": 5,
            "stage": "direction_audited",
            "epoch_binding": {"published_high_water": 98},
        },
    )
    (tmp_path / "pipeline_state_draft1.json").write_text(
        json.dumps({"next_v": 98, "stage": "selected"}), encoding="utf-8"
    )
    window = disk_hygiene.janitor_safe_window(
        tmp_path, published=set(), free_bytes=10 * GIB
    )
    assert window["open"] is True


def test_unparseable_draft_checkpoint_closes_window(
    monkeypatch, tmp_path: Path
):
    _no_handoff(monkeypatch)
    _write_checkpoint(
        tmp_path, {"next_v": 5, "stage": "selected"}
    )
    (tmp_path / "pipeline_state_draft1.json").write_text("{oops", encoding="utf-8")
    window = disk_hygiene.janitor_safe_window(
        tmp_path, published=set(), free_bytes=10 * GIB
    )
    assert window["open"] is False
    assert "draft_checkpoint_unreadable" in window["closed_reasons"]


def test_disk_tier_thresholds(monkeypatch, tmp_path: Path):
    _no_handoff(monkeypatch)
    # Defaults: tight = 6 GiB, critical = 4 GiB (POK_DISK_MIN_FREE_GB).
    tight = disk_hygiene.janitor_safe_window(
        tmp_path, free_bytes=int(5.0 * GIB)
    )
    assert tight["tier"] == "tight"
    assert tight["retired_purge_days"] == 7.0
    assert tight["abandon_txn_days"] == 30.0
    assert tight["identity_keep_cap"] is None
    assert tight["events_rotation_tier_allowed"] is False

    critical = disk_hygiene.janitor_safe_window(
        tmp_path, free_bytes=int(3.5 * GIB)
    )
    assert critical["tier"] == "critical"
    assert critical["retired_purge_days"] == 1.0
    assert critical["abandon_txn_days"] == 7.0
    assert critical["identity_keep_cap"] == 4
    assert critical["events_rotation_tier_allowed"] is True

    normal = disk_hygiene.janitor_safe_window(
        tmp_path, free_bytes=int(7.0 * GIB)
    )
    assert normal["tier"] == "normal"

    unknown = disk_hygiene.janitor_safe_window(tmp_path, free_bytes=None)
    assert unknown["tier"] == "unknown"
    # Unmeasurable free space never unlocks destructive tiers.
    assert unknown["retired_purge_days"] is None
    assert unknown["abandon_txn_days"] is None
    assert unknown["events_rotation_tier_allowed"] is False


def test_tier_env_windows_are_honoured(monkeypatch, tmp_path: Path):
    _no_handoff(monkeypatch)
    monkeypatch.setenv("POK_DISK_TIGHT_FREE_GB", "8")
    window = disk_hygiene.janitor_safe_window(
        tmp_path, free_bytes=int(7.0 * GIB)
    )
    assert window["tier"] == "tight"


def test_ab_and_b_dominate_tier(monkeypatch, tmp_path: Path):
    # Even at critical pressure, a closed A/B rule keeps the window shut.
    _handoff(monkeypatch, "pending")
    _write_checkpoint(tmp_path, {"next_v": 5, "stage": "workers_done"})
    window = disk_hygiene.janitor_safe_window(
        tmp_path, free_bytes=int(1 * GIB)
    )
    assert window["open"] is False
    assert window["tier"] == "critical"
