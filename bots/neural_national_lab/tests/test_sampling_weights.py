from __future__ import annotations

from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import sampling_weights as weights  # noqa: E402


def _row(
    opponent: str,
    seed: int,
    *,
    eligible: int = 12,
    selected: int = 12,
) -> dict:
    probability = selected / eligible
    return {
        "opponent": opponent,
        "deck_seed_base": seed,
        "bot_seed_base": seed + 100,
        "decision_sampling": "uniform",
        "eligible_decisions": eligible,
        "selected_decisions": selected,
        "decision_inclusion_probability": probability,
        "decision_inverse_probability_weight": 1.0 / probability,
    }


def test_sampling_ipw_preserves_inverse_probability_ratios() -> None:
    rows = [
        _row("national_v1", 1, eligible=24),
        _row("national_v1", 2, eligible=48),
    ]

    attached, report = weights.attach_training_row_weights(
        rows, scheme="sampling_ipw", modality="value"
    )

    assert attached[1]["_training_loss_weight"] == pytest.approx(
        2.0 * attached[0]["_training_loss_weight"]
    )
    assert report["sampling_ipw_used"] is True
    assert report["mean_row_weight"] == pytest.approx(1.0)


def test_opponent_balanced_ipw_equalizes_opponent_total_weight() -> None:
    rows = [
        _row("national_v1", 1, eligible=12),
        _row("national_v1", 2, eligible=24),
        _row("national_v1", 3, eligible=48),
        _row("national_v2", 4, eligible=12),
    ]

    attached, report = weights.attach_training_row_weights(
        rows,
        scheme="opponent_balanced_sampling_ipw",
        modality="value",
    )

    totals = report["per_opponent"]
    assert totals["national_v1"]["total_weight"] == pytest.approx(
        totals["national_v2"]["total_weight"]
    )
    assert sum(row["_training_loss_weight"] for row in attached) == pytest.approx(4.0)
    assert attached[2]["_training_loss_weight"] > attached[1]["_training_loss_weight"]


def test_behavior_rows_are_not_given_counterfactual_ipw() -> None:
    rows = [
        {"opponent": "national_v1", "deck_seed_base": 1, "bot_seed_base": 2},
        {"opponent": "national_v1", "deck_seed_base": 3, "bot_seed_base": 4},
        {"opponent": "national_v2", "deck_seed_base": 5, "bot_seed_base": 6},
    ]

    _, report = weights.attach_training_row_weights(
        rows,
        scheme="opponent_balanced_sampling_ipw",
        modality="behavior",
    )

    assert report["sampling_ipw_applicable"] is False
    assert report["sampling_ipw_used"] is False
    assert report["per_opponent"]["national_v1"]["total_weight"] == pytest.approx(
        report["per_opponent"]["national_v2"]["total_weight"]
    )


@pytest.mark.parametrize(
    "update",
    [
        {"decision_sampling": "first"},
        {"eligible_decisions": 0},
        {"selected_decisions": 13},
        {"decision_inclusion_probability": 0.9},
        {"decision_inverse_probability_weight": float("nan")},
        {"decision_inverse_probability_weight": 9.0},
    ],
)
def test_sampling_ipw_rejects_malformed_evidence(update: dict) -> None:
    row = _row("national_v1", 1)
    row.update(update)

    with pytest.raises(ValueError):
        weights.attach_training_row_weights(
            [row], scheme="sampling_ipw", modality="value"
        )


def test_legacy_uniform_mode_does_not_require_sampling_fields() -> None:
    rows = [{
        "opponent": "national_v1",
        "deck_seed_base": 1,
        "bot_seed_base": 2,
    }]

    attached, report = weights.attach_training_row_weights(
        rows, scheme="uniform", modality="value"
    )

    assert attached[0]["_training_loss_weight"] == 1.0
    assert report["sampling_ipw_used"] is False


def test_empty_rows_have_a_complete_report() -> None:
    attached, report = weights.attach_training_row_weights(
        [], scheme="opponent_balanced_sampling_ipw", modality="value"
    )

    assert attached == []
    assert report["rows"] == 0
    assert report["effective_sample_size"] == 0.0
