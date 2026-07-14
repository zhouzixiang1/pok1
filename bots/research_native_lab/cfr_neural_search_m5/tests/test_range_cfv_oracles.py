from __future__ import annotations

import math
import unittest
from dataclasses import replace
from pathlib import Path

from bots.research_native_lab.cfr_neural_search.blueprint.evaluation import (
    expected_returns,
    information_state_action_schema,
)
from bots.research_native_lab.cfr_neural_search.blueprint.small_games import (
    CHECK,
    KuhnPoker,
    LeducPoker,
    LeducState,
)
from bots.research_native_lab.cfr_neural_search.core.game import (
    CHANCE_PLAYER,
    TERMINAL_PLAYER,
)
from bots.research_native_lab.cfr_neural_search_m5.cfv.combo_index import (
    COMBO_TO_INDEX,
)
from bots.research_native_lab.cfr_neural_search_m5.cfv import hunl_micro_oracle
from bots.research_native_lab.cfr_neural_search_m5.cfv.hunl_micro_oracle import (
    FOLD_PROVIDER_ID,
    exact_fold_cfv,
    exact_river_call_or_fold_cfv,
    exact_showdown_cfv,
    exact_turn_allin_runout_cfv,
)
from bots.research_native_lab.cfr_neural_search_m5.cfv.pairwise import (
    exact_pairwise_cfv,
)
from bots.research_native_lab.cfr_neural_search_m5.cfv.public_state import (
    PublicHUNLState,
)
from bots.research_native_lab.cfr_neural_search_m5.cfv.ranges import (
    uniform_reach_range,
)
from bots.research_native_lab.cfr_neural_search_m5.cfv.semantics import (
    RangeCFVQuery,
    RangeCFVResult,
)
from bots.research_native_lab.cfr_neural_search_m5.cfv.toy_oracle import (
    KuhnPublicState,
    LeducPublicState,
    ToyRangeQuery,
    conditioned_profile_value0,
    exact_one_step_cutoff,
    exact_toy_cfv,
    raw_bilinear_value0,
)
from bots.research_native_lab.cfr_neural_search_m5.solver.public_range_depth_limited import (
    PrimaryLeafUnavailable,
    PublicRangeLeafConsumer,
)
from bots.research_native_lab.cfr_neural_search_m5.solver import range_cfv_contract
from bots.research_native_lab.cfr_neural_search_m5.solver.range_cfv_contract import (
    RangeCFVLeafContract,
    make_exact_oracle_contract,
)
from bots.research_native_lab.common_contracts import Action, ActionKind, NationalGameState
from bots.research_native_lab.common_contracts.cards import compare_hands


BOARD = (0, 21, 30, 39, 44)
FIRST_HAND = (48, 49)
SECOND_HAND = (4, 8)
ALTERNATE_HOLES = ((50, 51), (6, 7))


def _nonuniform_policy(game):
    policy = {}
    for key, actions in information_state_action_schema(game).items():
        total = math.fsum(index + 1 for index in range(len(actions)))
        policy[key] = {
            action: (index + 1) / total for index, action in enumerate(actions)
        }
    return policy


def _independent_value(state, policy, cache=None):
    if cache is None:
        cache = {}
    if state in cache:
        return cache[state]
    actor = state.current_player
    if actor == TERMINAL_PLAYER:
        result = state.returns()
    elif actor == CHANCE_PLAYER:
        outcomes = state.chance_outcomes()
        result = tuple(
            math.fsum(
                probability
                * _independent_value(state.child(action), policy, cache)[player]
                for action, probability in outcomes
            )
            for player in (0, 1)
        )
    else:
        row = policy[state.information_state_key(actor)]
        result = tuple(
            math.fsum(
                row[action]
                * _independent_value(state.child(action), policy, cache)[player]
                for action in state.legal_actions()
            )
            for player in (0, 1)
        )
    cache[state] = result
    return result


def _independent_one_step_action_values(query, policy):
    size = query.public_state.deck_size
    first_pair = next(
        (first, second)
        for first in range(size)
        for second in range(size)
        if first != second
        and first not in query.public_state.blocked_cards
        and second not in query.public_state.blocked_cards
    )
    representative = query.public_state.instantiate(*first_pair)
    actor = representative.current_player
    actions = representative.legal_actions()
    values = {action: [0.0] * size for action in actions}
    cache = {}
    for own in range(size):
        if not query.valid_masks[actor][own]:
            continue
        terms = {action: [] for action in actions}
        for other, weight in enumerate(query.private_ranges[1 - actor]):
            if weight <= 0.0 or other == own or other in query.public_state.blocked_cards:
                continue
            state = (
                query.public_state.instantiate(own, other)
                if actor == 0
                else query.public_state.instantiate(other, own)
            )
            for action in actions:
                terms[action].append(
                    weight
                    * _independent_value(state.child(action), policy, cache)[actor]
                )
        for action in actions:
            values[action][own] = math.fsum(terms[action])
    return actor, actions, {action: tuple(row) for action, row in values.items()}


def _new_hand(
    holes: tuple[tuple[int, int], tuple[int, int]] = (FIRST_HAND, SECOND_HAND),
) -> NationalGameState:
    return NationalGameState.new_hand(1, small_blind=0, hole_cards=holes)


def _to_flop(state: NationalGameState) -> NationalGameState:
    state = state.apply_action(Action(ActionKind.CALL))
    state = state.apply_action(Action(ActionKind.CHECK))
    return state.apply_chance(BOARD[:3])


def _close_checked_street(state: NationalGameState) -> NationalGameState:
    state = state.apply_action(Action(ActionKind.CHECK))
    return state.apply_action(Action(ActionKind.CALL))


def _to_turn(state: NationalGameState) -> NationalGameState:
    state = _close_checked_street(_to_flop(state))
    return state.apply_chance(BOARD[3:4])


def _to_river(state: NationalGameState) -> NationalGameState:
    state = _close_checked_street(_to_turn(state))
    return state.apply_chance(BOARD[4:5])


def _query(
    state: NationalGameState,
    first_hand: tuple[int, int] = FIRST_HAND,
    second_hand: tuple[int, int] = SECOND_HAND,
) -> RangeCFVQuery:
    public = PublicHUNLState.from_common_state(state)
    first = COMBO_TO_INDEX[tuple(sorted(first_hand))]
    second = COMBO_TO_INDEX[tuple(sorted(second_hand))]
    return RangeCFVQuery(
        public_state=public,
        private_ranges=(
            uniform_reach_range(public.board_card_ids, support_indices=(first,)),
            uniform_reach_range(public.board_card_ids, support_indices=(second,)),
        ),
    )


def _invalid_fold_provider(query: RangeCFVQuery):
    # The returned provider id intentionally differs from the sealed primary.
    return exact_fold_cfv(query)


def _nonzero_exact_provider(query: RangeCFVQuery):
    masks = query.valid_masks
    values = tuple(
        tuple(1.0 if masks[player][index] else 0.0 for index in range(1326))
        for player in (0, 1)
    )
    return RangeCFVResult.create(
        query,
        provider_id="route-b-m5-nonzero-exact-v1",
        raw_values=values,
    )


class ToyRangeCFVOracleTest(unittest.TestCase):
    def test_kuhn_root_matches_exact_game_and_raw_reach_derivatives(self):
        query = ToyRangeQuery(
            KuhnPublicState(),
            ((1.0 / 3.0,) * 3, (1.0 / 3.0,) * 3),
        )
        result = exact_toy_cfv(query, {})
        reference = expected_returns(KuhnPoker(), {})[0]
        self.assertAlmostEqual(conditioned_profile_value0(query, result), reference, places=12)
        self.assertLess(abs(result.zero_sum_residual), 1e-12)

        epsilon = 1e-7
        baseline = raw_bilinear_value0(
            result.pair_utility0,
            query.private_ranges[0],
            query.private_ranges[1],
        )
        for card in range(3):
            perturbed0 = list(query.private_ranges[0])
            perturbed0[card] += epsilon
            derivative0 = (
                raw_bilinear_value0(
                    result.pair_utility0,
                    tuple(perturbed0),
                    query.private_ranges[1],
                )
                - baseline
            ) / epsilon
            self.assertAlmostEqual(derivative0, result.values[0][card], places=8)

            perturbed1 = list(query.private_ranges[1])
            perturbed1[card] += epsilon
            derivative1 = -(
                raw_bilinear_value0(
                    result.pair_utility0,
                    query.private_ranges[0],
                    tuple(perturbed1),
                )
                - baseline
            ) / epsilon
            self.assertAlmostEqual(derivative1, result.values[1][card], places=8)

    def test_zero_own_reach_stays_evaluable_and_one_step_regret_solves(self):
        query = ToyRangeQuery(
            KuhnPublicState(),
            ((0.0, 0.5, 0.5), (1.0 / 3.0,) * 3),
        )
        result = exact_toy_cfv(query, {})
        self.assertTrue(result.valid_masks[0][0])
        self.assertNotEqual(
            result.values[0],
            tuple(-value for value in result.values[1]),
        )
        cutoff = exact_one_step_cutoff(query, {})
        self.assertEqual(cutoff.actor, 0)
        for card, enabled in enumerate(result.valid_masks[0]):
            if not enabled:
                continue
            uniform_value = math.fsum(
                cutoff.action_values[action][card] for action in cutoff.actions
            ) / len(cutoff.actions)
            self.assertAlmostEqual(uniform_value, cutoff.policy_values[card], places=12)
            self.assertAlmostEqual(
                math.fsum(cutoff.regrets[action][card] for action in cutoff.actions),
                0.0,
                places=12,
            )
        profile_value = math.fsum(
            query.private_ranges[0][card] * result.values[0][card]
            for card in range(3)
        )
        self.assertGreaterEqual(cutoff.greedy_profile_value + 1e-12, profile_value)

    def test_leduc_root_matches_exact_game_and_public_chance_is_conditioned(self):
        public = LeducPublicState.from_state(LeducState(private_cards=(0, 2)))
        query = ToyRangeQuery(public, ((1.0 / 6.0,) * 6, (1.0 / 6.0,) * 6))
        result = exact_toy_cfv(query, {})
        reference = expected_returns(LeducPoker(), {})[0]
        self.assertAlmostEqual(conditioned_profile_value0(query, result), reference, places=11)
        self.assertAlmostEqual(query.joint_compatible_mass, 5.0 / 6.0, places=12)

        # Closing the first betting round leaves only the future public card
        # chance in the oracle; no already-conditioned private/public prefix is
        # multiplied again.
        concrete = LeducState(private_cards=(0, 2)).child(CHECK).child(CHECK)
        chance_public = LeducPublicState.from_state(concrete)
        chance_query = ToyRangeQuery(
            chance_public,
            ((1.0 / 6.0,) * 6, (1.0 / 6.0,) * 6),
        )
        chance_result = exact_toy_cfv(chance_query, {})
        self.assertLess(abs(chance_result.zero_sum_residual), 1e-12)
        self.assertNotEqual(chance_result.values[0], result.values[0])

    def test_leduc_nonuniform_actor0_actor1_regrets_and_chance_match_independent_enumeration(self):
        game = LeducPoker()
        policy = _nonuniform_policy(game)
        uniform = (1.0 / 6.0,) * 6
        root_public = LeducPublicState.from_state(LeducState(private_cards=(0, 2)))
        root_query = ToyRangeQuery(root_public, (uniform, uniform))
        root_result = exact_toy_cfv(root_query, policy)
        self.assertAlmostEqual(
            conditioned_profile_value0(root_query, root_result),
            expected_returns(game, policy)[0],
            places=11,
        )

        actor1_public = LeducPublicState.from_state(
            LeducState(private_cards=(0, 2)).child(CHECK)
        )
        for query in (
            root_query,
            ToyRangeQuery(actor1_public, (uniform, uniform)),
        ):
            cutoff = exact_one_step_cutoff(query, policy)
            actor, actions, independent = _independent_one_step_action_values(
                query, policy
            )
            self.assertEqual(cutoff.actor, actor)
            self.assertEqual(cutoff.actions, actions)
            for action in actions:
                for card, enabled in enumerate(query.valid_masks[actor]):
                    if enabled:
                        self.assertAlmostEqual(
                            cutoff.action_values[action][card],
                            independent[action][card],
                            places=11,
                        )
            for own, enabled in enumerate(query.valid_masks[actor]):
                if not enabled:
                    continue
                other = next(card for card in range(6) if card != own)
                state = (
                    query.public_state.instantiate(own, other)
                    if actor == 0
                    else query.public_state.instantiate(other, own)
                )
                row = policy[state.information_state_key(actor)]
                independent_policy_value = math.fsum(
                    row[action] * independent[action][own] for action in actions
                )
                self.assertAlmostEqual(
                    cutoff.policy_values[own], independent_policy_value, places=11
                )
                for action in actions:
                    self.assertAlmostEqual(
                        cutoff.regrets[action][own],
                        independent[action][own] - independent_policy_value,
                        places=11,
                    )

        awaiting = LeducState(private_cards=(0, 2)).child(CHECK).child(CHECK)
        chance_public = LeducPublicState.from_state(awaiting)
        chance_query = ToyRangeQuery(chance_public, (uniform, uniform))
        chance_result = exact_toy_cfv(chance_query, policy)
        concrete = chance_public.instantiate(0, 2)
        self.assertEqual(concrete.current_player, CHANCE_PLAYER)
        independent_pair = math.fsum(
            probability * _independent_value(concrete.child(action), policy)[0]
            for action, probability in concrete.chance_outcomes()
        )
        self.assertAlmostEqual(
            chance_result.pair_utility0[0][2], independent_pair, places=12
        )


class PhysicalHUNLMicroOracleTest(unittest.TestCase):
    def test_query_rejects_locally_plausible_but_common_unreachable_public_state(self):
        public = PublicHUNLState.from_common_state(_new_hand())
        forged = replace(
            public,
            legal_action_mask=(False, True, False, True, False, True, True, True),
        )
        ranges = (
            uniform_reach_range((), support_indices=(COMBO_TO_INDEX[FIRST_HAND],)),
            uniform_reach_range((), support_indices=(COMBO_TO_INDEX[SECOND_HAND],)),
        )
        with self.assertRaisesRegex(ValueError, "Common replay|public fields differ"):
            RangeCFVQuery(forged, ranges)

    def test_query_canonicalizes_negative_zero_reach_identity(self):
        public = PublicHUNLState.from_common_state(_new_hand())
        ranges = (
            uniform_reach_range((), support_indices=(COMBO_TO_INDEX[FIRST_HAND],)),
            uniform_reach_range((), support_indices=(COMBO_TO_INDEX[SECOND_HAND],)),
        )
        changed = list(ranges[0])
        zero_index = next(index for index, value in enumerate(changed) if value == 0.0)
        changed[zero_index] = -0.0
        positive = RangeCFVQuery(public, ranges)
        negative = RangeCFVQuery(public, (tuple(changed), ranges[1]))
        self.assertEqual(math.copysign(1.0, negative.private_ranges[0][zero_index]), 1.0)
        self.assertEqual(negative.digest, positive.digest)

        mutated_ranges = list(positive.private_ranges[0])
        support_index = COMBO_TO_INDEX[FIRST_HAND]
        mutated_ranges[support_index] = 1
        object.__setattr__(
            positive,
            "private_ranges",
            (tuple(mutated_ranges), positive.private_ranges[1]),
        )
        with self.assertRaisesRegex(ValueError, "canonical representation"):
            positive.assert_authoritative()

    def test_query_rejects_object_level_numeric_type_public_state_mutation(self):
        public = PublicHUNLState.from_common_state(_new_hand())
        object.__setattr__(
            public,
            "stacks_bb",
            (public.stacks_bb[0], int(public.stacks_bb[1])),
        )
        ranges = (
            uniform_reach_range((), support_indices=(COMBO_TO_INDEX[FIRST_HAND],)),
            uniform_reach_range((), support_indices=(COMBO_TO_INDEX[SECOND_HAND],)),
        )
        with self.assertRaisesRegex(ValueError, "public fields differ"):
            RangeCFVQuery(public, ranges)

    def test_public_projection_has_no_private_deal_and_is_hole_invariant(self):
        first = _to_river(_new_hand())
        second = _to_river(_new_hand(ALTERNATE_HOLES))
        projected_first = PublicHUNLState.from_common_state(first)
        projected_second = PublicHUNLState.from_common_state(second)
        self.assertEqual(projected_first.to_payload(), projected_second.to_payload())
        self.assertEqual(projected_first.digest, projected_second.digest)
        self.assertEqual(projected_first.small_blind_player, first.small_blind)
        self.assertNotIn("hole", repr(projected_first.to_payload()).lower())
        boundary = _new_hand().apply_action(Action(ActionKind.CALL))
        explicit_close = boundary.apply_action(Action(ActionKind.CHECK))
        inferred_close = boundary.apply_action(
            Action(ActionKind.CHECK), inferred_from_boundary=True
        )
        self.assertNotEqual(
            explicit_close.hand_history[-1].inferred_from_boundary,
            inferred_close.hand_history[-1].inferred_from_boundary,
        )
        self.assertEqual(
            PublicHUNLState.from_common_state(explicit_close).to_payload(),
            PublicHUNLState.from_common_state(inferred_close).to_payload(),
        )
        ranges = (
            uniform_reach_range(BOARD, support_indices=(COMBO_TO_INDEX[FIRST_HAND],)),
            uniform_reach_range(BOARD, support_indices=(COMBO_TO_INDEX[SECOND_HAND],)),
        )
        query0 = RangeCFVQuery(projected_first, ranges)
        query1 = RangeCFVQuery(projected_second, ranges)
        self.assertEqual(query0.digest, query1.digest)
        deterministic0 = exact_pairwise_cfv(
            query0,
            lambda first_index, second_index: float(first_index - second_index),
            provider_id="route-b-m5-private-invariance-oracle-v1",
        )
        deterministic1 = exact_pairwise_cfv(
            query1,
            lambda first_index, second_index: float(first_index - second_index),
            provider_id="route-b-m5-private-invariance-oracle-v1",
        )
        self.assertEqual(deterministic0.to_payload(), deterministic1.to_payload())
        self.assertEqual(deterministic0.digest, deterministic1.digest)
        with self.assertRaises(TypeError):
            RangeCFVQuery(
                public_state=projected_first,
                private_ranges=ranges,
                sampled_private_deal=(FIRST_HAND, SECOND_HAND),
            )

    def test_fold_and_showdown_are_exact_masked_physical_cfvs(self):
        folded = _new_hand().apply_action(Action(ActionKind.FOLD))
        fold_query = _query(folded)
        fold_result = exact_fold_cfv(fold_query)
        first_index = COMBO_TO_INDEX[FIRST_HAND]
        second_index = COMBO_TO_INDEX[SECOND_HAND]
        self.assertEqual(fold_result.values[0][first_index], -0.5)
        self.assertEqual(fold_result.values[1][second_index], 0.5)
        zero_own_index = COMBO_TO_INDEX[(46, 47)]
        self.assertEqual(fold_query.private_ranges[0][zero_own_index], 0.0)
        self.assertTrue(fold_result.valid_masks[0][zero_own_index])
        self.assertEqual(fold_result.values[0][zero_own_index], -0.5)
        self.assertLess(abs(fold_result.raw_zero_sum_residual_bb), 1e-12)

        showdown = _close_checked_street(_to_river(_new_hand()))
        showdown_query = _query(showdown)
        showdown_result = exact_showdown_cfv(showdown_query)
        comparison = compare_hands(FIRST_HAND + BOARD, SECOND_HAND + BOARD)
        expected = float((comparison > 0) - (comparison < 0))
        self.assertEqual(showdown_result.values[0][first_index], expected)
        self.assertEqual(showdown_result.values[1][second_index], -expected)
        board_blocked = COMBO_TO_INDEX[(BOARD[0], 1)]
        self.assertFalse(showdown_result.valid_masks[0][board_blocked])
        self.assertEqual(showdown_result.values[0][board_blocked], 0.0)

    def test_small_blind_player_one_fold_uses_fixed_player_payoffs(self):
        state = NationalGameState.new_hand(
            1,
            small_blind=1,
            hole_cards=(FIRST_HAND, SECOND_HAND),
        ).apply_action(Action(ActionKind.FOLD))
        result = exact_fold_cfv(_query(state))
        self.assertEqual(result.values[0][COMBO_TO_INDEX[FIRST_HAND]], 0.5)
        self.assertEqual(result.values[1][COMBO_TO_INDEX[SECOND_HAND]], -0.5)

    def test_fold_dense_ranges_keep_raw_blocker_mass_not_posterior_normalization(self):
        folded = _new_hand().apply_action(Action(ActionKind.FOLD))
        public = PublicHUNLState.from_common_state(folded)
        uniform = uniform_reach_range(public.board_card_ids)
        query = RangeCFVQuery(public, (uniform, uniform))
        result = exact_fold_cfv(query)
        compatible_mass = math.comb(50, 2) / math.comb(52, 2)
        self.assertAlmostEqual(
            result.values[0][COMBO_TO_INDEX[FIRST_HAND]],
            -0.5 * compatible_mass,
            places=12,
        )
        self.assertAlmostEqual(
            result.values[1][COMBO_TO_INDEX[SECOND_HAND]],
            0.5 * compatible_mass,
            places=12,
        )
        self.assertNotAlmostEqual(
            result.values[0][COMBO_TO_INDEX[FIRST_HAND]],
            -0.5,
            places=6,
        )
        self.assertLess(abs(result.raw_zero_sum_residual_bb), 1e-12)

    def test_river_one_decision_uses_actor_hand_policy_and_past_contributions(self):
        facing = _to_river(_new_hand()).apply_action(Action(ActionKind.RAISE, 100))
        query = _query(facing)
        first_index = COMBO_TO_INDEX[FIRST_HAND]
        second_index = COMBO_TO_INDEX[SECOND_HAND]
        probabilities = [0.0] * 1326
        probabilities[first_index] = 0.25
        result = exact_river_call_or_fold_cfv(query, tuple(probabilities))
        self.assertGreater(compare_hands(FIRST_HAND + BOARD, SECOND_HAND + BOARD), 0)
        expected = 0.75 * -1.0 + 0.25 * 2.0
        self.assertAlmostEqual(result.values[0][first_index], expected, places=12)
        self.assertAlmostEqual(result.values[1][second_index], -expected, places=12)
        self.assertLess(abs(result.raw_zero_sum_residual_bb), 1e-12)

    def test_river_actor_one_policy_indexes_player_one_range(self):
        facing = _to_river(_new_hand()).apply_action(Action(ActionKind.CHECK))
        facing = facing.apply_action(Action(ActionKind.RAISE, 100))
        query = _query(facing)
        self.assertEqual(query.public_state.actor, 1)
        first_index = COMBO_TO_INDEX[FIRST_HAND]
        second_index = COMBO_TO_INDEX[SECOND_HAND]
        probabilities = [0.0] * 1326
        probabilities[first_index] = 1.0
        probabilities[second_index] = 0.25
        result = exact_river_call_or_fold_cfv(query, tuple(probabilities))
        expected = 0.75 * 1.0 + 0.25 * 2.0
        self.assertAlmostEqual(result.values[0][first_index], expected, places=12)
        self.assertAlmostEqual(result.values[1][second_index], -expected, places=12)

    def test_turn_allin_enumerates_exactly_44_conditioned_rivers(self):
        state = _to_turn(_new_hand())
        state = state.apply_action(Action(ActionKind.ALLIN))
        state = state.apply_action(Action(ActionKind.CALL))
        query = _query(state)
        result = exact_turn_allin_runout_cfv(query)
        first_index = COMBO_TO_INDEX[FIRST_HAND]
        second_index = COMBO_TO_INDEX[SECOND_HAND]
        used = set(BOARD[:4] + FIRST_HAND + SECOND_HAND)
        rivers = tuple(card for card in range(52) if card not in used)
        self.assertEqual(len(rivers), 44)
        expected = math.fsum(
            float(
                (compare_hands(FIRST_HAND + BOARD[:4] + (river,), SECOND_HAND + BOARD[:4] + (river,)) > 0)
                - (compare_hands(FIRST_HAND + BOARD[:4] + (river,), SECOND_HAND + BOARD[:4] + (river,)) < 0)
            )
            * 200.0
            for river in rivers
        ) / 44.0
        self.assertAlmostEqual(result.values[0][first_index], expected, places=10)
        self.assertAlmostEqual(result.values[1][second_index], -expected, places=10)
        self.assertLess(abs(result.raw_zero_sum_residual_bb), 1e-10)

    def test_terminal_kind_confusion_is_rejected(self):
        river_fold = _to_river(_new_hand()).apply_action(
            Action(ActionKind.RAISE, 100)
        ).apply_action(Action(ActionKind.FOLD))
        river_query = _query(river_fold)
        correct_fold = exact_fold_cfv(river_query)
        self.assertEqual(
            correct_fold.values[0][COMBO_TO_INDEX[FIRST_HAND]],
            -1.0,
        )
        with self.assertRaisesRegex(ValueError, "fold"):
            exact_showdown_cfv(river_query)

        turn_fold = _to_turn(_new_hand()).apply_action(Action(ActionKind.ALLIN))
        turn_fold = turn_fold.apply_action(Action(ActionKind.FOLD))
        turn_query = _query(turn_fold)
        self.assertEqual(
            exact_fold_cfv(turn_query).values[0][COMBO_TO_INDEX[FIRST_HAND]],
            -1.0,
        )
        with self.assertRaisesRegex(ValueError, "runout|all-in"):
            exact_turn_allin_runout_cfv(turn_query)


class PublicRangeLeafConsumerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fold_query = _query(_new_hand().apply_action(Action(ActionKind.FOLD)))
        source_root = Path(__file__).parents[1]
        cls.fallback = make_exact_oracle_contract(
            provider_id=FOLD_PROVIDER_ID,
            evaluator=exact_fold_cfv,
            provider_source_paths=(
                source_root / "cfv" / "hunl_micro_oracle.py",
                source_root / "cfv" / "pairwise.py",
                source_root / "cfv" / "semantics.py",
                source_root / "cfv" / "ranges.py",
                source_root / "cfv" / "public_state.py",
                source_root / "cfv" / "combo_index.py",
            ),
        )
        cls.invalid = make_exact_oracle_contract(
            provider_id="route-b-m5-invalid-primary-v1",
            evaluator=_invalid_fold_provider,
            provider_source_paths=(Path(__file__),),
        )

    def test_contract_is_sealed_and_primary_receipt_is_content_bound(self):
        with self.assertRaisesRegex(TypeError, "sealed"):
            RangeCFVLeafContract()
        result = self.fallback.evaluate(self.fold_query)
        self.assertEqual(result.provider_id, FOLD_PROVIDER_ID)
        receipt = self.fallback.receipt
        self.assertEqual(receipt["input"], "public_state_plus_two_complete_reach_ranges")
        self.assertEqual(len(receipt["provider_sources_sha256"]), 64)
        tampered = replace(
            result,
            raw_zero_sum_residual_bb=result.raw_zero_sum_residual_bb + 1.0,
        )
        with self.assertRaisesRegex(ValueError, "residual diagnostics"):
            tampered.validate_against(self.fold_query)

    def test_explicit_fallback_telemetry_and_formal_no_fallback(self):
        consumer = PublicRangeLeafConsumer(
            primary=self.invalid,
            fallback=self.fallback,
            formal_require_primary=False,
        )
        evaluation = consumer.evaluate(self.fold_query)
        self.assertTrue(evaluation.receipt.fallback_used)
        self.assertTrue(evaluation.receipt.primary_invalid)
        self.assertFalse(evaluation.receipt.model_used)
        self.assertIsNotNone(evaluation.receipt.primary_error_sha256)
        formal = PublicRangeLeafConsumer(
            primary=self.invalid,
            fallback=self.fallback,
            formal_require_primary=True,
            expected_primary_contract_sha256=self.invalid.digest,
        )
        with self.assertRaises(PrimaryLeafUnavailable):
            formal.evaluate(self.fold_query)
        missing = PublicRangeLeafConsumer(
            primary=None,
            fallback=self.fallback,
            formal_require_primary=True,
            expected_primary_contract_sha256=self.fallback.digest,
        )
        with self.assertRaises(PrimaryLeafUnavailable):
            missing.evaluate(self.fold_query)
        wrong_digest = PublicRangeLeafConsumer(
            primary=self.fallback,
            fallback=self.fallback,
            formal_require_primary=True,
            expected_primary_contract_sha256="0" * 64,
        )
        with self.assertRaisesRegex(PrimaryLeafUnavailable, "expected"):
            wrong_digest.evaluate(self.fold_query)
        formal_neural = PublicRangeLeafConsumer(
            primary=self.fallback,
            fallback=self.fallback,
            formal_require_primary=True,
            expected_primary_contract_sha256=self.fallback.digest,
            formal_require_neural_model=True,
        )
        with self.assertRaisesRegex(PrimaryLeafUnavailable, "neural_model"):
            formal_neural.evaluate(self.fold_query)

    def test_evaluator_replacement_and_nonzero_exact_provider_are_rejected(self):
        source_root = Path(__file__).parents[1]
        attack = make_exact_oracle_contract(
            provider_id=FOLD_PROVIDER_ID,
            evaluator=exact_fold_cfv,
            provider_source_paths=(
                source_root / "cfv" / "hunl_micro_oracle.py",
                source_root / "cfv" / "pairwise.py",
                source_root / "cfv" / "semantics.py",
                source_root / "cfv" / "ranges.py",
                source_root / "cfv" / "public_state.py",
                source_root / "cfv" / "combo_index.py",
            ),
        )
        original_digest = attack.digest
        object.__setattr__(attack, "_evaluator", _invalid_fold_provider)
        self.assertEqual(attack.digest, original_digest)
        with self.assertRaisesRegex(ValueError, "symbol|code object|source"):
            attack.evaluate(self.fold_query)

        nonzero = make_exact_oracle_contract(
            provider_id="route-b-m5-nonzero-exact-v1",
            evaluator=_nonzero_exact_provider,
            provider_source_paths=(Path(__file__),),
        )
        with self.assertRaisesRegex(ValueError, "zero-sum threshold"):
            nonzero.evaluate(self.fold_query)

    def test_runtime_helper_replacement_and_incomplete_requested_closure_are_rejected(self):
        automatic = make_exact_oracle_contract(
            provider_id=FOLD_PROVIDER_ID,
            evaluator=exact_fold_cfv,
            provider_source_paths=(),
        )
        self.assertIn(
            "bots/research_native_lab/cfr_neural_search_m5/cfv/pairwise.py",
            automatic.receipt["provider_sources"],
        )
        original = hunl_micro_oracle._fold_utility0
        try:
            hunl_micro_oracle._fold_utility0 = lambda state, folder: 0.0
            with self.assertRaisesRegex(ValueError, "runtime helper bindings"):
                automatic.evaluate(self.fold_query)
        finally:
            hunl_micro_oracle._fold_utility0 = original
        self.assertEqual(
            automatic.evaluate(self.fold_query).values[0][COMBO_TO_INDEX[FIRST_HAND]],
            -0.5,
        )

        frozen_manifest = range_cfv_contract._runtime_dependency_manifest(
            exact_fold_cfv
        )
        original_builder = range_cfv_contract._runtime_dependency_manifest
        try:
            hunl_micro_oracle._fold_utility0 = lambda state, folder: 0.0
            range_cfv_contract._runtime_dependency_manifest = (
                lambda evaluator: frozen_manifest
            )
            with self.assertRaisesRegex(
                ValueError,
                "verifier manifest builder|runtime helper bindings",
            ):
                automatic.evaluate(self.fold_query)
        finally:
            range_cfv_contract._runtime_dependency_manifest = original_builder
            hunl_micro_oracle._fold_utility0 = original

        try:
            hunl_micro_oracle._fold_utility0 = lambda state, folder: 0.0
            with self.assertRaisesRegex(ValueError, "known exact provider"):
                make_exact_oracle_contract(
                    provider_id=FOLD_PROVIDER_ID,
                    evaluator=exact_fold_cfv,
                    provider_source_paths=(),
                )
        finally:
            hunl_micro_oracle._fold_utility0 = original
        with self.assertRaisesRegex(ValueError, "known exact provider"):
            make_exact_oracle_contract(
                provider_id=FOLD_PROVIDER_ID,
                evaluator=_invalid_fold_provider,
                provider_source_paths=(Path(__file__),),
            )

        original_fsum = hunl_micro_oracle.math.fsum
        try:
            hunl_micro_oracle.math.fsum = lambda values: 0.0
            with self.assertRaisesRegex(
                ValueError,
                "verifier manifest builder|runtime helper bindings",
            ):
                automatic.evaluate(self.fold_query)
        finally:
            hunl_micro_oracle.math.fsum = original_fsum

        turn_state = _to_turn(_new_hand()).apply_action(Action(ActionKind.ALLIN))
        turn_query = _query(turn_state.apply_action(Action(ActionKind.CALL)))
        turn_contract = make_exact_oracle_contract(
            provider_id="route-b-m5-turn-nested-helper-test-v1",
            evaluator=exact_turn_allin_runout_cfv,
            provider_source_paths=(),
        )
        original_showdown = hunl_micro_oracle._showdown_utility0
        try:
            hunl_micro_oracle._showdown_utility0 = lambda *arguments, **keywords: 0.0
            with self.assertRaisesRegex(ValueError, "runtime helper bindings"):
                turn_contract.evaluate(turn_query)
        finally:
            hunl_micro_oracle._showdown_utility0 = original_showdown

    def test_pairwise_consumer_never_uses_sampled_private_scalar(self):
        provider = "route-b-m5-zero-pair-oracle-v1"
        result = exact_pairwise_cfv(
            self.fold_query,
            lambda first, second: 0.0,
            provider_id=provider,
        )
        self.assertEqual(result.query_sha256, self.fold_query.digest)
        self.assertEqual(result.provider_id, provider)
        self.assertEqual(result.values[0][COMBO_TO_INDEX[FIRST_HAND]], 0.0)

        masks = self.fold_query.valid_masks
        raw = tuple(
            tuple(1.0 if masks[player][index] else 0.0 for index in range(1326))
            for player in (0, 1)
        )
        deployed = (raw[0], tuple(-value for value in raw[1]))
        diagnostic = RangeCFVResult.create(
            self.fold_query,
            provider_id="route-b-m5-raw-residual-diagnostic-v1",
            raw_values=raw,
            deployed_values=deployed,
        )
        self.assertGreater(abs(diagnostic.raw_zero_sum_residual_bb), 1.0)
        self.assertLess(abs(diagnostic.deployed_zero_sum_residual_bb), 1e-12)


if __name__ == "__main__":
    unittest.main()
