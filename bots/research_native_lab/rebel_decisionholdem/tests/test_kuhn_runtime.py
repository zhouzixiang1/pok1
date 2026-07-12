from __future__ import annotations

import pytest

from ..common_runtime.evaluation import (
    best_response_value,
    expected_utility,
    exploitability,
    nash_conv,
)
from ..common_runtime.kuhn import terminal_utility, uniform_strategy


def test_terminal_utilities_are_zero_sum_and_use_net_stakes() -> None:
    assert terminal_utility((2, 0), "check-check", 0) == 1.0
    assert terminal_utility((2, 0), "bet-call", 0) == 2.0
    assert terminal_utility((0, 2), "bet-fold", 0) == 1.0
    assert terminal_utility((0, 2), "check-bet-fold", 0) == -1.0
    for history in ("check-check", "bet-fold", "bet-call", "check-bet-fold", "check-bet-call"):
        assert terminal_utility((0, 2), history, 0) == -terminal_utility(
            (0, 2), history, 1
        )


def test_uniform_profile_has_consistent_exact_best_response_metrics() -> None:
    profile = uniform_strategy()
    value0 = expected_utility(profile, player=0)
    value1 = expected_utility(profile, player=1)
    br0 = best_response_value(profile, 0)
    br1 = best_response_value(profile, 1)
    assert value0 == pytest.approx(-value1)
    assert nash_conv(profile) == pytest.approx(br0 + br1)
    assert exploitability(profile) == pytest.approx((br0 + br1) / 2.0)
    assert exploitability(profile) > 0.0


def test_known_kuhn_equilibrium_has_game_value_and_zero_exploitability() -> None:
    profile = uniform_strategy()
    alpha = 1.0 / 3.0
    profile[(0, 0, "")] = {"check": 1.0 - alpha, "bet": alpha}
    profile[(0, 1, "")] = {"check": 1.0, "bet": 0.0}
    profile[(0, 2, "")] = {"check": 1.0 - 3.0 * alpha, "bet": 3.0 * alpha}
    profile[(0, 0, "check-bet")] = {"fold": 1.0, "call": 0.0}
    profile[(0, 1, "check-bet")] = {
        "fold": 1.0 - (alpha + 1.0 / 3.0),
        "call": alpha + 1.0 / 3.0,
    }
    profile[(0, 2, "check-bet")] = {"fold": 0.0, "call": 1.0}
    profile[(1, 0, "check")] = {"check": 2.0 / 3.0, "bet": 1.0 / 3.0}
    profile[(1, 1, "check")] = {"check": 1.0, "bet": 0.0}
    profile[(1, 2, "check")] = {"check": 0.0, "bet": 1.0}
    profile[(1, 0, "bet")] = {"fold": 1.0, "call": 0.0}
    profile[(1, 1, "bet")] = {"fold": 2.0 / 3.0, "call": 1.0 / 3.0}
    profile[(1, 2, "bet")] = {"fold": 0.0, "call": 1.0}

    assert expected_utility(profile, player=0) == pytest.approx(-1.0 / 18.0)
    assert exploitability(profile) == pytest.approx(0.0, abs=1e-12)
