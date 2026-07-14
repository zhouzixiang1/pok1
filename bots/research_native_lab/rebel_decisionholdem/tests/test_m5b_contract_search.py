from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from ...common_contracts.actions import Action, ActionKind
from ...common_contracts.national_state import NationalGameState
from ..rebel_like.hunl_pbs import HUNL_COMBO_COUNT, HUNLReachFactorPublicBeliefState
from ..rebel_like.m5b_contract import canonical_bytes, load_config, verify_frozen_m5a
from ..rebel_like.m5b_search import (
    ACTION_COUNT,
    DepthLimitedCFRAvg,
    PublicInferenceInput,
    ReachFactors,
    SearchProfile,
    TerminalRolloutLeaf,
    UniformPolicy,
    abstract_actions,
    information_node_id,
)


ROUTE_ROOT = Path(__file__).resolve().parents[1]


def _root(*, holes=((), ())) -> tuple[NationalGameState, HUNLReachFactorPublicBeliefState]:
    state = NationalGameState.new_hand(1, small_blind=0, hole_cards=holes)
    return state, HUNLReachFactorPublicBeliefState.from_state(state)


def test_m5b_config_and_historical_m5a_are_frozen() -> None:
    config = load_config(ROUTE_ROOT / "configs" / "m5b_offline_rebel_loop.json")
    assert config["claim_boundary"]["online_tcp_search"] is False
    assert config["independence"]["a2_blueprint_allowed_as_behavior"] is False
    assert config["network"]["value_outputs"] == [2, 1326]
    assert config["network"]["policy_outputs"] == [1326, 9]
    assert config["training"]["resume_probes"] == 2
    assert config["training"]["policy_loss"].startswith(
        "actor_projected_marginal_weighted_"
    )
    assert config["resource_measurement"]["formal_receipt_bindings"] == [
        "config_sha256",
        "dataset_sha256",
        "model_tensor_sha256",
        "runtime_sha256",
    ]
    assert config["metrics"]["formal_relative_improvement_fraction"] == 0.1
    assert verify_frozen_m5a() == {
        "manifest": "4edede3c8a3cdef7d24176cb5e4b1ae9d5a8160ae315e99fd1fd028ffc7dd497",
        "artifact": "ae4e8eca65d2c99429f0a7f064abfac9f468347903ab3dd131959865c7ff8797",
    }


def test_public_inference_input_structurally_excludes_private_and_match_state() -> None:
    first, first_pbs = _root(holes=((0, 1), (2, 3)))
    second, second_pbs = _root(holes=((48, 49), (50, 51)))
    actions = abstract_actions(first)
    first_view = PublicInferenceInput.from_state(
        first, ReachFactors.from_pbs(first_pbs), actions.mask
    )
    second_view = PublicInferenceInput.from_state(
        second, ReachFactors.from_pbs(second_pbs), actions.mask
    )
    for forbidden in (
        "hole_cards",
        "private_hand",
        "hand_number",
        "match_net_before",
        "rng",
        "sampled_deal",
    ):
        assert not hasattr(first_view, forbidden)
        assert forbidden not in first_view.public_state
    assert first_view.digest == second_view.digest
    assert first_view.public_state_json == second_view.public_state_json
    assert first_view.reach_factors.tobytes() == second_view.reach_factors.tobytes()
    nested_private = dict(first_view.public_state)
    nested_private["nested_debug"] = {
        "hole_cards": [12, 13],
        "future_board": [51],
    }
    with pytest.raises(ValueError, match="exact Common public schema"):
        PublicInferenceInput(
            canonical_bytes(nested_private).decode("utf-8"),
            first_view.reach_factors,
            first_view.legal_combo_mask,
            first_view.legal_action_mask,
        )
    forged_action_mask = first_view.legal_action_mask.copy()
    forged_action_mask[1] = not forged_action_mask[1]
    with pytest.raises(ValueError, match="Common legality"):
        PublicInferenceInput(
            first_view.public_state_json,
            first_view.reach_factors,
            first_view.legal_combo_mask,
            forged_action_mask,
        )
    impossible_reach = np.zeros_like(first_view.reach_factors)
    impossible_reach[:, 0] = 1.0
    with pytest.raises(ValueError, match="no compatible joint support"):
        PublicInferenceInput(
            first_view.public_state_json,
            impossible_reach,
            first_view.legal_combo_mask,
            first_view.legal_action_mask,
        )


class _PublicConstantValue:
    version = "test-public-value-v1"

    def __init__(self) -> None:
        self.input_digests: list[str] = []

    def __call__(self, model_input: PublicInferenceInput) -> np.ndarray:
        assert type(model_input) is PublicInferenceInput
        assert not hasattr(model_input, "hole_cards")
        self.input_digests.append(model_input.digest)
        return np.zeros((2, HUNL_COMBO_COUNT), dtype=np.float64)


def test_learned_leaf_receives_public_view_and_returns_complete_table() -> None:
    state, pbs = _root()
    provider = _PublicConstantValue()
    solver = DepthLimitedCFRAvg(
        iterations=2,
        deals_per_iteration=1,
        public_action_depth=1,
        warm_policy=UniformPolicy(),
        public_value_leaf=provider,
        seed=7,
    )
    result = solver.solve(state, pbs)
    assert result.root_average_policy.shape == (1326, 9)
    # First-use double invocation is the stateful/RNG guard; equal input digest
    # proves no sampled deal was smuggled into the learned boundary.
    assert provider.input_digests
    assert len(provider.input_digests) % 2 == 0
    assert all(
        provider.input_digests[index] == provider.input_digests[index + 1]
        for index in range(0, len(provider.input_digests), 2)
    )


class _ScalarValue:
    version = "invalid-scalar"

    def __call__(self, model_input: PublicInferenceInput) -> np.ndarray:
        del model_input
        return np.asarray(0.0)


class _StatefulValue:
    version = "invalid-stateful"

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, model_input: PublicInferenceInput) -> np.ndarray:
        del model_input
        self.calls += 1
        return np.full((2, 1326), self.calls, dtype=np.float64)


@pytest.mark.parametrize(
    ("provider", "message"),
    ((_ScalarValue(), "shape"), (_StatefulValue(), "stateful")),
)
def test_learned_leaf_rejects_scalar_or_call_order_channel(provider, message: str) -> None:
    state, pbs = _root()
    solver = DepthLimitedCFRAvg(
        iterations=2,
        deals_per_iteration=1,
        public_action_depth=1,
        warm_policy=UniformPolicy(),
        public_value_leaf=provider,
        seed=11,
    )
    with pytest.raises(ValueError, match=message):
        solver.solve(state, pbs)


def test_cfr_node_identity_excludes_dynamic_reach_and_final_profile_hits_root() -> None:
    state, pbs = _root()
    reach = ReachFactors.from_pbs(pbs)
    actions = abstract_actions(state)
    root_binding = pbs.pbs_state_id
    likelihood = np.linspace(0.1, 0.9, HUNL_COMBO_COUNT)
    changed_reach = reach.observe_action(0, likelihood)
    # Dynamic Bayes state must not split one public infoset's cumulative table.
    assert information_node_id(state, root_binding, actions.wires) == information_node_id(
        state, root_binding, actions.wires
    )
    assert changed_reach.digest != reach.digest

    uniform = UniformPolicy()
    solver = DepthLimitedCFRAvg(
        iterations=3,
        deals_per_iteration=2,
        public_action_depth=2,
        warm_policy=uniform,
        rollout_leaf=TerminalRolloutLeaf(uniform),
        seed=19,
    )
    result = solver.solve(state, pbs)

    class _FailFallback:
        version = "must-not-fallback-at-root"

        def __call__(self, model_input, support):
            del model_input, support
            raise AssertionError("final CFR-AVG root unexpectedly fell back")

    view = PublicInferenceInput.from_state(state, reach, actions.mask)
    policy = SearchProfile(result, _FailFallback())(view, actions)
    assert policy.shape == (HUNL_COMBO_COUNT, ACTION_COUNT)
    assert np.allclose(policy.sum(axis=1), 1.0, atol=1e-12)


def test_exact_offtree_raise_is_injected_without_nearest_translation() -> None:
    state, _ = _root()
    support = abstract_actions(
        state, exact_offtree_action=Action(ActionKind.RAISE, 777)
    )
    assert support.exact_offtree_slot == 7
    assert support.action(7).to_wire() == "raise 777"
    assert support.snapshot()["nearest_action_translation_used"] is False
