from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


torch = pytest.importorskip("torch")
ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "bots" / "neural_national_lab" / "tools"
sys.path.insert(0, str(TOOLS))

import opp_value_runtime as runtime  # noqa: E402
import train_opponent_value_net as trainer  # noqa: E402


def test_pure_python_runtime_matches_torch_for_short_histories() -> None:
    torch.manual_seed(17)
    model = trainer.OpponentAwareValueNet(
        48,
        12,
        gru_hidden=4,
        hidden=8,
        dropout=0.0,
    )
    model.eval()
    payload = {
        "meta": {
            "format": "opp_value_gru_v1",
            "task": "classification",
            "state_dim": 48,
            "profile_dim": 12,
            "gru_hidden": 4,
            "hidden": 8,
            "max_hist": 16,
            "cross_hand_dim": 20,
        },
        "weights": {key: value.detach().tolist() for key, value in model.state_dict().items()},
    }
    pure = runtime.OppValueGRURuntime(payload)

    for history_length in (0, 1, 3, 8, 15, 16):
        sample = {
            "state": [0.01] * 48,
            "profile": [0.02] * 12,
            "cross_hand": [0.03] * 20,
            "history": [[0.001 * (step + 1)] * 15 for step in range(history_length)],
            "target": [0.0] * 6,
            "rule_id": 1,
            "candidate_id": None,
        }
        state, profile, cross, history, lengths, *_ = trainer.collate(
            [sample], max_hist=16, device="cpu"
        )
        with torch.no_grad():
            expected = torch.sigmoid(
                model(state, profile, history, lengths, cross)
            )[0].tolist()
        actual = pure.predict(
            sample["state"], sample["profile"], sample["history"], sample["cross_hand"]
        )

        assert max(abs(left - right) for left, right in zip(expected, actual)) < 1e-5


def test_v149_reproduces_legacy_padded_training_graph() -> None:
    version = (
        ROOT / "bots" / "neural_national_lab" / "versions"
        / "v149_national_v148_gru_trainaligned_tcp"
    )
    spec = importlib.util.spec_from_file_location(
        "v149_opp_value_runtime", version / "opp_value_runtime.py"
    )
    assert spec is not None and spec.loader is not None
    version_runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(version_runtime)
    payload = json.loads(
        (version / "opp_value_gru_cls_crosshand_h96_seed8001.json").read_text(
            encoding="utf-8"
        )
    )
    meta = payload["meta"]
    model = trainer.OpponentAwareValueNet(
        meta["state_dim"],
        meta["profile_dim"],
        gru_hidden=meta["gru_hidden"],
        hidden=meta["hidden"],
        dropout=0.0,
    )
    model.load_state_dict({
        key: torch.tensor(value, dtype=torch.float32)
        for key, value in payload["weights"].items()
    })
    model.eval()
    pure = version_runtime.OppValueGRURuntime(payload)

    for history_length in (1, 3, 8, 15, 16):
        observed = [[0.002 * (step + 1)] * 15 for step in range(history_length)]
        padded = observed + [[0.0] * 15 for _ in range(16 - history_length)]
        state = torch.tensor([[0.01] * meta["state_dim"]])
        profile = torch.tensor([[0.02] * meta["profile_dim"]])
        cross = torch.tensor([[0.03] * 20])
        history = torch.tensor([padded])
        # The legacy trainer treated every non-empty row as all 16 GRU steps.
        lengths = torch.tensor([16])
        with torch.no_grad():
            expected = torch.sigmoid(
                model(state, profile, history, lengths, cross)
            )[0].tolist()
        actual = pure.predict(
            [0.01] * meta["state_dim"],
            [0.02] * meta["profile_dim"],
            observed,
            [0.03] * 20,
        )

        assert max(abs(left - right) for left, right in zip(expected, actual)) < 1e-5
