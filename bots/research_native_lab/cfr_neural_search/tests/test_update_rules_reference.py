from __future__ import annotations

import math
import unittest

from bots.research_native_lab.cfr_neural_search.blueprint.mccfr import (
    SolverConfig,
    _new_strategy_weight,
    _updated_regret,
)


def _reference_regrets(config: SolverConfig, deltas: tuple[float, ...]) -> list[float]:
    """Literal paper-order recurrence, independent of the solver helper."""

    values: list[float] = []
    cumulative = 0.0
    for iteration, delta in enumerate(deltas, start=1):
        provisional = cumulative + delta
        if config.update_rule == "vanilla":
            cumulative = provisional
        elif config.update_rule == "cfr_plus":
            cumulative = max(0.0, provisional)
        elif config.update_rule == "linear":
            cumulative = provisional * iteration / (iteration + 1.0)
        else:
            exponent = config.dcfr_alpha if provisional >= 0.0 else config.dcfr_beta
            power = iteration**exponent
            cumulative = provisional * power / (power + 1.0)
        values.append(cumulative)
    return values


class UpdateRulesReferenceTest(unittest.TestCase):
    def test_all_regret_rules_match_literal_reference_across_sign_crossings(self):
        deltas = (3.25, -8.5, 1.125, 10.0, -20.0, 7.75)
        configs = (
            SolverConfig(update_rule="vanilla"),
            SolverConfig(update_rule="linear"),
            SolverConfig(update_rule="cfr_plus"),
            SolverConfig(
                update_rule="dcfr",
                dcfr_alpha=1.5,
                dcfr_beta=0.0,
                dcfr_gamma=2.0,
            ),
        )
        for config in configs:
            with self.subTest(rule=config.update_rule):
                expected = _reference_regrets(config, deltas)
                observed: list[float] = []
                cumulative = 0.0
                for iteration, delta in enumerate(deltas, start=1):
                    cumulative = _updated_regret(config, iteration, cumulative, delta)
                    observed.append(cumulative)
                self.assertEqual(len(observed), len(expected))
                for actual, reference in zip(observed, expected, strict=True):
                    self.assertAlmostEqual(actual, reference, places=14)

    def test_linear_scaled_recurrence_equals_iteration_weighted_regret(self):
        deltas = (3.25, -8.5, 1.125, 10.0, -20.0, 7.75)
        config = SolverConfig(update_rule="linear")
        cumulative = 0.0
        for iteration, delta in enumerate(deltas, start=1):
            cumulative = _updated_regret(config, iteration, cumulative, delta)
        reference = math.fsum(
            iteration * delta
            for iteration, delta in enumerate(deltas, start=1)
        ) / (len(deltas) + 1.0)
        self.assertAlmostEqual(cumulative, reference, places=14)

    def test_average_strategy_weights_match_rule_contracts(self):
        vanilla = SolverConfig(update_rule="vanilla")
        linear = SolverConfig(update_rule="linear")
        cfr_plus = SolverConfig(update_rule="cfr_plus", cfr_plus_delay=2)
        dcfr = SolverConfig(update_rule="dcfr", dcfr_gamma=1.75)
        self.assertEqual([_new_strategy_weight(vanilla, t) for t in range(1, 6)], [1.0] * 5)
        self.assertEqual(
            [_new_strategy_weight(linear, t) for t in range(1, 6)],
            [1.0, 2.0, 3.0, 4.0, 5.0],
        )
        self.assertEqual(
            [_new_strategy_weight(cfr_plus, t) for t in range(1, 6)],
            [0.0, 0.0, 1.0, 2.0, 3.0],
        )
        for iteration in range(1, 6):
            self.assertAlmostEqual(
                _new_strategy_weight(dcfr, iteration),
                iteration**1.75,
                places=14,
            )


if __name__ == "__main__":
    unittest.main()
