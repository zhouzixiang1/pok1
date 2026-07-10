from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pytest
import torch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import opp_catastrophe_ensemble_runtime as ensemble_runtime  # noqa: E402
import opp_catastrophe_runtime as runtime  # noqa: E402
import evaluate_catastrophe_policy_ab as policy_ab  # noqa: E402
import train_catastrophe_risk_head as trainer  # noqa: E402


def _payload(
    head: trainer.CatastropheRiskHead,
    *,
    base_sha256: str = "abc",
    scale: float = 0.5,
    bias: float = -0.2,
) -> dict:
    return {
        "meta": {
            "format": "opp_catastrophe_head_v1",
            "labels": [
                "fold", "call", "raise_half", "raise_pot", "raise_2pot", "allin"
            ],
            "latent_dim": 3,
            "base_model": {"sha256": base_sha256},
            "model": {"hidden": 4},
            "risk": {
                "catastrophe_threshold": 5000.0,
                "severity_clip": 100.0,
                "calibration": {"scale": scale, "bias": bias},
            },
        },
        "weights": {
            key: tensor.detach().tolist()
            for key, tensor in head.state_dict().items()
        },
    }


def test_catastrophe_runtime_matches_torch_head() -> None:
    torch.manual_seed(11)
    head = trainer.CatastropheRiskHead(3, 4).eval()
    payload = _payload(head)
    pure = runtime.OpponentCatastropheRuntime(
        payload, expected_base_sha256="abc"
    )
    latent = [0.25, -0.5, 0.75]
    with torch.no_grad():
        raw = head(torch.tensor([latent], dtype=torch.float32))[0]
    expected_probability = torch.sigmoid(0.5 * raw[:6] - 0.2).tolist()
    expected_severity = (100.0 * torch.sigmoid(raw[6:])).tolist()
    actual = pure.predict(latent, rule_label_id=1)
    for action_id in range(6):
        if action_id == 1:
            assert actual["probability"][action_id] == 0.0
            assert actual["severity"][action_id] == 0.0
            assert actual["expected_loss"][action_id] == 0.0
            continue
        assert actual["probability"][action_id] == pytest.approx(
            expected_probability[action_id], abs=2e-7
        )
        assert actual["severity"][action_id] == pytest.approx(
            expected_severity[action_id], abs=2e-5
        )
        assert actual["expected_loss"][action_id] == pytest.approx(
            expected_probability[action_id] * expected_severity[action_id],
            abs=2e-5,
        )


def test_catastrophe_runtime_rejects_base_hash_mismatch(tmp_path: Path) -> None:
    head = trainer.CatastropheRiskHead(3, 4)
    path = tmp_path / "risk.json"
    path.write_text(json.dumps(_payload(head)), encoding="utf-8")
    assert runtime.OpponentCatastropheRuntime.load(
        path, expected_base_sha256="different"
    ) is None


class _FakeBase:
    labels = ["fold", "call", "raise_half", "raise_pot", "raise_2pot", "allin"]
    meta = {"model": {"latent": 3}}

    def __init__(self, latent: list[float]) -> None:
        self.latent = latent

    def encode(self, *_args, **_kwargs) -> list[float]:
        return list(self.latent)


def test_catastrophe_ensemble_reports_probability_ucb() -> None:
    first = trainer.CatastropheRiskHead(3, 4)
    second = trainer.CatastropheRiskHead(3, 4)
    with torch.no_grad():
        for parameter in first.parameters():
            parameter.zero_()
        for parameter in second.parameters():
            parameter.zero_()
        second.head[2].bias[:6].fill_(2.0)
    first_runtime = runtime.OpponentCatastropheRuntime(_payload(first))
    second_runtime = runtime.OpponentCatastropheRuntime(_payload(second))
    ensemble = ensemble_runtime.OpponentCatastropheEnsemble([
        (_FakeBase([0.0, 0.0, 0.0]), first_runtime),
        (_FakeBase([0.0, 0.0, 0.0]), second_runtime),
    ])
    result = ensemble.predict([], [], [], [], 1, [])
    assert result["members"] == 2
    assert result["probability_upper"][0] > result["probability"][0]
    assert 0.0 <= result["probability_upper"][0] <= 1.0
    assert result["probability"][1] == 0.0


def test_cluster_bootstrap_is_deterministic_and_opponent_stratified() -> None:
    rows = []
    for opponent in ("a", "b"):
        for seed in range(3):
            rows.append({
                "opponent": opponent,
                "deck_seed_base": seed,
                "bot_seed_base": 10 + seed,
                "marker": f"{opponent}-{seed}",
            })
    first, first_report = trainer._cluster_bootstrap(rows, seed=7)
    second, second_report = trainer._cluster_bootstrap(rows, seed=7)
    assert first == second
    assert first_report == second_report
    assert first_report["source_clusters"] == 6
    assert first_report["sampled_draws"] == 6
    assert len(first) == 6
    assert sum(row["opponent"] == "a" for row in first) == 3
    assert sum(row["opponent"] == "b" for row in first) == 3


def test_platt_grid_improves_shifted_logits() -> None:
    logits = np.asarray([-1.0, -0.5, 0.0, 0.5, 1.0], dtype=np.float64)
    labels = np.asarray([0.0, 0.0, 0.0, 1.0, 1.0], dtype=np.float64)
    calibration = trainer._fit_platt(logits, labels)
    assert calibration["nll_after"] < calibration["nll_before"]
    assert calibration["samples"] == 5


def test_runtime_file_hash_matches_sha256(tmp_path: Path) -> None:
    path = tmp_path / "base.json"
    path.write_bytes(b"base-model")
    assert runtime.OpponentCatastropheRuntime.file_sha256(path) == hashlib.sha256(
        b"base-model"
    ).hexdigest()


def test_policy_risk_filter_uses_probability_upper_bound() -> None:
    rows = [{
        "source_row_index": 0,
        "candidates": [
            {
                "label_id": 3,
                "label": "raise_pot",
                "catastrophe_probability_upper": 0.11,
            },
            {
                "label_id": 5,
                "label": "allin",
                "catastrophe_probability_upper": 0.09,
            },
        ],
    }]
    filtered, report = policy_ab._filter_risk(rows, max_probability=0.1)
    assert [row["label"] for row in filtered[0]["candidates"]] == ["allin"]
    assert report["removed_candidates"] == 1
    assert report["removed_by_action"] == {"raise_pot": 1}


def test_policy_trace_records_predicted_and_observed_catastrophe() -> None:
    rows = [{
        "source_row_index": 4,
        "candidates": [{
            "label_id": 5,
            "catastrophe_probability_upper": 0.08,
            "catastrophe_expected_loss_upper": 1200.0,
        }],
    }]
    result = {"override_trace": [{
        "source_row_index": 4,
        "candidate": {"label_id": 5},
        "prediction": {},
        "observed": {"hand_delta": -6000.0},
    }]}
    policy_ab._annotate_selected_risk(
        result, rows, catastrophe_threshold=5000.0
    )
    assert result["catastrophe_risk"]["observed_catastrophes"] == 1
    assert result["catastrophe_risk"]["max_probability_upper"] == 0.08
    assert result["override_trace"][0]["prediction"][
        "catastrophe_expected_loss_upper"
    ] == 1200.0
