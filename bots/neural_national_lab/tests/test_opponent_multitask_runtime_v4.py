from __future__ import annotations

import copy
from pathlib import Path
import sys

import pytest
import torch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import export_opponent_multitask_v4 as exporter  # noqa: E402
import opponent_multitask_model_v4 as models  # noqa: E402
import opponent_multitask_runtime_v4 as runtime  # noqa: E402


def _inputs(*, empty: bool = False) -> dict[str, torch.Tensor]:
    generator = torch.Generator().manual_seed(20260711)
    history_steps = 0 if empty else 3
    cross_steps = 0 if empty else 2
    cross = torch.rand(1, cross_steps, 16, generator=generator)
    if cross_steps:
        cross[:, :, 15] = cross[:, :, 15] * 2.0 - 1.0
    return {
        "state": torch.rand(1, 81, generator=generator),
        "profile": torch.rand(1, 12, generator=generator),
        "history": torch.rand(1, history_steps, 24, generator=generator),
        "history_lengths": torch.tensor([history_steps]),
        "cross_sequence": cross,
        "cross_lengths": torch.tensor([cross_steps]),
        "rule_action": torch.tensor([[0.0, 0.0, 1.0, 0.0, 0.0, 0.0]]),
        "strategy_context": torch.rand(1, 66, generator=generator),
        "hero_action": torch.rand(1, 10, generator=generator),
        "legal_action_mask": torch.tensor([[1.0, 0.0, 1.0, 1.0, 1.0]]),
    }


def _payload(model) -> dict:
    return exporter.build_export_payload(
        model,
        {"schema": "test_v4_checkpoint", "code_artifacts": {}},
        checkpoint_sha256="a" * 64,
    )


def _value_inputs(inputs: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
    keys = (
        "state", "profile", "history", "history_lengths", "cross_sequence",
        "cross_lengths", "rule_action", "strategy_context",
    )
    return {key: inputs[key] for key in keys}


def _stdlib_inputs(inputs: dict[str, torch.Tensor]) -> dict:
    return {
        "state": inputs["state"][0].tolist(),
        "profile": inputs["profile"][0].tolist(),
        "history": inputs["history"][0].tolist(),
        "cross_sequence": inputs["cross_sequence"][0].tolist(),
        "rule_action": inputs["rule_action"][0].tolist(),
        "strategy_context": inputs["strategy_context"][0].tolist(),
    }


@pytest.mark.parametrize("encoder", ["none", "deep_set", "gru", "gru_moe"])
def test_stdlib_outcome_logits_and_probabilities_match_torch(encoder: str) -> None:
    torch.manual_seed(101)
    model = models.model_from_scale(
        "small", cross_encoder=encoder, dropout=0.0
    ).eval()
    inputs = _inputs()
    with torch.no_grad():
        expected_logits = model.forward_match_outcome(**_value_inputs(inputs))[0]
    stdlib = runtime.OpponentMultiTaskRuntimeV4(_payload(model))
    actual = stdlib.predict_match_outcome(**_stdlib_inputs(inputs))

    assert actual["logits"] == pytest.approx(
        expected_logits.tolist(), abs=1.0e-5
    )
    assert actual["probabilities"] == pytest.approx(
        torch.sigmoid(expected_logits).tolist(), abs=1.0e-6
    )


def test_v4_runtime_preserves_value_and_response_paths() -> None:
    torch.manual_seed(211)
    model = models.model_from_scale("small", cross_encoder="gru", dropout=0.0).eval()
    inputs = _inputs(empty=True)
    stdlib = runtime.OpponentMultiTaskRuntimeV4(_payload(model))
    with torch.no_grad():
        expected_value = model.forward_value(**_value_inputs(inputs))
        expected_response = model.forward_response(**{
            key: inputs[key]
            for key in (
                "state", "profile", "history", "history_lengths",
                "cross_sequence", "cross_lengths", "hero_action",
                "legal_action_mask",
            )
        })
    actual_value = stdlib.predict_value(**_stdlib_inputs(inputs))
    actual_response = stdlib.predict_response(
        state=inputs["state"][0].tolist(),
        profile=inputs["profile"][0].tolist(),
        history=[],
        cross_sequence=[],
        hero_action=inputs["hero_action"][0].tolist(),
        legal_action_mask=inputs["legal_action_mask"][0].tolist(),
    )

    assert actual_value["match_delta_vs_rule"]["mean"] == pytest.approx(
        expected_value["match_delta_vs_rule"]["mean"][0].tolist(), abs=1.0e-5
    )
    assert actual_response["logits"] == pytest.approx(
        expected_response["logits"][0].tolist(), abs=1.0e-5
    )


def test_runtime_rejects_outcome_weight_or_metadata_drift() -> None:
    model = models.model_from_scale("small", dropout=0.0).eval()
    payload = _payload(model)
    malformed = copy.deepcopy(payload)
    malformed["weights"]["match_outcome_head.2.weight"][0].pop()
    with pytest.raises(ValueError, match="wrong vector shape"):
        runtime.OpponentMultiTaskRuntimeV4(malformed)

    malformed = copy.deepcopy(payload)
    malformed["model_metadata"]["match_outcome_hands"] = 69
    with pytest.raises(ValueError, match="metadata changed"):
        runtime.OpponentMultiTaskRuntimeV4(malformed)


def test_export_bytes_are_deterministic_and_strictly_reloadable(
    tmp_path: Path,
) -> None:
    torch.manual_seed(307)
    model = models.model_from_scale("small", dropout=0.0).eval()
    payload = _payload(model)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    first_artifact = exporter.write_export(first, payload)
    second_artifact = exporter.write_export(second, payload)

    assert first.read_bytes() == second.read_bytes()
    assert first_artifact["sha256"] == second_artifact["sha256"]
    assert runtime.OpponentMultiTaskRuntimeV4.load(first) is not None
