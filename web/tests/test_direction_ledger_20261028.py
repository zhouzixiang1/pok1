"""Durable direction ledger regression (2026-10-28 audit fix).

The tail scan of master_io.txt recorded the WRONG selected symbol on 12/12
surviving generations (the log tail ends in the per-proposal compilation
contract block), so the audit's mechanical repetitive veto never fired and
generations burned symbols already exhausted in reaped v-trees. Fix: the
sealed proposal_binding.change_symbol is recorded durably at plan acceptance
into direction_ledger.jsonl, and recent_change_symbols prefers the ledger,
falling back to the tail scan only for pre-ledger versions.
"""

from __future__ import annotations

import json
from pathlib import Path

import recent_directions


def _mk_tree(results: Path, version: int, tail_symbols: "list[str]") -> None:
    d = results / f"v{version}" / "logs"
    d.mkdir(parents=True)
    body = "\n".join(json.dumps({"change_symbol": s}) for s in tail_symbols)
    (d / "master_io.txt").write_text(body, encoding="utf-8")


def test_record_and_load_roundtrip(tmp_path):
    ok = recent_directions.record_planned_direction(
        640,
        632,
        {"change_symbol": "policy.py:_selected_one", "selected_proposal_id": "p2"},
        results_dir=tmp_path,
    )
    assert ok is True
    rows = recent_directions._load_ledger_rows(tmp_path)
    assert rows == {640: "policy.py:_selected_one"}


def test_record_rejects_missing_or_malformed_binding(tmp_path):
    assert recent_directions.record_planned_direction(641, None, None, results_dir=tmp_path) is False
    assert recent_directions.record_planned_direction(
        641, None, {"change_symbol": "policy.py:not-an-ident!"}, results_dir=tmp_path
    ) is False
    assert not (tmp_path / "direction_ledger.jsonl").exists()


def test_reallocated_version_rewrites_its_row(tmp_path):
    recent_directions.record_planned_direction(
        642, 1, {"change_symbol": "policy.py:_first"}, results_dir=tmp_path
    )
    recent_directions.record_planned_direction(
        642, 1, {"change_symbol": "policy.py:_second"}, results_dir=tmp_path
    )
    rows = recent_directions._load_ledger_rows(tmp_path)
    assert rows == {642: "policy.py:_second"}


def test_ledger_wins_over_the_wrong_tail_scan(tmp_path):
    # Simulate the production corruption: tree tail says ensemble proposal #3
    # (wrong), sealed ledger says the actual selected plan.
    _mk_tree(tmp_path, 650, ["policy.py:_proposal_a", "policy.py:_proposal_b", "policy.py:_wrong_tail"])
    recent_directions.record_planned_direction(
        650, 1, {"change_symbol": "policy.py:_sealed_selected"}, results_dir=tmp_path
    )
    rows = recent_directions.recent_change_symbols(12, results_dir=tmp_path)
    assert rows == [(650, "policy.py:_sealed_selected")]


def test_tail_scan_fallback_only_for_pre_ledger_versions(tmp_path):
    _mk_tree(tmp_path, 651, ["policy.py:_legacy_tail"])
    _mk_tree(tmp_path, 652, ["policy.py:_other_tail"])
    recent_directions.record_planned_direction(
        652, 1, {"change_symbol": "policy.py:_sealed"}, results_dir=tmp_path
    )
    rows = recent_directions.recent_change_symbols(12, results_dir=tmp_path)
    assert dict(rows) == {652: "policy.py:_sealed", 651: "policy.py:_legacy_tail"}
    assert rows[0][0] == 652  # newest-first preserved


def test_exclude_version_drops_own_generation_from_counts(tmp_path):
    recent_directions.record_planned_direction(
        653, 1, {"change_symbol": "policy.py:_self"}, results_dir=tmp_path
    )
    counts = recent_directions.recent_symbol_counts(
        6, results_dir=tmp_path, exclude_version=653
    )
    assert counts == {}


def test_repetitive_veto_data_flows_through_counts(tmp_path):
    # Two prior sealed attempts + a third planning generation excluded.
    recent_directions.record_planned_direction(654, 1, {"change_symbol": "policy.py:_same"}, results_dir=tmp_path)
    recent_directions.record_planned_direction(655, 1, {"change_symbol": "policy.py:_same"}, results_dir=tmp_path)
    recent_directions.record_planned_direction(656, 1, {"change_symbol": "policy.py:_other"}, results_dir=tmp_path)
    counts = recent_directions.recent_symbol_counts(6, results_dir=tmp_path, exclude_version=657)
    assert counts["policy.py:_same"] == 2  # >= 2 of last 6 => repetitive override


def test_corrupt_ledger_lines_are_skipped_not_fatal(tmp_path):
    ledger = tmp_path / "direction_ledger.jsonl"
    ledger.write_text(
        "not json\n"
        + json.dumps({"version": "x", "change_symbol": "policy.py:_a"}) + "\n"
        + json.dumps({"version": 660, "change_symbol": "policy.py:_good"}) + "\n",
        encoding="utf-8",
    )
    rows = recent_directions.recent_change_symbols(12, results_dir=tmp_path)
    assert rows == [(660, "policy.py:_good")]
