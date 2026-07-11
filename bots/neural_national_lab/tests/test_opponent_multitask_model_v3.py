from __future__ import annotations

from pathlib import Path
import sys

import pytest
import torch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import opponent_multitask_model_v3 as model_v3  # noqa: E402


def _inputs(batch: int = 3) -> dict:
    torch.manual_seed(7)
    return {
        "state": torch.rand(batch, 81),
        "profile": torch.rand(batch, 12),
        "history": torch.rand(batch, 5, 24),
        "history_lengths": torch.tensor([5, 3, 0][:batch]),
        "cross_sequence": torch.rand(batch, 4, 16),
        "cross_lengths": torch.tensor([4, 2, 0][:batch]),
        "rule_action": torch.eye(6)[:batch],
        "strategy_context": torch.rand(batch, 66),
        "hero_action": torch.rand(batch, 10),
        "legal_action_mask": torch.tensor([
            [1, 0, 1, 1, 1],
            [1, 1, 0, 1, 1],
            [1, 1, 0, 0, 0],
        ][:batch]),
    }


def test_value_and_response_shapes_are_distributional() -> None:
    model = model_v3.model_from_scale("small", cross_encoder="deep_set", dropout=0.0)
    model.eval()
    values = _inputs()

    output = model.forward_value(
        **{key: values[key] for key in (
            "state", "profile", "history", "history_lengths",
            "cross_sequence", "cross_lengths", "rule_action",
            "strategy_context",
        )}
    )
    response = model.forward_response(
        **{key: values[key] for key in (
            "state", "profile", "history", "history_lengths",
            "cross_sequence", "cross_lengths", "hero_action",
            "legal_action_mask",
        )}
    )

    assert set(output) == set(model_v3.VALUE_FIELDS)
    for field in output.values():
        assert field["mean"].shape == (3, 6)
        assert field["quantiles"].shape == (3, 6, 4)
        assert bool((field["quantiles"][:, :, 1:] >= field["quantiles"][:, :, :-1]).all())
    assert response["logits"].shape == (3, 5)
    assert response["size"].shape == (3, 2)
    assert bool(((response["size"] >= 0.0) & (response["size"] <= 1.0)).all())


def test_response_masks_illegal_logits() -> None:
    logits = torch.tensor([[1.0, 100.0, 3.0, 4.0, 5.0]])
    mask = torch.tensor([[1, 0, 1, 0, 0]])

    masked = model_v3.masked_response_logits(logits, mask)

    assert masked[0, 1] < -1.0e8
    assert masked[0, 3] < -1.0e8
    assert masked[0, 2] == 3.0
    with pytest.raises(ValueError, match="at least one legal"):
        model_v3.masked_response_logits(logits, torch.zeros_like(mask))


def test_response_path_masks_private_state_inside_model() -> None:
    model = model_v3.model_from_scale("small", dropout=0.0)
    model.eval()
    values = _inputs(batch=1)
    private = model.metadata()["response_private_state_masked"]
    changed = values["state"].clone()
    changed[:, private] = 1.0 - changed[:, private]

    def response(state: torch.Tensor) -> dict[str, torch.Tensor]:
        return model.forward_response(
            state=state,
            profile=values["profile"],
            history=values["history"],
            history_lengths=values["history_lengths"],
            cross_sequence=values["cross_sequence"],
            cross_lengths=values["cross_lengths"],
            hero_action=values["hero_action"],
            legal_action_mask=values["legal_action_mask"],
        )

    first = response(values["state"])
    second = response(changed)

    assert torch.equal(first["logits"], second["logits"])
    assert torch.equal(first["size"], second["size"])


def test_deep_set_cross_hand_encoder_is_permutation_invariant() -> None:
    model = model_v3.model_from_scale("small", cross_encoder="deep_set", dropout=0.0)
    model.eval()
    values = _inputs(batch=1)

    original = model.cross_encoder(
        values["cross_sequence"], values["cross_lengths"]
    )
    permuted = model.cross_encoder(
        values["cross_sequence"][:, [2, 0, 3, 1]], values["cross_lengths"]
    )

    assert torch.allclose(original, permuted, atol=1e-7, rtol=0.0)


@pytest.mark.parametrize("encoder", ["none", "deep_set", "gru", "gru_moe"])
def test_supported_cross_encoders_handle_empty_sequences(encoder: str) -> None:
    model = model_v3.model_from_scale("small", cross_encoder=encoder, dropout=0.0)
    model.eval()
    sequence = torch.zeros(2, 1, 16)
    lengths = torch.zeros(2, dtype=torch.long)

    encoded = model.cross_encoder(sequence, lengths)

    assert encoded.shape == (2, 48)
    assert torch.isfinite(encoded).all()
    assert torch.equal(encoded, torch.zeros_like(encoded))


def test_scale_parameter_counts_increase_and_metadata_is_explicit() -> None:
    counts = []
    for scale in ("small", "medium", "large"):
        model = model_v3.model_from_scale(scale, dropout=0.0)
        metadata = model.metadata()
        counts.append(metadata["parameters"])
        assert metadata["format"] == "opponent_multitask_distributional_v3"
        assert metadata["strategy_context_value_head_only"] is True
        assert metadata["max_current_hand_history"] == 16
        assert metadata["quantile_levels"] == [0.05, 0.1, 0.2, 0.5]
        assert metadata["response_size_targets"] == [
            "aggressive_increment_pot_log", "aggressive_stack_fraction"
        ]

    assert counts == sorted(counts)
    assert len(set(counts)) == 3
