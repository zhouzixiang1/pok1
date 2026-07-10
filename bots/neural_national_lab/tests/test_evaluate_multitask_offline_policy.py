from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
TOOL = (
    ROOT / "bots" / "neural_national_lab" / "tools"
    / "evaluate_multitask_offline_policy.py"
)


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "evaluate_multitask_offline_policy", TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_offline_policy_uses_lcb_and_response_signal() -> None:
    tool = _load_tool()
    values = {
        field: {
            "lower": [0.0, 0.0, 100.0, 80.0, 0.0, 0.0],
            "mean": [0.0, 0.0, 150.0, 140.0, 0.0, 0.0],
        }
        for field in ("delta_vs_rule", "tail_delta_vs_rule", "match_delta_vs_rule")
    }
    rows = [{
        "opponent": "national_v1",
        "rule_id": 1,
        "values": values,
        "candidates": [
            {"label_id": 2, "response_signal": 0.0, "hand_delta": 5.0,
             "tail_delta": 5.0, "match_delta": 10.0},
            {"label_id": 3, "response_signal": 100.0, "hand_delta": 10.0,
             "tail_delta": 10.0, "match_delta": 20.0},
        ],
    }]

    no_response = tool._evaluate_config(
        rows,
        {"margin": 0.0, "hand_weight": 1.0, "response_weight": 0.0,
         "use_lower": True},
        bootstrap_samples=10,
        bootstrap_seed=1,
    )
    with_response = tool._evaluate_config(
        rows,
        {"margin": 0.0, "hand_weight": 1.0, "response_weight": 0.5,
         "use_lower": True},
        bootstrap_samples=10,
        bootstrap_seed=1,
    )

    assert no_response["match_total"] == 10.0
    assert with_response["match_total"] == 20.0


def test_cluster_bootstrap_resamples_whole_matches() -> None:
    tool = _load_tool()

    ci = tool._cluster_bootstrap_mean_ci(
        {"positive": [100.0, 100.0], "negative": [-100.0, -100.0]},
        samples=200,
        seed=7,
    )

    assert ci["mean"] == 0.0
    assert ci["lower"] == -100.0
    assert ci["upper"] == 100.0
