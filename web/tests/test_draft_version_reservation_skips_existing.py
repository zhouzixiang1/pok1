"""Regression test: reserve_draft_version must skip versions whose canonical
BOTS_DIR dir already exists, preventing the DRAFT_PREIMAGE_CLEARED busy-spin
(v173 collision, 2026-08-13)."""
import pytest


def test_bump_past_existing_bot_dirs_skips_materialized(tmp_path, monkeypatch):
    import producer_consumer_slice2b as p
    from bot_namespace import bot_name
    import evolution_infra
    # Two existing canonical dirs: v200, v201. A reservation starting at 200
    # must bump to 202.
    monkeys = {"BOTS_DIR": tmp_path}
    for k, v in monkeys.items():
        monkeypatch.setattr(evolution_infra, k, v)
    (tmp_path / bot_name(200)).mkdir()
    (tmp_path / bot_name(201)).mkdir()
    assert p.CandidateLifecycle._bump_past_existing_bot_dirs(200) == 202
    assert p.CandidateLifecycle._bump_past_existing_bot_dirs(199) == 199  # 199 absent
    assert p.CandidateLifecycle._bump_past_existing_bot_dirs(202) == 202


def test_bump_no_existing_returns_unchanged(tmp_path, monkeypatch):
    import producer_consumer_slice2b as p
    import evolution_infra
    monkeypatch.setattr(evolution_infra, "BOTS_DIR", tmp_path)
    assert p.CandidateLifecycle._bump_past_existing_bot_dirs(999) == 999
