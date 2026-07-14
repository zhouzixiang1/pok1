from __future__ import annotations

import pytest

from ..common_runtime.kuhn import CARDS, is_terminal
from ..rebel_like.pbs import KuhnMarginalPublicBeliefState, KuhnPublicBeliefState
from ..rebel_like.toy_loop import fixture_policy, run_toy_selfplay


def test_exact_joint_oracle_bayes_update_and_card_exclusion() -> None:
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


def test_paper_shaped_marginals_project_and_bayes_update_consistently() -> None:
    joint = KuhnPublicBeliefState.initial()
    marginal = KuhnMarginalPublicBeliefState.initial()
    assert joint.to_marginal_projection() == marginal

    bet_likelihood = {
        0: {"check": 0.9, "bet": 0.1},
        1: {"check": 0.8, "bet": 0.2},
        2: {"check": 0.3, "bet": 0.7},
    }
    assert marginal.action_probability("bet", bet_likelihood) == pytest.approx(
        joint.action_probability("bet", bet_likelihood)
    )
    marginal_after = marginal.observe("bet", bet_likelihood)
    joint_after = joint.observe("bet", bet_likelihood)

    # The acting player's Delta-S Bayes update agrees with exact joint truth.
    assert marginal_after.range_for(0) == pytest.approx(joint_after.range_for(0))
    assert marginal_after.range_for(0) == pytest.approx({0: 0.1, 1: 0.2, 2: 0.7})

    # A pair of marginal reach ranges does not carry cross-player blockers.  The
    # paper-shaped update leaves the non-acting range unchanged, while the exact
    # toy oracle projects the blocker-induced correlation for validation.
    assert marginal_after.range_for(1) == pytest.approx(
        {card: 1.0 / 3.0 for card in CARDS}
    )
    joint_projection = KuhnMarginalPublicBeliefState.from_joint(joint_after)
    assert joint_projection.range_for(1) == pytest.approx(
        {0: 0.45, 1: 0.40, 2: 0.15}
    )
    assert joint_projection.range_for(1) != pytest.approx(marginal_after.range_for(1))


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
    assert "marginal_before" in first["trace"][0]
    assert "joint_oracle_before" in first["trace"][0]
    assert "terminal_marginal_pbs" in first
    assert "terminal_joint_oracle" in first


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


def test_both_marginal_ranges_update_when_each_player_publicly_acts() -> None:
    profile = fixture_policy()
    pbs = KuhnMarginalPublicBeliefState.initial()
    player0_before = pbs.range_for(0)
    player1_before = pbs.range_for(1)
    pbs = pbs.observe(
        "bet",
        {card: profile[(0, card, "")] for card in CARDS},
    )
    assert pbs.range_for(0) != pytest.approx(player0_before)
    assert pbs.range_for(1) == pytest.approx(player1_before)

    player0_after_bet = pbs.range_for(0)
    pbs = pbs.observe(
        "call",
        {card: profile[(1, card, "bet")] for card in CARDS},
    )
    assert pbs.range_for(0) == pytest.approx(player0_after_bet)
    assert pbs.range_for(1) != pytest.approx(player1_before)


def test_counterfactual_action_labels_mix_back_to_on_policy_value() -> None:
    pbs = KuhnPublicBeliefState.initial()
    profile = fixture_policy()
    action_values = pbs.conditional_deviation_action_values(profile)
    on_policy = pbs.on_policy_infostate_values(profile)[0]
    for card in CARDS:
        policy = profile[(0, card, "")]
        mixed = sum(
            policy[action] * value
            for action, value in action_values[card].items()
        )
        assert mixed == pytest.approx(on_policy[card], abs=1e-12)

    with pytest.raises(ValueError, match="terminal"):
        pbs.observe(
            "bet", {card: profile[(0, card, "")] for card in CARDS}
        ).observe(
            "call", {card: profile[(1, card, "bet")] for card in CARDS}
        ).conditional_deviation_action_values(profile)


def test_standard_cfv_rejects_a_pbs_from_another_profile() -> None:
    fixture = fixture_policy()
    pbs = KuhnPublicBeliefState.initial().observe(
        "bet", {card: fixture[(0, card, "")] for card in CARDS}
    )
    from ..common_runtime.kuhn import uniform_strategy

    with pytest.raises(ValueError, match="posterior does not match"):
        pbs.cfr_counterfactual_action_values(uniform_strategy())
