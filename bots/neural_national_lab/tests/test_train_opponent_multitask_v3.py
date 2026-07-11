from __future__ import annotations

import copy
from pathlib import Path
import sys

import pytest
import torch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import opponent_multitask_batch_v3 as batches  # noqa: E402
import opponent_multitask_model_v3 as models  # noqa: E402
import train_opponent_multitask_v3 as trainer  # noqa: E402


def _common(*, response: bool, opponent: str = "national_v1") -> dict:
    return {
        "encoded_context_schema": "opponent_multitask_inference_context_v3",
        "encoded_row_schema": "opponent_multitask_encoded_row_v3",
        "response_mode": response,
        "state": [0.1] * 81,
        "opponent_profile": [0.2] * 12,
        "history": [[0.3] * 24],
        "cross_hand_sequence": [[0.25] * 15 + [-0.1]],
        "row_weight": 1.0,
        "opponent": opponent,
    }


def _value_row(offset: float = 0.0) -> dict:
    row = _common(response=False)
    mask = [0, 0, 1, 1, 0, 1]
    targets = [0.0, 0.0, 0.0, 200.0 + offset, 0.0, -150.0 + offset]
    row.update({
        "rule_label_id": 2,
        "rule_action": [0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
        "strategy_context": [0.0] * 66,
        "strategy_context_available": False,
        "legal_action_mask": mask,
        "value_targets": {
            field: list(targets) for field in models.VALUE_FIELDS
        },
        "value_target_masks": {
            field: list(mask) for field in models.VALUE_FIELDS
        },
    })
    return row


def _response_row(target: int = 2) -> dict:
    row = _common(response=True)
    row.update({
        "hero_action_features": [0.0, 0.0, 1.0, 0.0, 0.0, 0.0,
                                 0.01, 0.20, 0.01, 0.99],
        "response_legal_action_mask": [1, 0, 1, 1, 1],
        "response_target": target,
        "response_size_targets": [0.4, 0.1] if target >= 3 else [0.0, 0.0],
        "response_size_target_mask": [1, 1] if target >= 3 else [0, 0],
    })
    return row


def _config() -> dict:
    return {
        "learning_rate": 1.0e-3,
        "weight_decay": 0.0,
        "seed": 7,
        "epochs": 2,
        "patience": 2,
        "minimum_improvement": 0.0,
        "batch_size": 2,
        "clips": dict(trainer.DEFAULT_CLIPS),
        "field_weights": dict(trainer.DEFAULT_FIELD_WEIGHTS),
        "mean_loss_weight": 1.0,
        "quantile_loss_weight": 1.0,
        "match_ranking_weight": 0.5,
        "match_q20_ranking_weight": 0.25,
        "ranking_margin": 100.0,
        "ranking_temperature": 0.25,
        "response_size_weight": 0.25,
        "response_loss_weight": 1.0,
        "gradient_clip_norm": 1.0,
    }


def test_multitask_objectives_are_finite_and_differentiable() -> None:
    model = models.model_from_scale("small", dropout=0.0)
    value_batch = batches.collate_encoded_rows([_value_row()], response=False)
    value_output = model.forward_value(**value_batch["inputs"])
    value_loss, value_metrics = trainer.value_objective(
        value_output,
        value_batch,
        clips=trainer.DEFAULT_CLIPS,
        field_weights=trainer.DEFAULT_FIELD_WEIGHTS,
        mean_weight=1.0,
        quantile_weight=1.0,
        ranking_weight=0.5,
        lower_ranking_weight=0.25,
        ranking_margin=100.0,
        ranking_temperature=0.25,
    )
    response_batch = batches.collate_encoded_rows(
        [_response_row(target=3)], response=True
    )
    response_output = model.forward_response(**response_batch["inputs"])
    response_loss, response_metrics = trainer.response_objective(
        response_output,
        response_batch,
        class_weights=torch.ones(5),
        size_weight=0.25,
    )

    loss = value_loss + response_loss
    loss.backward()

    assert torch.isfinite(loss)
    assert value_metrics["match_ranking"] > 0.0
    assert response_metrics["response.size"] >= 0.0
    assert any(parameter.grad is not None for parameter in model.parameters())


def test_value_objective_ignores_targets_outside_mask() -> None:
    first = _value_row()
    second = copy.deepcopy(first)
    for field in models.VALUE_FIELDS:
        second["value_targets"][field][0] = 1.0e9
    model = models.model_from_scale("small", dropout=0.0)

    def loss(row: dict) -> float:
        batch = batches.collate_encoded_rows([row], response=False)
        output = model.forward_value(**batch["inputs"])
        value, _ = trainer.value_objective(
            output,
            batch,
            clips=trainer.DEFAULT_CLIPS,
            field_weights=trainer.DEFAULT_FIELD_WEIGHTS,
            mean_weight=1.0,
            quantile_weight=1.0,
            ranking_weight=0.5,
            lower_ranking_weight=0.25,
            ranking_margin=100.0,
            ranking_temperature=0.25,
        )
        return float(value.item())

    assert loss(first) == pytest.approx(loss(second))


def test_train_evaluate_and_calibration_prediction_smoke() -> None:
    train = {
        "value": [_value_row(float(index * 10)) for index in range(4)],
        "behavior": [
            _response_row(2), _response_row(3), _response_row(4), _response_row(2)
        ],
    }
    early = {
        "value": [_value_row(5.0), _value_row(-5.0)],
        "behavior": [_response_row(2), _response_row(3)],
    }
    model = models.model_from_scale("small", dropout=0.0)

    history, best_epoch, report = trainer.train_model(
        model, train, early, config=_config(), device="cpu"
    )
    value_rows, response_rows = trainer.calibration_predictions(
        model,
        early,
        clips=trainer.DEFAULT_CLIPS,
        batch_size=2,
        device="cpu",
        lower_quantile=0.20,
    )

    assert len(history) == 2
    assert best_epoch in (1, 2)
    assert report["selection_score_is_strength_evidence"] is False
    assert len(value_rows) == 12
    assert len(response_rows) == 2
    assert all(row["opponent"] == "national_v1" for row in response_rows)


def test_checkpoint_authorization_binds_training_artifacts() -> None:
    class Dataset:
        run_id = "run-v3"
        manifest_sha256 = "a" * 64

    phase = {
        "roles": {
            "train": {"provenance": {"artifact_sha256": "b" * 64}},
            "early_stop": {"provenance": {"artifact_sha256": "c" * 64}},
        }
    }

    authorization = trainer.checkpoint_authorization(
        Dataset(), phase, checkpoint_sha256="d" * 64
    )

    assert authorization["frozen"] is True
    assert authorization["early_stop_complete"] is True
    assert authorization["training_artifact_sha256"] == {
        "train": "b" * 64,
        "early_stop": "c" * 64,
    }
    assert authorization["checkpoint_sha256"] == "d" * 64


def test_calibration_residual_uses_the_training_target_clip() -> None:
    model = models.model_from_scale("small", dropout=0.0)
    clipped = _value_row()
    extreme = copy.deepcopy(clipped)
    for field in models.VALUE_FIELDS:
        clipped["value_targets"][field][3] = trainer.DEFAULT_CLIPS[field]
        extreme["value_targets"][field][3] = 1.0e9
    role_clipped = {"value": [clipped], "behavior": [_response_row()]}
    role_extreme = {"value": [extreme], "behavior": [_response_row()]}

    clipped_rows, _ = trainer.calibration_predictions(
        model,
        role_clipped,
        clips=trainer.DEFAULT_CLIPS,
        batch_size=1,
        device="cpu",
        lower_quantile=0.20,
    )
    extreme_rows, _ = trainer.calibration_predictions(
        model,
        role_extreme,
        clips=trainer.DEFAULT_CLIPS,
        batch_size=1,
        device="cpu",
        lower_quantile=0.20,
    )

    clipped_action = [
        row for row in clipped_rows
        if row["field"] == "match_delta_vs_rule" and row["action_id"] == 3
    ][0]
    extreme_action = [
        row for row in extreme_rows
        if row["field"] == "match_delta_vs_rule" and row["action_id"] == 3
    ][0]
    assert extreme_action["residual"] == pytest.approx(clipped_action["residual"])


def test_checkpoint_loader_recreates_exact_model(tmp_path: Path) -> None:
    model = models.model_from_scale("small", cross_encoder="gru", dropout=0.0)
    model.eval()
    batch = batches.collate_encoded_rows([_value_row()], response=False)
    expected = model.forward_value(**batch["inputs"])["match_delta_vs_rule"][
        "quantiles"
    ].detach()
    path = tmp_path / "checkpoint.pt"
    torch.save({
        "schema": trainer.CHECKPOINT_SCHEMA,
        "model_metadata": model.metadata(),
        "state_dict": model.state_dict(),
    }, path)

    loaded, payload = trainer.load_checkpoint(path)
    actual = loaded.forward_value(**batch["inputs"])["match_delta_vs_rule"][
        "quantiles"
    ].detach()

    assert payload["schema"] == trainer.CHECKPOINT_SCHEMA
    assert torch.equal(actual, expected)
