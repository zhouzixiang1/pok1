from __future__ import annotations

from pathlib import Path
import sys

import torch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import opponent_multitask_model_v3 as v3  # noqa: E402
import opponent_multitask_model_v4 as v4  # noqa: E402


def _inputs(batch: int = 2) -> dict[str, torch.Tensor]:
    return {
        "state": torch.randn(batch, 81),
        "profile": torch.rand(batch, 12),
        "history": torch.randn(batch, 3, 24),
        "history_lengths": torch.tensor([3, 1][:batch]),
        "cross_sequence": torch.randn(batch, 4, 16),
        "cross_lengths": torch.tensor([4, 2][:batch]),
        "rule_action": torch.tensor(
            [[0.0, 0.0, 1.0, 0.0, 0.0, 0.0]] * batch
        ),
        "strategy_context": torch.rand(batch, 66),
    }


def test_v4_joint_forward_keeps_v3_values_and_adds_per_action_logits() -> None:
    model = v4.model_from_scale("small", dropout=0.0)
    joint = model.forward_joint_value(**_inputs())

    assert set(joint["values"]) == set(v3.VALUE_FIELDS)
    assert joint["match_positive_logits"].shape == (2, 6)
    assert model.forward_value(**_inputs())["match_delta_vs_rule"]["mean"].shape == (
        2, 6
    )
    assert torch.isfinite(joint["match_positive_logits"]).all()


def test_outcome_head_is_trainable_and_v3_format_is_unchanged() -> None:
    model = v4.model_from_scale("small", dropout=0.0)
    loss = model.forward_match_outcome(**_inputs()).sum()
    loss.backward()

    assert model.match_outcome_head[-1].weight.grad is not None
    assert model.metadata()["format"] == (
        "opponent_multitask_distributional_outcome_v4"
    )
    assert model.metadata()["parent_value_format"] == (
        "opponent_multitask_distributional_v3"
    )
    assert model.metadata()["match_outcome_hands"] == 70
    assert v3.model_from_scale("small").metadata()["format"] == (
        "opponent_multitask_distributional_v3"
    )


def test_v4_parameter_scales_remain_ordered() -> None:
    counts = [
        v4.model_from_scale(scale, dropout=0.0).metadata()["parameters"]
        for scale in ("small", "medium", "large")
    ]

    assert counts == sorted(counts)
    assert len(set(counts)) == 3


def test_v4_temporal_transformer_preserves_outcome_contract() -> None:
    model = v4.model_from_scale(
        "small", cross_encoder="transformer", transformer_heads=4, dropout=0.0
    )
    output = model.forward_joint_value(**_inputs())
    metadata = model.metadata()

    assert output["match_positive_logits"].shape == (2, 6)
    assert metadata["cross_encoder"] == "transformer"
    assert metadata["cross_transformer_heads"] == 4
    assert metadata["match_outcome_hands"] == 70
