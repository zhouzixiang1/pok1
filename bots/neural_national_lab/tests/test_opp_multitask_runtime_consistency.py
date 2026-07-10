from __future__ import annotations

import sys
from pathlib import Path

import pytest


torch = pytest.importorskip("torch")
ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "bots" / "neural_national_lab" / "tools"
sys.path.insert(0, str(TOOLS))

import opp_multitask_runtime as runtime  # noqa: E402
import opp_multitask_ensemble_runtime as ensemble_runtime  # noqa: E402
import train_opponent_multitask_net as trainer  # noqa: E402
from train_opponent_value_net import collate  # noqa: E402


def test_response_samples_mask_unobservable_private_cards() -> None:
    row = {
        "opponent": "national_v1",
        "request": {
            "num_players": 2,
            "dealer_id": 0,
            "my_id": 0,
            "my_chips": 19950,
            "my_cards": [48, 49],
            "public_cards": [],
            "history": [],
            "hand": 0,
            "max_hand": 70,
            "opponent_profile": {},
        },
        "state": {"pot": 150, "to_call": 50},
        "hero_action": 300,
        "hero_action_label_id": 3,
        "opponent_action_label_id": 0,
        "opponent_action_pot_ratio": 0.0,
        "delta_vs_rule": [None, 0.0, None, 100.0, None, None],
        "tail_delta_vs_rule": [None, 0.0, None, 0.0, None, None],
        "match_delta_vs_rule": [None, 0.0, None, 100.0, None, None],
        "target_mask": [0, 1, 0, 1, 0, 0],
    }

    value = trainer.build_value_sample(row, max_hist=16)
    response = trainer.build_behavior_sample(row, max_hist=16)

    assert response is not None
    assert any(value["state"][index] != 0.0 for index in trainer.PRIVATE_STATE_INDICES)
    assert all(response["state"][index] == 0.0 for index in trainer.PRIVATE_STATE_INDICES)


def test_multitask_runtime_matches_torch_outputs() -> None:
    torch.manual_seed(29)
    model = trainer.OpponentAwareMultiTaskNet(
        48,
        12,
        gru_hidden=5,
        hidden=16,
        latent=9,
        cross_hidden=7,
        head_hidden=11,
        dropout=0.0,
    )
    model.eval()
    clips = {
        "delta_vs_rule": 5000.0,
        "tail_delta_vs_rule": 2000.0,
        "match_delta_vs_rule": 2000.0,
    }
    payload = {
        "meta": {
            "format": "opp_multitask_gru_v1",
            "labels": list(trainer.LABELS),
            "opponent_action_labels": list(trainer.OPPONENT_ACTION_LABELS),
            "value_fields": list(trainer.VALUE_FIELDS),
            "response_private_state_masked": list(trainer.PRIVATE_STATE_INDICES),
            "rule_action_dim": trainer.RULE_ACTION_DIM,
            "lower_calibration": {
                field: {"offsets": [10.0] * trainer.NUM_ACTIONS}
                for field in trainer.VALUE_FIELDS
            },
            "response_calibration": {"temperature": 2.0},
            "model": {"gru_hidden": 5, "max_hist": 16},
            "training": {"clips": clips},
        },
        "weights": {key: value.detach().tolist() for key, value in model.state_dict().items()},
    }
    pure = runtime.OpponentMultiTaskRuntime(payload)
    hero = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.02, 0.1, 0.0, 1.0]

    for history_length in (0, 1, 6, 15, 16):
        sample = {
            "state": [0.01] * 48,
            "profile": [0.02] * 12,
            "cross_hand": [0.03] * 20,
            "history": [[0.001 * (step + 1)] * 15 for step in range(history_length)],
            "target": [0.0] * 6,
            "rule_id": 1,
            "candidate_id": None,
        }
        state, profile, cross, history, lengths, *_ = collate(
            [sample], max_hist=16, device="cpu"
        )
        with torch.no_grad():
            rule_action = torch.nn.functional.one_hot(
                torch.tensor([1]), num_classes=trainer.NUM_ACTIONS
            ).float()
            latent = model.encode(
                state, profile, history, lengths, cross, rule_action=rule_action
            )
            torch_values = model.value(latent)
            public_state = state.clone()
            public_state[:, list(trainer.PRIVATE_STATE_INDICES)] = 0.0
            response_latent = model.encode(
                public_state, profile, history, lengths, cross, response=True
            )
            torch_response = model.response(
                response_latent, torch.tensor([hero], dtype=torch.float32)
            )[0]

        actual_values = pure.predict_values(
            sample["state"], sample["profile"], sample["history"],
            sample["cross_hand"], 1,
        )
        for field in trainer.VALUE_FIELDS:
            raw = torch_values[field][0].tolist()
            expected_mean = [
                value * clips[field] for value in raw[:trainer.NUM_ACTIONS]
            ]
            expected_lower = [
                value * clips[field] + 10.0
                for value in raw[trainer.NUM_ACTIONS:]
            ]
            expected_mean[1] = 0.0
            expected_lower[1] = 0.0
            expected = expected_mean + expected_lower
            actual = actual_values[field]["mean"] + actual_values[field]["lower"]
            assert max(abs(left - right) for left, right in zip(expected, actual)) < 1e-3

        actual_response = pure.predict_response(
            sample["state"], sample["profile"], sample["history"],
            sample["cross_hand"], hero,
        )
        expected_probabilities = torch.softmax(
            torch_response[:len(trainer.OPPONENT_ACTION_LABELS)] / 2.0, dim=0
        ).tolist()
        actual_probabilities = [
            actual_response["probabilities"][label]
            for label in trainer.OPPONENT_ACTION_LABELS
        ]
        assert max(
            abs(left - right)
            for left, right in zip(expected_probabilities, actual_probabilities)
        ) < 1e-6
        expected_ratio = 4.0 * torch.sigmoid(torch_response[-1]).item()
        assert actual_response["raise_pot_ratio"] == pytest.approx(expected_ratio, abs=1e-6)


def test_ensemble_uses_member_and_seed_uncertainty() -> None:
    class Stub:
        labels = list(trainer.LABELS)
        response_labels = list(trainer.OPPONENT_ACTION_LABELS)
        value_fields = list(trainer.VALUE_FIELDS)
        max_hist = 16

        def __init__(self, offset: float) -> None:
            self.offset = offset

        def predict_values(self, *_args):
            return {
                field: {
                    "mean": [self.offset + index for index in range(6)],
                    "lower": [self.offset + index - 2.0 for index in range(6)],
                }
                for field in self.value_fields
            }

        def predict_response(self, *_args):
            return {
                "probabilities": {
                    label: (0.6 if label == "fold" else 0.1)
                    for label in self.response_labels
                },
                "raise_pot_ratio": 1.0 + self.offset,
            }

    ensemble = ensemble_runtime.OpponentMultiTaskEnsemble(
        [Stub(0.0), Stub(2.0)], std_multiplier=1.0
    )

    values = ensemble.predict_values([], [], [], [], 1)
    response = ensemble.predict_response([], [], [], [], [])

    assert values["match_delta_vs_rule"]["mean"][2] == 3.0
    assert values["match_delta_vs_rule"]["std"][2] == 1.0
    assert values["match_delta_vs_rule"]["lower"][2] == 0.0
    assert values["match_delta_vs_rule"]["mean"][1] == 0.0
    assert response["probabilities"]["fold"] == pytest.approx(0.6)
    assert response["raise_pot_ratio"] == 2.0
    assert response["members"] == 2
