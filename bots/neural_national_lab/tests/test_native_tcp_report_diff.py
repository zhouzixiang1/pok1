from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOOL = ROOT / "bots" / "neural_national_lab" / "tools" / "native_tcp_report_diff.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("native_tcp_report_diff", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(opponent: str, match_idx: int, net: int, *, seed: int = 10) -> dict:
    return {
        "opponent": opponent,
        "match_idx": match_idx,
        "deck_seed_base": seed,
        "bot_seed_base": 1000 + match_idx,
        "hands_played": 4,
        "leg": "paired",
        "net_chips": net,
        "hand_net_chips": [net // 2, net - net // 2],
        "passed_compliance": True,
        "issues": [],
    }


def test_diff_validates_pair_metadata_and_emits_deterministic_ci() -> None:
    tool = _load_tool()
    candidate = {"rows": [_row("a", 0, 40), _row("b", 0, -10)]}
    baseline = {"rows": [_row("a", 0, 0), _row("b", 0, 0)]}

    rows = tool._diff_rows(candidate, baseline)
    first = tool._summary(
        rows,
        candidate,
        baseline,
        2,
        bootstrap_samples=500,
        bootstrap_seed=7,
    )
    second = tool._summary(
        rows,
        candidate,
        baseline,
        2,
        bootstrap_samples=500,
        bootstrap_seed=7,
    )

    assert first["combined"]["sum"] == 30
    assert first["combined"]["delta_per_hand"] == 3.75
    assert first["combined"]["bootstrap_mean_paired_chips"] == second["combined"]["bootstrap_mean_paired_chips"]
    assert first["combined"]["stratified_bootstrap_mean_paired_chips"]["groups"] == 2


def test_diff_rejects_seed_mismatch() -> None:
    tool = _load_tool()
    candidate = {"rows": [_row("a", 0, 10, seed=10)]}
    baseline = {"rows": [_row("a", 0, 0, seed=11)]}

    with pytest.raises(SystemExit, match="deck_seed_base"):
        tool._diff_rows(candidate, baseline)


def _strength_row(opponent: str, match_idx: int, net: int, seed: int) -> dict:
    row = _row(opponent, match_idx, net, seed=seed)
    row.update({
        "hands_played": 140,
        "hand_net_chips": [0] * 69 + [net],
        "wrapper_used": False,
        "candidate_illegal": 0,
        "candidate_timeouts": 0,
        "opponent_illegal": 0,
        "opponent_timeouts": 0,
        "adapter_actions_candidate": 0,
        "adapter_actions_opponent": 0,
    })
    return row


def _strength_report(rows: list[dict], candidate: str) -> dict:
    seeds = [row["deck_seed_base"] for row in rows]
    return {
        "format": "native_tcp_evaluation_v2",
        "execution_mode": "native_tcp",
        "candidate_path": candidate,
        "opponent_paths": ["opponent"],
        "hands_per_match": 70,
        "seeds": seeds,
        "actual_deck_seed_bases": seeds,
        "deck_seed_scheme": "opponent_disjoint_match_blocks_v1",
        "opponent_seed_stride": 10_000_000,
        "bot_seed_base": 2_000,
        "bot_seed_stride": 10,
        "paired": True,
        "requires_native_opponents": True,
        "legacy_debug_wrapper_enabled": False,
        "wrapper_used": False,
        "execution_artifacts": {
            "candidate": {
                "path": candidate,
                "sha256_before": "a" * 64,
                "sha256_after": "a" * 64,
                "stable": True,
            },
            "opponents": [{
                "path": "opponent",
                "sha256_before": "b" * 64,
                "sha256_after": "b" * 64,
                "stable": True,
            }],
        },
        "strength_evidence": {"passed": True},
        "rows": rows,
    }


def test_strength_diff_accepts_independent_complete_reports() -> None:
    tool = _load_tool()
    candidate_rows = [
        _strength_row("a", index, net, seed)
        for index, (net, seed) in enumerate(((100, 1_000), (200, 1_080), (300, 1_160)))
    ]
    baseline_rows = [
        _strength_row("a", index, 0, seed)
        for index, seed in enumerate((1_000, 1_080, 1_160))
    ]

    rows = tool._diff_rows(
        _strength_report(candidate_rows, "candidate"),
        _strength_report(baseline_rows, "baseline"),
        require_strength=True,
    )
    summary = tool._summary(
        rows,
        _strength_report(candidate_rows, "candidate"),
        _strength_report(baseline_rows, "baseline"),
        2,
        bootstrap_samples=100,
        bootstrap_seed=7,
    )

    assert len(rows) == 3
    assert summary["combined"]["leave_one_block_out"] == {
        "blocks": 3,
        "estimates": 3,
        "min_delta_per_hand": 1.071429,
        "max_delta_per_hand": 1.785714,
        "negative_estimates": 0,
        "sign_flips": 0,
    }


def test_strength_diff_rejects_overlapping_deck_windows() -> None:
    tool = _load_tool()
    rows = [
        _strength_row("a", index, 0, seed)
        for index, seed in enumerate((7_000, 7_001, 7_002))
    ]
    report = _strength_report(rows, "candidate")

    with pytest.raises(SystemExit, match="overlapping_deck_windows"):
        tool._diff_rows(report, report, require_strength=True)
