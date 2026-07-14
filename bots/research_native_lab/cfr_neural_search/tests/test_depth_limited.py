from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import ClassVar

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
    LeducPoker,
)
from bots.research_native_lab.cfr_neural_search.online_solver.depth_limited import (
    DepthLimitedGame,
    LeafValueContract,
    _validated_leaf_value,
    rollout_leaf,
)


class _DriftControls:
    chance_left = 0.5
    payoff_scale = 1.0
    flip_transition = False

    @classmethod
    def reset(cls):
        cls.chance_left = 0.5
        cls.payoff_scale = 1.0
        cls.flip_transition = False


@dataclass(frozen=True, slots=True)
class _DriftState:
    chance_action: int | None = None
    terminal_action: str | None = None

    @property
    def current_player(self):
        if self.terminal_action is not None:
            return -2
        if self.chance_action is None:
            return -1
        return 0

    @property
    def depth(self):
        return int(self.chance_action is not None) + int(self.terminal_action is not None)

    def chance_outcomes(self):
        if self.current_player != -1:
            raise ValueError("not chance")
        return ((0, _DriftControls.chance_left), (1, 1.0 - _DriftControls.chance_left))

    def legal_actions(self):
        return ("a", "b") if self.current_player == 0 else ()

    def child(self, action):
        if self.current_player == -1:
            return _DriftState(chance_action=action)
        if self.current_player == 0:
            resolved = {"a": "b", "b": "a"}[action] if _DriftControls.flip_transition else action
            return _DriftState(self.chance_action, resolved)
        raise ValueError("terminal")

    def information_state_key(self, player):
        if self.current_player != 0 or player != 0:
            raise ValueError("not a decision")
        return "semantic-drift:p0"

    def returns(self):
        if self.current_player != -2:
            raise ValueError("not terminal")
        winning_action = "a" if self.chance_action == 0 else "b"
        value = _DriftControls.payoff_scale * (
            1.0 if self.terminal_action == winning_action else -1.0
        )
        return (value, -value)


@dataclass(frozen=True, slots=True)
class _DriftGame:
    name: ClassVar[str] = "semantic-drift"

    def new_initial_state(self):
        return _DriftState()


class DepthLimitedTest(unittest.TestCase):
    def tearDown(self):
        _DriftControls.reset()

    def test_exact_rollout_leaf_preserves_policy_value_at_every_cutoff(self):
        game = KuhnPoker()
        reference = expected_returns(game, {})
        leaf = rollout_leaf({}, game=game, label="uniform-kuhn-v1")
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
            rollout_leaf({}, game=game, label="first"),
        )
        second = DepthLimitedGame(
            game,
            4,
            rollout_leaf({}, game=game, label="second"),
        )
        self.assertNotEqual(first.name, second.name)
        state = SolverState(first.name, SolverConfig())
        with self.assertRaisesRegex(ValueError, "state is for"):
            train_batches(second, state, batches=1)

    def test_rollout_leaf_snapshots_mutable_policy(self):
        mutable_policy = {}

        def collect_uniform(state):
            if state.current_player == -2:
                return
            if state.current_player == -1:
                for action, _ in state.chance_outcomes():
                    collect_uniform(state.child(action))
                return
            key = state.information_state_key(state.current_player)
            legal = state.legal_actions()
            mutable_policy[key] = {action: 1.0 / len(legal) for action in legal}
            for action in legal:
                collect_uniform(state.child(action))

        collect_uniform(KuhnPoker().new_initial_state())
        mutable_policy["kuhn:p0:r0:h=root"] = {CHECK: 1.0, BET: 0.0}
        leaf = rollout_leaf(
            mutable_policy,
            game=KuhnPoker(),
            label="snapshot-test",
        )
        before = leaf(KuhnPoker().new_initial_state())
        mutable_policy["kuhn:p0:r0:h=root"] = {CHECK: 0.0, BET: 1.0}
        after = leaf(KuhnPoker().new_initial_state())
        self.assertEqual(before, after)

    def test_invalid_leaf_values_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "zero sum"):
            _validated_leaf_value((1.0, 1.0))
        for value in ((True, -1.0), ("1.0", "-1.0")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(TypeError, "bool/string"):
                    _validated_leaf_value(value)  # type: ignore[arg-type]

    def test_arbitrary_callable_cannot_reuse_a_leaf_identity(self):
        leaf = rollout_leaf({}, game=KuhnPoker(), label="sealed")
        with self.assertRaisesRegex(PermissionError, "rollout_leaf"):
            LeafValueContract(
                leaf.identity,
                leaf.game_binding,
                lambda _state: (100.0, -100.0),
            )

    def test_rollout_leaf_rejects_partial_and_coerced_profiles_at_freeze(self):
        game = KuhnPoker()
        partial = {"kuhn:p0:r0:h=root": {CHECK: 0.5, BET: 0.5}}
        with self.assertRaisesRegex(ValueError, "missing"):
            rollout_leaf(partial, game=game, label="partial")

        complete = {}

        def collect_uniform(state):
            if state.current_player == -2:
                return
            if state.current_player == -1:
                for action, _ in state.chance_outcomes():
                    collect_uniform(state.child(action))
                return
            key = state.information_state_key(state.current_player)
            legal = state.legal_actions()
            complete[key] = {action: 1.0 / len(legal) for action in legal}
            for action in legal:
                collect_uniform(state.child(action))

        collect_uniform(game.new_initial_state())
        complete["kuhn:p0:r0:h=root"] = {CHECK: True, BET: False}
        with self.assertRaisesRegex(TypeError, "JSON numbers"):
            rollout_leaf(complete, game=game, label="bool-policy")

    def test_leaf_cannot_be_reused_for_a_different_game(self):
        leaf = rollout_leaf({}, game=KuhnPoker(), label="kuhn-only")
        with self.assertRaisesRegex(ValueError, "different game"):
            DepthLimitedGame(LeducPoker(), 2, leaf)

    def test_full_semantic_fingerprint_rejects_payoff_chance_and_transition_drift(self):
        policy = {"semantic-drift:p0": {"a": 0.5, "b": 0.5}}
        mutations = (
            lambda: setattr(_DriftControls, "payoff_scale", 7.0),
            lambda: setattr(_DriftControls, "chance_left", 0.25),
            lambda: setattr(_DriftControls, "flip_transition", True),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                _DriftControls.reset()
                game = _DriftGame()
                leaf = rollout_leaf(policy, game=game, label="semantic-binding")
                self.assertEqual(leaf(game.new_initial_state()), (0.0, 0.0))
                mutate()
                with self.assertRaisesRegex(ValueError, "different game"):
                    DepthLimitedGame(game, 0, leaf)
                with self.assertRaisesRegex(ValueError, "frozen game/policy"):
                    leaf(game.new_initial_state())

    def test_mccfr_solves_a_depth_limited_game(self):
        base_game = KuhnPoker()
        game = DepthLimitedGame(
            base_game,
            4,
            rollout_leaf({}, game=base_game, label="uniform-kuhn-v1"),
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
