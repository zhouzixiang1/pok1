from __future__ import annotations

import pytest

from ..common_runtime.kuhn import CARDS, is_terminal
from ..rebel_like.pbs import KuhnPublicBeliefState
from ..rebel_like.toy_loop import fixture_policy, run_toy_selfplay


def test_exact_joint_pbs_bayes_update_and_card_exclusion() -> None:
    pbs = KuhnPublicBeliefState.initial()
    assert pbs.range_for(0) == pytest.approx({card: 1.0 / 3.0 for card in CARDS})
    assert pbs.range_for(1) == pytest.approx({card: 1.0 / 3.0 for card in CARDS})

    bet_likelihood = {
        0: {"check": 0.9, "bet": 0.1},
        1: {"check": 0.8, "bet": 0.2},
        2: {"check": 0.3, "bet": 0.7},
    }
    assert pbs.action_probability("bet", bet_likelihood) == pytest.approx(1.0 / 3.0)
    posterior = pbs.observe("bet", bet_likelihood)
    assert posterior.history == "bet"
    assert posterior.range_for(0) == pytest.approx({0: 0.1, 1: 0.2, 2: 0.7})
    assert all(deal[0] != deal[1] for deal in posterior.deal_probabilities)
    conditional = posterior.conditional_opponent_range(0, 2)
    assert conditional[2] == 0.0
    assert conditional[0] + conditional[1] == pytest.approx(1.0)


def test_zero_likelihood_observation_is_rejected() -> None:
    pbs = KuhnPublicBeliefState.initial()
    impossible = {
        card: {"check": 1.0, "bet": 0.0} for card in CARDS
    }
    with pytest.raises(ValueError, match="zero probability"):
        pbs.observe("bet", impossible)


def test_non_normalized_action_policy_is_rejected() -> None:
    pbs = KuhnPublicBeliefState.initial()
    malformed = {
        card: {"check": 0.8, "bet": 0.8} for card in CARDS
    }
    with pytest.raises(ValueError, match="sum to one"):
        pbs.observe("bet", malformed)


def test_toy_loop_is_deterministic_terminal_and_zero_sum() -> None:
    first = run_toy_selfplay(deal=(0, 2), seed=19)
    second = run_toy_selfplay(deal=(0, 2), seed=19)
    assert first == second
    assert is_terminal(first["terminal_history"])
    assert sum(first["utility"]) == 0.0
    assert first["trace"]


def test_on_policy_pbs_values_preserve_zero_sum_in_expectation() -> None:
    pbs = KuhnPublicBeliefState.initial()
    profile = fixture_policy()
    values = pbs.on_policy_infostate_values(profile)
    expectations = []
    for player in (0, 1):
        marginal = pbs.range_for(player)
        expectations.append(
            sum(marginal[card] * values[player][card] for card in CARDS)
        )
    assert expectations[0] == pytest.approx(-expectations[1])
