from __future__ import annotations

import copy
from pathlib import Path
import sys

import pytest
import torch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import export_opponent_multitask_v3 as exporter  # noqa: E402
import opponent_multitask_model_v3 as models  # noqa: E402
import opponent_multitask_runtime_v3 as runtime  # noqa: E402


def _inputs(*, empty: bool = False) -> dict:
    generator = torch.Generator().manual_seed(20260711)
    history_steps = 0 if empty else 3
    cross_steps = 0 if empty else 2
    state = torch.rand(1, 81, generator=generator)
    profile = torch.rand(1, 12, generator=generator)
    history = torch.rand(1, history_steps, 24, generator=generator)
    cross = torch.rand(1, cross_steps, 16, generator=generator)
    if cross_steps:
        cross[:, :, 15] = cross[:, :, 15] * 2.0 - 1.0
    return {
        "state": state,
        "profile": profile,
        "history": history,
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
        {"schema": "test_checkpoint", "code_artifacts": {}},
        checkpoint_sha256="a" * 64,
    )


def _max_difference(expected, actual) -> float:
    differences = []
    for field in models.VALUE_FIELDS:
        differences.extend(
            abs(left - right)
            for left, right in zip(
                expected[field]["mean"][0].tolist(),
                actual[field]["mean"],
            )
        )
        differences.extend(
            abs(left - right)
            for expected_row, actual_row in zip(
                expected[field]["quantiles"][0].tolist(),
                actual[field]["quantiles"],
            )
            for left, right in zip(expected_row, actual_row)
        )
    return max(differences, default=0.0)


@pytest.mark.parametrize("encoder", ["none", "deep_set", "gru", "gru_moe"])
def test_stdlib_value_and_response_match_torch(encoder: str) -> None:
    torch.manual_seed(101)
    model = models.model_from_scale(
        "small", cross_encoder=encoder, dropout=0.0
    ).eval()
    inputs = _inputs()
    with torch.no_grad():
        torch_value = model.forward_value(**{
            key: inputs[key]
            for key in (
                "state", "profile", "history", "history_lengths",
                "cross_sequence", "cross_lengths", "rule_action",
                "strategy_context",
            )
        })
        torch_response = model.forward_response(**{
            key: inputs[key]
            for key in (
                "state", "profile", "history", "history_lengths",
                "cross_sequence", "cross_lengths", "hero_action",
                "legal_action_mask",
            )
        })
    stdlib = runtime.OpponentMultiTaskRuntimeV3(_payload(model))
    common = {
        "state": inputs["state"][0].tolist(),
        "profile": inputs["profile"][0].tolist(),
        "history": inputs["history"][0].tolist(),
        "cross_sequence": inputs["cross_sequence"][0].tolist(),
    }
    value = stdlib.predict_value(
        **common,
        rule_action=inputs["rule_action"][0].tolist(),
        strategy_context=inputs["strategy_context"][0].tolist(),
    )
    response = stdlib.predict_response(
        **common,
        hero_action=inputs["hero_action"][0].tolist(),
        legal_action_mask=inputs["legal_action_mask"][0].tolist(),
    )

    assert _max_difference(torch_value, value) < 1.0e-5
    assert response["logits"] == pytest.approx(
        torch_response["logits"][0].tolist(), abs=1.0e-5
    )
    assert response["size"] == pytest.approx(
        torch_response["size"][0].tolist(), abs=1.0e-5
    )


def test_empty_sequences_match_torch() -> None:
    torch.manual_seed(211)
    model = models.model_from_scale(
        "small", cross_encoder="gru_moe", dropout=0.0
    ).eval()
    inputs = _inputs(empty=True)
    with torch.no_grad():
        expected = model.forward_value(**{
            key: inputs[key]
            for key in (
                "state", "profile", "history", "history_lengths",
                "cross_sequence", "cross_lengths", "rule_action",
                "strategy_context",
            )
        })
    stdlib = runtime.OpponentMultiTaskRuntimeV3(_payload(model))
    actual = stdlib.predict_value(
        state=inputs["state"][0].tolist(),
        profile=inputs["profile"][0].tolist(),
        history=[],
        cross_sequence=[],
        rule_action=inputs["rule_action"][0].tolist(),
        strategy_context=inputs["strategy_context"][0].tolist(),
    )

    assert _max_difference(expected, actual) < 1.0e-5


def test_response_runtime_masks_private_state_again() -> None:
    torch.manual_seed(307)
    model = models.model_from_scale("small", dropout=0.0).eval()
    stdlib = runtime.OpponentMultiTaskRuntimeV3(_payload(model))
    inputs = _inputs()
    base = inputs["state"][0].tolist()
    changed = list(base)
    for index in runtime.PRIVATE_STATE_INDICES:
        changed[index] = 1000.0 + index
    kwargs = {
        "profile": inputs["profile"][0].tolist(),
        "history": inputs["history"][0].tolist(),
        "cross_sequence": inputs["cross_sequence"][0].tolist(),
        "hero_action": inputs["hero_action"][0].tolist(),
        "legal_action_mask": inputs["legal_action_mask"][0].tolist(),
    }

    first = stdlib.predict_response(state=base, **kwargs)
    second = stdlib.predict_response(state=changed, **kwargs)

    assert first == pytest.approx(second, abs=1.0e-12)


def test_runtime_rejects_weight_shape_or_key_drift() -> None:
    model = models.model_from_scale("small", dropout=0.0).eval()
    payload = _payload(model)
    malformed = copy.deepcopy(payload)
    malformed["weights"]["state_encoder.0.weight"][0].pop()
    with pytest.raises(ValueError, match="wrong vector shape"):
        runtime.OpponentMultiTaskRuntimeV3(malformed)

    malformed = copy.deepcopy(payload)
    malformed["weights"]["unexpected.weight"] = [[0.0]]
    with pytest.raises(ValueError, match="weight keys changed"):
        runtime.OpponentMultiTaskRuntimeV3(malformed)


def test_export_bytes_are_deterministic_and_strictly_reloadable(
    tmp_path: Path,
) -> None:
    torch.manual_seed(401)
    model = models.model_from_scale(
        "small", cross_encoder="deep_set", dropout=0.0
    ).eval()
    payload = _payload(model)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    first_artifact = exporter.write_export(first, payload)
    second_artifact = exporter.write_export(second, payload)

    assert first.read_bytes() == second.read_bytes()
    assert first_artifact["sha256"] == second_artifact["sha256"]
    assert runtime.OpponentMultiTaskRuntimeV3.load(first) is not None
