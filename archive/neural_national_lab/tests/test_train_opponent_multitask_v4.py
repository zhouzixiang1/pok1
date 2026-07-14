from __future__ import annotations

import copy
from pathlib import Path
import sys

import pytest
import torch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import opponent_multitask_batch_v4 as batches  # noqa: E402
import opponent_multitask_model_v4 as models  # noqa: E402
import train_opponent_multitask_v3 as v3  # noqa: E402
import train_opponent_multitask_v4 as trainer  # noqa: E402


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


def _value_row(*, baseline_positive: bool, offset: float = 0.0) -> dict:
    row = _common(response=False)
    mask = [0, 0, 1, 1, 0, 1]
    baseline = 100.0 if baseline_positive else -100.0
    positives = [0, 0, int(baseline_positive), 1, 0, 0]
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
        "match_outcome_supervision": {
            "schema": "national_70_hand_match_outcome_supervision_v1",
            "estimand": (
                "single_decision_70_hand_positive_outcome_uplift_clustered_v1"
            ),
            "hands": 70,
            "baseline_match_net_chips": baseline,
            "baseline_match_positive": int(baseline_positive),
            "match_positive_targets": positives,
            "match_positive_uplift_targets": [
                value - int(baseline_positive) if mask[index] else 0
                for index, value in enumerate(positives)
            ],
            "target_mask": mask,
        },
    })
    return row


def _response_row(target: int = 2) -> dict:
    row = _common(response=True)
    row.update({
        "hero_action_features": [
            0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.01, 0.20, 0.01, 0.99,
        ],
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
        "clips": dict(v3.DEFAULT_CLIPS),
        "field_weights": dict(v3.DEFAULT_FIELD_WEIGHTS),
        "mean_loss_weight": 1.0,
        "quantile_loss_weight": 1.0,
        "match_ranking_weight": 0.5,
        "match_q20_ranking_weight": 0.25,
        "ranking_margin": 100.0,
        "ranking_temperature": 0.25,
        "outcome_loss_weight": 2.0,
        "outcome_pairwise_weight": 0.5,
        "outcome_pairwise_temperature": 1.0,
        "response_size_weight": 0.25,
        "response_loss_weight": 1.0,
        "gradient_clip_norm": 1.0,
    }


def test_outcome_objective_is_masked_finite_and_differentiable() -> None:
    model = models.model_from_scale("small", dropout=0.0)
    row = _value_row(baseline_positive=False)
    batch = batches.collate_encoded_rows([row], response=False)
    logits = model.forward_match_outcome(**batch["inputs"])
    loss, metrics = trainer.outcome_objective(
        logits,
        batch,
        pairwise_weight=0.5,
        pairwise_temperature=1.0,
    )
    loss.backward()

    assert torch.isfinite(loss)
    assert metrics["match_outcome.bce"] > 0.0
    assert metrics["match_outcome.pair_weight"] > 0.0
    assert model.match_outcome_head[-1].weight.grad is not None

    changed = copy.deepcopy(row)
    changed["match_outcome_supervision"]["match_positive_targets"][0] = 1
    with pytest.raises(ValueError, match="outside its mask"):
        batches.collate_encoded_rows([changed], response=False)


def test_early_stop_key_is_lexicographic_with_outcome_first() -> None:
    best = [0.10, 0.10, 0.10, 0.10]

    assert trainer._key_improved([0.09, 1.0, 1.0, 1.0], best, 0.0)
    assert not trainer._key_improved([0.11, 0.0, 0.0, 0.0], best, 0.0)
    assert trainer._key_improved([0.10, 0.09, 1.0, 1.0], best, 0.0)


def test_train_evaluate_and_checkpoint_round_trip(tmp_path: Path) -> None:
    train = {
        "value": [
            _value_row(baseline_positive=bool(index % 2), offset=index * 10.0)
            for index in range(4)
        ],
        "behavior": [
            _response_row(2), _response_row(3), _response_row(4), _response_row(2),
        ],
    }
    early = {
        "value": [
            _value_row(baseline_positive=False, offset=5.0),
            _value_row(baseline_positive=True, offset=-5.0),
        ],
        "behavior": [_response_row(2), _response_row(3)],
    }
    model = models.model_from_scale("small", cross_encoder="gru", dropout=0.0)

    history, best_epoch, report = trainer.train_model(
        model, train, early, config=_config(), device="cpu"
    )

    assert len(history) == 2
    assert best_epoch in (1, 2)
    assert report["selection_key_is_lexicographic"] is True
    assert report["match_outcome"]["effective_weight"] > 0.0
    assert report["selection_score_is_strength_evidence"] is False

    batch = batches.collate_encoded_rows([early["value"][0]], response=False)
    expected = model.forward_match_outcome(**batch["inputs"]).detach()
    path = tmp_path / "checkpoint.pt"
    torch.save({
        "schema": trainer.CHECKPOINT_SCHEMA,
        "model_metadata": model.metadata(),
        "state_dict": model.state_dict(),
    }, path)
    loaded, payload = trainer.load_checkpoint(path)
    actual = loaded.forward_match_outcome(**batch["inputs"]).detach()

    assert payload["schema"] == trainer.CHECKPOINT_SCHEMA
    assert torch.equal(actual, expected)


def test_transformer_checkpoint_round_trip_and_parent_code_binding(
    tmp_path: Path,
) -> None:
    torch.manual_seed(31)
    model = models.model_from_scale(
        "small", cross_encoder="transformer", transformer_heads=4, dropout=0.0
    ).eval()
    path = tmp_path / "transformer.pt"
    torch.save({
        "schema": trainer.CHECKPOINT_SCHEMA,
        "model_metadata": model.metadata(),
        "state_dict": model.state_dict(),
    }, path)

    loaded, _ = trainer.load_checkpoint(path)
    assert loaded.metadata() == model.metadata()
    assert loaded.metadata()["cross_transformer_heads"] == 4

    for field in ("cross_transformer_heads", "cross_transformer_layers"):
        malformed = tmp_path / f"malformed-{field}.pt"
        metadata = dict(model.metadata())
        metadata[field] = True
        torch.save({
            "schema": trainer.CHECKPOINT_SCHEMA,
            "model_metadata": metadata,
            "state_dict": model.state_dict(),
        }, malformed)
        with pytest.raises(ValueError, match="invalid transformer metadata"):
            trainer.load_checkpoint(malformed)

    artifacts = trainer._code_artifacts()
    assert {
        "parent_model",
        "parent_batch",
        "dependency:freeze_opponent_role_dataset",
        "dependency:opponent_exposure_ledger",
        "dependency:role_dataset_access",
        "dependency:sampling_weights",
    } <= set(artifacts)
    assert all(len(row["sha256"]) == 64 for row in artifacts.values())


def test_training_rejects_code_drift_before_checkpoint_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup = {"trainer": {"bytes": 10, "sha256": "a" * 64}}
    monkeypatch.setattr(
        trainer,
        "_code_artifacts",
        lambda: {"trainer": {"bytes": 11, "sha256": "b" * 64}},
    )

    with pytest.raises(RuntimeError, match="changed while v4 training"):
        trainer._verify_code_artifacts_unchanged(startup)
