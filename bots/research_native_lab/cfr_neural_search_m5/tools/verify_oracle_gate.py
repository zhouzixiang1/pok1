"""Machine-readable M5 prereq gate for exact oracles and public-range leaves."""

from __future__ import annotations

import json
import math
from dataclasses import replace

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
from bots.research_native_lab.cfr_neural_search.core.identity import payload_sha256
from bots.research_native_lab.cfr_neural_search.core.strict_io import stable_tree_manifest
from bots.research_native_lab.common_contracts import Action, ActionKind, NationalGameState
from bots.research_native_lab.common_contracts.cards import compare_hands

from ..cfv.combo_index import COMBO_TO_INDEX
from ..cfv import hunl_micro_oracle
from ..cfv.hunl_micro_oracle import (
    FOLD_PROVIDER_ID,
    exact_fold_cfv,
    exact_river_call_or_fold_cfv,
    exact_showdown_cfv,
    exact_turn_allin_runout_cfv,
)
from ..cfv.public_state import PublicHUNLState
from ..cfv.ranges import uniform_reach_range
from ..cfv.semantics import RangeCFVQuery
from ..cfv.toy_oracle import (
    KuhnPublicState,
    LeducPublicState,
    ToyRangeQuery,
    conditioned_profile_value0,
    exact_one_step_cutoff,
    exact_toy_cfv,
)
from ..core.contracts import M5_ROOT, REPOSITORY_ROOT, load_oracle_gate_contract
from ..solver.public_range_depth_limited import (
    PrimaryLeafUnavailable,
    PublicRangeLeafConsumer,
)
from ..solver import range_cfv_contract
from ..solver.range_cfv_contract import make_exact_oracle_contract
from .verify_foundation import verify_foundation


BOARD = (0, 21, 30, 39, 44)
FIRST_HAND = (48, 49)
SECOND_HAND = (4, 8)


def _tampered_fold_utility(state, folder):
    return 0.0


def _nonuniform_policy(game):
    result = {}
    for key, actions in information_state_action_schema(game).items():
        total = math.fsum(index + 1 for index in range(len(actions)))
        result[key] = {
            action: (index + 1) / total for index, action in enumerate(actions)
        }
    return result


def _independent_policy_value(state, policy, cache=None):
    if cache is None:
        cache = {}
    if state in cache:
        return cache[state]
    actor = state.current_player
    if actor == TERMINAL_PLAYER:
        value = state.returns()
    elif actor == CHANCE_PLAYER:
        outcomes = state.chance_outcomes()
        value = tuple(
            math.fsum(
                probability
                * _independent_policy_value(state.child(action), policy, cache)[player]
                for action, probability in outcomes
            )
            for player in (0, 1)
        )
    else:
        row = policy[state.information_state_key(actor)]
        value = tuple(
            math.fsum(
                row[action]
                * _independent_policy_value(state.child(action), policy, cache)[player]
                for action in state.legal_actions()
            )
            for player in (0, 1)
        )
    cache[state] = value
    return value


def _new_hand(
    holes: tuple[tuple[int, int], tuple[int, int]] = (FIRST_HAND, SECOND_HAND),
) -> NationalGameState:
    return NationalGameState.new_hand(1, small_blind=0, hole_cards=holes)


def _close_checked(state: NationalGameState) -> NationalGameState:
    return state.apply_action(Action(ActionKind.CHECK)).apply_action(
        Action(ActionKind.CALL)
    )


def _to_turn(state: NationalGameState) -> NationalGameState:
    state = state.apply_action(Action(ActionKind.CALL))
    state = state.apply_action(Action(ActionKind.CHECK))
    state = state.apply_chance(BOARD[:3])
    state = _close_checked(state).apply_chance(BOARD[3:4])
    return state


def _to_river(state: NationalGameState) -> NationalGameState:
    return _close_checked(_to_turn(state)).apply_chance(BOARD[4:5])


def _query(state: NationalGameState) -> RangeCFVQuery:
    public = PublicHUNLState.from_common_state(state)
    return RangeCFVQuery(
        public,
        (
            uniform_reach_range(
                public.board_card_ids,
                support_indices=(COMBO_TO_INDEX[FIRST_HAND],),
            ),
            uniform_reach_range(
                public.board_card_ids,
                support_indices=(COMBO_TO_INDEX[SECOND_HAND],),
            ),
        ),
    )


def verify_oracle_gate() -> dict[str, object]:
    foundation = verify_foundation()
    gate_contract = load_oracle_gate_contract()
    toy_tolerance = float(gate_contract["toy"]["root_value_abs_tolerance"])
    kuhn_query = ToyRangeQuery(
        KuhnPublicState(),
        ((1.0 / 3.0,) * 3, (1.0 / 3.0,) * 3),
    )
    kuhn_result = exact_toy_cfv(kuhn_query, {})
    kuhn_value = conditioned_profile_value0(kuhn_query, kuhn_result)
    kuhn_reference = expected_returns(KuhnPoker(), {})[0]
    if not math.isclose(kuhn_value, kuhn_reference, rel_tol=0.0, abs_tol=toy_tolerance):
        raise ValueError("Kuhn range CFV differs from exact root value")
    kuhn_cutoff = exact_one_step_cutoff(kuhn_query, {})

    leduc_query = ToyRangeQuery(
        LeducPublicState.from_state(LeducState(private_cards=(0, 2))),
        ((1.0 / 6.0,) * 6, (1.0 / 6.0,) * 6),
    )
    leduc_result = exact_toy_cfv(leduc_query, {})
    leduc_value = conditioned_profile_value0(leduc_query, leduc_result)
    leduc_reference = expected_returns(LeducPoker(), {})[0]
    if not math.isclose(leduc_value, leduc_reference, rel_tol=0.0, abs_tol=toy_tolerance):
        raise ValueError("Leduc range CFV differs from exact root value")
    leduc_game = LeducPoker()
    leduc_policy = _nonuniform_policy(leduc_game)
    leduc_nonuniform = exact_toy_cfv(leduc_query, leduc_policy)
    leduc_nonuniform_value = conditioned_profile_value0(
        leduc_query, leduc_nonuniform
    )
    leduc_nonuniform_reference = expected_returns(leduc_game, leduc_policy)[0]
    if not math.isclose(
        leduc_nonuniform_value,
        leduc_nonuniform_reference,
        rel_tol=0.0,
        abs_tol=toy_tolerance,
    ):
        raise ValueError("nonuniform Leduc CFV differs from exact root value")
    leduc_cutoff0 = exact_one_step_cutoff(leduc_query, leduc_policy)
    leduc_actor1_query = ToyRangeQuery(
        LeducPublicState.from_state(LeducState(private_cards=(0, 2)).child(CHECK)),
        ((1.0 / 6.0,) * 6, (1.0 / 6.0,) * 6),
    )
    leduc_cutoff1 = exact_one_step_cutoff(leduc_actor1_query, leduc_policy)
    if leduc_cutoff0.actor != 0 or leduc_cutoff1.actor != 1:
        raise ValueError("Leduc one-step cutoff did not cover both actors")
    awaiting_public = LeducState(private_cards=(0, 2)).child(CHECK).child(CHECK)
    chance_query = ToyRangeQuery(
        LeducPublicState.from_state(awaiting_public),
        ((1.0 / 6.0,) * 6, (1.0 / 6.0,) * 6),
    )
    chance_result = exact_toy_cfv(chance_query, leduc_policy)
    concrete_chance = chance_query.public_state.instantiate(0, 2)
    independent_chance = math.fsum(
        probability
        * _independent_policy_value(concrete_chance.child(action), leduc_policy)[0]
        for action, probability in concrete_chance.chance_outcomes()
    )
    if not math.isclose(
        chance_result.pair_utility0[0][2],
        independent_chance,
        rel_tol=0.0,
        abs_tol=toy_tolerance,
    ):
        raise ValueError("Leduc future public chance differs from independent enumeration")

    root_query = _query(_new_hand())
    forged_public = replace(
        root_query.public_state,
        legal_action_mask=(False, True, False, True, False, True, True, True),
    )
    try:
        RangeCFVQuery(forged_public, root_query.private_ranges)
    except ValueError:
        forged_public_state_rejected = True
    else:
        raise ValueError("CFV query accepted a Common-unreachable public state")

    fold_query = _query(_new_hand().apply_action(Action(ActionKind.FOLD)))
    fold_result = exact_fold_cfv(fold_query)
    small_blind_one_fold = NationalGameState.new_hand(
        1,
        small_blind=1,
        hole_cards=(FIRST_HAND, SECOND_HAND),
    ).apply_action(Action(ActionKind.FOLD))
    small_blind_one_result = exact_fold_cfv(_query(small_blind_one_fold))
    if small_blind_one_result.values[0][COMBO_TO_INDEX[FIRST_HAND]] != 0.5:
        raise ValueError("small-blind-player=1 fold payoff changed fixed player identity")
    showdown_state = _close_checked(_to_river(_new_hand()))
    showdown_result = exact_showdown_cfv(_query(showdown_state))
    river_state = _to_river(_new_hand()).apply_action(Action(ActionKind.RAISE, 100))
    river_query = _query(river_state)
    calls = [0.0] * 1326
    calls[COMBO_TO_INDEX[FIRST_HAND]] = 0.25
    river_result = exact_river_call_or_fold_cfv(river_query, tuple(calls))
    river_actor_one_state = _to_river(_new_hand()).apply_action(
        Action(ActionKind.CHECK)
    ).apply_action(Action(ActionKind.RAISE, 100))
    river_actor_one_query = _query(river_actor_one_state)
    actor_one_calls = [0.0] * 1326
    actor_one_calls[COMBO_TO_INDEX[FIRST_HAND]] = 1.0
    actor_one_calls[COMBO_TO_INDEX[SECOND_HAND]] = 0.25
    river_actor_one_result = exact_river_call_or_fold_cfv(
        river_actor_one_query,
        tuple(actor_one_calls),
    )
    if not math.isclose(
        river_actor_one_result.values[0][COMBO_TO_INDEX[FIRST_HAND]],
        1.25,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError("river actor-one policy did not index player-one range")

    turn_state = _to_turn(_new_hand()).apply_action(Action(ActionKind.ALLIN))
    turn_state = turn_state.apply_action(Action(ActionKind.CALL))
    turn_query = _query(turn_state)
    turn_result = exact_turn_allin_runout_cfv(turn_query)
    used = set(BOARD[:4] + FIRST_HAND + SECOND_HAND)
    rivers = tuple(card for card in range(52) if card not in used)
    if len(rivers) != 44:
        raise ValueError("turn all-in fixture does not expose 44 conditioned rivers")
    expected_turn = math.fsum(
        (
            (compare_hands(FIRST_HAND + BOARD[:4] + (river,), SECOND_HAND + BOARD[:4] + (river,)) > 0)
            - (compare_hands(FIRST_HAND + BOARD[:4] + (river,), SECOND_HAND + BOARD[:4] + (river,)) < 0)
        )
        * 200.0
        for river in rivers
    ) / len(rivers)
    observed_turn = turn_result.values[0][COMBO_TO_INDEX[FIRST_HAND]]
    if not math.isclose(observed_turn, expected_turn, rel_tol=0.0, abs_tol=1e-10):
        raise ValueError("turn all-in CFV differs from physical 44-river enumeration")

    river_fold_query = _query(river_state.apply_action(Action(ActionKind.FOLD)))
    river_fold_result = exact_fold_cfv(river_fold_query)
    try:
        exact_showdown_cfv(river_fold_query)
    except ValueError:
        river_fold_as_showdown_rejected = True
    else:
        raise ValueError("showdown oracle accepted a Common fold terminal")
    turn_fold_state = _to_turn(_new_hand()).apply_action(Action(ActionKind.ALLIN))
    turn_fold_query = _query(turn_fold_state.apply_action(Action(ActionKind.FOLD)))
    turn_fold_result = exact_fold_cfv(turn_fold_query)
    try:
        exact_turn_allin_runout_cfv(turn_fold_query)
    except ValueError:
        turn_fold_as_runout_rejected = True
    else:
        raise ValueError("turn runout oracle accepted a Common fold terminal")

    source_paths = tuple(
        M5_ROOT / "cfv" / name
        for name in (
            "combo_index.py",
            "hunl_micro_oracle.py",
            "pairwise.py",
            "public_state.py",
            "ranges.py",
            "semantics.py",
        )
    )
    fold_contract = make_exact_oracle_contract(
        provider_id=FOLD_PROVIDER_ID,
        evaluator=exact_fold_cfv,
        provider_source_paths=(),
    )
    primary_evaluation = PublicRangeLeafConsumer(
        primary=fold_contract,
        fallback=fold_contract,
        formal_require_primary=True,
        expected_primary_contract_sha256=fold_contract.digest,
    ).evaluate(_query(_new_hand().apply_action(Action(ActionKind.FOLD))))
    if primary_evaluation.receipt.fallback_used:
        raise ValueError("formal public-range consumer unexpectedly used fallback")
    if not set(
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in source_paths
    ).issubset(fold_contract.receipt["provider_sources"]):
        raise ValueError("exact provider factory omitted its fixed source closure")
    original_fold_utility = hunl_micro_oracle._fold_utility0
    try:
        hunl_micro_oracle._fold_utility0 = _tampered_fold_utility
        try:
            fold_contract.evaluate(fold_query)
        except ValueError:
            runtime_helper_replacement_rejected = True
        else:
            raise ValueError("sealed exact provider accepted a replaced runtime helper")
    finally:
        hunl_micro_oracle._fold_utility0 = original_fold_utility
    frozen_runtime_manifest = range_cfv_contract._runtime_dependency_manifest(
        exact_fold_cfv
    )
    original_manifest_builder = range_cfv_contract._runtime_dependency_manifest
    try:
        hunl_micro_oracle._fold_utility0 = _tampered_fold_utility
        range_cfv_contract._runtime_dependency_manifest = (
            lambda evaluator: frozen_runtime_manifest
        )
        try:
            fold_contract.evaluate(fold_query)
        except ValueError:
            second_order_manifest_builder_replacement_rejected = True
        else:
            raise ValueError("sealed provider accepted a replaced verifier builder")
    finally:
        range_cfv_contract._runtime_dependency_manifest = original_manifest_builder
        hunl_micro_oracle._fold_utility0 = original_fold_utility
    try:
        hunl_micro_oracle._fold_utility0 = _tampered_fold_utility
        try:
            make_exact_oracle_contract(
                provider_id=FOLD_PROVIDER_ID,
                evaluator=exact_fold_cfv,
                provider_source_paths=(),
            )
        except ValueError:
            preconstruction_helper_replacement_rejected = True
        else:
            raise ValueError("known provider pin accepted a preconstruction helper replacement")
    finally:
        hunl_micro_oracle._fold_utility0 = original_fold_utility
    try:
        PublicRangeLeafConsumer(
            primary=None,
            fallback=fold_contract,
            formal_require_primary=True,
            expected_primary_contract_sha256=fold_contract.digest,
        ).evaluate(_query(_new_hand().apply_action(Action(ActionKind.FOLD))))
    except PrimaryLeafUnavailable:
        formal_missing_primary_rejected = True
    else:
        raise ValueError("formal public-range consumer accepted a missing primary")
    try:
        PublicRangeLeafConsumer(
            primary=fold_contract,
            fallback=fold_contract,
            formal_require_primary=True,
            expected_primary_contract_sha256=fold_contract.digest,
            formal_require_neural_model=True,
        ).evaluate(_query(_new_hand().apply_action(Action(ActionKind.FOLD))))
    except PrimaryLeafUnavailable:
        formal_exact_as_model_rejected = True
    else:
        raise ValueError("formal neural mode accepted an exact-oracle primary")

    source_manifest = stable_tree_manifest(M5_ROOT)
    return {
        "schema": "route-b-m5-oracle-consumer-gate-v1",
        "status": "passed_no_labels_no_training",
        "foundation_contract_sha256": foundation["foundation_contract_sha256"],
        "oracle_gate_contract_schema": gate_contract["schema"],
        "source_file_count": len(source_manifest),
        "source_manifest_sha256": payload_sha256({"files": source_manifest}),
        "toy": {
            "kuhn_conditioned_root_value": kuhn_value,
            "kuhn_exact_reference": kuhn_reference,
            "kuhn_raw_zero_sum_residual": kuhn_result.zero_sum_residual,
            "kuhn_cutoff_actor": kuhn_cutoff.actor,
            "kuhn_cutoff_actions": [str(action) for action in kuhn_cutoff.actions],
            "leduc_conditioned_root_value": leduc_value,
            "leduc_exact_reference": leduc_reference,
            "leduc_raw_zero_sum_residual": leduc_result.zero_sum_residual,
            "leduc_nonuniform_root_value": leduc_nonuniform_value,
            "leduc_nonuniform_exact_reference": leduc_nonuniform_reference,
            "leduc_cutoff_actors": [leduc_cutoff0.actor, leduc_cutoff1.actor],
            "leduc_chance_pair_value": chance_result.pair_utility0[0][2],
            "leduc_chance_independent_value": independent_chance,
        },
        "hunl": {
            "forged_public_state_rejected": forged_public_state_rejected,
            "fold_result_sha256": fold_result.digest,
            "small_blind_one_fold_result_sha256": small_blind_one_result.digest,
            "showdown_result_sha256": showdown_result.digest,
            "river_one_decision_result_sha256": river_result.digest,
            "river_actor_one_result_sha256": river_actor_one_result.digest,
            "turn_allin_result_sha256": turn_result.digest,
            "river_fold_result_sha256": river_fold_result.digest,
            "turn_fold_result_sha256": turn_fold_result.digest,
            "river_fold_as_showdown_rejected": river_fold_as_showdown_rejected,
            "turn_fold_as_runout_rejected": turn_fold_as_runout_rejected,
            "turn_conditioned_river_count": len(rivers),
            "max_raw_zero_sum_residual_bb": max(
                abs(result.raw_zero_sum_residual_bb)
                for result in (fold_result, showdown_result, river_result, turn_result)
            ),
        },
        "leaf_contract": {
            "provider_id": fold_contract.provider_id,
            "contract_sha256": fold_contract.digest,
            "formal_fallback_used": primary_evaluation.receipt.fallback_used,
            "formal_missing_primary_rejected": formal_missing_primary_rejected,
            "formal_exact_as_model_rejected": formal_exact_as_model_rejected,
            "runtime_helper_replacement_rejected": runtime_helper_replacement_rejected,
            "second_order_manifest_builder_replacement_rejected": (
                second_order_manifest_builder_replacement_rejected
            ),
            "preconstruction_helper_replacement_rejected": (
                preconstruction_helper_replacement_rejected
            ),
        },
    }


def main() -> int:
    print(json.dumps(verify_oracle_gate(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
