from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


torch = pytest.importorskip("torch")
ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "bots" / "neural_national_lab" / "tools"
sys.path.insert(0, str(TOOLS))

import opp_multitask_runtime as runtime  # noqa: E402
import opp_multitask_ensemble_runtime as ensemble_runtime  # noqa: E402
import run_multitask_scaling_sweep as scaling  # noqa: E402
import train_opponent_multitask_net as trainer  # noqa: E402


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


def test_pairwise_ranking_loss_prefers_rule_relative_direction() -> None:
    target = torch.tensor([
        [float("nan"), 0.0, 500.0, -500.0, float("nan"), float("nan")]
    ])
    rule_ids = torch.tensor([1])
    lower = torch.zeros(1, trainer.NUM_ACTIONS)
    good_mean = torch.tensor([[0.0, 0.0, 0.5, -0.5, 0.0, 0.0]])
    bad_mean = -good_mean
    kwargs = {
        "margin": 100.0,
        "temperature": 0.1,
        "positive_weight": torch.tensor(1.0),
        "action_weights": torch.ones(trainer.NUM_ACTIONS),
    }

    good, count = trainer._pairwise_ranking_loss(
        torch.cat([good_mean, lower], dim=1), target, rule_ids, **kwargs
    )
    bad, _ = trainer._pairwise_ranking_loss(
        torch.cat([bad_mean, lower], dim=1), target, rule_ids, **kwargs
    )

    assert count == 2
    assert good.item() < bad.item()


@pytest.mark.parametrize(
    "encoder", ["gru", "gru_moe", "deep_set", "transformer"]
)
def test_scaling_config_preserves_temporal_encoder(encoder: str) -> None:
    config = scaling._parse_config(
        f"tiny_{encoder}@{encoder}:64:32:16:16:12:32",
        cross_transformer_heads=2,
        cross_moe_experts=3,
    )

    assert config["cross_sequence_encoder"] == encoder
    assert config["cross_sequence_hidden"] == 12
    assert config["cross_transformer_heads"] == 2
    assert config["cross_moe_experts"] == 3


def test_scaling_config_rejects_incompatible_transformer_heads() -> None:
    with pytest.raises(SystemExit, match="divisible by heads"):
        scaling._parse_config(
            "bad@transformer:64:32:16:16:13:32",
            cross_transformer_heads=2,
        )


def test_scaling_expands_ranking_weight_as_selection_dimension() -> None:
    base = scaling._parse_config(
        "tiny@gru:64:32:16:16:12:32",
        cross_transformer_heads=2,
        cross_moe_experts=3,
    )

    configs, weights = scaling._expand_ranking_weights(
        [base], "0,0.5,1,0.5", default=0.25
    )

    assert weights == [0.0, 0.5, 1.0]
    assert [config["name"] for config in configs] == [
        "tiny_rw0", "tiny_rw0p5", "tiny_rw1"
    ]
    assert [config["match_ranking_weight"] for config in configs] == weights


def test_offline_candidate_gate_rejects_relaxed_or_incomplete_evidence(
    tmp_path: Path,
) -> None:
    requirements = scaling.OFFLINE_CANDIDATE_REQUIREMENTS
    args = SimpleNamespace(
        selection_mode="policy",
        policy_min_overrides=0,
        policy_min_selection_clusters=requirements["selection_clusters"],
        policy_min_override_clusters=requirements[
            "selection_override_clusters"
        ],
        policy_min_overrides_per_opponent=requirements[
            "selection_overrides_per_opponent"
        ],
        policy_min_calibration_overrides=requirements[
            "calibration_overrides"
        ],
        policy_min_calibration_override_clusters=requirements[
            "calibration_override_clusters"
        ],
        policy_min_held_out_overrides=requirements["held_out_overrides"],
        policy_min_held_out_override_clusters=requirements[
            "held_out_override_clusters"
        ],
        policy_bootstrap_samples=requirements["bootstrap_samples"],
        policy_min_override_hand_mean=0.0,
        policy_min_selection_ci_lower=-1.0,
        policy_min_calibration_ci_lower=0.0,
        policy_min_held_out_ci_lower=0.0,
        policy_min_match_weight=requirements["minimum_match_weight"],
        policy_allow_negative_opponent=False,
        allow_missing_cross_hand_sequence=False,
    )
    audit = {
        "value_rows": {"train": requirements["value_train_rows"]},
        "behavior_rows": {"train": requirements["behavior_train_rows"]},
        "opponents": {
            split: [f"{split}-{index}" for index in range(requirements[f"{split}_opponents"])]
            for split in ("train", "val", "calibration", "held_out")
        },
    }
    (tmp_path / "freeze_manifest.json").write_text(
        json.dumps({
            "allow_incomplete": True,
            "source_completed_passes": 10,
            "source_requested_passes": 160,
        }),
        encoding="utf-8",
    )

    gate = scaling._offline_candidate_gate(
        args=args,
        audit_report=audit,
        data_dir=tmp_path,
        seeds=[101, 211, 307],
        post_selection_policy={
            "passed": True,
            "policy_config": {
                "hand_weight": 0.5,
                "tail_weight": 0.25,
                "match_weight": 0.25,
            },
        },
    )

    assert gate["passed"] is False
    assert "selection_overrides<10" in gate["errors"]
    assert "selection_ci_lower_threshold<0" in gate["errors"]
    assert "dataset_freeze_incomplete" in gate["errors"]
    assert "dataset_pass_count_incomplete" in gate["errors"]

    args.policy_min_overrides = requirements["selection_overrides"]
    args.policy_min_selection_ci_lower = 0.0
    (tmp_path / "freeze_manifest.json").write_text(
        json.dumps({
            "allow_incomplete": False,
            "source_completed_passes": 160,
            "source_requested_passes": 160,
        }),
        encoding="utf-8",
    )
    passing = scaling._offline_candidate_gate(
        args=args,
        audit_report=audit,
        data_dir=tmp_path,
        seeds=[101, 211, 307],
        post_selection_policy={
            "passed": True,
            "policy_config": {
                "hand_weight": 0.5,
                "tail_weight": 0.25,
                "match_weight": 0.25,
            },
        },
    )

    assert passing["passed"] is True
    assert passing["errors"] == []


@pytest.mark.parametrize(
    "cross_sequence_encoder",
    [None, "gru", "gru_moe", "deep_set", "transformer"],
)
def test_multitask_runtime_matches_torch_outputs(
    cross_sequence_encoder: str | None,
) -> None:
    temporal_cross_hand = cross_sequence_encoder is not None
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
        cross_sequence_hidden=4 if temporal_cross_hand else 0,
        cross_sequence_encoder=cross_sequence_encoder or "none",
        cross_transformer_heads=2,
        cross_moe_experts=3,
    )
    model.eval()
    clips = {
        "delta_vs_rule": 5000.0,
        "tail_delta_vs_rule": 2000.0,
        "match_delta_vs_rule": 2000.0,
    }
    payload = {
        "meta": {
            "format": (
                "opp_multitask_gru_v2" if temporal_cross_hand
                else "opp_multitask_gru_v1"
            ),
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
            "model": {
                "gru_hidden": 5,
                "max_hist": 16,
                "cross_sequence_hidden": 4 if temporal_cross_hand else 0,
                "max_cross_hands": 32,
                "cross_sequence_encoder": cross_sequence_encoder or "none",
                "cross_transformer_heads": 2,
                "cross_moe_experts": 3,
            },
            "training": {"clips": clips},
        },
        "weights": {key: value.detach().tolist() for key, value in model.state_dict().items()},
    }
    pure = runtime.OpponentMultiTaskRuntime(payload)
    hero = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.02, 0.1, 0.0, 1.0]

    for history_length, cross_length in (
        (0, 0), (1, 1), (6, 6), (15, 15), (16, 32), (16, 40)
    ):
        sample = {
            "state": [0.01] * 48,
            "profile": [0.02] * 12,
            "cross_hand": [0.03] * 20,
            "cross_hand_sequence": [
                [0.004 * (step + 1)] * 16
                for step in range(cross_length)
            ],
            "history": [[0.001 * (step + 1)] * 15 for step in range(history_length)],
            "target": [0.0] * 6,
            "rule_id": 1,
            "candidate_id": None,
        }
        (
            state, profile, history, lengths, cross,
            cross_sequence, cross_lengths,
        ) = trainer._context_tensors(
            [sample], max_hist=16, device="cpu"
        )
        with torch.no_grad():
            rule_action = torch.nn.functional.one_hot(
                torch.tensor([1]), num_classes=trainer.NUM_ACTIONS
            ).float()
            latent = model.encode(
                state, profile, history, lengths, cross,
                rule_action=rule_action,
                cross_sequence=cross_sequence,
                cross_lengths=cross_lengths,
            )
            torch_values = model.value(latent)
            public_state = state.clone()
            public_state[:, list(trainer.PRIVATE_STATE_INDICES)] = 0.0
            response_latent = model.encode(
                public_state, profile, history, lengths, cross,
                response=True,
                cross_sequence=cross_sequence,
                cross_lengths=cross_lengths,
            )
            torch_response = model.response(
                response_latent, torch.tensor([hero], dtype=torch.float32)
            )[0]

        actual_values = pure.predict_values(
            sample["state"], sample["profile"], sample["history"],
            sample["cross_hand"], 1,
            sample["cross_hand_sequence"],
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
            sample["cross_hand_sequence"],
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
