from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "bots" / "neural_national_lab" / "tools"
sys.path.insert(0, str(TOOLS))

import freeze_oppmodel_dataset as freeze  # noqa: E402


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
            field: mask for field in (
                "delta_vs_rule", "tail_delta_vs_rule", "match_delta_vs_rule"
            )
        },
        "probes": [{
            "status": "ok",
            "forced_label": "call",
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


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def _collection(source: Path, *, requested_passes: int = 1) -> None:
    source.mkdir()
    rows = {
        "train": [("national_v1", 11), ("national_v2", 12)],
        "val": [("national_v3", 13), ("national_v5", 15)],
        "held_out": [("national_v4", 14)],
    }
    for split, opponents in rows.items():
        _write_rows(
            source / f"cf_{split}.jsonl",
            [_value_row(opponent, seed) for opponent, seed in opponents],
        )
        _write_rows(
            source / f"opponent_actions_{split}.jsonl",
            [_behavior_row(opponent, seed) for opponent, seed in opponents],
        )
    (source / "collection_manifest.json").write_text(
        json.dumps({"passes_requested": requested_passes}), encoding="utf-8"
    )
    (source / "pool_snapshots.jsonl").write_text(
        json.dumps({"pass": 1}) + "\n", encoding="utf-8"
    )


def test_freeze_moves_whole_opponents_to_calibration(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "frozen"
    _collection(source)

    manifest = freeze.freeze_dataset(
        source,
        output,
        calibration_opponents={"national_v1"},
        min_value_train=1,
        min_behavior_train=1,
        required_alternative_labels={"call"},
    )

    audit = json.loads((output / "dataset_audit.json").read_text())
    assert audit["passed"] is True
    assert audit["opponents"]["train"] == ["national_v2"]
    assert audit["opponents"]["calibration"] == ["national_v1"]
    assert audit["opponents"]["val"] == ["national_v3", "national_v5"]
    assert audit["opponents"]["held_out"] == ["national_v4"]
    assert manifest["output_files"]["cf_calibration.jsonl"]["rows"] == 1
    calibration_row = json.loads(
        (output / "cf_calibration.jsonl").read_text().strip()
    )
    assert calibration_row["_split"] == "calibration"


def test_freeze_preserves_legacy_validation_partition_mode(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "frozen"
    _collection(source)

    manifest = freeze.freeze_dataset(
        source,
        output,
        calibration_opponents={"national_v5"},
        selection_val_opponents={"national_v3"},
        min_value_train=1,
        min_behavior_train=1,
        required_alternative_labels={"call"},
    )

    audit = json.loads((output / "dataset_audit.json").read_text())
    assert audit["passed"] is True
    assert audit["opponents"]["train"] == ["national_v1", "national_v2"]
    assert audit["opponents"]["val"] == ["national_v3"]
    assert audit["opponents"]["calibration"] == ["national_v5"]
    assert manifest["partition_mode"] == "validation_to_calibration"
    assert manifest["selection_val_opponents"] == ["national_v3"]
    assert manifest["outputs"]["cf_calibration"]["opponents"] == [
        "national_v5"
    ]
    assert "cf_train.jsonl" in manifest["source_files"]


def test_freeze_refuses_incomplete_collection(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _collection(source, requested_passes=2)

    with pytest.raises(RuntimeError, match="collection incomplete"):
        freeze.freeze_dataset(
            source,
            tmp_path / "frozen",
            calibration_opponents={"national_v1"},
            min_value_train=1,
            min_behavior_train=1,
            required_alternative_labels={"call"},
        )
