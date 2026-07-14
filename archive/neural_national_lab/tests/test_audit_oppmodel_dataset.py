from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
TOOL = ROOT / "bots" / "neural_national_lab" / "tools" / "audit_oppmodel_dataset.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("audit_oppmodel_dataset", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, row: dict) -> None:
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def _value_row(opponent: str, seed: int) -> dict:
    values = [None, 0.0, 100.0, None, None, None]
    mask = [0, 1, 1, 0, 0, 0]
    return {
        "status": "ok",
        "opponent": opponent,
        "deck_seed_base": seed,
        "bot_seed_base": seed + 100,
        "hand": 1,
        "hand_decision_index": 0,
        "cross_hand_sequence_schema": "public_opponent_hand_v1",
        "cross_hand_sequence": [],
        "rule_label_id": 1,
        "delta_vs_rule": values,
        "tail_delta_vs_rule": [None, 0.0, 25.0, None, None, None],
        "match_delta_vs_rule": [None, 0.0, 125.0, None, None, None],
        "target_masks": {
            "delta_vs_rule": mask,
            "tail_delta_vs_rule": mask,
            "match_delta_vs_rule": mask,
        },
        "probes": [{
            "status": "ok",
            "force_confirmed": True,
            "illegal_actions": 0,
            "issues": [],
            "delta_vs_rule": 100.0,
            "tail_delta_vs_rule": 25.0,
            "match_delta_vs_rule": 125.0,
        }],
    }


def _behavior_row(opponent: str, seed: int) -> dict:
    return {
        "source": "baseline_native_action_response",
        "opponent": opponent,
        "deck_seed_base": seed,
        "bot_seed_base": seed + 100,
        "hand": 1,
        "hand_decision_index": 0,
        "cross_hand_sequence_schema": "public_opponent_hand_v1",
        "cross_hand_sequence": [],
        "opponent_action": "fold",
        "opponent_action_label_id": 0,
    }


def test_audit_accepts_disjoint_protocol_clean_splits(tmp_path: Path) -> None:
    tool = _load_tool()
    for index, split in enumerate(tool.SPLITS):
        opponent = f"national_v{index + 1}"
        value = _value_row(opponent, index + 10)
        if split == "held_out":
            value["delta_vs_rule"][2] = 100.0
            value["tail_delta_vs_rule"][2] = 987_654_321.0
            value["match_delta_vs_rule"][2] = 987_654_421.0
            value["probes"][0].update({
                "delta_vs_rule": 100.0,
                "tail_delta_vs_rule": 987_654_321.0,
                "match_delta_vs_rule": 987_654_421.0,
            })
        _write(tmp_path / f"cf_{split}.jsonl", value)
        _write(
            tmp_path / f"opponent_actions_{split}.jsonl",
            _behavior_row(opponent, index + 10),
        )

    report = tool.audit(
        tmp_path,
        min_value_rows=1,
        min_behavior_rows=1,
        require_cross_hand_sequence=True,
    )

    assert report["passed"] is True
    assert report["valid_probes"] == len(tool.SPLITS)
    for split in ("train", "val", "calibration"):
        assert report["long_horizon_target_leverage"][split]["samples"] == 1
    assert report["long_horizon_target_leverage"]["held_out"] == {
        "redacted": True
    }
    assert report["targets"]["held_out"] == {"redacted": True}
    assert "987654321" not in json.dumps(report)


def test_long_horizon_leverage_reports_cluster_concentration() -> None:
    tool = _load_tool()
    probes = [
        {
            "opponent": "national_v1",
            "deck_seed_base": 10,
            "bot_seed_base": 20,
            "hand_delta": 100.0,
            "tail_delta": 12_000.0,
        },
        {
            "opponent": "national_v1",
            "deck_seed_base": 10,
            "bot_seed_base": 20,
            "hand_delta": 500.0,
            "tail_delta": -15_000.0,
        },
        {
            "opponent": "national_v2",
            "deck_seed_base": 30,
            "bot_seed_base": 40,
            "hand_delta": 0.0,
            "tail_delta": 50.0,
        },
    ]

    report = tool._long_horizon_target_leverage(probes)

    assert report["samples"] == 3
    assert report["clusters"] == 2
    assert report["large_tail_probes"] == 2
    assert report["small_hand_large_tail_probes"] == 1
    assert report["clusters_with_large_tail"] == 1
    assert report["largest_clusters"][0]["large_tail_probes"] == 2


def test_audit_rejects_opponent_leakage(tmp_path: Path) -> None:
    tool = _load_tool()
    for index, split in enumerate(tool.SPLITS):
        opponent = "national_v1" if split != "held_out" else "national_v3"
        _write(tmp_path / f"cf_{split}.jsonl", _value_row(opponent, index + 20))
        _write(
            tmp_path / f"opponent_actions_{split}.jsonl",
            _behavior_row(opponent, index + 20),
        )

    report = tool.audit(tmp_path, min_value_rows=1, min_behavior_rows=1)

    assert report["passed"] is False
    assert any("opponent leakage" in error for error in report["errors"])


def test_audit_rejects_future_hand_sequence_leakage(tmp_path: Path) -> None:
    tool = _load_tool()
    for index, split in enumerate(tool.SPLITS):
        opponent = f"national_v{index + 1}"
        value = _value_row(opponent, index + 30)
        behavior = _behavior_row(opponent, index + 30)
        if split == "train":
            value["cross_hand_sequence"] = [[0.0] * 16]
        _write(tmp_path / f"cf_{split}.jsonl", value)
        _write(tmp_path / f"opponent_actions_{split}.jsonl", behavior)

    report = tool.audit(
        tmp_path,
        min_value_rows=1,
        min_behavior_rows=1,
        require_cross_hand_sequence=True,
    )

    assert report["passed"] is False
    assert any("strictly-prior expected" in error for error in report["errors"])
