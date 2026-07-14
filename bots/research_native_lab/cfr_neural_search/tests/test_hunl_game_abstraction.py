from __future__ import annotations

import itertools
import json
import random
import unittest

from bots.research_native_lab.common_contracts import Action, ActionKind, NationalGameState, Street
from bots.research_native_lab.cfr_neural_search.blueprint.hunl_abstraction import (
    HUNLAbstractionConfig,
    PREFLOP_CLASSES,
    abstraction_asset_payload,
    backoff_keys_from_exact_key,
    canonical_suit_state,
    deterministic_equity,
    information_descriptor,
    legal_action_map,
    own_observation_recall,
    preflop_class,
)
from bots.research_native_lab.cfr_neural_search.blueprint.hunl_game import (
    HUNLTrainingGame,
    hunl_component_identities,
)
from bots.research_native_lab.cfr_neural_search.core.game import (
    CHANCE_PLAYER,
    TERMINAL_PLAYER,
)


class HUNLAbstractionTest(unittest.TestCase):
    @staticmethod
    def _passive_turn_state(cards: tuple[int, ...]) -> NationalGameState:
        state = NationalGameState.new_hand(
            1,
            small_blind=0,
            hole_cards=((cards[0], cards[2]), (cards[1], cards[3])),
        )
        state = state.apply_action(Action(ActionKind.CALL))
        state = state.apply_action(Action(ActionKind.CHECK))
        state = state.apply_chance(cards[4:7])
        state = state.apply_action(Action(ActionKind.CHECK))
        state = state.apply_action(Action(ActionKind.CALL))
        return state.apply_chance(cards[7:8])

    @staticmethod
    def _legacy_projection(exact_key: str) -> str:
        payload = json.loads(exact_key.split(":exact:", 1)[1])
        payload.pop("own_recall")
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def test_169_registry_and_all_1326_combinations_are_complete(self):
        self.assertEqual(len(PREFLOP_CLASSES), 169)
        self.assertEqual(len(set(PREFLOP_CLASSES)), 169)
        counts: dict[str, int] = {}
        for cards in itertools.combinations(range(52), 2):
            name = preflop_class(cards)
            counts[name] = counts.get(name, 0) + 1
        self.assertEqual(sum(counts.values()), 1326)
        self.assertEqual(set(counts), set(PREFLOP_CLASSES))
        asset = abstraction_asset_payload(HUNLAbstractionConfig())
        self.assertEqual(asset["combo_count"], 1326)
        self.assertEqual(len(asset["combo_to_class"]), 1326)

    def test_suit_isomorphism_and_equity_are_deterministic_with_card_removal(self):
        hole = (48, 45)
        board = (0, 5, 10, 19)
        first = deterministic_equity(hole, board, samples=64)
        self.assertEqual(first, deterministic_equity(hole, board, samples=64))
        permutation = (2, 3, 1, 0)

        def remap(card: int) -> int:
            return (card // 4) * 4 + permutation[card % 4]

        mapped_hole = tuple(remap(card) for card in hole)
        mapped_board = tuple(remap(card) for card in board)
        self.assertEqual(
            canonical_suit_state(hole, board),
            canonical_suit_state(mapped_hole, mapped_board),
        )
        self.assertEqual(
            first,
            deterministic_equity(mapped_hole, mapped_board, samples=64),
        )
        with self.assertRaises(ValueError):
            deterministic_equity(hole, board + (hole[0],), samples=8)

    def test_actions_are_common_derived_deduplicated_and_raise_200_to_400(self):
        state = NationalGameState.new_hand(
            1,
            small_blind=0,
            hole_cards=((48, 44), (49, 45)),
        )
        actions = legal_action_map(state)
        common = tuple(item.common_action for item in actions)
        self.assertEqual(len(common), len(set(common)))
        self.assertTrue(all(state.legal_actions().contains(action) for action in common))
        first_raise = next(item for item in actions if item.label == "raise:min")
        self.assertEqual(first_raise.common_action, Action(ActionKind.RAISE, 200))
        state = state.apply_action(first_raise.common_action)
        reply = legal_action_map(state)
        second_raise = next(item for item in reply if item.label == "raise:min")
        self.assertEqual(second_raise.common_action, Action(ActionKind.RAISE, 400))

    def test_infoset_backoffs_are_exactly_rederivable_and_hide_opponent_cards(self):
        config = HUNLAbstractionConfig()
        first = NationalGameState.new_hand(
            1,
            small_blind=0,
            hole_cards=((48, 44), (49, 45)),
        )
        second = NationalGameState.new_hand(
            1,
            small_blind=0,
            hole_cards=((48, 44), (3, 7)),
        )
        descriptor = information_descriptor(first, 0, config)
        self.assertEqual(
            descriptor.exact_key,
            information_descriptor(second, 0, config).exact_key,
        )
        self.assertEqual(
            descriptor.backoff_keys,
            backoff_keys_from_exact_key(descriptor.exact_key),
        )

    def test_cross_street_own_observation_recall_closes_old_key_collision(self):
        config = HUNLAbstractionConfig(equity_samples=16)
        first = self._passive_turn_state(
            (44, 12, 8, 36, 19, 35, 1, 30)
        )
        second = self._passive_turn_state(
            (16, 0, 33, 38, 5, 19, 25, 50)
        )
        first_descriptor = information_descriptor(first, 1, config)
        second_descriptor = information_descriptor(second, 1, config)
        first_recall = own_observation_recall(first, 1, config)
        second_recall = own_observation_recall(second, 1, config)
        self.assertEqual(
            self._legacy_projection(first_descriptor.exact_key),
            self._legacy_projection(second_descriptor.exact_key),
        )
        self.assertNotEqual(first_recall, second_recall)
        self.assertNotEqual(first_descriptor.exact_key, second_descriptor.exact_key)
        self.assertEqual(
            [entry["action"] for entry in first_recall],
            ["check", "check"],
        )

    def test_fixed_reachable_sample_has_one_recall_signature_per_exact_key(self):
        """Audit the perfect-recall partition, not only one regression hand."""

        config = HUNLAbstractionConfig(equity_samples=8)
        rng = random.Random(20260714)
        exact_signatures: dict[str, set[str]] = {}
        legacy_signatures: dict[str, set[str]] = {}
        visit_count = 0
        suit_permutation = (2, 3, 1, 0)
        for _ in range(96):
            deck = list(range(52))
            rng.shuffle(deck)
            cards = tuple(deck[:8])
            permuted = tuple(
                (card // 4) * 4 + suit_permutation[card % 4]
                for card in cards
            )
            for sample in (cards, permuted):
                state = self._passive_turn_state(sample)
                descriptor = information_descriptor(state, 1, config)
                signature = json.dumps(
                    own_observation_recall(state, 1, config),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                exact_signatures.setdefault(descriptor.exact_key, set()).add(signature)
                legacy_signatures.setdefault(
                    self._legacy_projection(descriptor.exact_key),
                    set(),
                ).add(signature)
                visit_count += 1
        self.assertGreater(visit_count, len(exact_signatures))
        self.assertTrue(all(len(value) == 1 for value in exact_signatures.values()))
        self.assertTrue(any(len(value) > 1 for value in legacy_signatures.values()))


class HUNLTrainingGameTest(unittest.TestCase):
    @staticmethod
    def _dealt_game() -> tuple[HUNLTrainingGame, object]:
        game = HUNLTrainingGame()
        state = game.new_initial_state().child("small_blind:0")
        for card in (48, 44, 49, 45):
            state = state.child(card)
        return game, state

    def test_real_four_street_checkdown_and_zero_sum_utility(self):
        _game, state = self._dealt_game()
        self.assertEqual(state.current_player, 0)
        self.assertEqual(state.common_state.stacks, (19950, 19900))
        state = state.child("call").child("check")
        self.assertEqual(state.current_player, CHANCE_PLAYER)
        for card in (0, 5, 10):
            state = state.child(card)
        self.assertEqual(state.common_state.street, Street.FLOP)
        state = state.child("check").child("call").child(16)
        self.assertEqual(state.common_state.street, Street.TURN)
        state = state.child("check").child("call").child(21)
        self.assertEqual(state.common_state.street, Street.RIVER)
        state = state.child("check").child("call")
        self.assertEqual(state.current_player, TERMINAL_PLAYER)
        utility = state.returns()
        self.assertAlmostEqual(sum(utility), 0.0)

    def test_fold_and_allin_call_runout_are_real_common_transitions(self):
        _game, state = self._dealt_game()
        folded = state.child("fold")
        self.assertEqual(folded.current_player, TERMINAL_PLAYER)
        self.assertEqual(folded.returns(), (-0.5, 0.5))

        _game, state = self._dealt_game()
        state = state.child("allin").child("call")
        self.assertEqual(state.current_player, CHANCE_PLAYER)
        for card in (0, 5, 10, 16, 21):
            state = state.child(card)
        self.assertEqual(state.current_player, TERMINAL_PLAYER)
        self.assertAlmostEqual(sum(state.returns()), 0.0)
        self.assertEqual(len(state.common_state.board), 5)

    def test_chance_deal_rejects_duplicates_and_exact_bool_aliases(self):
        game = HUNLTrainingGame()
        state = game.new_initial_state().child("small_blind:0").child(0)
        self.assertNotIn(0, {action for action, _ in state.chance_outcomes()})
        with self.assertRaises((TypeError, ValueError)):
            state.child(False)

    def test_full_identity_changes_with_same_named_abstraction_variant(self):
        first = HUNLTrainingGame(HUNLAbstractionConfig(equity_samples=48))
        second = HUNLTrainingGame(HUNLAbstractionConfig(equity_samples=49))
        self.assertEqual(first.name, second.name)
        self.assertNotEqual(first.identity_sha256(), second.identity_sha256())
        components = hunl_component_identities(first)
        self.assertEqual(components["game_sha256"], first.identity_sha256())
        self.assertEqual(
            set(components),
            {
                "game_sha256",
                "rules_sha256",
                "card_abstraction_sha256",
                "action_abstraction_sha256",
                "abstraction_asset_sha256",
                "common_dependency_sha256",
                "utility_sha256",
                "source_sha256",
            },
        )


if __name__ == "__main__":
    unittest.main()
