from __future__ import annotations

import copy
import hashlib
import json
from functools import lru_cache
from math import fsum
from pathlib import Path

import pytest

from ...common_contracts.actions import Action, ActionKind
from ...common_contracts.cards import legal_combo_mask
from ...common_contracts.national_state import NationalGameState
from ..common_runtime.kuhn import (
    current_player,
    is_terminal,
    legal_actions,
    next_history,
    ordered_deals,
    terminal_utility,
)
from ..common_runtime.leduc import (
    apply_action as leduc_apply_action,
    card_rank as leduc_card_rank,
    information_set as leduc_information_set,
    initial_state as leduc_initial_state,
    legal_actions as leduc_legal_actions,
    ordered_deals as leduc_ordered_deals,
    terminal_utility as leduc_terminal_utility,
)
from ..decisionholdem_like.secure_files import (
    canonical_bytes,
    stable_read_path,
    strict_json_loads,
)
from ..rebel_like.label_contract import (
    M5A_CRITICAL_SOURCE_PATHS,
    M5A_PRIVATE_STATE_ORDER,
    assign_split,
    build_label_artifact,
    build_label_example,
    canonical_hunl_board_family,
    load_m5a_config,
    public_family_payload,
    small_game_pbs_input,
    small_game_pbs_provenance,
    small_game_pbs_state_id,
    validate_label_artifact,
    validate_label_example,
    validate_m5a_config,
    verify_label_artifact_files,
)
from ..rebel_like.hunl_pbs import HUNLReachFactorPublicBeliefState


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PACKAGE_ROOT.parent
CONFIG_PATH = PACKAGE_ROOT / "configs" / "m5a_pbs_label_contract.json"
ARTIFACT_PATH = PACKAGE_ROOT / "artifacts" / "m5a_exact_label_fixture.json"


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


@pytest.fixture(scope="module")
def config_and_sha():
    return load_m5a_config(CONFIG_PATH)


@pytest.fixture(scope="module")
def artifact():
    return strict_json_loads(stable_read_path(ARTIFACT_PATH))


def _root_kuhn_example(artifact):
    return next(
        item
        for item in artifact["body"]["examples"]
        if item["game"] == "kuhn"
        and item["pbs_input"]["public_state"]["history"] == ""
    )


def _flop_hunl_network_input(board: tuple[int, int, int]):
    root = NationalGameState.new_hand(1, small_blind=0)
    pending = root.apply_action(Action(ActionKind.CALL)).apply_action(
        Action(ActionKind.CHECK)
    )
    state = pending.apply_chance(board)
    public = state.hand_public_dict()
    public.pop("terminal_reason")
    public.pop("winner")
    mask = legal_combo_mask(board)
    probability = 1.0 / sum(mask)
    factor = tuple(probability if legal else 0.0 for legal in mask)
    pbs = HUNLReachFactorPublicBeliefState(
        json.dumps(public, sort_keys=True, separators=(",", ":")),
        (factor, factor),
    )
    return pbs.network_input()


def test_frozen_config_is_exact_oracle_only(config_and_sha) -> None:
    config, _ = config_and_sha
    assert config["future_search_variants"] == {
        "cfr": False,
        "cfr_avg": False,
        "cfr_d": False,
    }
    assert config["large_training_authorized"] is False
    assert config["network_training_started"] is False
    assert config["online_search_implemented"] is False
    assert config["coverage_contract"] == {
        "identity_manifest_sha256": "ef1be544a7aff691d791c2935a2c0342684005735c08c61f24d1a5e86015afe0",
        "kuhn_public_decision_nodes": 4,
        "leduc_public_decision_nodes": 96,
        "minimum_cfr_q_max_abs_separation": 0.000001,
        "schema": "route-a1-m5a-complete-public-tree-coverage-v1",
        "split_counts": {"test": 5, "train": 80, "validation": 15},
        "total_public_decision_nodes": 100,
    }
    assert set(config["value_label_semantics"]) == {
        "oracle_forced_action_conditional_q",
        "oracle_on_policy_private_values",
        "oracle_unnormalized_cfr_action_values",
        "payoff_origin",
        "player_perspective",
        "utility_unit",
    }
    drifted = copy.deepcopy(config)
    drifted["future_search_variants"]["cfr_d"] = True
    with pytest.raises(ValueError, match="cannot claim"):
        validate_m5a_config(drifted)
    drifted = copy.deepcopy(config)
    drifted["future_search_variants"]["cfr"] = 0
    with pytest.raises(ValueError, match="cannot claim"):
        validate_m5a_config(drifted)
    drifted = copy.deepcopy(config)
    drifted["artifact_gate"]["canonical_json"] = 1
    with pytest.raises(ValueError, match="artifact gate"):
        validate_m5a_config(drifted)
    drifted = copy.deepcopy(config)
    drifted["coverage_contract"]["kuhn_public_decision_nodes"] = 4.0
    with pytest.raises(ValueError, match="coverage contract"):
        validate_m5a_config(drifted)
    drifted = copy.deepcopy(config)
    drifted["coverage_contract"]["split_counts"]["test"] = 5.0
    with pytest.raises(ValueError, match="coverage contract"):
        validate_m5a_config(drifted)
    drifted = copy.deepcopy(config)
    drifted["coverage_contract"]["total_public_decision_nodes"] = 100.0
    with pytest.raises(ValueError, match="coverage contract"):
        validate_m5a_config(drifted)


def test_reach_factor_projection_matches_reduced_blocker_golden() -> None:
    pbs = small_game_pbs_input(
        game="kuhn",
        public_state={"history": ""},
        private_state_order=M5A_PRIVATE_STATE_ORDER,
        reach_factors=(
            (0.5, 1.0 / 3.0, 1.0 / 6.0),
            (2.0 / 15.0, 1.0 / 3.0, 8.0 / 15.0),
        ),
    )
    assert pbs["projected_marginals"][0] == pytest.approx(
        (0.590909090909, 0.303030303030, 0.106060606061), abs=1e-12
    )
    assert pbs["reach_factors"][0] != pytest.approx(
        pbs["projected_marginals"][0]
    )
    assert pbs["positive_reach_mask"] == [[True] * 3, [True] * 3]
    assert pbs["label_valid_mask"] == [[True] * 3, [True] * 3]


def test_pbs_identity_excludes_trace_but_provenance_binds_it() -> None:
    pbs = small_game_pbs_input(
        game="kuhn",
        public_state={"history": "check"},
        private_state_order=M5A_PRIVATE_STATE_ORDER,
        reach_factors=((0.2, 0.3, 0.5), (1.0 / 3.0,) * 3),
    )
    first = small_game_pbs_provenance(
        pbs_input=pbs,
        belief_policy_kind="average_policy",
        belief_profile_sha256="1" * 64,
        belief_update_trace=(
            {
                "actor": 0,
                "action": "check",
                "belief_policy_kind": "average_policy",
                "belief_profile_sha256": "1" * 64,
            },
        ),
    )
    second = small_game_pbs_provenance(
        pbs_input=pbs,
        belief_policy_kind="average_policy",
        belief_profile_sha256="2" * 64,
        belief_update_trace=(
            {
                "actor": 0,
                "action": "check",
                "belief_policy_kind": "average_policy",
                "belief_profile_sha256": "2" * 64,
            },
        ),
    )
    assert first["pbs_state_id"] == second["pbs_state_id"] == small_game_pbs_state_id(pbs)
    assert _digest(first) != _digest(second)


def test_hunl_public_family_split_is_suit_isomorphic(config_and_sha) -> None:
    config, _ = config_and_sha
    first = _flop_hunl_network_input((0, 5, 10))
    second = _flop_hunl_network_input((1, 6, 11))
    assert canonical_hunl_board_family(first["public_state"]["board"]) == canonical_hunl_board_family(second["public_state"]["board"])
    assert assign_split("hunl", first, config) == assign_split("hunl", second, config)

    for field, value in (
        ("oppo_hands", [0, 1]),
        ("earnChips", 100),
        ("unknown_private_alias", [48, 49]),
    ):
        invalid = copy.deepcopy(first)
        invalid[field] = value
        with pytest.raises(ValueError, match="network input fields"):
            public_family_payload("hunl", invalid)
    invalid = copy.deepcopy(first)
    invalid["public_state"]["terminal_reason"] = "showdown"
    with pytest.raises(ValueError, match="fields differ"):
        public_family_payload("hunl", invalid)


def test_committed_artifact_is_complete_content_bound_and_current(
    config_and_sha, artifact
) -> None:
    config, _ = config_and_sha
    verified = verify_label_artifact_files(
        artifact,
        config_path=CONFIG_PATH,
        source_root=SOURCE_ROOT,
    )
    body = verified["body"]
    assert len(body["examples"]) == 100
    assert sum(item["game"] == "kuhn" for item in body["examples"]) == 4
    assert sum(item["game"] == "leduc" for item in body["examples"]) == 96
    assert body["split_counts"] == {"train": 80, "validation": 15, "test": 5}
    assert set(body["source_snapshot"]) >= M5A_CRITICAL_SOURCE_PATHS
    assert set(body["oracle_bundles"]) == {"kuhn", "leduc"}
    identity_manifest = [
        {
            "game": item["game"],
            "public_family_id": item["public_family_id"],
            "pbs_state_id": item["pbs_state_id"],
            "actor": item["actor"],
            "action_support": item["action_support"],
        }
        for item in body["examples"]
    ]
    assert _digest(identity_manifest) == config["coverage_contract"][
        "identity_manifest_sha256"
    ]
    assert all(
        item["generator"]["correctness_certificate"]
        == "complete_exact_tree_recomputation_v1"
        for item in body["examples"]
    )


def test_independent_kuhn_enumerator_matches_complete_public_tree(artifact) -> None:
    profile = {}
    for row in artifact["body"]["oracle_bundles"]["kuhn"]["average_profile"]:
        profile[(row["player"], row["private_state"], row["public_history"])] = row["actions"]

    @lru_cache(maxsize=None)
    def continuation(deal, history, player):
        if is_terminal(history):
            return terminal_utility(deal, history, player)
        actor = current_player(history)
        return fsum(
            profile[(actor, deal[actor], history)][action]
            * continuation(deal, next_history(history, action), player)
            for action in legal_actions(history)
        )

    examples = [
        item for item in artifact["body"]["examples"] if item["game"] == "kuhn"
    ]
    assert len(examples) == 4
    for example in examples:
        history = example["pbs_input"]["public_state"]["history"]
        actions = [] if not history else history.split("-")

        def replay_reaches(deal):
            cursor = ""
            reaches = [1.0, 1.0]
            for observed in actions:
                actor = current_player(cursor)
                reaches[actor] *= profile[(actor, deal[actor], cursor)][observed]
                cursor = next_history(cursor, observed)
            assert cursor == history
            return tuple(reaches)

        reaches = {deal: replay_reaches(deal) for deal in ordered_deals()}
        raw = {
            deal: (1.0 / 6.0) * value[0] * value[1]
            for deal, value in reaches.items()
        }
        normalizer = fsum(raw.values())
        posterior = {deal: value / normalizer for deal, value in raw.items()}
        factors = [[1.0 / 3.0] * 3, [1.0 / 3.0] * 3]
        cursor = ""
        for observed in actions:
            actor = current_player(cursor)
            likelihoods = [
                profile[(actor, rank, cursor)][observed] for rank in range(3)
            ]
            evidence = fsum(
                factors[actor][rank] * likelihoods[rank] for rank in range(3)
            )
            factors[actor] = [
                factors[actor][rank] * likelihoods[rank] / evidence
                for rank in range(3)
            ]
            cursor = next_history(cursor, observed)
        actor = current_player(history)
        support = legal_actions(history)
        assert example["actor"] == actor
        assert example["action_support"] == list(support)
        for player in (0, 1):
            projected = [
                fsum(
                    probability
                    for deal, probability in posterior.items()
                    if deal[player] == rank
                )
                for rank in range(3)
            ]
            assert example["pbs_input"]["reach_factors"][player] == pytest.approx(
                factors[player], abs=1e-12
            )
            assert example["pbs_input"]["projected_marginals"][player] == pytest.approx(
                projected, abs=1e-12
            )
            expected_values = []
            for rank in range(3):
                mass = projected[rank]
                expected_values.append(
                    fsum(
                        probability * continuation(deal, history, player)
                        for deal, probability in posterior.items()
                        if deal[player] == rank
                    )
                    / mass
                )
            assert example["oracle_on_policy_private_values"][player] == pytest.approx(
                expected_values, abs=1e-12
            )
        q_differs = False
        for rank in range(3):
            actor_mass = example["pbs_input"]["projected_marginals"][actor][rank]
            for action in support:
                expected_q = (
                    fsum(
                        probability
                        * continuation(deal, next_history(history, action), actor)
                        for deal, probability in posterior.items()
                        if deal[actor] == rank
                    )
                    / actor_mass
                )
                expected_cfv = fsum(
                    (1.0 / 6.0)
                    * reaches[deal][1 - actor]
                    * continuation(deal, next_history(history, action), actor)
                    for deal in ordered_deals()
                    if deal[actor] == rank
                )
                assert example["oracle_actor_policy"][rank][action] == pytest.approx(
                    profile[(actor, rank, history)][action], abs=1e-12
                )
                assert example["oracle_forced_action_conditional_q"][rank][action] == pytest.approx(expected_q, abs=1e-12)
                assert example["oracle_unnormalized_cfr_action_values"][rank][action] == pytest.approx(expected_cfv, abs=1e-12)
                q_differs |= abs(expected_q - expected_cfv) > 1e-6
        assert q_differs


def test_independent_leduc_enumerator_matches_complete_cross_chance_tree(
    artifact,
) -> None:
    profile = {}
    for row in artifact["body"]["oracle_bundles"]["leduc"]["average_profile"]:
        profile[
            (
                row["player"],
                row["private_state"],
                row["public_rank"],
                row["public_history"],
            )
        ] = row["actions"]
    @lru_cache(maxsize=None)
    def continuation(node, deal, player):
        if node.terminal:
            return leduc_terminal_utility(node, deal, player)
        key = leduc_information_set(node, deal)
        return fsum(
            profile[key][action]
            * continuation(leduc_apply_action(node, action), deal, player)
            for action in leduc_legal_actions(node)
        )

    examples = [
        item for item in artifact["body"]["examples"] if item["game"] == "leduc"
    ]
    assert len(examples) == 96
    assert any(
        "/" in item["pbs_input"]["public_state"]["history"]
        and item["pbs_input"]["public_state"]["history"].index("/")
        < len(item["pbs_input"]["public_state"]["history"]) - 1
        for item in examples
    )
    for example in examples:
        public = example["pbs_input"]["public_state"]
        public_rank = public["public_rank"]
        consistent_deals = [
            deal
            for deal in leduc_ordered_deals()
            if public_rank is None or leduc_card_rank(deal[2]) == public_rank
        ]

        def replay(deal):
            state = leduc_initial_state()
            reaches = [1.0, 1.0]
            for token in public["history"]:
                if token == "/":
                    continue
                actor = state.actor
                reaches[actor] *= profile[leduc_information_set(state, deal)][token]
                state = leduc_apply_action(state, token)
            assert list(state.history) == public["history"]
            return state, tuple(reaches)

        replayed = {deal: replay(deal) for deal in consistent_deals}
        raw = {
            deal: (1.0 / 120.0) * reaches[0] * reaches[1]
            for deal, (_, reaches) in replayed.items()
        }
        normalizer = fsum(raw.values())
        posterior = {deal: weight / normalizer for deal, weight in raw.items()}
        state = next(iter(replayed.values()))[0]
        assert state.actor == example["actor"]
        assert list(leduc_legal_actions(state)) == example["action_support"]

        factors = [[1.0 / 3.0] * 3, [1.0 / 3.0] * 3]
        cursor = leduc_initial_state()
        for token in public["history"]:
            if token == "/":
                continue
            actor = cursor.actor
            rank_key = -1 if cursor.street == 0 else public_rank
            history_key = ",".join(cursor.history)
            likelihoods = [
                profile[(actor, rank, rank_key, history_key)][token]
                for rank in range(3)
            ]
            evidence = fsum(
                factors[actor][rank] * likelihoods[rank] for rank in range(3)
            )
            factors[actor] = [
                factors[actor][rank] * likelihoods[rank] / evidence
                for rank in range(3)
            ]
            cursor = leduc_apply_action(cursor, token)
        projected: list[list[float]] = [[], []]
        for player in (0, 1):
            projected[player] = [
                fsum(
                    probability
                    for deal, probability in posterior.items()
                    if leduc_card_rank(deal[player]) == rank
                )
                for rank in range(3)
            ]
            assert example["pbs_input"]["reach_factors"][player] == pytest.approx(
                factors[player], abs=1e-12
            )
            assert example["pbs_input"]["projected_marginals"][player] == pytest.approx(
                projected[player], abs=1e-12
            )
            expected_values = []
            for rank in range(3):
                mass = projected[player][rank]
                expected_values.append(
                    fsum(
                        probability * continuation(state, deal, player)
                        for deal, probability in posterior.items()
                        if leduc_card_rank(deal[player]) == rank
                    )
                    / mass
                )
            assert example["oracle_on_policy_private_values"][player] == pytest.approx(
                expected_values, abs=1e-10
            )
        actor = state.actor
        q_differs = False
        rank_key = -1 if state.street == 0 else public_rank
        history_key = ",".join(state.history)
        for rank in range(3):
            mass = projected[actor][rank]
            for action in leduc_legal_actions(state):
                expected_q = (
                    fsum(
                        probability
                        * continuation(leduc_apply_action(state, action), deal, actor)
                        for deal, probability in posterior.items()
                        if leduc_card_rank(deal[actor]) == rank
                    )
                    / mass
                )
                expected_cfv = fsum(
                    (1.0 / 120.0)
                    * replayed[deal][1][1 - actor]
                    * continuation(leduc_apply_action(state, action), deal, actor)
                    for deal in consistent_deals
                    if leduc_card_rank(deal[actor]) == rank
                )
                assert example["oracle_actor_policy"][rank][action] == pytest.approx(
                    profile[(actor, rank, rank_key, history_key)][action], abs=1e-12
                )
                assert example["oracle_forced_action_conditional_q"][rank][action] == pytest.approx(expected_q, abs=1e-10)
                assert example["oracle_unnormalized_cfr_action_values"][rank][action] == pytest.approx(expected_cfv, abs=1e-10)
                q_differs |= abs(expected_q - expected_cfv) > 1e-6
        assert q_differs


def test_projected_marginals_not_factors_weight_label_diagnostics(artifact) -> None:
    for example in artifact["body"]["examples"]:
        pbs = example["pbs_input"]
        values = example["oracle_on_policy_private_values"]
        projected = [
            fsum(pbs["projected_marginals"][player][rank] * values[player][rank] for rank in range(3))
            for player in (0, 1)
        ]
        factor_weighted = [
            fsum(pbs["reach_factors"][player][rank] * values[player][rank] for rank in range(3))
            for player in (0, 1)
        ]
        if any(abs(projected[player] - factor_weighted[player]) > 1e-6 for player in (0, 1)):
            assert example["diagnostics"]["projected_marginal_weighted_values"] == pytest.approx(projected, abs=1e-12)
            assert example["diagnostics"]["projected_marginal_weighted_values"] != pytest.approx(factor_weighted, abs=1e-6)
            break
    else:
        pytest.fail("complete fixture lacks a blocker-sensitive weighting example")


def test_zero_self_consistent_fake_labels_fail_exact_recomputation(
    config_and_sha, artifact
) -> None:
    config, _ = config_and_sha
    source = _root_kuhn_example(artifact)
    support = source["action_support"]
    zeros = [{action: 0.0 for action in support} for _ in range(3)]
    with pytest.raises(ValueError, match="namespaces are not separated|regenerated exact oracle"):
        build_label_example(
            game="kuhn",
            pbs_input=source["pbs_input"],
            pbs_provenance=source["pbs_provenance"],
            actor=source["actor"],
            action_support=support,
            oracle_actor_policy=source["oracle_actor_policy"],
            oracle_forced_action_conditional_q=zeros,
            oracle_unnormalized_cfr_action_values=zeros,
            oracle_on_policy_private_values=[[0.0] * 3, [0.0] * 3],
            generator=source["generator"],
            config=config,
            optional_policy_target=source["optional_policy_target"],
        )


def test_forged_reach_factors_fail_profile_bayes_replay(
    config_and_sha, artifact
) -> None:
    config, _ = config_and_sha
    source = _root_kuhn_example(artifact)
    forged_pbs = small_game_pbs_input(
        game="kuhn",
        public_state={"history": ""},
        private_state_order=M5A_PRIVATE_STATE_ORDER,
        reach_factors=((0.5, 0.3, 0.2), (1.0 / 3.0,) * 3),
    )
    forged_provenance = small_game_pbs_provenance(
        pbs_input=forged_pbs,
        belief_policy_kind=source["generator"]["belief_policy_kind"],
        belief_profile_sha256=source["generator"]["belief_profile_sha256"],
        belief_update_trace=(),
    )
    with pytest.raises(ValueError, match="do not follow exact profile Bayes"):
        build_label_example(
            game="kuhn",
            pbs_input=forged_pbs,
            pbs_provenance=forged_provenance,
            actor=source["actor"],
            action_support=source["action_support"],
            oracle_actor_policy=source["oracle_actor_policy"],
            oracle_forced_action_conditional_q=source["oracle_forced_action_conditional_q"],
            oracle_unnormalized_cfr_action_values=source["oracle_unnormalized_cfr_action_values"],
            oracle_on_policy_private_values=source["oracle_on_policy_private_values"],
            generator=source["generator"],
            config=config,
            optional_policy_target=source["optional_policy_target"],
        )


def test_nonexact_generator_and_bool_actor_trace_fail_closed(
    config_and_sha, artifact
) -> None:
    config, _ = config_and_sha
    example = copy.deepcopy(_root_kuhn_example(artifact))
    example["generator"]["kind"] = "cfr_d"
    with pytest.raises(ValueError, match="exact_oracle"):
        validate_label_example(example, config)

    example = copy.deepcopy(_root_kuhn_example(artifact))
    example["generator"]["search_iterations"] = False
    with pytest.raises(ValueError, match="search/certificate boundary"):
        validate_label_example(example, config)

    example = copy.deepcopy(_root_kuhn_example(artifact))
    example["actor"] = True
    with pytest.raises(ValueError, match="actor must"):
        validate_label_example(example, config)

    child = next(
        item
        for item in artifact["body"]["examples"]
        if item["game"] == "kuhn"
        and item["pbs_input"]["public_state"]["history"] == "check"
    )
    example = copy.deepcopy(child)
    example["pbs_provenance"]["belief_update_trace"][0]["actor"] = False
    with pytest.raises(ValueError, match="actor is invalid|action differs"):
        validate_label_example(example, config)


def test_artifact_rejects_missing_public_node_and_profile_payload_drift(
    config_and_sha, artifact
) -> None:
    config, _ = config_and_sha
    missing = copy.deepcopy(artifact)
    removed = missing["body"]["examples"].pop()
    missing["body"]["example_sha256"] = _digest(missing["body"]["examples"])
    missing["body"]["split_counts"][removed["split"]] -= 1
    missing["body_sha256"] = _digest(missing["body"])
    with pytest.raises(ValueError, match="complete exact public tree"):
        validate_label_artifact(missing, config)

    drifted = copy.deepcopy(artifact)
    row = drifted["body"]["oracle_bundles"]["kuhn"]["average_profile"][0]
    action = next(iter(row["actions"]))
    row["actions"][action] += 0.01
    drifted["body"]["oracle_bundles_sha256"] = _digest(
        drifted["body"]["oracle_bundles"]
    )
    drifted["body_sha256"] = _digest(drifted["body"])
    with pytest.raises(ValueError, match="embedded exact solver/profile bundles"):
        validate_label_artifact(drifted, config)


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("future_search_variants", "cfr"), 0),
        (("artifact_gate", "canonical_json"), 1),
        (("coverage_contract", "total_public_decision_nodes"), 100.0),
    ),
)
def test_artifact_embedded_config_rejects_numeric_type_aliases(
    config_and_sha, artifact, path, value
) -> None:
    config, _ = config_and_sha
    drifted = copy.deepcopy(artifact)
    drifted["body"]["config"][path[0]][path[1]] = value
    drifted["body_sha256"] = _digest(drifted["body"])
    with pytest.raises(ValueError, match="cannot claim|artifact gate|coverage contract"):
        validate_label_artifact(drifted, config)


def test_artifact_split_counts_reject_float_alias(config_and_sha, artifact) -> None:
    config, _ = config_and_sha
    drifted = copy.deepcopy(artifact)
    drifted["body"]["split_counts"]["test"] = 5.0
    drifted["body_sha256"] = _digest(drifted["body"])
    with pytest.raises(ValueError, match="every deterministic split"):
        validate_label_artifact(drifted, config)


def test_artifact_source_manifest_and_strict_json_fail_closed(
    config_and_sha, artifact
) -> None:
    config, config_sha = config_and_sha
    with pytest.raises(ValueError, match="omits critical files"):
        build_label_artifact(
            config=config,
            config_file_sha256=config_sha,
            source_snapshot={
                "rebel_decisionholdem/rebel_like/label_contract.py": "0" * 64
            },
        )
    for omitted in M5A_CRITICAL_SOURCE_PATHS:
        sources = dict(artifact["body"]["source_snapshot"])
        del sources[omitted]
        with pytest.raises(ValueError, match="omits critical files"):
            build_label_artifact(
                config=config,
                config_file_sha256=config_sha,
                source_snapshot=sources,
            )
    with pytest.raises(ValueError, match="duplicate JSON key"):
        strict_json_loads('{"a":1,"a":2}')
    with pytest.raises(ValueError, match="non-finite"):
        strict_json_loads('{"a":NaN}')
