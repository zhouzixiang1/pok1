from __future__ import annotations

import pytest

from ..common_runtime.leduc import information_set, legal_actions, uniform_strategy
from ..common_runtime.leduc_evaluation import expected_utility
from ..decisionholdem_like.leduc_linear_cfr import LeducLinearCFR
from ..rebel_like.leduc_pbs import LeducPublicBeliefState


def _informative_profile():
    profile = uniform_strategy()
    for rank, raise_probability in enumerate((0.1, 0.4, 0.9)):
        profile[(0, rank, -1, "")] = {
            "check": 1.0 - raise_probability,
            "raise": raise_probability,
        }
    for rank, call_probability in enumerate((0.1, 0.5, 0.8)):
        profile[(1, rank, -1, "raise")] = {
            "fold": 1.0 - call_probability,
            "call": call_probability,
            "raise": 0.0,
        }
    return profile


def test_leduc_exact_pbs_updates_each_players_range_and_public_blockers() -> None:
    profile = _informative_profile()
    pbs = LeducPublicBeliefState.initial()
    uniform = {0: 1.0 / 3.0, 1: 1.0 / 3.0, 2: 1.0 / 3.0}
    assert pbs.range_for(0) == pytest.approx(uniform)
    assert pbs.range_for(1) == pytest.approx(uniform)

    p0_before = pbs.range_for(0)
    p1_before = pbs.range_for(1)
    after_raise = pbs.observe_action("raise", profile)
    assert after_raise.range_for(0) != pytest.approx(p0_before)
    # The exact joint oracle exposes card-removal correlation in the other
    # player's marginal as well; the learnable marginal PBS must not hide it.
    assert after_raise.range_for(1) != pytest.approx(p1_before)

    p0_after_raise = after_raise.range_for(0)
    p1_after_raise = after_raise.range_for(1)
    after_call = after_raise.observe_action("call", profile)
    assert after_call.chance_pending
    assert after_call.range_for(0) != pytest.approx(p0_after_raise)
    assert after_call.range_for(1) != pytest.approx(p1_after_raise)
    with pytest.raises(ValueError, match="public rank"):
        after_call.action_probability("check", profile)

    before_board = (after_call.range_for(0), after_call.range_for(1))
    postflop = after_call.observe_public_rank(2)
    assert not postflop.chance_pending
    assert postflop.public_rank == 2
    assert postflop.range_for(0) != pytest.approx(before_board[0])
    assert postflop.range_for(1) != pytest.approx(before_board[1])
    assert all(
        information_set(postflop.state, deal)[2] == 2
        for deal, probability in postflop.deal_probabilities.items()
        if probability > 0.0
    )


def test_leduc_counterfactual_labels_mix_to_on_policy_and_are_zero_sum() -> None:
    profile = _informative_profile()
    pbs = LeducPublicBeliefState.initial()
    values = pbs.on_policy_private_values(profile)
    action_values = pbs.conditional_deviation_action_values(profile)
    for rank in range(3):
        policy = profile[(0, rank, -1, "")]
        assert sum(
            policy[action] * value
            for action, value in action_values[rank].items()
        ) == pytest.approx(values[0][rank], abs=1e-12)

    expected = [
        sum(
            pbs.range_for(player)[rank] * values[player][rank]
            for rank in range(3)
        )
        for player in (0, 1)
    ]
    assert expected[0] == pytest.approx(-expected[1], abs=1e-12)
    assert expected[0] == pytest.approx(expected_utility(profile, 0), abs=1e-12)
    assert expected[1] == pytest.approx(expected_utility(profile, 1), abs=1e-12)


def test_leduc_pbs_rejects_zero_reach_action_and_out_of_order_chance() -> None:
    profile = uniform_strategy()
    for rank in range(3):
        profile[(0, rank, -1, "")] = {"check": 1.0, "raise": 0.0}
    pbs = LeducPublicBeliefState.initial()
    with pytest.raises(ValueError, match="zero probability"):
        pbs.observe_action("raise", profile)
    with pytest.raises(ValueError, match="not awaiting"):
        pbs.observe_public_rank(0)


def test_leduc_unnormalized_cfv_matches_the_lcfr_regret_equation() -> None:
    profile = uniform_strategy()
    pbs = LeducPublicBeliefState.initial()
    cfv = pbs.cfr_counterfactual_action_values(profile)
    solver = LeducLinearCFR()
    solver.train(1)
    actions = legal_actions(pbs.state)
    for rank in range(3):
        key = (0, rank, -1, "")
        node = sum(profile[key][action] * cfv[rank][action] for action in actions)
        expected_regrets = [cfv[rank][action] - node for action in actions]
        assert solver.regrets[key] == pytest.approx(expected_regrets, abs=1e-12)


def test_leduc_standard_cfv_rejects_a_pbs_from_another_profile() -> None:
    informative = _informative_profile()
    pbs = LeducPublicBeliefState.initial().observe_action("raise", informative)
    with pytest.raises(ValueError, match="posterior does not match"):
        pbs.cfr_counterfactual_action_values(uniform_strategy())
