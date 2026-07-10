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
