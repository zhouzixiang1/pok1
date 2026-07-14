from __future__ import annotations

import itertools
import math
import unittest
from unittest.mock import patch

from bots.research_native_lab.cfr_neural_search.blueprint.evaluation import (
    BestResponseResult,
    best_response,
    expected_returns,
    exploitability,
)
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
    def test_absent_policy_rows_are_the_only_uniform_fallback(self):
        self.assertEqual(expected_returns(KuhnPoker(), {}), (0.12500000000000003, -0.12500000000000003))
        malformed_rows = (
            {_key(0, 0, ()): {CHECK: -0.1, BET: 1.1}},
            {_key(0, 0, ()): {CHECK: math.nan, BET: math.nan}},
            {_key(0, 0, ()): {}},
            {_key(0, 0, ()): {CHECK: 1.0}},
            {_key(0, 0, ()): {CHECK: 0.4, BET: 0.4}},
            {_key(0, 0, ()): {CHECK: True, BET: False}},
            {_key(0, 0, ()): {CHECK: "0.5", BET: "0.5"}},
            {_key(0, 0, ()): {CHECK: 0.5, BET: 0.5, "unknown": 0.0}},
            {_key(0, 0, ()): {CHECK: 0.5, BET: 0.5}},
        )
        for policy in malformed_rows:
            with self.subTest(policy=policy):
                with self.assertRaises(ValueError):
                    expected_returns(KuhnPoker(), policy)
                with self.assertRaises(ValueError):
                    exploitability(KuhnPoker(), policy)

        complete_with_garbage = kuhn_equilibrium_policy()
        complete_with_garbage["garbage:unreachable"] = {CHECK: 0.5, BET: 0.5}
        with self.assertRaisesRegex(ValueError, "unknown"):
            expected_returns(KuhnPoker(), complete_with_garbage)

    def test_materially_negative_nashconv_fails_and_tiny_noise_is_recorded(self):
        on_policy = expected_returns(KuhnPoker(), {})
        material = (
            BestResponseResult(0, on_policy[0] - 1e-4, {}, {}),
            BestResponseResult(1, on_policy[1], {}, {}),
        )
        with patch(
            "bots.research_native_lab.cfr_neural_search.blueprint.evaluation.best_response",
            side_effect=material,
        ):
            with self.assertRaisesRegex(RuntimeError, "materially negative"):
                exploitability(KuhnPoker(), {})

        tiny = (
            BestResponseResult(0, on_policy[0] - 5e-13, {}, {}),
            BestResponseResult(1, on_policy[1], {}, {}),
        )
        with patch(
            "bots.research_native_lab.cfr_neural_search.blueprint.evaluation.best_response",
            side_effect=tiny,
        ):
            result = exploitability(KuhnPoker(), {})
        self.assertEqual(result.player_improvements, (0.0, 0.0))
        self.assertAlmostEqual(result.raw_player_improvements[0], -5e-13, places=16)
        self.assertEqual(result.raw_player_improvements[1], 0.0)
        self.assertTrue(result.numerical_tolerance_clamped)

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

    def test_best_response_matches_exhaustive_pure_strategy_enumeration(self):
        game = KuhnPoker()
        opponent_policy = kuhn_equilibrium_policy()

        for player in (0, 1):
            action_sets = {}

            def collect(state):
                actor = state.current_player
                if actor == -2:
                    return
                if actor == -1:
                    for action, _ in state.chance_outcomes():
                        collect(state.child(action))
                    return
                if actor == player:
                    action_sets[state.information_state_key(player)] = state.legal_actions()
                for action in state.legal_actions():
                    collect(state.child(action))

            collect(game.new_initial_state())
            keys = tuple(sorted(action_sets))
            exhaustive_value = float("-inf")
            for choices in itertools.product(*(action_sets[key] for key in keys)):
                response_policy = {
                    key: dict(values) for key, values in opponent_policy.items()
                }
                for key, action in zip(keys, choices, strict=True):
                    response_policy[key] = {
                        legal: float(legal == action) for legal in action_sets[key]
                    }
                exhaustive_value = max(
                    exhaustive_value,
                    expected_returns(game, response_policy)[player],
                )

            result = best_response(game, opponent_policy, player)
            self.assertAlmostEqual(result.value, exhaustive_value, places=12)

    def test_best_response_counterfactual_top_values_decompose_root_value(self):
        game = KuhnPoker()
        for player in (0, 1):
            result = best_response(game, {}, player)
            if player == 0:
                top_keys = [_key(0, rank, ()) for rank in range(3)]
            else:
                top_keys = [
                    _key(1, rank, history)
                    for rank in range(3)
                    for history in ((CHECK,), (BET,))
                ]
            self.assertAlmostEqual(
                sum(result.counterfactual_values[key] for key in top_keys),
                result.value,
                places=12,
            )


if __name__ == "__main__":
    unittest.main()
