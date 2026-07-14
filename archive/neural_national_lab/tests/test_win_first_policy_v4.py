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


def _action_values(
    *,
    hand: dict[int, float],
    tail: dict[int, float] | None = None,
    match: dict[int, float] | None = None,
) -> dict:
    def vector(overrides: dict[int, float]) -> list[float]:
        result = [0.0] * 6
        for label_id, value in overrides.items():
            result[label_id] = value
        return result

    tail = hand if tail is None else tail
    match = hand if match is None else match
    return {
        "delta_vs_rule": {"lower": vector(hand)},
        "tail_delta_vs_rule": {"lower": vector(tail)},
        "match_delta_vs_rule": {"lower": vector(match)},
    }


def _deterministic_outcomes(probabilities: dict[int, float]) -> dict:
    row = [0.1] * 6
    for label_id, value in probabilities.items():
        row[label_id] = value
    return policy.aggregate_member_probabilities(
        [row], uncertainty_std_weight=0.0
    )


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


def test_positive_probability_lcb_equal_to_half_is_eligible() -> None:
    selected = policy.select_candidate(
        _policy(min_positive_probability_lcb=0.5),
        _deterministic_outcomes({2: 0.2, 3: 0.5}),
        _action_values(hand={3: 1.0}),
        [{"label_id": 3}],
        rule_label_id=2,
    )

    assert selected is not None
    assert selected["label_id"] == 3
    assert selected["prediction"]["candidate_probability_lcb"] == 0.5


def test_candidate_lcb_equal_to_rule_ucb_is_ineligible() -> None:
    assert policy.select_candidate(
        _policy(min_positive_probability_lcb=0.5),
        _deterministic_outcomes({2: 0.5, 3: 0.5}),
        _action_values(hand={3: 1_000.0}),
        [{"label_id": 3}],
        rule_label_id=2,
    ) is None


def test_immediate_hand_lcb_rejects_negative_epsilon_and_accepts_zero() -> None:
    outcomes = _deterministic_outcomes({2: 0.2, 3: 0.8})
    candidate = [{"label_id": 3}]

    assert policy.select_candidate(
        _policy(min_positive_probability_lcb=0.5),
        outcomes,
        _action_values(
            hand={3: -1.0e-12}, tail={3: 10.0}, match={3: 10.0}
        ),
        candidate,
        rule_label_id=2,
    ) is None

    selected = policy.select_candidate(
        _policy(min_positive_probability_lcb=0.5),
        outcomes,
        _action_values(hand={3: 0.0}, tail={3: 10.0}, match={3: 10.0}),
        candidate,
        rule_label_id=2,
    )
    assert selected is not None
    assert selected["prediction"]["hand"] == 0.0


def test_chip_score_must_strictly_exceed_margin() -> None:
    outcomes = _deterministic_outcomes({2: 0.2, 3: 0.8})
    candidate = [{"label_id": 3}]
    chip_only = {
        "min_positive_probability_lcb": 0.5,
        "chip_margin": 10.0,
        "hand_weight": 1.0,
        "tail_weight": 0.0,
        "match_weight": 0.0,
    }

    assert policy.select_candidate(
        _policy(**chip_only),
        outcomes,
        _action_values(hand={3: 10.0}),
        candidate,
        rule_label_id=2,
    ) is None

    selected = policy.select_candidate(
        _policy(**chip_only),
        outcomes,
        _action_values(hand={3: 10.0 + 1.0e-9}),
        candidate,
        rule_label_id=2,
    )
    assert selected is not None
    assert selected["prediction"]["chip_score"] > 10.0


def test_selection_uses_chip_score_as_third_lexicographic_key() -> None:
    selected = policy.select_candidate(
        _policy(min_positive_probability_lcb=0.5),
        _deterministic_outcomes({2: 0.2, 3: 0.8, 5: 0.8}),
        _action_values(hand={3: 1.0, 5: 2.0}),
        [{"label_id": 3}, {"label_id": 5}],
        rule_label_id=2,
    )

    assert selected is not None
    assert selected["label_id"] == 5
    assert selected["prediction"]["chip_score"] == 2.0


@pytest.mark.parametrize("ordered_labels", [(5, 3), (3, 5)])
def test_exact_three_key_tie_preserves_candidate_input_order(
    ordered_labels: tuple[int, int],
) -> None:
    selected = policy.select_candidate(
        _policy(min_positive_probability_lcb=0.5),
        _deterministic_outcomes({2: 0.2, 3: 0.8, 5: 0.8}),
        _action_values(hand={3: 1.0, 5: 1.0}),
        [{"label_id": label_id} for label_id in ordered_labels],
        rule_label_id=2,
    )

    assert selected is not None
    assert selected["label_id"] == ordered_labels[0]


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


def test_scoring_recomputes_uncertainty_bounds_and_value_dimensions() -> None:
    outcomes = policy.aggregate_member_probabilities(
        [
            [0.1, 0.1, 0.2, 0.7, 0.1, 0.1],
            [0.1, 0.1, 0.2, 0.9, 0.1, 0.1],
        ],
        uncertainty_std_weight=1.0,
    )
    outcomes["lower"][3] = 0.75
    with pytest.raises(ValueError, match="do not match uncertainty"):
        policy.select_candidate(
            _policy(), outcomes, _values(), [{"label_id": 3}], rule_label_id=2
        )

    outcomes = policy.aggregate_member_probabilities(
        [[0.1, 0.1, 0.2, 0.8, 0.1, 0.1]],
        uncertainty_std_weight=0.0,
    )
    malformed = _values()
    malformed["delta_vs_rule"]["lower"] = [0.0, 1.0]
    with pytest.raises(ValueError, match="value prediction is malformed"):
        policy.select_candidate(
            _policy(), outcomes, malformed, [{"label_id": 3}], rule_label_id=2
        )
