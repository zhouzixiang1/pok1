from __future__ import annotations

import unittest
from collections import defaultdict

from bots.research_native_lab.cfr_neural_search.blueprint.mccfr import (
    SolverConfig,
    SolverState,
    _external_sample,
)
from bots.research_native_lab.cfr_neural_search.blueprint.small_games import KuhnPoker
from bots.research_native_lab.cfr_neural_search.core.game import (
    CHANCE_PLAYER,
    TERMINAL_PLAYER,
)


class ExternalSamplingReferenceTest(unittest.TestCase):
    @staticmethod
    def _full_tree_regret_delta(player: int):
        game = KuhnPoker()
        result = defaultdict(lambda: defaultdict(float))

        def traverse(state, reach):
            actor = state.current_player
            if actor == TERMINAL_PLAYER:
                return state.returns()[player]
            if actor == CHANCE_PLAYER:
                value = 0.0
                for action, probability in state.chance_outcomes():
                    child_reach = list(reach)
                    child_reach[2] *= probability
                    value += probability * traverse(state.child(action), child_reach)
                return value

            legal = state.legal_actions()
            probabilities = {action: 1.0 / len(legal) for action in legal}
            child_values = {}
            for action in legal:
                child_reach = list(reach)
                child_reach[actor] *= probabilities[action]
                child_values[action] = traverse(state.child(action), child_reach)
            value = sum(
                probabilities[action] * child_values[action] for action in legal
            )
            if actor == player:
                counterfactual_reach = reach[1 - player] * reach[2]
                key = state.information_state_key(player)
                for action in legal:
                    result[key][str(action)] += counterfactual_reach * (
                        child_values[action] - value
                    )
            return value

        traverse(game.new_initial_state(), [1.0, 1.0, 1.0])
        return result

    def test_external_sampling_mean_matches_independent_full_tree_delta(self):
        game = KuhnPoker()
        state = SolverState.new_for_game(
            game,
            SolverConfig(seed=314159, samples_per_player=1),
        )
        sample_count = 20_000
        for player in (0, 1):
            expected = self._full_tree_regret_delta(player)
            observed = defaultdict(lambda: defaultdict(float))
            for sample_id in range(sample_count):
                sample = _external_sample(
                    game.new_initial_state(),
                    state,
                    player,
                    sample_id,
                )
                for key, vector in sample.regret_delta.items():
                    for action, value in vector.items():
                        observed[key][action] += value / sample_count

            self.assertEqual(set(observed), set(expected))
            for key, vector in expected.items():
                self.assertEqual(set(observed[key]), set(vector))
                for action, value in vector.items():
                    self.assertAlmostEqual(
                        observed[key][action],
                        value,
                        delta=0.012,
                    )


if __name__ == "__main__":
    unittest.main()
