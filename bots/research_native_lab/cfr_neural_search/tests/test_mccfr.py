from __future__ import annotations

import unittest

from bots.research_native_lab.cfr_neural_search.blueprint.evaluation import exploitability
from bots.research_native_lab.cfr_neural_search.blueprint.mccfr import (
    SolverConfig,
    SolverState,
    _new_strategy_weight,
    _updated_regret,
    average_policy,
    train_batches,
)
from bots.research_native_lab.cfr_neural_search.blueprint.small_games import (
    KuhnPoker,
    LeducPoker,
)


class MCCFRTest(unittest.TestCase):
    def test_linear_accumulator_discount_formula(self):
        linear = SolverConfig(update_rule="linear")
        round1 = _updated_regret(linear, 1, 0.0, 2.0)
        round2 = _updated_regret(linear, 2, round1, 2.0)
        self.assertEqual(round1, 1.0)
        self.assertEqual(round2, 2.0)
        self.assertEqual(_new_strategy_weight(linear, 3), 3.0)

    def test_linear_recurrence_is_iteration_weighted_regret(self):
        linear = SolverConfig(update_rule="linear")
        deltas = (3.0, -2.0, 5.0, -7.0)
        cumulative = 0.0
        for iteration, delta in enumerate(deltas, start=1):
            cumulative = _updated_regret(
                linear,
                iteration,
                cumulative,
                delta,
            )
        expected = sum(
            iteration * delta
            for iteration, delta in enumerate(deltas, start=1)
        ) / (len(deltas) + 1.0)
        self.assertAlmostEqual(cumulative, expected, places=12)

    def test_dcfr_two_round_positive_to_negative_uses_updated_sign(self):
        dcfr = SolverConfig(update_rule="dcfr", dcfr_alpha=1.5, dcfr_beta=0.0)
        round1 = _updated_regret(dcfr, 1, 0.0, 4.0)
        round2 = _updated_regret(dcfr, 2, round1, -5.0)
        self.assertEqual(round1, 2.0)
        self.assertEqual(round2, -1.5)

    def test_dcfr_two_round_negative_to_positive_uses_updated_sign(self):
        dcfr = SolverConfig(update_rule="dcfr", dcfr_alpha=1.5, dcfr_beta=0.0)
        round1 = _updated_regret(dcfr, 1, 0.0, -4.0)
        round2 = _updated_regret(dcfr, 2, round1, 5.0)
        positive_factor = 2**1.5 / (2**1.5 + 1.0)
        self.assertEqual(round1, -2.0)
        self.assertAlmostEqual(
            round2,
            3.0 * positive_factor,
        )
        self.assertEqual(_new_strategy_weight(dcfr, 2), 4.0)

    def test_all_update_rules_converge_on_kuhn(self):
        for rule in ("linear", "cfr_plus", "dcfr"):
            with self.subTest(rule=rule):
                game = KuhnPoker()
                state = SolverState(
                    game_name=game.name,
                    config=SolverConfig(update_rule=rule, seed=17),
                )
                train_batches(game, state, batches=2000)
                result = exploitability(game, average_policy(state))
                self.assertLess(result.exploitability, 0.06)
                if rule == "cfr_plus":
                    self.assertTrue(
                        all(
                            value >= 0.0
                            for vector in state.regrets.values()
                            for value in vector.values()
                        )
                    )

    def test_regret_and_average_strategy_are_separate(self):
        game = KuhnPoker()
        state = SolverState(game.name, SolverConfig(seed=99))
        train_batches(game, state, batches=10)
        self.assertIsNot(state.regrets, state.strategy_sum)
        self.assertEqual(set(state.regrets), set(state.strategy_sum))
        self.assertTrue(
            any(state.regrets[key] != state.strategy_sum[key] for key in state.actions)
        )

    def test_leduc_exploitability_improves_over_uniform(self):
        game = LeducPoker()
        uniform = exploitability(game, {}).exploitability
        state = SolverState(game.name, SolverConfig(update_rule="linear", seed=23))
        train_batches(game, state, batches=1500)
        trained = exploitability(game, average_policy(state)).exploitability
        self.assertLess(trained, uniform * 0.35)
        self.assertLess(trained, 0.75)


if __name__ == "__main__":
    unittest.main()
