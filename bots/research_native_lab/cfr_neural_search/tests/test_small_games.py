from __future__ import annotations

import unittest

from bots.research_native_lab.cfr_neural_search.blueprint.small_games import (
    CALL,
    CHECK,
    FOLD,
    RAISE,
    KuhnPoker,
    KuhnState,
    LeducPoker,
)
from bots.research_native_lab.cfr_neural_search.core.game import (
    CHANCE_PLAYER,
    TERMINAL_PLAYER,
)


class SmallGameTest(unittest.TestCase):
    def _validate_tree(self, root) -> tuple[int, int]:
        seen = set()
        terminals = 0

        def visit(state):
            nonlocal terminals
            if state in seen:
                return
            seen.add(state)
            if state.current_player == TERMINAL_PLAYER:
                returns = state.returns()
                self.assertAlmostEqual(returns[0] + returns[1], 0.0)
                terminals += 1
                return
            if state.current_player == CHANCE_PLAYER:
                outcomes = state.chance_outcomes()
                self.assertAlmostEqual(sum(probability for _, probability in outcomes), 1.0)
                for action, probability in outcomes:
                    self.assertGreater(probability, 0.0)
                    visit(state.child(action))
                return
            legal = state.legal_actions()
            self.assertTrue(legal)
            for action in legal:
                visit(state.child(action))

        visit(root)
        return len(seen), terminals

    def test_kuhn_tree_is_finite_and_zero_sum(self):
        states, terminals = self._validate_tree(KuhnPoker().new_initial_state())
        self.assertGreater(states, 20)
        self.assertGreater(terminals, 10)

    def test_leduc_tree_is_finite_and_zero_sum(self):
        states, terminals = self._validate_tree(LeducPoker().new_initial_state())
        self.assertGreater(states, 1000)
        self.assertGreater(terminals, 500)

    def test_kuhn_information_sets_hide_opponent_card(self):
        state_a = KuhnState(cards=(0, 1))
        state_b = KuhnState(cards=(0, 2))
        self.assertEqual(
            state_a.information_state_key(0),
            state_b.information_state_key(0),
        )

    def test_leduc_raise_cap_and_fold_payoff(self):
        state = LeducPoker().new_initial_state().child(0).child(2)
        state = state.child(RAISE)
        self.assertIn(RAISE, state.legal_actions())
        capped = state.child(RAISE)
        self.assertEqual(capped.legal_actions(), (CALL, FOLD))

        folded = state.child(FOLD)
        self.assertEqual(folded.returns(), (1.0, -1.0))

    def test_leduc_public_round_and_showdown(self):
        state = LeducPoker().new_initial_state().child(0).child(2)
        state = state.child(CHECK).child(CHECK)
        self.assertEqual(state.current_player, CHANCE_PLAYER)
        state = state.child(4).child(CHECK).child(CHECK)
        self.assertEqual(state.current_player, TERMINAL_PLAYER)
        self.assertEqual(state.returns(), (-1.0, 1.0))

    def test_leduc_equal_private_ranks_tie(self):
        state = LeducPoker().new_initial_state().child(0).child(1)
        state = state.child(CHECK).child(CHECK).child(2)
        state = state.child(CHECK).child(CHECK)
        self.assertEqual(state.returns(), (0.0, -0.0))


if __name__ == "__main__":
    unittest.main()
