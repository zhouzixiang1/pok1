from __future__ import annotations

import argparse
import copy
from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import run_opponent_multitask_v3_scaling as scaling  # noqa: E402


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        role_manifest=tmp_path / "roles.json",
        ledger=tmp_path / "ledger.json",
        run_id_prefix="sweep",
        allow_incomplete_smoke=True,
        moe_experts=4,
        dropout=0.1,
        epochs=2,
        patience=1,
        batch_size=16,
        learning_rate=5.0e-4,
        weight_decay=1.0e-5,
        hand_clip=2000.0,
        tail_clip=2000.0,
        match_clip=2000.0,
        device="cpu",
    )


def _report(*, scale: str, encoder: str, seed: int, run_id: str) -> dict:
    return {
        "schema": "opponent_multitask_training_report_v3",
        "run_id": run_id,
        "opened_roles": ["train", "early_stop"],
        "model_calibration_opened": False,
        "policy_roles_opened": False,
        "calibration_payload_sha256": None,
        "calibration_summary": None,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "native_tcp_evaluated": False,
        "model": {"scale": scale, "cross_encoder": encoder},
        "config": {"seed": seed},
        "early_stop": {"selection_score": 1.25},
    }


def test_scaling_command_never_opens_model_calibration(tmp_path: Path) -> None:
    args = _args(tmp_path)

    command = scaling.build_training_command(
        args,
        scale="small",
        encoder="deep_set",
        seed=101,
        output_dir=tmp_path / "out",
        run_id="sweep-small",
    )

    assert "--open-model-calibration" not in command
    assert "--allow-incomplete-smoke" in command
    assert command[command.index("--seed") + 1] == "101"


def test_report_contract_rejects_any_calibration_exposure() -> None:
    report = _report(
        scale="small", encoder="deep_set", seed=101, run_id="run-1"
    )
    scaling.validate_training_report(
        report,
        scale="small",
        encoder="deep_set",
        seed=101,
        run_id="run-1",
    )

    report = copy.deepcopy(report)
    report["opened_roles"].append("model_calibration")
    report["model_calibration_opened"] = True
    with pytest.raises(ValueError, match="role contract"):
        scaling.validate_training_report(
            report,
            scale="small",
            encoder="deep_set",
            seed=101,
            run_id="run-1",
        )


def test_summary_requires_every_seed_and_uses_median_score() -> None:
    rows = [
        {
            "scale": "small",
            "encoder": "deep_set",
            "seed": seed,
            "completed": True,
            "selection_score": score,
            "parameters": 100,
        }
        for seed, score in ((101, 1.0), (211, 1.2), (307, 1.1))
    ]
    rows.extend([
        {
            "scale": "medium",
            "encoder": "gru",
            "seed": 101,
            "completed": True,
            "selection_score": 0.5,
            "parameters": 200,
        },
        {
            "scale": "medium",
            "encoder": "gru",
            "seed": 211,
            "completed": False,
        },
    ])

    configurations, best = scaling.summarize_runs(
        rows, required_seeds=[101, 211, 307]
    )

    small = [row for row in configurations if row["scale"] == "small"][0]
    medium = [row for row in configurations if row["scale"] == "medium"][0]
    assert small["median_selection_score"] == pytest.approx(1.1)
    assert small["all_seeds_completed"] is True
    assert medium["all_seeds_completed"] is False
    assert best["scale"] == "small"
    assert scaling.formal_selection_allowed(
        rows,
        configurations,
        best,
        allow_incomplete_smoke=True,
    ) is False

    complete_rows = rows[:3]
    for row in complete_rows:
        row["source_collection_complete"] = True
    complete_configurations, complete_best = scaling.summarize_runs(
        complete_rows, required_seeds=[101, 211, 307]
    )
    assert scaling.formal_selection_allowed(
        complete_rows,
        complete_configurations,
        complete_best,
        allow_incomplete_smoke=False,
    ) is True


def test_csv_and_seed_parsing_rejects_invalid_grid() -> None:
    assert scaling._csv_values(
        "small,large,small", choices=scaling.SCALES, field="scale"
    ) == ["small", "large"]
    assert scaling._seeds("101,211,101") == [101, 211]
    with pytest.raises(ValueError, match="invalid"):
        scaling._csv_values(
            "transformer", choices=scaling.ENCODERS, field="encoder"
        )
    with pytest.raises(ValueError, match="non-negative"):
        scaling._seeds("-1")
