from __future__ import annotations

from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import win_first_policy_v4 as policy  # noqa: E402


def _policy(**updates) -> dict:
    payload = {
        "schema": policy.POLICY_SCHEMA,
        "selection_priority": policy.SELECTION_PRIORITY,
        "min_positive_probability_lcb": 0.55,
        "min_probability_uplift_lcb": 0.0,
        "chip_margin": 0.0,
        "hand_weight": 0.25,
        "tail_weight": 0.25,
        "match_weight": 0.50,
        "response_weight": 0.0,
        "min_hand_lcb": 0.0,
        "use_lower": True,
    }
    payload.update(updates)
    return payload


def _values() -> dict:
    return {
        "delta_vs_rule": {
            "lower": [0.0, 0.0, 0.0, 1000.0, 0.0, 1.0]
        },
        "tail_delta_vs_rule": {
            "lower": [0.0, 0.0, 0.0, 1000.0, 0.0, 1.0]
        },
        "match_delta_vs_rule": {
            "lower": [0.0, 0.0, 0.0, 1000.0, 0.0, 1.0]
        },
    }


def test_outcome_aggregation_uses_population_epistemic_uncertainty() -> None:
    result = policy.aggregate_member_probabilities(
        [
            [0.2, 0.2, 0.3, 0.6, 0.2, 0.7],
            [0.2, 0.2, 0.5, 0.8, 0.2, 0.9],
        ],
        uncertainty_std_weight=1.0,
    )

    assert result["mean"][2] == pytest.approx(0.4)
    assert result["member_probability_std"][2] == pytest.approx(0.1)
    assert result["lower"][3] == pytest.approx(0.6)
    assert result["upper"][3] == pytest.approx(0.8)


def test_selection_prioritizes_positive_probability_before_chip_magnitude() -> None:
    outcomes = policy.aggregate_member_probabilities(
        [
            [0.1, 0.1, 0.30, 0.70, 0.1, 0.80],
            [0.1, 0.1, 0.30, 0.70, 0.1, 0.80],
        ],
        uncertainty_std_weight=1.0,
    )

    selected = policy.select_candidate(
        _policy(),
        outcomes,
        _values(),
        [{"label_id": 3}, {"label_id": 5}],
        rule_label_id=2,
    )

    assert selected is not None
    assert selected["label_id"] == 5
    assert selected["prediction"]["candidate_probability_lcb"] == 0.8
    assert selected["prediction"]["chip_score"] == 1.0


def test_probability_floor_and_rule_ucb_override_large_chip_score() -> None:
    below_floor = policy.aggregate_member_probabilities(
        [[0.1, 0.1, 0.30, 0.54, 0.1, 0.1]],
        uncertainty_std_weight=1.0,
    )
    assert policy.select_candidate(
        _policy(), below_floor, _values(), [{"label_id": 3}], rule_label_id=2
    ) is None

    cannot_beat_rule = policy.aggregate_member_probabilities(
        [
            [0.1, 0.1, 0.60, 0.70, 0.1, 0.1],
            [0.1, 0.1, 0.80, 0.70, 0.1, 0.1],
        ],
        uncertainty_std_weight=1.0,
    )
    assert policy.select_candidate(
        _policy(),
        cannot_beat_rule,
        _values(),
        [{"label_id": 3}],
        rule_label_id=2,
    ) is None


def test_policy_rejects_weakened_or_opponent_specific_contract() -> None:
    with pytest.raises(ValueError, match="cannot be below 0.5"):
        policy.normalize_policy(_policy(min_positive_probability_lcb=0.49))

    opponent_specific = _policy()
    opponent_specific["national_v141_threshold"] = 0.9
    with pytest.raises(ValueError, match="unknown or missing"):
        policy.normalize_policy(opponent_specific)


def test_scoring_rejects_drifted_outcome_bounds() -> None:
    outcomes = policy.aggregate_member_probabilities(
        [[0.1, 0.1, 0.2, 0.8, 0.1, 0.1]],
        uncertainty_std_weight=0.0,
    )
    outcomes["lower"][3] = 0.9

    with pytest.raises(ValueError, match="bounds are invalid"):
        policy.select_candidate(
            _policy(), outcomes, _values(), [{"label_id": 3}], rule_label_id=2
        )
