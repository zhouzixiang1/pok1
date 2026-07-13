from __future__ import annotations

import unittest

from bots.research_native_lab.cfr_neural_search.blueprint.evaluation import (
    expected_returns,
    exploitability,
)
from bots.research_native_lab.cfr_neural_search.blueprint.mccfr import (
    SolverConfig,
    SolverState,
    average_policy,
    train_batches,
)
from bots.research_native_lab.cfr_neural_search.blueprint.small_games import (
    BET,
    CHECK,
    KuhnPoker,
)
from bots.research_native_lab.cfr_neural_search.online_solver.depth_limited import (
    DepthLimitedGame,
    LeafValueContract,
    rollout_leaf,
)


class DepthLimitedTest(unittest.TestCase):
    def test_exact_rollout_leaf_preserves_policy_value_at_every_cutoff(self):
        game = KuhnPoker()
        reference = expected_returns(game, {})
        leaf = rollout_leaf({}, label="uniform-kuhn-v1")
        for max_depth in range(7):
            with self.subTest(max_depth=max_depth):
                truncated = DepthLimitedGame(game, max_depth, leaf)
                value = expected_returns(truncated, {})
                self.assertAlmostEqual(value[0], reference[0], places=12)
                self.assertAlmostEqual(value[1], reference[1], places=12)

    def test_leaf_contract_identity_binds_solver_game_name(self):
        game = KuhnPoker()
        first = DepthLimitedGame(
            game,
            4,
            rollout_leaf({}, label="first"),
        )
        second = DepthLimitedGame(
            game,
            4,
            rollout_leaf({}, label="second"),
        )
        self.assertNotEqual(first.name, second.name)
        state = SolverState(first.name, SolverConfig())
        with self.assertRaisesRegex(ValueError, "state is for"):
            train_batches(second, state, batches=1)

    def test_rollout_leaf_snapshots_mutable_policy(self):
        mutable_policy = {
            "kuhn:p0:r0:h=root": {CHECK: 1.0, BET: 0.0},
        }
        leaf = rollout_leaf(mutable_policy, label="snapshot-test")
        before = leaf(KuhnPoker().new_initial_state())
        mutable_policy["kuhn:p0:r0:h=root"] = {CHECK: 0.0, BET: 1.0}
        after = leaf(KuhnPoker().new_initial_state())
        self.assertEqual(before, after)

    def test_invalid_leaf_values_fail_closed(self):
        game = DepthLimitedGame(
            KuhnPoker(),
            0,
            LeafValueContract("invalid-leaf", lambda _state: (1.0, 1.0)),
        )
        with self.assertRaisesRegex(ValueError, "zero sum"):
            expected_returns(game, {})

    def test_mccfr_solves_a_depth_limited_game(self):
        game = DepthLimitedGame(
            KuhnPoker(),
            4,
            rollout_leaf({}, label="uniform-kuhn-v1"),
        )
        baseline = exploitability(game, {}).exploitability
        state = SolverState(
            game.name,
            SolverConfig(
                update_rule="linear",
                seed=31,
                samples_per_player=2,
            ),
        )
        train_batches(game, state, batches=500, shard_count=2)
        trained = exploitability(game, average_policy(state)).exploitability
        self.assertGreater(baseline, 0.4)
        self.assertLess(trained, 0.05)


if __name__ == "__main__":
    unittest.main()
