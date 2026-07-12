from __future__ import annotations

import unittest

from bots.research_native_lab.cfr_neural_search.blueprint.evaluation import exploitability
from bots.research_native_lab.cfr_neural_search.blueprint.small_games import (
    BET,
    CALL,
    CHECK,
    FOLD,
    KuhnPoker,
)


def _key(player: int, rank: int, history: tuple[str, ...]) -> str:
    encoded = ",".join(history) or "root"
    return f"kuhn:p{player}:r{rank}:h={encoded}"


def kuhn_equilibrium_policy():
    policy = {
        _key(0, 0, ()): {CHECK: 2 / 3, BET: 1 / 3},
        _key(0, 1, ()): {CHECK: 1.0, BET: 0.0},
        _key(0, 2, ()): {CHECK: 0.0, BET: 1.0},
        _key(1, 0, (CHECK,)): {CHECK: 2 / 3, BET: 1 / 3},
        _key(1, 1, (CHECK,)): {CHECK: 1.0, BET: 0.0},
        _key(1, 2, (CHECK,)): {CHECK: 0.0, BET: 1.0},
        _key(1, 0, (BET,)): {CALL: 0.0, FOLD: 1.0},
        _key(1, 1, (BET,)): {CALL: 1 / 3, FOLD: 2 / 3},
        _key(1, 2, (BET,)): {CALL: 1.0, FOLD: 0.0},
        _key(0, 0, (CHECK, BET)): {CALL: 0.0, FOLD: 1.0},
        _key(0, 1, (CHECK, BET)): {CALL: 2 / 3, FOLD: 1 / 3},
        _key(0, 2, (CHECK, BET)): {CALL: 1.0, FOLD: 0.0},
    }
    return policy


class EvaluationTest(unittest.TestCase):
    def test_exact_kuhn_equilibrium(self):
        result = exploitability(KuhnPoker(), kuhn_equilibrium_policy())
        self.assertAlmostEqual(result.on_policy_returns[0], -1 / 18, places=12)
        self.assertAlmostEqual(result.on_policy_returns[1], 1 / 18, places=12)
        self.assertAlmostEqual(result.nash_conv, 0.0, places=12)
        self.assertAlmostEqual(result.exploitability, 0.0, places=12)

    def test_uniform_kuhn_is_exploitable(self):
        result = exploitability(KuhnPoker(), {})
        self.assertGreater(result.exploitability, 0.2)
        self.assertGreater(result.nash_conv, 0.4)


if __name__ == "__main__":
    unittest.main()
