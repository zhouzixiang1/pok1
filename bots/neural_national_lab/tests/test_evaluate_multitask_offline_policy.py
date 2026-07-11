from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


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
    assert no_response["override_clusters"] == 1
    assert no_response["override_opponents"] == 1
    assert no_response["by_opponent"]["national_v1"]["overrides"] == 1
    assert no_response["override_trace"] == [{
        "source_row_index": 0,
        "opponent": "national_v1",
        "cluster": "row:0",
        "decision": {},
        "rule_id": 1,
        "candidate": {"label_id": 2, "label": None, "action": None},
        "prediction": {
            "value_key": "lower",
            "hand": 100.0,
            "tail": 100.0,
            "match": 100.0,
            "hand_weight": 1.0,
            "tail_weight": 0.0,
            "match_weight": 0.0,
            "response_signal": 0.0,
            "policy_score": 100.0,
        },
        "observed": {
            "hand_delta": 5.0,
            "tail_delta": 5.0,
            "match_delta": 10.0,
        },
    }]


def test_offline_policy_applies_per_decision_hand_lcb_floor() -> None:
    tool = _load_tool()
    values = {
        "delta_vs_rule": {
            "lower": [0.0, 0.0, -10.0, 20.0, 0.0, 0.0],
            "mean": [0.0, 0.0, 100.0, 80.0, 0.0, 0.0],
        },
        "tail_delta_vs_rule": {
            "lower": [0.0] * 6,
            "mean": [0.0] * 6,
        },
        "match_delta_vs_rule": {
            "lower": [0.0, 0.0, 200.0, 100.0, 0.0, 0.0],
            "mean": [0.0, 0.0, 200.0, 100.0, 0.0, 0.0],
        },
    }
    rows = [{
        "opponent": "national_v1",
        "rule_id": 1,
        "values": values,
        "candidates": [
            {"label_id": 2, "response_signal": 0.0, "hand_delta": -100.0,
             "tail_delta": 0.0, "match_delta": 200.0},
            {"label_id": 3, "response_signal": 0.0, "hand_delta": 20.0,
             "tail_delta": 0.0, "match_delta": 100.0},
        ],
    }]

    result = tool._evaluate_config(
        rows,
        {"margin": 0.0, "hand_weight": 0.25, "match_weight": 0.75,
         "response_weight": 0.0, "use_lower": True, "min_hand_lcb": 0.0},
        bootstrap_samples=10,
        bootstrap_seed=1,
    )

    assert result["match_total"] == 100.0
    assert result["override_trace"][0]["candidate"]["label_id"] == 3


def test_policy_eligibility_requires_override_cluster_coverage() -> None:
    tool = _load_tool()
    result = {
        "overrides": 4,
        "match_clusters": 4,
        "override_clusters": 1,
        "override_hand_mean": 100.0,
        "match_cluster_bootstrap_mean_ci": {"lower": 10.0},
        "match_opponent_stratified_cluster_ci": {"lower": 10.0},
        "by_opponent": {
            "national_v1": {"overrides": 4, "mean": 100.0},
        },
    }

    errors = tool._selection_eligibility(
        result,
        min_overrides=3,
        min_selection_clusters=3,
        min_override_clusters=2,
        min_overrides_per_opponent=2,
        min_override_hand_mean=0.0,
        require_nonnegative_opponent_mean=True,
    )

    assert errors == ["override_clusters<2"]


def test_calibration_gate_rejects_zero_override_evidence() -> None:
    tool = _load_tool()
    result = {
        "overrides": 0,
        "override_clusters": 0,
        "match_cluster_bootstrap_mean_ci": {"lower": 0.0},
        "match_opponent_stratified_cluster_ci": {"lower": 0.0},
        "by_opponent": {"national_v1": {"mean": 0.0}},
    }

    gate = tool._calibration_gate(
        result,
        min_overrides=2,
        min_override_clusters=2,
        require_nonnegative_opponent_mean=True,
    )

    assert gate == {
        "passed": False,
        "errors": [
            "overrides<2",
            "override_clusters<2",
            "cluster_ci_lower<=0.0",
            "opponent_stratified_cluster_ci_lower<=0.0",
        ],
    }


def test_calibration_gate_requires_both_cluster_confidence_bounds() -> None:
    tool = _load_tool()
    result = {
        "overrides": 4,
        "override_clusters": 4,
        "match_cluster_bootstrap_mean_ci": {"lower": -1.0},
        "match_opponent_stratified_cluster_ci": {"lower": 10.0},
        "by_opponent": {"national_v1": {"mean": 10.0}},
    }

    gate = tool._calibration_gate(
        result,
        min_overrides=2,
        min_override_clusters=2,
        require_nonnegative_opponent_mean=True,
    )

    assert gate == {
        "passed": False,
        "errors": ["cluster_ci_lower<=0.0"],
    }


def test_unopened_policy_gate_manifest_does_not_touch_file(
    tmp_path: Path,
) -> None:
    tool = _load_tool()
    missing = tmp_path / "must_remain_blind.jsonl"

    manifest = tool._unopened_file_manifest(
        missing, role="policy_gate_not_final_blind"
    )

    assert manifest == {
        "path": str(missing.resolve()),
        "role": "policy_gate_not_final_blind",
        "opened": False,
        "bytes": None,
        "sha256": None,
    }
    assert tool._may_open_policy_gate(missing, None) is False
    assert tool._may_open_policy_gate(
        missing, {"passed": False, "errors": ["failed"]}
    ) is False
    assert tool._may_open_policy_gate(
        missing, {"passed": True, "errors": []}
    ) is True


def test_main_does_not_read_or_hash_policy_gate_after_calibration_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tool = _load_tool()
    model = tmp_path / "model.json"
    selection = tmp_path / "selection.jsonl"
    calibration = tmp_path / "calibration.jsonl"
    policy_gate = tmp_path / "policy_gate_must_stay_missing.jsonl"
    output = tmp_path / "report.json"
    for path in (model, selection, calibration):
        path.write_text("{}\n", encoding="utf-8")
    reads = []

    monkeypatch.setattr(
        tool.OpponentMultiTaskEnsemble,
        "load",
        staticmethod(lambda _paths: object()),
    )

    def read_rows(path: Path):
        reads.append(path)
        return []

    monkeypatch.setattr(tool, "_read", read_rows)
    monkeypatch.setattr(
        tool,
        "select_offline_policy",
        lambda *_args, **_kwargs: {
            "grid": [],
            "selected": {"config": {
                "response_weight": 0.0,
                "use_lower": True,
            }},
            "selection_failure": None,
        },
    )
    monkeypatch.setattr(
        tool,
        "_evaluate_config",
        lambda *_args, **_kwargs: {
            "overrides": 0,
            "override_clusters": 0,
            "match_cluster_bootstrap_mean_ci": {"lower": 0.0},
            "match_opponent_stratified_cluster_ci": {"lower": 0.0},
            "by_opponent": {},
        },
    )
    monkeypatch.setattr(sys, "argv", [
        "evaluate_multitask_offline_policy.py",
        "--model", str(model),
        "--selection-data", str(selection),
        "--calibration-data", str(calibration),
        "--held-out-data", str(policy_gate),
        "--output", str(output),
        "--bootstrap-samples", "2",
    ])

    assert tool.main() == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert reads == [selection, calibration]
    assert payload["held_out_opened"] is False
    assert payload["held_out"] is None
    assert payload["data_manifests"]["held_out"] == {
        "path": str(policy_gate.resolve()),
        "role": "policy_gate_not_final_blind",
        "opened": False,
        "bytes": None,
        "sha256": None,
    }


def test_policy_selection_rejects_negative_cluster_confidence_bound() -> None:
    tool = _load_tool()
    values = {
        field: {
            "lower": [0.0, 0.0, 100.0, 0.0, 0.0, 0.0],
            "mean": [0.0, 0.0, 100.0, 0.0, 0.0, 0.0],
        }
        for field in ("delta_vs_rule", "tail_delta_vs_rule", "match_delta_vs_rule")
    }
    rows = []
    for index, delta in enumerate((100.0, 100.0, 100.0, -250.0)):
        rows.append({
            "opponent": f"national_v{index % 2 + 1}",
            "cluster": f"match-{index}",
            "rule_id": 1,
            "values": values,
            "candidates": [{
                "label_id": 2,
                "label": "raise_small",
                "response_signal": 0.0,
                "hand_delta": 100.0,
                "tail_delta": delta,
                "match_delta": delta,
            }],
        })

    selection = tool.select_offline_policy(
        rows,
        margins=[0.0],
        hand_weights=[1.0],
        response_weights=[0.0],
        min_overrides=4,
        min_selection_clusters=4,
        min_override_clusters=4,
        min_overrides_per_opponent=2,
        min_override_hand_mean=0.0,
        require_nonnegative_opponent_mean=False,
        bootstrap_samples=200,
        bootstrap_seed=7,
        min_cluster_ci_lower=0.0,
        min_opponent_stratified_ci_lower=0.0,
    )

    assert selection["selected"] is None
    assert selection["selection_failure"] is not None
    errors = selection["grid"][0]["eligibility_errors"]
    assert "cluster_ci_lower<=0.0" in errors


def test_policy_grid_reserves_weight_for_match_value() -> None:
    tool = _load_tool()

    selection = tool.select_offline_policy(
        [],
        margins=[0.0],
        hand_weights=[0.5, 0.75, 1.0],
        tail_weights=[0.0, 0.25],
        min_match_weight=0.25,
        response_weights=[0.0],
        min_overrides=0,
        min_selection_clusters=0,
        min_override_clusters=0,
        min_overrides_per_opponent=0,
        min_override_hand_mean=0.0,
        require_nonnegative_opponent_mean=True,
        bootstrap_samples=10,
        bootstrap_seed=1,
        min_cluster_ci_lower=-1.0,
        min_opponent_stratified_ci_lower=-1.0,
    )

    weights = {
        (
            row["config"]["hand_weight"],
            row["config"]["tail_weight"],
            row["config"]["match_weight"],
        )
        for row in selection["grid"]
    }
    assert weights == {
        (0.5, 0.0, 0.5),
        (0.5, 0.25, 0.25),
        (0.75, 0.0, 0.25),
    }


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
