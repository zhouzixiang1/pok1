from __future__ import annotations

import json
import math
from math import fsum

import pytest

from ...common_contracts.actions import Action, ActionKind
from ...common_contracts.cards import all_hole_combinations
from ...common_contracts.constants import CONTRACT_VERSION
from ...common_contracts.national_state import NationalGameState

from ..rebel_like.hunl_pbs import (
    HUNL_COMBOS,
    HUNL_COMBO_COUNT,
    HUNL_COMBO_REGISTRY_SHA256,
    HUNLPublicActionSupport,
    HUNLReachFactorPublicBeliefState,
    build_public_action_support,
)
from ..decisionholdem_like.hunl_abstraction import preflop_class


def _preflop_state(
    *,
    hand_number: int = 1,
    holes: tuple[tuple[int, ...], tuple[int, ...]] = ((), ()),
    match_net_before: tuple[int, int] = (0, 0),
) -> NationalGameState:
    return NationalGameState.new_hand(
        hand_number,
        small_blind=0,
        hole_cards=holes,
        match_net_before=match_net_before,
    )


def _passive_to_next_street(state: NationalGameState) -> NationalGameState:
    if not state.street_actions:
        first = Action(ActionKind.CALL if state.street.value == "preflop" else ActionKind.CHECK)
        state = state.apply_action(first)
    closing = Action(
        ActionKind.CHECK if state.street.value == "preflop" else ActionKind.CALL
    )
    return state.apply_action(closing)


def _offtree_support(state: NationalGameState):
    observed = Action(ActionKind.RAISE, 733)
    support = build_public_action_support(
        state,
        (
            Action(ActionKind.FOLD),
            Action(ActionKind.CALL),
            Action(ActionKind.RAISE, 200),
            Action(ActionKind.ALLIN),
        ),
        observed_action=observed,
    )
    return support, observed


def _informative_policy(action_wires: tuple[str, ...]):
    rows = []
    for first, second in all_hole_combinations():
        ace = first // 4 == 12 or second // 4 == 12
        observed_probability = 0.8 if ace else 0.2
        remainder = (1.0 - observed_probability) / (len(action_wires) - 1)
        rows.append(
            {
                wire: observed_probability if wire == "raise 733" else remainder
                for wire in action_wires
            }
        )
    return tuple(rows)


def _sparse_factor(weights: dict[tuple[int, int], float]) -> tuple[float, ...]:
    return tuple(weights.get(combo, 0.0) for combo in HUNL_COMBOS)


def _reduced_joint(beta0, beta1):
    weights = {
        (first, second): beta0[first] * beta1[second]
        for first in range(3)
        for second in range(3)
        if first != second
    }
    normalizer = fsum(weights.values())
    return {deal: value / normalizer for deal, value in weights.items()}


def _reduced_projected(joint, player):
    return tuple(
        fsum(probability for deal, probability in joint.items() if deal[player] == card)
        for card in range(3)
    )


def test_factor_joint_matches_direct_bayes_reduced_deck_exhaustive() -> None:
    beta0 = (0.5, 1.0 / 3.0, 1.0 / 6.0)
    beta1 = (2.0 / 15.0, 1.0 / 3.0, 8.0 / 15.0)
    likelihood = (0.1, 0.5, 0.9)
    joint = _reduced_joint(beta0, beta1)
    projected0 = _reduced_projected(joint, 0)
    assert projected0 == pytest.approx(
        (0.590909090909, 0.303030303030, 0.106060606061), abs=1e-12
    )
    factor_normalizer = fsum(
        beta0[index] * likelihood[index] for index in range(3)
    )
    joint_evidence = fsum(
        projected0[index] * likelihood[index] for index in range(3)
    )
    assert factor_normalizer == pytest.approx(0.366666666667, abs=1e-12)
    assert joint_evidence == pytest.approx(0.306060606061, abs=1e-12)
    assert factor_normalizer != pytest.approx(joint_evidence)

    updated_beta0 = tuple(
        beta0[index] * likelihood[index] / factor_normalizer
        for index in range(3)
    )
    rebuilt = _reduced_joint(updated_beta0, beta1)
    direct = {
        deal: probability * likelihood[deal[0]] / joint_evidence
        for deal, probability in joint.items()
    }
    assert rebuilt == pytest.approx(direct, abs=1e-15)
    assert _reduced_projected(rebuilt, 1) != pytest.approx(
        _reduced_projected(joint, 1)
    )


def test_factor_normalizer_is_not_joint_event_probability_blocker_counterexample() -> None:
    state = _preflop_state()
    base = HUNLReachFactorPublicBeliefState.from_state(state)
    blocked = (0, 1)
    compatible = (2, 3)
    factors = (
        _sparse_factor({blocked: 0.5, compatible: 0.5}),
        _sparse_factor({blocked: 1.0}),
    )
    pbs = HUNLReachFactorPublicBeliefState(base.public_state_json, factors)
    support, _ = _offtree_support(state)
    rows = []
    for combo in HUNL_COMBOS:
        observed = 1.0 if combo == blocked else 0.0
        rows.append(
            {
                wire: (
                    observed
                    if wire == "raise 733"
                    else 1.0 - observed
                    if wire == "call"
                    else 0.0
                )
                for wire in support.action_wires
            }
        )
    assert pbs.factor_action_normalizer(state, support, rows) == pytest.approx(0.5)
    assert pbs.action_probability(state, support, rows) == 0.0
    blocked_index = HUNL_COMBOS.index(blocked)
    assert pbs.positive_reach_mask(0)[blocked_index] is True
    assert pbs.label_valid_mask(0)[blocked_index] is False
    with pytest.raises(ValueError, match="zero/non-finite joint evidence"):
        pbs.observe_action(
            state, support, rows, belief_policy_kind="fixed_profile"
        )


def test_invalid_joint_support_and_joint_zero_evidence_fail_closed() -> None:
    base = HUNLReachFactorPublicBeliefState.from_state(_preflop_state())
    same = _sparse_factor({(0, 1): 1.0})
    with pytest.raises(ValueError, match="zero/inconsistent compatible joint"):
        HUNLReachFactorPublicBeliefState(
            base.public_state_json,
            (same, same),
        )


def test_hunl_pbs_is_public_only_and_ignores_holes_and_match_context() -> None:
    first = _preflop_state(
        hand_number=1,
        holes=((0, 1), (2, 3)),
        match_net_before=(0, 0),
    )
    second = _preflop_state(
        hand_number=70,
        holes=((48, 49), (50, 51)),
        match_net_before=(1234, -1234),
    )
    first_pbs = HUNLReachFactorPublicBeliefState.from_state(first)
    second_pbs = HUNLReachFactorPublicBeliefState.from_state(second)
    assert first_pbs == second_pbs
    assert first_pbs.public_pbs_state_id == second_pbs.public_pbs_state_id
    assert first_pbs.snapshot() == second_pbs.snapshot()
    def keys(value):
        if isinstance(value, dict):
            return set(value).union(*(keys(child) for child in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(child) for child in value))
        return set()

    snapshot_keys = keys(first_pbs.snapshot())
    for forbidden in (
        "hole_cards",
        "private_hand",
        "information_state_id",
        "observation_id",
        "full_state_id",
        "match_context_id",
        "match_net_before",
        "hand_number",
    ):
        assert forbidden not in snapshot_keys
    assert first_pbs.public_state["contract_version"] == CONTRACT_VERSION
    assert (
        first_pbs.network_input()["combo_registry_sha256"]
        == HUNL_COMBO_REGISTRY_SHA256
    )


def test_public_board_blockers_condition_both_1326_marginals() -> None:
    preflop = _preflop_state()
    pbs = HUNLReachFactorPublicBeliefState.from_state(preflop)
    assert HUNL_COMBO_COUNT == 1326
    assert pbs.board_legal_combo_count == 1326
    assert all(len(pbs.reach_factor_for(player)) == 1326 for player in (0, 1))
    assert pbs.compatible_ordered_joint_count == 1_624_350

    chance = _passive_to_next_street(preflop)
    flop = chance.apply_chance((0, 5, 10))
    pbs = pbs.observe_action(
        preflop,
        build_public_action_support(
            preflop,
            (Action(ActionKind.CALL),),
            observed_action=Action(ActionKind.CALL),
        ),
        tuple({"call": 1.0} for _ in range(HUNL_COMBO_COUNT)),
        belief_policy_kind="fixed_profile",
    )
    pbs = pbs.observe_action(
        preflop.apply_action(Action(ActionKind.CALL)),
        build_public_action_support(
            preflop.apply_action(Action(ActionKind.CALL)),
            (Action(ActionKind.CHECK),),
            observed_action=Action(ActionKind.CHECK),
        ),
        tuple({"check": 1.0} for _ in range(HUNL_COMBO_COUNT)),
        belief_policy_kind="fixed_profile",
    )
    pbs = pbs.observe_public_chance(chance, flop)
    assert pbs.board_legal_combo_count == 1176
    assert pbs.compatible_ordered_joint_count == 1_271_256
    for player in (0, 1):
        assert fsum(pbs.reach_factor_for(player)) == pytest.approx(1.0, abs=1e-14)
        assert all(
            probability == 0.0
            for combo, probability in zip(
                all_hole_combinations(), pbs.reach_factor_for(player)
            )
            if set(combo).intersection(flop.board)
        )

    turn_pending = _passive_to_next_street(flop)
    turn = turn_pending.apply_chance((15,))
    pbs = pbs.observe_action(
        flop,
        build_public_action_support(
            flop,
            (Action(ActionKind.CHECK),),
            observed_action=Action(ActionKind.CHECK),
        ),
        tuple({"check": 1.0} for _ in range(HUNL_COMBO_COUNT)),
        belief_policy_kind="fixed_profile",
    )
    second_actor = flop.apply_action(Action(ActionKind.CHECK))
    pbs = pbs.observe_action(
        second_actor,
        build_public_action_support(
            second_actor,
            (Action(ActionKind.CALL),),
            observed_action=Action(ActionKind.CALL),
        ),
        tuple({"call": 1.0} for _ in range(HUNL_COMBO_COUNT)),
        belief_policy_kind="fixed_profile",
    )
    pbs = pbs.observe_public_chance(turn_pending, turn)
    assert pbs.board_legal_combo_count == 1128
    assert pbs.compatible_ordered_joint_count == 1_167_480

    river_pending = _passive_to_next_street(turn)
    river = river_pending.apply_chance((20,))
    pbs = pbs.observe_action(
        turn,
        build_public_action_support(
            turn,
            (Action(ActionKind.CHECK),),
            observed_action=Action(ActionKind.CHECK),
        ),
        tuple({"check": 1.0} for _ in range(HUNL_COMBO_COUNT)),
        belief_policy_kind="fixed_profile",
    )
    second_actor = turn.apply_action(Action(ActionKind.CHECK))
    pbs = pbs.observe_action(
        second_actor,
        build_public_action_support(
            second_actor,
            (Action(ActionKind.CALL),),
            observed_action=Action(ActionKind.CALL),
        ),
        tuple({"call": 1.0} for _ in range(HUNL_COMBO_COUNT)),
        belief_policy_kind="fixed_profile",
    )
    pbs = pbs.observe_public_chance(river_pending, river)
    assert pbs.board_legal_combo_count == 1081
    assert pbs.compatible_ordered_joint_count == 1_070_190


def test_public_action_bayes_updates_only_actor_and_keeps_exact_offtree_raise() -> None:
    state = _preflop_state()
    pbs = HUNLReachFactorPublicBeliefState.from_state(state)
    support, observed = _offtree_support(state)
    policy = _informative_policy(support.action_wires)
    before_actor = pbs.reach_factor_for(0)
    before_other = pbs.reach_factor_for(1)
    projected_other = pbs.projected_marginal(1)
    evidence = fsum(
        before_actor[index] * policy[index]["raise 733"]
        for index in range(HUNL_COMBO_COUNT)
    )
    posterior = pbs.observe_action(
        state, support, policy, belief_policy_kind="fixed_profile"
    )
    assert posterior.reach_factor_for(1) == before_other
    assert posterior.reach_factor_for(0) == pytest.approx(
        tuple(
            before_actor[index] * policy[index]["raise 733"] / evidence
            for index in range(HUNL_COMBO_COUNT)
        ),
        abs=1e-15,
    )
    assert posterior.projected_marginal(1) != pytest.approx(projected_other)
    assert pbs.reach_factor_for(0) == before_actor  # immutable input
    history = posterior.public_state["hand_history"]
    assert history[-1]["kind"] == "raise"
    assert history[-1]["amount"] == 733
    assert support.exact_observed_raise_to == 733
    assert support.snapshot()["nearest_action_translation_used"] is False
    support.assert_bound(state, observed)
    with pytest.raises(ValueError, match="differs from the exact support token"):
        support.assert_bound(state, Action(ActionKind.RAISE, 700))


def test_hunl_pbs_updates_are_fail_closed_and_transactional() -> None:
    state = _preflop_state()
    pbs = HUNLReachFactorPublicBeliefState.from_state(state)
    support, _ = _offtree_support(state)
    before = pbs.snapshot()
    zero = tuple(
        {
            wire: (1.0 if wire == "call" else 0.0)
            for wire in support.action_wires
        }
        for _ in range(HUNL_COMBO_COUNT)
    )
    with pytest.raises(ValueError, match="zero/non-finite"):
        pbs.observe_action(
            state, support, zero, belief_policy_kind="fixed_profile"
        )
    assert pbs.snapshot() == before

    malformed = list(_informative_policy(support.action_wires))
    malformed[17] = dict(malformed[17])
    malformed[17].pop("fold")
    with pytest.raises(ValueError, match="does not match action support"):
        pbs.observe_action(
            state, support, malformed, belief_policy_kind="fixed_profile"
        )
    assert pbs.snapshot() == before

    nonfinite = list(_informative_policy(support.action_wires))
    nonfinite[19] = dict(nonfinite[19])
    nonfinite[19]["raise 733"] = math.inf
    with pytest.raises(ValueError, match="finite/non-negative"):
        pbs.observe_action(
            state, support, nonfinite, belief_policy_kind="fixed_profile"
        )
    assert pbs.snapshot() == before


def test_direct_payload_rejects_private_context_and_contract_drift() -> None:
    pbs = HUNLReachFactorPublicBeliefState.from_state(_preflop_state())
    payload = pbs.public_state
    payload["hole_cards"] = [[0, 1], []]
    with pytest.raises(ValueError, match="forbidden private/context"):
        HUNLReachFactorPublicBeliefState(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            pbs.reach_factors,
        )

    payload = pbs.public_state
    payload["contract_version"] = "drifted"
    with pytest.raises(ValueError, match="contract version"):
        HUNLReachFactorPublicBeliefState(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            pbs.reach_factors,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("seed", 7),
        ("timing", 0.3),
        ("outcome", "win"),
        ("payoff", 100),
        ("oppo_hands", [0, 1]),
        ("future_turn", 51),
        ("extra_field", "unknown"),
        ("terminal_reason", "showdown"),
    ),
)
def test_direct_payload_exact_common_schema_rejects_unknown_fields(
    field: str, value: object
) -> None:
    pbs = HUNLReachFactorPublicBeliefState.from_state(_preflop_state())
    payload = pbs.public_state
    payload[field] = value
    with pytest.raises(ValueError, match="fields differ|forbidden|non-canonical scalar"):
        HUNLReachFactorPublicBeliefState(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            pbs.reach_factors,
        )


def test_direct_payload_requires_strict_player_types_and_common_replay() -> None:
    pbs = HUNLReachFactorPublicBeliefState.from_state(
        NationalGameState.new_hand(1, small_blind=1)
    )
    payload = pbs.public_state
    payload["small_blind"] = True
    payload["actor"] = True
    with pytest.raises(ValueError, match="small blind"):
        HUNLReachFactorPublicBeliefState(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            pbs.reach_factors,
        )

    payload = pbs.public_state
    payload.update({"street": "flop", "board": [0, 0, 1]})
    with pytest.raises(ValueError, match="board"):
        HUNLReachFactorPublicBeliefState(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            pbs.reach_factors,
        )

    payload = pbs.public_state
    payload["hand_history"] = [
        {"actor": True, "kind": "call", "amount": None, "street": "preflop"}
    ]
    with pytest.raises(ValueError, match="action identity"):
        HUNLReachFactorPublicBeliefState(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            pbs.reach_factors,
        )


def test_uniform_from_state_rejects_midhand_silent_reset() -> None:
    midhand = _preflop_state().apply_action(Action(ActionKind.CALL))
    with pytest.raises(ValueError, match="true new-hand root"):
        HUNLReachFactorPublicBeliefState.from_state(midhand)


def test_pbs_identity_excludes_trace_but_provenance_binds_it_and_aliases() -> None:
    base = HUNLReachFactorPublicBeliefState.from_state(_preflop_state())
    external_trace: list[tuple[str, str, str]] = []
    first = HUNLReachFactorPublicBeliefState(
        base.public_state_json,
        base.reach_factors,
        external_trace,
    )
    external_trace.append(("fixed_profile", "0" * 64, "call"))
    assert first.belief_update_trace == ()
    second = HUNLReachFactorPublicBeliefState(
        base.public_state_json,
        base.reach_factors,
        (("fixed_profile", "0" * 64, "call"),),
    )
    assert first.public_pbs_state_id == second.public_pbs_state_id
    assert first.pbs_state_id == second.pbs_state_id
    assert first == second
    assert hash(first) == hash(second)
    assert first.network_input() == second.network_input()
    assert first.provenance_snapshot_sha256 != second.provenance_snapshot_sha256

    factor = list(base.reach_factor_for(0))
    epsilon = factor[1] / 2.0
    factor[0] += epsilon
    factor[1] -= epsilon
    different = HUNLReachFactorPublicBeliefState(
        base.public_state_json,
        (factor, base.reach_factor_for(1)),
    )
    assert different.public_pbs_state_id == base.public_pbs_state_id
    assert different.pbs_state_id != base.pbs_state_id

    wires = ["fold"]
    support = HUNLPublicActionSupport(
        base.public_pbs_state_id, wires, "fold", None
    )
    wires.append("call")
    assert support.action_wires == ("fold",)


def test_sha256_fields_are_strict_lowercase_hex() -> None:
    base = HUNLReachFactorPublicBeliefState.from_state(_preflop_state())
    with pytest.raises(ValueError, match="digest"):
        HUNLPublicActionSupport("A" * 64, ("fold",), "fold", None)
    with pytest.raises(ValueError, match="provenance trace"):
        HUNLReachFactorPublicBeliefState(
            base.public_state_json,
            base.reach_factors,
            (("fixed_profile", "G" * 64, "call"),),
        )


@pytest.mark.parametrize("player", (True, False, 2, -1))
def test_player_accessors_reject_bool_and_out_of_range(player: object) -> None:
    pbs = HUNLReachFactorPublicBeliefState.from_state(_preflop_state())
    for accessor in (
        pbs.reach_factor_for,
        pbs.projected_marginal,
        pbs.positive_reach_mask,
        pbs.label_valid_mask,
    ):
        with pytest.raises(ValueError, match="player must"):
            accessor(player)

def test_action_support_rejects_translated_or_excess_observation() -> None:
    state = _preflop_state()
    support, _ = _offtree_support(state)
    with pytest.raises(ValueError, match="translated or corrupted"):
        HUNLPublicActionSupport(
            support.public_pbs_state_id,
            support.action_wires,
            "raise 733",
            700,
        )
    with pytest.raises(ValueError, match="nine-action cap"):
        build_public_action_support(
            state,
            tuple(Action(ActionKind.RAISE, amount) for amount in range(200, 1800, 200))
            + (Action(ActionKind.CALL),),
            observed_action=Action(ActionKind.RAISE, 1900),
        )


def test_relayed_vs_proven_suppressed_close_same_pbs_exactly_once() -> None:
    root = _preflop_state()
    pbs = HUNLReachFactorPublicBeliefState.from_state(root)
    call_support = build_public_action_support(
        root,
        (Action(ActionKind.CALL),),
        observed_action=Action(ActionKind.CALL),
    )
    pbs = pbs.observe_action(
        root,
        call_support,
        tuple({"call": 1.0} for _ in HUNL_COMBOS),
        belief_policy_kind="fixed_profile",
    )
    before_close = root.apply_action(Action(ActionKind.CALL))
    relayed = before_close.apply_action(Action(ActionKind.CHECK))
    inferred, record = before_close.infer_omitted_closing_action()
    assert record.inferred_from_boundary is True
    close_support = build_public_action_support(
        before_close,
        (Action(ActionKind.CHECK),),
        observed_action=Action(ActionKind.CHECK),
    )
    closed = pbs.observe_action(
        before_close,
        close_support,
        tuple({"check": 1.0} for _ in HUNL_COMBOS),
        belief_policy_kind="fixed_profile",
    )
    closed.assert_matches(relayed)
    closed.assert_matches(inferred)
    assert relayed.hand_public_dict() == inferred.hand_public_dict()
    assert len(closed.belief_update_trace) == 2
    with pytest.raises(ValueError, match="stale"):
        pbs.observe_action(
            inferred,
            close_support,
            tuple({"check": 1.0} for _ in HUNL_COMBOS),
            belief_policy_kind="fixed_profile",
        )


def test_169_alias_with_different_blockers_cannot_share_range_label() -> None:
    state = _preflop_state()
    base = HUNLReachFactorPublicBeliefState.from_state(state)
    first_aks = (44, 48)
    second_aks = (45, 49)
    opponent = (0, 44)
    assert preflop_class(first_aks) == preflop_class(second_aks) == "AKs"
    pbs = HUNLReachFactorPublicBeliefState(
        base.public_state_json,
        (
            _sparse_factor({first_aks: 0.5, second_aks: 0.5}),
            _sparse_factor({opponent: 1.0}),
        ),
    )
    first_index = HUNL_COMBOS.index(first_aks)
    second_index = HUNL_COMBOS.index(second_aks)
    assert pbs.reach_factor_for(0)[first_index] == 0.5
    assert pbs.reach_factor_for(0)[second_index] == 0.5
    assert pbs.projected_marginal(0)[first_index] == 0.0
    assert pbs.projected_marginal(0)[second_index] == 1.0
    assert pbs.label_valid_mask(0)[first_index] is False
    assert pbs.label_valid_mask(0)[second_index] is True
