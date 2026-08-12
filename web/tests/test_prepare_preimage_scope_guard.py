"""Regression tests for the draft-preimage quarantine scope guard (2026-08-12).

Root cause of the v170 livelock: ``0f6975f4`` added a DRAFT_PREIMAGE_CLEARED
short-circuit in ``tool_gates_prepare.py`` that quarantines ``next_dir`` under
ANY slot override. But ``get_bot_dir`` returns the canonical ``BOTS_DIR`` path
when it already exists — EVEN under a draft/consumer slot override — so a slot
preparing a version the PRIMARY already materialized resolved ``next_dir`` to
the PRIMARY's live dir and ``shutil.move``d it to quarantine, destroying the
primary artifact (ArtifactIntegrityError at direction_audited → livelock).

The fix only quarantines a dir the slot actually owns (under draft_candidates/
or a consumer-candidate tree), never a canonical ``BOTS_DIR`` path. These tests
lock in the two invariants the fix depends on.
"""
import pytest

import evolution_infra


def test_get_bot_dir_returns_canonical_when_it_exists_under_draft_override(monkeypatch, tmp_path):
    """The root cause: a draft override does NOT redirect to draft_candidates
    once the canonical dir exists. So next_dir can be the PRIMARY's dir."""
    bots = tmp_path / "bots"
    results = tmp_path / "results"
    monkeys = {"BOTS_DIR": bots, "RESULTS_DIR": results}
    for k, v in monkeys.items():
        monkeypatch.setattr(evolution_infra, k, v)
    bots.mkdir()
    results.mkdir()
    # Canonical dir already materialized (e.g. by the PRIMARY).
    v = 999
    name = evolution_infra.bot_name(v)
    canonical = bots / name
    canonical.mkdir()
    # Draft slot override active.
    monkeypatch.setattr(evolution_infra, "current_slot_override", lambda: "draft1")
    resolved = evolution_infra.get_bot_dir(v)
    # Under a draft override WITH an existing canonical dir, get_bot_dir returns
    # the canonical path (the primary's), NOT draft_candidates/...
    assert resolved == canonical
    assert not resolved.is_relative_to(results / "draft_candidates")


def test_canonical_path_classified_as_bots_owned_not_slot_scoped(tmp_path):
    """The scope guard's classification: a canonical BOTS_DIR path is
    primary/published-owned (must NOT be quarantined); a draft_candidates path
    is slot-scoped (safe to quarantine)."""
    bots = tmp_path / "bots"
    draft_root = tmp_path / "results" / "draft_candidates"
    bots.mkdir(); (draft_root / "draft1").mkdir(parents=True)
    canonical = bots / "national_cloud_v999"
    slot_scoped = draft_root / "draft1" / "national_cloud_v999"
    # The fix uses Path.is_relative_to(BOTS_DIR) to decide ownership.
    assert canonical.resolve().is_relative_to(bots.resolve())
    assert not slot_scoped.resolve().is_relative_to(bots.resolve())
