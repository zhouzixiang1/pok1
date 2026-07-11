from __future__ import annotations

from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import run_opponent_multitask_v4_scaling as scaling  # noqa: E402
from opponent_multitask_model_v4 import MODEL_FORMAT  # noqa: E402
from train_opponent_multitask_v4 import REPORT_SCHEMA  # noqa: E402


def _row(
    scale: str,
    encoder: str,
    seed: int,
    key: list[float],
    *,
    complete_source: bool = True,
    device: str = "cuda",
) -> dict:
    return {
        "scale": scale,
        "encoder": encoder,
        "seed": seed,
        "completed": True,
        "selection_key": key,
        "parameters": 100 if encoder == "gru" else 200,
        "source_collection_complete": complete_source,
        "source_completed_passes": 160 if complete_source else 159,
        "source_requested_passes": 160,
        "training_device": device,
    }


def test_scaling_aggregates_every_v4_selection_key_component() -> None:
    seeds = [101, 211, 307]
    rows = [
        *[
            _row("small", "gru", seed, [0.20, 0.01, 0.01, 0.01])
            for seed in seeds
        ],
        *[
            _row("small", "deep_set", seed, [0.10, 0.99, 0.99, 0.99])
            for seed in seeds
        ],
    ]

    configurations, selected = scaling.summarize_runs(
        rows, required_seeds=seeds
    )

    assert selected is not None
    assert selected["encoder"] == "deep_set"
    assert selected["median_selection_key"] == [0.10, 0.99, 0.99, 0.99]
    assert all(
        len(row["median_selection_key"]) == len(scaling.SELECTION_KEY_ORDER)
        for row in configurations
    )


def test_formal_scaling_requires_three_seeds_two_configs_and_complete_source() -> None:
    seeds = [101, 211, 307]
    rows = [
        *[_row(scale, encoder, seed, [0.1] * 4)
          for scale in ("small", "medium")
          for encoder in ("gru", "deep_set")
          for seed in seeds],
    ]
    configurations, selected = scaling.summarize_runs(
        rows, required_seeds=seeds
    )

    assert scaling.formal_selection_allowed(
        rows,
        configurations,
        selected,
        allow_incomplete_smoke=False,
    )
    assert not scaling.formal_selection_allowed(
        rows,
        [row for row in configurations if row["scale"] == "small"],
        selected,
        allow_incomplete_smoke=False,
    )
    rows[0]["source_collection_complete"] = False
    assert not scaling.formal_selection_allowed(
        rows,
        configurations,
        selected,
        allow_incomplete_smoke=False,
    )
    assert not scaling.formal_selection_allowed(
        rows,
        configurations,
        selected,
        allow_incomplete_smoke=True,
    )


def test_formal_scaling_rejects_cpu_training_rows() -> None:
    seeds = [101, 211, 307]
    rows = [
        _row(scale, encoder, seed, [0.1] * 4)
        for scale in ("small", "medium")
        for encoder in ("gru", "deep_set")
        for seed in seeds
    ]
    configurations, selected = scaling.summarize_runs(
        rows, required_seeds=seeds
    )
    rows[0]["training_device"] = "cpu"

    assert not scaling.formal_selection_allowed(
        rows,
        configurations,
        selected,
        allow_incomplete_smoke=False,
    )
    rows[0]["training_device"] = "cuda:0"
    assert scaling.formal_selection_allowed(
        rows,
        configurations,
        selected,
        allow_incomplete_smoke=False,
    )


def test_training_report_device_must_match_requested_device() -> None:
    report = {
        "schema": REPORT_SCHEMA,
        "run_id": "run-1",
        "opened_roles": ["train", "early_stop"],
        "model_calibration_opened": False,
        "policy_roles_opened": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "native_tcp_evaluated": False,
        "model": {
            "format": MODEL_FORMAT,
            "scale": "small",
            "cross_encoder": "gru",
        },
        "config": {"seed": 101},
        "environment": {"device": "cuda"},
        "early_stop": {
            "selection_key": [0.1, 0.2, 0.3, 0.4],
            "selection_key_order": list(scaling.SELECTION_KEY_ORDER),
            "selection_key_is_lexicographic": True,
            "selection_score_is_strength_evidence": False,
        },
    }

    assert scaling.validate_training_report(
        report,
        scale="small",
        encoder="gru",
        seed=101,
        run_id="run-1",
        device="cuda",
    ) == [0.1, 0.2, 0.3, 0.4]
    with pytest.raises(ValueError, match="role contract"):
        scaling.validate_training_report(
            report,
            scale="small",
            encoder="gru",
            seed=101,
            run_id="run-1",
            device="cpu",
        )
