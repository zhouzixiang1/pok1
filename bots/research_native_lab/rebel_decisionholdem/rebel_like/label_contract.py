"""Strict M5a public-belief input, exact-label, split and artifact contracts.

M5a is an exact-oracle correctness gate.  It freezes public input semantics and
the provenance needed by later depth-limited CFR and neural stages, but does
not implement or claim either stage.  Accepted labels come only from complete
Kuhn or Leduc terminal trees.

The two private-state vectors in a PBS are *reach factors*, not true marginals.
True projected marginals are reconstructed from the exact public-card
compatibility kernel and are the only weights permitted for label losses or
zero-sum diagnostics.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import math
from functools import lru_cache
from math import fsum
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...common_contracts.constants import CONTRACT_VERSION
from ..common_runtime.kuhn import (
    CARD_NAMES as KUHN_PRIVATE_NAMES,
    all_infosets as kuhn_all_infosets,
    current_player as kuhn_current_player,
    is_terminal as kuhn_is_terminal,
    legal_actions as kuhn_legal_actions,
    next_history as kuhn_next_history,
)
from ..common_runtime.leduc import (
    RANK_NAMES as LEDUC_PRIVATE_NAMES,
    actions_by_infoset as leduc_actions_by_infoset,
    apply_action as leduc_apply_action,
    card_rank as leduc_card_rank,
    initial_state as leduc_initial_state,
    legal_actions as leduc_legal_actions,
    ordered_deals as leduc_ordered_deals,
)
from ..decisionholdem_like.leduc_linear_cfr import LeducLinearCFR
from ..decisionholdem_like.linear_cfr import LinearCFR
from ..decisionholdem_like.secure_files import (
    canonical_bytes,
    sha256_bytes,
    stable_read_path,
    stable_read_relative,
    strict_json_loads,
)
from .hunl_pbs import (
    HUNL_COMBO_ORDER,
    HUNL_COMBO_REGISTRY_SHA256,
    HUNL_PBS_SCHEMA,
    validate_hunl_network_input,
)
from .leduc_pbs import LeducPublicBeliefState
from .pbs import KuhnPublicBeliefState


M5A_CONFIG_SCHEMA = "route-a1-m5a-pbs-label-config-v1"
M5A_LABEL_SCHEMA = "route-a1-m5a-value-label-v1"
M5A_ARTIFACT_SCHEMA = "route-a1-m5a-exact-label-artifact-v1"
M5A_SMALL_GAME_PBS_SCHEMA = "route-a1-m5a-small-game-reach-factor-pbs-v1"
M5A_SMALL_GAME_PROVENANCE_SCHEMA = "route-a1-m5a-small-game-pbs-provenance-v1"
M5A_ORACLE_BUNDLE_SCHEMA = "route-a1-m5a-exact-oracle-bundle-v1"
M5A_EXACT_CERTIFICATE_KIND = "complete_exact_tree_recomputation_v1"
M5A_SPLIT_CONTRACT = "public-family-suit-isomorphic-v1"
M5A_ALLOWED_GAMES = ("kuhn", "leduc")
M5A_ALLOWED_GENERATOR = "exact_oracle"
M5A_SPLITS = ("train", "validation", "test")
M5A_PRIVATE_STATE_ORDER = ("J", "Q", "K")
M5A_PROFILE_KINDS = frozenset(
    {"fixed_profile", "current_policy", "average_policy"}
)
M5A_CRITICAL_SOURCE_PATHS = frozenset(
    {
        "common_contracts/actions.py",
        "common_contracts/cards.py",
        "common_contracts/constants.py",
        "common_contracts/national_state.py",
        "rebel_decisionholdem/common_runtime/evaluation.py",
        "rebel_decisionholdem/common_runtime/kuhn.py",
        "rebel_decisionholdem/common_runtime/leduc.py",
        "rebel_decisionholdem/decisionholdem_like/leduc_linear_cfr.py",
        "rebel_decisionholdem/decisionholdem_like/linear_cfr.py",
        "rebel_decisionholdem/decisionholdem_like/secure_files.py",
        "rebel_decisionholdem/rebel_like/hunl_pbs.py",
        "rebel_decisionholdem/rebel_like/label_contract.py",
        "rebel_decisionholdem/rebel_like/leduc_pbs.py",
        "rebel_decisionholdem/rebel_like/pbs.py",
        "rebel_decisionholdem/tools/build_m5a_label_fixture.py",
    }
)

_FORBIDDEN_INPUT_KEYS = frozenset(
    {
        "hole_cards",
        "private_hand",
        "information_state_id",
        "observation_id",
        "full_state_id",
        "match_context_id",
        "hand_number",
        "match_net_before",
        "sampled_deal",
        "deal_probabilities",
        "joint_posterior",
        "winner",
        "outcome",
        "payoff",
        "future_board",
        "deck",
        "seed",
        "timing",
        "rng",
    }
)

_EXPECTED_CONFIG_FIELDS = {
    "artifact_gate",
    "common_contract_version",
    "coverage_contract",
    "data_root_seed",
    "exact_oracle_profiles",
    "future_search_variants",
    "hunl_combo_order",
    "hunl_combo_registry_sha256",
    "hunl_pbs_schema",
    "large_training_authorized",
    "label_contract_schema",
    "network_init_seed",
    "network_training_started",
    "online_search_implemented",
    "policy_sampling_seed",
    "schema",
    "split",
    "stage",
    "value_label_semantics",
}

_SMALL_PBS_FIELDS = {
    "compatibility_oracle",
    "game",
    "joint_oracle_used_for_labels_only",
    "label_valid_mask",
    "legal_mask",
    "positive_reach_mask",
    "private_state_order",
    "private_state_sample_in_input",
    "projected_marginals",
    "public_state",
    "reach_factors",
    "representation",
    "schema",
}

_SMALL_PBS_PROVENANCE_FIELDS = {
    "belief_policy_kind",
    "belief_profile_sha256",
    "belief_update_trace",
    "pbs_state_id",
    "schema",
}


def _digest(value: object) -> str:
    return sha256_bytes(canonical_bytes(value))


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _assert_public_only(value: object, *, path: str = "pbs_input") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if type(key) is not str:
                raise ValueError(f"{path} contains a non-string key")
            if key in _FORBIDDEN_INPUT_KEYS:
                raise ValueError(f"{path} contains forbidden private/joint key {key}")
            _assert_public_only(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_public_only(child, path=f"{path}[{index}]")
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ValueError(f"{path} contains an unsupported value")
    elif type(value) is float and not math.isfinite(value):
        raise ValueError(f"{path} contains a non-finite value")


def validate_m5a_config(config: object) -> dict[str, object]:
    if not isinstance(config, dict) or set(config) != _EXPECTED_CONFIG_FIELDS:
        raise ValueError("M5a config fields are invalid")
    if config["schema"] != M5A_CONFIG_SCHEMA:
        raise ValueError("M5a config schema differs")
    if config["stage"] != "M5a PBS and exact-label contract correctness gate only":
        raise ValueError("M5a stage boundary differs")
    if config["common_contract_version"] != CONTRACT_VERSION:
        raise ValueError("M5a Common contract version differs")
    if config["hunl_combo_order"] != HUNL_COMBO_ORDER:
        raise ValueError("M5a HUNL combo order differs")
    if config["hunl_combo_registry_sha256"] != HUNL_COMBO_REGISTRY_SHA256:
        raise ValueError("M5a HUNL combo registry digest differs")
    if config["hunl_pbs_schema"] != HUNL_PBS_SCHEMA:
        raise ValueError("M5a HUNL PBS schema differs")
    if config["label_contract_schema"] != M5A_LABEL_SCHEMA:
        raise ValueError("M5a label schema differs")
    coverage = config["coverage_contract"]
    coverage_fields = {
        "identity_manifest_sha256",
        "kuhn_public_decision_nodes",
        "leduc_public_decision_nodes",
        "minimum_cfr_q_max_abs_separation",
        "schema",
        "split_counts",
        "total_public_decision_nodes",
    }
    if not isinstance(coverage, dict) or set(coverage) != coverage_fields:
        raise ValueError("M5a complete public-tree coverage fields differ")
    coverage_split = coverage["split_counts"]
    if (
        coverage["identity_manifest_sha256"]
        != "ef1be544a7aff691d791c2935a2c0342684005735c08c61f24d1a5e86015afe0"
        or type(coverage["kuhn_public_decision_nodes"]) is not int
        or coverage["kuhn_public_decision_nodes"] != 4
        or type(coverage["leduc_public_decision_nodes"]) is not int
        or coverage["leduc_public_decision_nodes"] != 96
        or type(coverage["minimum_cfr_q_max_abs_separation"]) is not float
        or coverage["minimum_cfr_q_max_abs_separation"] != 0.000001
        or coverage["schema"] != "route-a1-m5a-complete-public-tree-coverage-v1"
        or not isinstance(coverage_split, dict)
        or set(coverage_split) != set(M5A_SPLITS)
        or any(
            type(coverage_split[split]) is not int
            for split in M5A_SPLITS
        )
        or coverage_split != {"test": 5, "train": 80, "validation": 15}
        or type(coverage["total_public_decision_nodes"]) is not int
        or coverage["total_public_decision_nodes"] != 100
    ):
        raise ValueError("M5a complete public-tree coverage contract differs")
    for flag in (
        "large_training_authorized",
        "network_training_started",
        "online_search_implemented",
    ):
        if config[flag] is not False:
            raise ValueError(f"M5a requires {flag}=false")
    future = config["future_search_variants"]
    if (
        not isinstance(future, dict)
        or set(future) != {"cfr", "cfr_avg", "cfr_d"}
        or any(future[field] is not False for field in future)
    ):
        raise ValueError("M5a cannot claim a depth-limited search variant")
    seeds = tuple(
        config[field]
        for field in ("data_root_seed", "policy_sampling_seed", "network_init_seed")
    )
    if any(type(seed) is not int or seed < 0 for seed in seeds) or len(set(seeds)) != 3:
        raise ValueError("M5a root seeds must be distinct non-negative integers")
    profiles = config["exact_oracle_profiles"]
    if not isinstance(profiles, dict) or set(profiles) != {
        "belief_policy_kind",
        "kuhn_lcfr_iterations",
        "label_profile_kind",
        "leduc_lcfr_iterations",
    }:
        raise ValueError("M5a exact-oracle profile config differs")
    if any(
        type(profiles[field]) is not int or profiles[field] < 0
        for field in ("kuhn_lcfr_iterations", "leduc_lcfr_iterations")
    ):
        raise ValueError("M5a exact-oracle iteration counts are invalid")
    if profiles["label_profile_kind"] != "average_policy":
        raise ValueError("M5a exact labels must use the frozen average policy")
    if profiles["belief_policy_kind"] != "average_policy":
        raise ValueError("M5a belief updates must use the frozen average policy")
    split = config["split"]
    if not isinstance(split, dict) or set(split) != {
        "group_contract",
        "seed",
        "test_basis_points",
        "train_basis_points",
        "validation_basis_points",
    }:
        raise ValueError("M5a split config fields differ")
    if split["group_contract"] != M5A_SPLIT_CONTRACT:
        raise ValueError("M5a split group contract differs")
    if type(split["seed"]) is not int or split["seed"] < 0:
        raise ValueError("M5a split seed is invalid")
    if split["seed"] in seeds:
        raise ValueError("M5a split seed must be independent")
    basis = tuple(
        split[field]
        for field in (
            "train_basis_points",
            "validation_basis_points",
            "test_basis_points",
        )
    )
    if any(type(value) is not int or value <= 0 for value in basis) or sum(basis) != 10_000:
        raise ValueError("M5a split basis points must be positive and sum to 10,000")
    expected_semantics = {
        "oracle_forced_action_conditional_q": "posterior_normalized_forced_action_v1",
        "oracle_on_policy_private_values": "posterior_normalized_on_policy_continuation_v1",
        "oracle_unnormalized_cfr_action_values": "unnormalized_omit_own_reach_v1",
        "payoff_origin": "per_hand_net_from_initial_contribution_v1",
        "player_perspective": "fixed_player_index_v1",
        "utility_unit": "chips",
    }
    if config["value_label_semantics"] != expected_semantics:
        raise ValueError("M5a value-label semantics differ")
    artifact = config["artifact_gate"]
    if (
        not isinstance(artifact, dict)
        or set(artifact)
        != {
            "canonical_json",
            "duplicate_keys_rejected",
            "nonfinite_rejected",
            "require_all_splits",
            "required_games",
            "schema",
        }
        or any(
            artifact[field] is not True
            for field in (
                "canonical_json",
                "duplicate_keys_rejected",
                "nonfinite_rejected",
                "require_all_splits",
            )
        )
        or artifact["required_games"] != ["kuhn", "leduc"]
        or artifact["schema"] != M5A_ARTIFACT_SCHEMA
    ):
        raise ValueError("M5a artifact gate differs")
    return copy.deepcopy(config)


def load_m5a_config(path: str | Path) -> tuple[dict[str, object], str]:
    raw = stable_read_path(path)
    return validate_m5a_config(strict_json_loads(raw)), hashlib.sha256(raw).hexdigest()


def canonical_hunl_board_family(board: Sequence[int]) -> tuple[tuple[int, int], ...]:
    """Canonicalize public suits, treating only the flop as unordered."""

    cards = tuple(board)
    if len(cards) not in (0, 3, 4, 5):
        raise ValueError("HUNL public board must contain 0, 3, 4 or 5 cards")
    if any(type(card) is not int or not 0 <= card < 52 for card in cards):
        raise ValueError("HUNL public board contains an invalid card")
    if len(set(cards)) != len(cards):
        raise ValueError("HUNL public board repeats a card")
    candidates: list[tuple[tuple[int, int], ...]] = []
    for permutation in itertools.permutations(range(4)):
        mapped = tuple((card // 4, permutation[card % 4]) for card in cards)
        if len(mapped) >= 3:
            mapped = tuple(sorted(mapped[:3])) + mapped[3:]
        candidates.append(mapped)
    return min(candidates, default=())


def _kuhn_public_actions(history: str) -> tuple[tuple[int, str], ...]:
    if type(history) is not str or kuhn_is_terminal(history):
        raise ValueError("Kuhn label public history must be a decision history")
    kuhn_legal_actions(history)
    cursor = ""
    result: list[tuple[int, str]] = []
    if history:
        for action in history.split("-"):
            actor = kuhn_current_player(cursor)
            cursor = kuhn_next_history(cursor, action)
            result.append((actor, action))
    if cursor != history:
        raise ValueError("Kuhn public history is not replay-canonical")
    return tuple(result)


def _leduc_public_actions(public_state: Mapping[str, object]) -> tuple[tuple[int, str], ...]:
    if set(public_state) != {"history", "public_rank", "street"}:
        raise ValueError("Leduc public-state fields differ")
    history = public_state["history"]
    if not isinstance(history, list) or any(type(token) is not str for token in history):
        raise ValueError("Leduc public history is invalid")
    cursor = leduc_initial_state()
    result: list[tuple[int, str]] = []
    for token in history:
        if token == "/":
            continue
        if cursor.terminal or token not in leduc_legal_actions(cursor):
            raise ValueError("Leduc public history contains an illegal action")
        result.append((cursor.actor, token))
        cursor = leduc_apply_action(cursor, token)
    if cursor.terminal or list(cursor.history) != history:
        raise ValueError("Leduc label public history is not a decision history")
    if type(public_state["street"]) is not int or public_state["street"] != cursor.street:
        raise ValueError("Leduc public street differs from replay")
    public_rank = public_state["public_rank"]
    if cursor.street == 0:
        if public_rank is not None:
            raise ValueError("preflop Leduc public rank must be null")
    elif type(public_rank) is not int or public_rank not in (0, 1, 2):
        raise ValueError("postflop Leduc public rank must be 0, 1 or 2")
    return tuple(result)


def _validate_public_state(game: str, public_state: object) -> tuple[tuple[int, str], ...]:
    if not isinstance(public_state, dict):
        raise ValueError("small-game public state must be an object")
    if game == "kuhn":
        if set(public_state) != {"history"}:
            raise ValueError("Kuhn public-state fields differ")
        return _kuhn_public_actions(public_state["history"])
    if game == "leduc":
        return _leduc_public_actions(public_state)
    raise ValueError("M5a small-game PBS supports only Kuhn and Leduc")


def public_family_payload(game: str, pbs_input: Mapping[str, object]) -> dict[str, object]:
    if game not in (*M5A_ALLOWED_GAMES, "hunl"):
        raise ValueError("unsupported public-family game")
    if not isinstance(pbs_input, Mapping):
        raise ValueError("PBS input must be an object")
    _assert_public_only(dict(pbs_input))
    public = pbs_input.get("public_state")
    if not isinstance(public, dict):
        raise ValueError("PBS input is missing its public state")
    if game in M5A_ALLOWED_GAMES:
        _validate_public_state(game, public)
        family_public = copy.deepcopy(public)
    else:
        hunl_pbs = validate_hunl_network_input(dict(pbs_input))
        family_public = hunl_pbs.public_state
        board = family_public.get("board")
        if not isinstance(board, list):
            raise ValueError("HUNL public family is missing its board")
        family_public["board_suit_isomorphic_family"] = [
            list(card) for card in canonical_hunl_board_family(board)
        ]
        del family_public["board"]
    return {
        "contract": M5A_SPLIT_CONTRACT,
        "game": game,
        "public_state_family": family_public,
    }


def public_family_id(game: str, pbs_input: Mapping[str, object]) -> str:
    return _digest(public_family_payload(game, pbs_input))


def assign_split(
    game: str,
    pbs_input: Mapping[str, object],
    config: Mapping[str, object],
) -> tuple[str, str]:
    validated = validate_m5a_config(dict(config))
    family_id = public_family_id(game, pbs_input)
    split = validated["split"]
    assert isinstance(split, dict)
    bucket = int(
        _digest(
            {
                "contract": M5A_SPLIT_CONTRACT,
                "public_family_id": family_id,
                "seed": split["seed"],
            }
        )[:16],
        16,
    ) % 10_000
    train_end = int(split["train_basis_points"])
    validation_end = train_end + int(split["validation_basis_points"])
    if bucket < train_end:
        assigned = "train"
    elif bucket < validation_end:
        assigned = "validation"
    else:
        assigned = "test"
    return assigned, family_id


def _compatibility_matrix(
    game: str, public_state: Mapping[str, object]
) -> tuple[tuple[int, ...], ...]:
    if game == "kuhn":
        return tuple(
            tuple(0 if first == second else 1 for second in range(3))
            for first in range(3)
        )
    if game != "leduc":
        raise ValueError("compatibility matrix supports only Kuhn and Leduc")
    public_rank = public_state["public_rank"]
    rows = [[0 for _ in range(3)] for _ in range(3)]
    for deal in leduc_ordered_deals():
        if public_rank is not None and leduc_card_rank(deal[2]) != public_rank:
            continue
        rows[leduc_card_rank(deal[0])][leduc_card_rank(deal[1])] += 1
    return tuple(tuple(row) for row in rows)


def _numeric_vector(value: object, *, length: int, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"{label} has the wrong length")
    result: list[float] = []
    for item in value:
        if type(item) not in (int, float) or not math.isfinite(float(item)):
            raise ValueError(f"{label} contains a non-finite/non-numeric value")
        result.append(float(item))
    return result


def _bool_matrix(value: object, *, length: int, label: str) -> list[list[bool]]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{label} must contain both players")
    result: list[list[bool]] = []
    for player, row in enumerate(value):
        if not isinstance(row, list) or len(row) != length or any(type(item) is not bool for item in row):
            raise ValueError(f"{label} row {player} is invalid")
        result.append(list(row))
    return result


def _project_reach_factors(
    reach_factors: Sequence[Sequence[float]],
    compatibility: Sequence[Sequence[int]],
) -> tuple[float, tuple[tuple[float, ...], tuple[float, ...]]]:
    normalizer = fsum(
        float(compatibility[first][second])
        * float(reach_factors[0][first])
        * float(reach_factors[1][second])
        for first in range(3)
        for second in range(3)
    )
    if not math.isfinite(normalizer) or normalizer <= 0.0:
        raise ValueError("small-game reach factors have zero compatible joint support")
    first = tuple(
        float(reach_factors[0][rank])
        * fsum(
            float(compatibility[rank][other]) * float(reach_factors[1][other])
            for other in range(3)
        )
        / normalizer
        for rank in range(3)
    )
    second = tuple(
        float(reach_factors[1][rank])
        * fsum(
            float(compatibility[other][rank]) * float(reach_factors[0][other])
            for other in range(3)
        )
        / normalizer
        for rank in range(3)
    )
    if abs(fsum(first) - 1.0) > 1e-12 or abs(fsum(second) - 1.0) > 1e-12:
        raise ValueError("small-game projected marginal is not normalized")
    return normalizer, (first, second)


def _compatibility_oracle(
    game: str,
    public_state: Mapping[str, object],
    matrix: Sequence[Sequence[int]],
) -> dict[str, object]:
    return {
        "kind": "complete_exact_physical_deal_compatibility_v1",
        "ordered_deal_count": 6 if game == "kuhn" else len(leduc_ordered_deals()),
        "public_condition": (
            {"history": public_state["history"]}
            if game == "kuhn"
            else {"public_rank": public_state["public_rank"]}
        ),
        "count_matrix_sha256": _digest(matrix),
    }


def _validate_trace(
    trace: object,
    *,
    expected_actions: Sequence[tuple[int, str]],
    belief_policy_kind: str,
    belief_profile_sha256: str,
) -> list[dict[str, object]]:
    if not isinstance(trace, list) or len(trace) != len(expected_actions):
        raise ValueError("belief update trace differs from public action history")
    result: list[dict[str, object]] = []
    expected_fields = {
        "action",
        "actor",
        "belief_policy_kind",
        "belief_profile_sha256",
    }
    for index, (entry, expected) in enumerate(zip(trace, expected_actions, strict=True)):
        if not isinstance(entry, dict) or set(entry) != expected_fields:
            raise ValueError(f"belief update trace entry {index} fields differ")
        if type(entry["actor"]) is not int or entry["actor"] not in (0, 1):
            raise ValueError(f"belief update trace entry {index} actor is invalid")
        if (entry["actor"], entry["action"]) != expected:
            raise ValueError(f"belief update trace entry {index} action differs")
        if entry["belief_policy_kind"] != belief_policy_kind:
            raise ValueError(f"belief update trace entry {index} policy kind differs")
        if entry["belief_profile_sha256"] != belief_profile_sha256:
            raise ValueError(f"belief update trace entry {index} profile differs")
        result.append(copy.deepcopy(entry))
    return result


def validate_small_game_pbs_input(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _SMALL_PBS_FIELDS:
        raise ValueError("M5a small-game PBS fields are invalid")
    _assert_public_only(payload)
    if payload["schema"] != M5A_SMALL_GAME_PBS_SCHEMA:
        raise ValueError("M5a small-game PBS schema differs")
    game = payload["game"]
    if game not in M5A_ALLOWED_GAMES:
        raise ValueError("M5a small-game PBS game differs")
    if payload["representation"] != "two_player_reach_factors_with_exact_projected_marginals":
        raise ValueError("M5a PBS representation differs")
    if payload["joint_oracle_used_for_labels_only"] is not True:
        raise ValueError("M5a exact joint oracle boundary is missing")
    if payload["private_state_sample_in_input"] is not False:
        raise ValueError("M5a PBS input leaked a private-state sample")
    if payload["private_state_order"] != list(M5A_PRIVATE_STATE_ORDER):
        raise ValueError("M5a private-state order differs")
    if tuple(M5A_PRIVATE_STATE_ORDER) != tuple(KUHN_PRIVATE_NAMES) or tuple(M5A_PRIVATE_STATE_ORDER) != tuple(LEDUC_PRIVATE_NAMES):
        raise ValueError("small-game runtime private-state registry drifted")
    public_state = payload["public_state"]
    _validate_public_state(game, public_state)
    factors = payload["reach_factors"]
    if not isinstance(factors, list) or len(factors) != 2:
        raise ValueError("M5a PBS reach factors are invalid")
    normalized = [
        _numeric_vector(row, length=3, label=f"player {player} reach factor")
        for player, row in enumerate(factors)
    ]
    if any(
        any(value < 0.0 for value in row) or abs(fsum(row) - 1.0) > 1e-12
        for row in normalized
    ):
        raise ValueError("M5a PBS reach factors must be normalized/non-negative")
    compatibility = _compatibility_matrix(game, public_state)
    expected_oracle = _compatibility_oracle(game, public_state, compatibility)
    if payload["compatibility_oracle"] != expected_oracle:
        raise ValueError("M5a compatibility oracle binding differs")
    _, projected = _project_reach_factors(normalized, compatibility)
    supplied_projected = payload["projected_marginals"]
    if not isinstance(supplied_projected, list) or len(supplied_projected) != 2:
        raise ValueError("M5a projected marginals are invalid")
    converted_projected = [
        _numeric_vector(row, length=3, label=f"player {player} projected marginal")
        for player, row in enumerate(supplied_projected)
    ]
    if any(
        any(value < 0.0 for value in row) or abs(fsum(row) - 1.0) > 1e-12
        for row in converted_projected
    ):
        raise ValueError("M5a projected marginals must be normalized/non-negative")
    if any(
        abs(converted_projected[player][rank] - projected[player][rank]) > 1e-12
        for player in (0, 1)
        for rank in range(3)
    ):
        raise ValueError("M5a projected marginals do not follow factors and blockers")
    expected_legal = [
        [any(compatibility[rank][other] > 0 for other in range(3)) for rank in range(3)],
        [any(compatibility[other][rank] > 0 for other in range(3)) for rank in range(3)],
    ]
    expected_positive = [
        [normalized[player][rank] > 0.0 for rank in range(3)]
        for player in (0, 1)
    ]
    expected_valid = [
        [projected[player][rank] > 0.0 for rank in range(3)]
        for player in (0, 1)
    ]
    if _bool_matrix(payload["legal_mask"], length=3, label="legal mask") != expected_legal:
        raise ValueError("M5a legal mask differs from compatibility")
    if _bool_matrix(payload["positive_reach_mask"], length=3, label="positive reach mask") != expected_positive:
        raise ValueError("M5a positive reach mask differs from reach factors")
    if _bool_matrix(payload["label_valid_mask"], length=3, label="label-valid mask") != expected_valid:
        raise ValueError("M5a label-valid mask differs from projected marginals")
    return copy.deepcopy(payload)


def small_game_pbs_input(
    *,
    game: str,
    public_state: Mapping[str, object],
    private_state_order: Sequence[str],
    reach_factors: Sequence[Sequence[float]],
) -> dict[str, object]:
    if tuple(private_state_order) != M5A_PRIVATE_STATE_ORDER:
        raise ValueError("small-game private-state order differs")
    if len(reach_factors) != 2:
        raise ValueError("small-game PBS requires two reach factors")
    converted: list[list[float]] = []
    for player, values in enumerate(reach_factors):
        if len(values) != 3:
            raise ValueError(f"player {player} reach-factor length differs")
        row = [float(value) for value in values]
        if any(not math.isfinite(value) or value < 0.0 for value in row):
            raise ValueError(f"player {player} reach factor is not finite/non-negative")
        if abs(fsum(row) - 1.0) > 1e-12:
            raise ValueError(f"player {player} reach factor must sum to one")
        converted.append(row)
    public = copy.deepcopy(dict(public_state))
    _validate_public_state(game, public)
    compatibility = _compatibility_matrix(game, public)
    _, projected = _project_reach_factors(converted, compatibility)
    payload = {
        "schema": M5A_SMALL_GAME_PBS_SCHEMA,
        "game": game,
        "public_state": public,
        "private_state_order": list(M5A_PRIVATE_STATE_ORDER),
        "reach_factors": converted,
        "projected_marginals": [list(projected[0]), list(projected[1])],
        "legal_mask": [
            [any(compatibility[rank][other] > 0 for other in range(3)) for rank in range(3)],
            [any(compatibility[other][rank] > 0 for other in range(3)) for rank in range(3)],
        ],
        "positive_reach_mask": [
            [converted[player][rank] > 0.0 for rank in range(3)]
            for player in (0, 1)
        ],
        "label_valid_mask": [
            [projected[player][rank] > 0.0 for rank in range(3)]
            for player in (0, 1)
        ],
        "compatibility_oracle": _compatibility_oracle(game, public, compatibility),
        "representation": "two_player_reach_factors_with_exact_projected_marginals",
        "joint_oracle_used_for_labels_only": True,
        "private_state_sample_in_input": False,
    }
    return validate_small_game_pbs_input(payload)


def small_game_model_input(payload: object) -> dict[str, object]:
    """Return only the mathematical PBS features intended for a later net."""

    validated = validate_small_game_pbs_input(payload)
    return {
        "schema": M5A_SMALL_GAME_PBS_SCHEMA,
        "game": validated["game"],
        "public_state": validated["public_state"],
        "private_state_order": validated["private_state_order"],
        "reach_factors": validated["reach_factors"],
        "representation": "two_player_reach_factors",
    }


def small_game_pbs_state_id(payload: object) -> str:
    return _digest(small_game_model_input(payload))


def small_game_pbs_provenance(
    *,
    pbs_input: Mapping[str, object],
    belief_policy_kind: str,
    belief_profile_sha256: str,
    belief_update_trace: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    validated = validate_small_game_pbs_input(dict(pbs_input))
    if belief_policy_kind not in M5A_PROFILE_KINDS or not _is_sha256(belief_profile_sha256):
        raise ValueError("small-game belief policy identity is invalid")
    actions = _validate_public_state(validated["game"], validated["public_state"])
    trace = _validate_trace(
        [copy.deepcopy(dict(entry)) for entry in belief_update_trace],
        expected_actions=actions,
        belief_policy_kind=belief_policy_kind,
        belief_profile_sha256=belief_profile_sha256,
    )
    return {
        "schema": M5A_SMALL_GAME_PROVENANCE_SCHEMA,
        "pbs_state_id": small_game_pbs_state_id(validated),
        "belief_policy_kind": belief_policy_kind,
        "belief_profile_sha256": belief_profile_sha256,
        "belief_update_trace": trace,
    }


def validate_small_game_pbs_provenance(
    provenance: object,
    *,
    pbs_input: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(provenance, dict) or set(provenance) != _SMALL_PBS_PROVENANCE_FIELDS:
        raise ValueError("M5a PBS provenance fields are invalid")
    if provenance["schema"] != M5A_SMALL_GAME_PROVENANCE_SCHEMA:
        raise ValueError("M5a PBS provenance schema differs")
    rebuilt = small_game_pbs_provenance(
        pbs_input=pbs_input,
        belief_policy_kind=provenance["belief_policy_kind"],
        belief_profile_sha256=provenance["belief_profile_sha256"],
        belief_update_trace=provenance["belief_update_trace"],
    )
    if provenance != rebuilt:
        raise ValueError("M5a PBS provenance differs")
    return copy.deepcopy(provenance)


def _profile_payload(game: str, profile: Mapping[tuple[Any, ...], Mapping[str, float]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for key in sorted(profile):
        if game == "kuhn":
            player, private_state, public_history = key
            identity = {
                "player": player,
                "private_state": private_state,
                "public_history": public_history,
            }
        elif game == "leduc":
            player, private_state, public_rank, public_history = key
            identity = {
                "player": player,
                "private_state": private_state,
                "public_rank": public_rank,
                "public_history": public_history,
            }
        else:
            raise ValueError("exact profile payload game differs")
        rows.append(
            {
                **identity,
                "actions": {
                    action: float(probability)
                    for action, probability in sorted(profile[key].items())
                },
            }
        )
    return rows


@lru_cache(maxsize=4)
def _oracle_runtime(
    game: str,
    iterations: int,
) -> tuple[
    dict[str, object],
    Mapping[tuple[Any, ...], Mapping[str, float]],
    Mapping[tuple[Any, ...], Mapping[str, float]],
]:
    """Rebuild the frozen exact solver/profile content from source."""

    if type(iterations) is not int or iterations < 0:
        raise ValueError("exact oracle iterations are invalid")
    if game == "kuhn":
        solver = LinearCFR()
        solver.train(iterations)
        current = {
            key: dict(
                zip(
                    kuhn_legal_actions(key[2]),
                    solver.current_strategy(key),
                    strict=True,
                )
            )
            for key in kuhn_all_infosets()
        }
        average = solver.average_strategy()
    elif game == "leduc":
        solver = LeducLinearCFR()
        solver.train(iterations)
        action_registry = leduc_actions_by_infoset()
        current = {
            key: dict(
                zip(
                    action_registry[key],
                    solver.current_strategy(key),
                    strict=True,
                )
            )
            for key in sorted(action_registry)
        }
        average = solver.average_strategy()
    else:
        raise ValueError("exact oracle game differs")
    checkpoint = solver.checkpoint_payload()
    current_payload = _profile_payload(game, current)
    average_payload = _profile_payload(game, average)
    bundle = {
        "schema": M5A_ORACLE_BUNDLE_SCHEMA,
        "game": game,
        "solver_algorithm": "alternating_linear_cfr_exact_full_tree",
        "solver_iterations": iterations,
        "solver_checkpoint": checkpoint,
        "solver_checkpoint_sha256": _digest(checkpoint),
        "current_profile": current_payload,
        "current_profile_sha256": _digest(current_payload),
        "average_profile": average_payload,
        "average_profile_sha256": _digest(average_payload),
    }
    return bundle, current, average


def exact_oracle_bundle(
    game: str,
    config: Mapping[str, object],
) -> dict[str, object]:
    validated = validate_m5a_config(dict(config))
    profiles = validated["exact_oracle_profiles"]
    assert isinstance(profiles, dict)
    bundle, _, _ = _oracle_runtime(game, int(profiles[f"{game}_lcfr_iterations"]))
    return copy.deepcopy(bundle)


_GENERATOR_FIELDS = {
    "average_profile_sha256",
    "belief_policy_kind",
    "belief_profile_sha256",
    "config_file_sha256",
    "correctness_certificate",
    "current_profile_sha256",
    "depth_limit",
    "game",
    "kind",
    "label_profile_kind",
    "label_profile_sha256",
    "leaf_source",
    "search_iterations",
    "search_variant",
    "solver_algorithm",
    "solver_checkpoint_sha256",
    "solver_iterations",
    "source_snapshot_sha256",
}


def exact_oracle_provenance(
    *,
    game: str,
    solver_checkpoint_sha256: str,
    solver_iterations: int,
    current_profile_sha256: str,
    average_profile_sha256: str,
    label_profile_kind: str,
    label_profile_sha256: str,
    belief_policy_kind: str,
    belief_profile_sha256: str,
    config_file_sha256: str,
    source_snapshot_sha256: str,
) -> dict[str, object]:
    if game not in M5A_ALLOWED_GAMES:
        raise ValueError("exact-oracle provenance game differs")
    digests = (
        solver_checkpoint_sha256,
        current_profile_sha256,
        average_profile_sha256,
        label_profile_sha256,
        belief_profile_sha256,
        config_file_sha256,
        source_snapshot_sha256,
    )
    if any(not _is_sha256(value) for value in digests):
        raise ValueError("exact-oracle provenance digest is invalid")
    if type(solver_iterations) is not int or solver_iterations < 0:
        raise ValueError("exact-oracle solver iteration count is invalid")
    if label_profile_kind not in M5A_PROFILE_KINDS or belief_policy_kind not in M5A_PROFILE_KINDS:
        raise ValueError("exact-oracle policy kind is invalid")
    if label_profile_kind == "current_policy" and label_profile_sha256 != current_profile_sha256:
        raise ValueError("current label profile digest differs")
    if label_profile_kind == "average_policy" and label_profile_sha256 != average_profile_sha256:
        raise ValueError("average label profile digest differs")
    if belief_policy_kind == "current_policy" and belief_profile_sha256 != current_profile_sha256:
        raise ValueError("current belief profile digest differs")
    if belief_policy_kind == "average_policy" and belief_profile_sha256 != average_profile_sha256:
        raise ValueError("average belief profile digest differs")
    return {
        "kind": M5A_ALLOWED_GENERATOR,
        "game": game,
        "solver_algorithm": "alternating_linear_cfr_exact_full_tree",
        "solver_checkpoint_sha256": solver_checkpoint_sha256,
        "solver_iterations": solver_iterations,
        "current_profile_sha256": current_profile_sha256,
        "average_profile_sha256": average_profile_sha256,
        "label_profile_kind": label_profile_kind,
        "label_profile_sha256": label_profile_sha256,
        "belief_policy_kind": belief_policy_kind,
        "belief_profile_sha256": belief_profile_sha256,
        "config_file_sha256": config_file_sha256,
        "source_snapshot_sha256": source_snapshot_sha256,
        "search_variant": None,
        "search_iterations": 0,
        "depth_limit": None,
        "leaf_source": "complete_exact_terminal_tree",
        "correctness_certificate": M5A_EXACT_CERTIFICATE_KIND,
    }


def _validate_generator(
    generator: object,
    *,
    game: str,
    config: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(generator, dict) or set(generator) != _GENERATOR_FIELDS:
        raise ValueError("M5a generator provenance fields differ")
    if (
        type(generator["search_iterations"]) is not int
        or generator["search_iterations"] != 0
        or generator["search_variant"] is not None
        or generator["depth_limit"] is not None
        or generator["leaf_source"] != "complete_exact_terminal_tree"
        or generator["correctness_certificate"] != M5A_EXACT_CERTIFICATE_KIND
    ):
        raise ValueError("M5a exact-oracle search/certificate boundary differs")
    if generator["kind"] != M5A_ALLOWED_GENERATOR:
        raise ValueError("M5a accepts exact_oracle labels only")
    rebuilt = exact_oracle_provenance(
        game=game,
        solver_checkpoint_sha256=generator["solver_checkpoint_sha256"],
        solver_iterations=generator["solver_iterations"],
        current_profile_sha256=generator["current_profile_sha256"],
        average_profile_sha256=generator["average_profile_sha256"],
        label_profile_kind=generator["label_profile_kind"],
        label_profile_sha256=generator["label_profile_sha256"],
        belief_policy_kind=generator["belief_policy_kind"],
        belief_profile_sha256=generator["belief_profile_sha256"],
        config_file_sha256=generator["config_file_sha256"],
        source_snapshot_sha256=generator["source_snapshot_sha256"],
    )
    if canonical_bytes(generator) != canonical_bytes(rebuilt):
        raise ValueError("M5a exact-oracle provenance differs")
    profiles = config["exact_oracle_profiles"]
    assert isinstance(profiles, dict)
    expected_iterations = profiles[f"{game}_lcfr_iterations"]
    if generator["solver_iterations"] != expected_iterations:
        raise ValueError("M5a solver iterations differ from frozen config")
    if generator["label_profile_kind"] != profiles["label_profile_kind"]:
        raise ValueError("M5a label profile kind differs from frozen config")
    if generator["belief_policy_kind"] != profiles["belief_policy_kind"]:
        raise ValueError("M5a belief policy kind differs from frozen config")
    bundle, _, _ = _oracle_runtime(game, int(expected_iterations))
    if (
        generator["solver_checkpoint_sha256"]
        != bundle["solver_checkpoint_sha256"]
        or generator["current_profile_sha256"]
        != bundle["current_profile_sha256"]
        or generator["average_profile_sha256"]
        != bundle["average_profile_sha256"]
    ):
        raise ValueError("M5a generator differs from regenerated exact solver")
    return copy.deepcopy(generator)


def _validate_action_rows(
    value: object,
    *,
    length: int,
    support: tuple[str, ...],
    label: str,
    probabilities: bool,
) -> list[dict[str, float]]:
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"{label} has the wrong private-state count")
    expected = set(support)
    result: list[dict[str, float]] = []
    for index, row in enumerate(value):
        if not isinstance(row, dict) or set(row) != expected:
            raise ValueError(f"{label} row {index} differs from action support")
        converted: dict[str, float] = {}
        for action in support:
            item = row[action]
            if type(item) not in (int, float) or not math.isfinite(float(item)):
                raise ValueError(f"{label} row {index} is non-finite/non-numeric")
            number = float(item)
            if probabilities and number < 0.0:
                raise ValueError(f"{label} row {index} has negative probability")
            converted[action] = number
        if probabilities and abs(fsum(converted.values()) - 1.0) > 1e-12:
            raise ValueError(f"{label} row {index} does not sum to one")
        result.append(converted)
    return result


def _profile_for_generator(
    game: str,
    generator: Mapping[str, object],
    config: Mapping[str, object],
) -> Mapping[tuple[Any, ...], Mapping[str, float]]:
    profiles = config["exact_oracle_profiles"]
    assert isinstance(profiles, dict)
    _, current, average = _oracle_runtime(
        game, int(profiles[f"{game}_lcfr_iterations"])
    )
    if generator["label_profile_kind"] == "current_policy":
        return current
    if generator["label_profile_kind"] == "average_policy":
        return average
    raise ValueError("M5a fixed label profile lacks an embedded exact profile")


def _update_factor_row(
    factors: list[list[float]],
    *,
    actor: int,
    likelihoods: Sequence[float],
) -> None:
    evidence = fsum(
        factors[actor][rank] * float(likelihoods[rank]) for rank in range(3)
    )
    if not math.isfinite(evidence) or evidence <= 0.0:
        raise ValueError("exact profile gives the public action zero factor evidence")
    factors[actor] = [
        factors[actor][rank] * float(likelihoods[rank]) / evidence
        for rank in range(3)
    ]


def _leduc_policy_rows(
    joint: LeducPublicBeliefState,
    profile: Mapping[tuple[Any, ...], Mapping[str, float]],
) -> list[Mapping[str, float]]:
    public_rank = -1 if joint.state.street == 0 else joint.public_rank
    if joint.state.street == 1 and public_rank is None:
        raise ValueError("Leduc policy rows require an observed public rank")
    history = ",".join(joint.state.history)
    return [
        profile[(joint.state.actor, rank, public_rank, history)]
        for rank in range(3)
    ]


def _reconstruct_exact_joint_and_factors(
    *,
    game: str,
    pbs_input: Mapping[str, object],
    provenance: Mapping[str, object],
    profile: Mapping[tuple[Any, ...], Mapping[str, float]],
) -> tuple[object, list[list[float]]]:
    factors = [[1.0 / 3.0] * 3, [1.0 / 3.0] * 3]
    trace = provenance["belief_update_trace"]
    assert isinstance(trace, list)
    if game == "kuhn":
        joint: object = KuhnPublicBeliefState.initial()
        assert isinstance(joint, KuhnPublicBeliefState)
        for entry in trace:
            assert isinstance(entry, dict)
            actor = kuhn_current_player(joint.history)
            if type(entry["actor"]) is not int or entry["actor"] != actor:
                raise ValueError("Kuhn trace actor differs during exact replay")
            action = entry["action"]
            policy_rows = {
                rank: profile[(actor, rank, joint.history)] for rank in range(3)
            }
            _update_factor_row(
                factors,
                actor=actor,
                likelihoods=[policy_rows[rank][action] for rank in range(3)],
            )
            joint = joint.observe(action, policy_rows)
        if joint.history != pbs_input["public_state"]["history"]:
            raise ValueError("Kuhn exact replay public history differs")
    elif game == "leduc":
        joint = LeducPublicBeliefState.initial()
        target_public_rank = pbs_input["public_state"]["public_rank"]
        for entry in trace:
            assert isinstance(entry, dict)
            if joint.chance_pending:
                if type(target_public_rank) is not int:
                    raise ValueError("Leduc postflop replay lacks its public rank")
                joint = joint.observe_public_rank(target_public_rank)
            actor = joint.state.actor
            if type(entry["actor"]) is not int or entry["actor"] != actor:
                raise ValueError("Leduc trace actor differs during exact replay")
            action = entry["action"]
            rows = _leduc_policy_rows(joint, profile)
            _update_factor_row(
                factors,
                actor=actor,
                likelihoods=[rows[rank][action] for rank in range(3)],
            )
            joint = joint.observe_action(action, profile)
        if joint.chance_pending:
            if type(target_public_rank) is not int:
                raise ValueError("Leduc postflop replay lacks its public rank")
            joint = joint.observe_public_rank(target_public_rank)
        public = pbs_input["public_state"]
        if (
            list(joint.state.history) != public["history"]
            or joint.state.street != public["street"]
            or joint.public_rank != public["public_rank"]
        ):
            raise ValueError("Leduc exact replay public state differs")
    else:
        raise ValueError("exact replay game differs")
    for player in (0, 1):
        for rank in range(3):
            if abs(factors[player][rank] - pbs_input["reach_factors"][player][rank]) > 1e-12:
                raise ValueError("PBS reach factors do not follow exact profile Bayes updates")
            exact_marginal = joint.range_for(player)[rank]
            if abs(exact_marginal - pbs_input["projected_marginals"][player][rank]) > 1e-10:
                raise ValueError("PBS projected marginal differs from exact joint oracle")
    return joint, factors


def _rows_match(
    actual: Sequence[Mapping[str, float]],
    expected: Sequence[Mapping[str, float]],
    support: Sequence[str],
    *,
    tolerance: float = 1e-10,
) -> bool:
    return all(
        abs(float(actual[rank][action]) - float(expected[rank][action])) <= tolerance
        for rank in range(3)
        for action in support
    )


def _validate_exact_label_content(
    *,
    game: str,
    pbs_input: Mapping[str, object],
    provenance: Mapping[str, object],
    generator: Mapping[str, object],
    actor: int,
    support: tuple[str, ...],
    actor_policy: Sequence[Mapping[str, float]],
    conditional: Sequence[Mapping[str, float]],
    cfr_values: Sequence[Mapping[str, float]],
    values: Sequence[Sequence[float]],
    optional_policy_target: object,
    config: Mapping[str, object],
) -> str:
    profile = _profile_for_generator(game, generator, config)
    joint, _ = _reconstruct_exact_joint_and_factors(
        game=game,
        pbs_input=pbs_input,
        provenance=provenance,
        profile=profile,
    )
    if game == "kuhn":
        assert isinstance(joint, KuhnPublicBeliefState)
        expected_policy = [
            profile[(actor, rank, joint.history)] for rank in range(3)
        ]
        expected_values_map = joint.on_policy_infostate_values(profile)
        expected_conditional_map = joint.conditional_deviation_action_values(profile)
        expected_cfr_map = joint.cfr_counterfactual_action_values(profile)
    else:
        assert isinstance(joint, LeducPublicBeliefState)
        expected_policy = _leduc_policy_rows(joint, profile)
        expected_values_map = joint.on_policy_private_values(profile)
        expected_conditional_map = joint.conditional_deviation_action_values(profile)
        expected_cfr_map = joint.cfr_counterfactual_action_values(profile)
    expected_values = [
        [float(expected_values_map[player][rank]) for rank in range(3)]
        for player in (0, 1)
    ]
    expected_conditional = [expected_conditional_map[rank] for rank in range(3)]
    expected_cfr = [expected_cfr_map[rank] for rank in range(3)]
    if not _rows_match(actor_policy, expected_policy, support):
        raise ValueError("oracle actor policy differs from regenerated exact profile")
    if not _rows_match(conditional, expected_conditional, support):
        raise ValueError("forced-action Q differs from regenerated exact oracle")
    if not _rows_match(cfr_values, expected_cfr, support):
        raise ValueError("unnormalized CFR values differ from regenerated exact oracle")
    if any(
        abs(float(values[player][rank]) - expected_values[player][rank]) > 1e-10
        for player in (0, 1)
        for rank in range(3)
    ):
        raise ValueError("on-policy private values differ from regenerated exact oracle")
    if optional_policy_target is not None:
        assert isinstance(optional_policy_target, list)
        if not _rows_match(optional_policy_target, expected_policy, support):
            raise ValueError("optional policy target differs from exact label profile")
    certificate_payload = {
        "kind": M5A_EXACT_CERTIFICATE_KIND,
        "game": game,
        "pbs_state_id": small_game_pbs_state_id(pbs_input),
        "pbs_provenance_sha256": _digest(provenance),
        "solver_checkpoint_sha256": generator["solver_checkpoint_sha256"],
        "label_profile_sha256": generator["label_profile_sha256"],
        "actor": actor,
        "action_support": list(support),
        "oracle_actor_policy": [dict(row) for row in expected_policy],
        "oracle_on_policy_private_values": expected_values,
        "oracle_forced_action_conditional_q": [
            dict(row) for row in expected_conditional
        ],
        "oracle_unnormalized_cfr_action_values": [
            dict(row) for row in expected_cfr
        ],
    }
    return _digest(certificate_payload)


_LABEL_FIELDS = {
    "action_support",
    "actor",
    "diagnostics",
    "exact_oracle_certificate_sha256",
    "example_id",
    "game",
    "generator",
    "optional_policy_target",
    "oracle_actor_policy",
    "oracle_forced_action_conditional_q",
    "oracle_on_policy_private_values",
    "oracle_unnormalized_cfr_action_values",
    "pbs_input",
    "pbs_provenance",
    "pbs_provenance_sha256",
    "pbs_snapshot_sha256",
    "pbs_state_id",
    "public_family_id",
    "schema",
    "semantics",
    "split",
}


def validate_label_example(
    example: object,
    config: Mapping[str, object],
) -> dict[str, object]:
    validated_config = validate_m5a_config(dict(config))
    if not isinstance(example, dict) or set(example) != _LABEL_FIELDS:
        raise ValueError("M5a label fields are invalid")
    if example["schema"] != M5A_LABEL_SCHEMA:
        raise ValueError("M5a label schema differs")
    game = example["game"]
    if game not in M5A_ALLOWED_GAMES:
        raise ValueError("M5a labels support only exact Kuhn/Leduc")
    pbs_input = validate_small_game_pbs_input(example["pbs_input"])
    if pbs_input["game"] != game:
        raise ValueError("M5a label and PBS games differ")
    expected_pbs_state_id = small_game_pbs_state_id(pbs_input)
    if not _is_sha256(example["pbs_state_id"]) or example["pbs_state_id"] != expected_pbs_state_id:
        raise ValueError("M5a mathematical PBS state digest differs")
    if not _is_sha256(example["pbs_snapshot_sha256"]) or example["pbs_snapshot_sha256"] != _digest(pbs_input):
        raise ValueError("M5a full PBS snapshot digest differs")
    pbs_provenance = validate_small_game_pbs_provenance(
        example["pbs_provenance"], pbs_input=pbs_input
    )
    if not _is_sha256(example["pbs_provenance_sha256"]) or example["pbs_provenance_sha256"] != _digest(pbs_provenance):
        raise ValueError("M5a PBS provenance digest differs")
    generator = _validate_generator(example["generator"], game=game, config=validated_config)
    if pbs_provenance["belief_policy_kind"] != generator["belief_policy_kind"] or pbs_provenance["belief_profile_sha256"] != generator["belief_profile_sha256"]:
        raise ValueError("M5a PBS belief identity differs from generator")
    actor = example["actor"]
    if type(actor) is not int or actor not in (0, 1):
        raise ValueError("M5a decision label actor must be 0 or 1")
    public_actions = _validate_public_state(game, pbs_input["public_state"])
    if game == "kuhn":
        expected_actor = kuhn_current_player(pbs_input["public_state"]["history"])
    else:
        cursor = leduc_initial_state()
        for _, action in public_actions:
            cursor = leduc_apply_action(cursor, action)
        expected_actor = cursor.actor
    if actor != expected_actor:
        raise ValueError("M5a label actor differs from public state")
    support_value = example["action_support"]
    if (
        not isinstance(support_value, list)
        or not support_value
        or len(set(support_value)) != len(support_value)
        or any(type(action) is not str for action in support_value)
    ):
        raise ValueError("M5a action support is invalid")
    support = tuple(support_value)
    expected_support = (
        kuhn_legal_actions(pbs_input["public_state"]["history"])
        if game == "kuhn"
        else leduc_legal_actions(cursor)
    )
    if support != tuple(expected_support):
        raise ValueError("M5a action support differs from exact game")
    label_valid = pbs_input["label_valid_mask"]
    if not all(all(row) for row in label_valid):
        raise ValueError("M5a exact label rows require every label-valid mask bit")
    values_payload = example["oracle_on_policy_private_values"]
    if not isinstance(values_payload, list) or len(values_payload) != 2:
        raise ValueError("M5a on-policy values must contain both players")
    values = [
        _numeric_vector(row, length=3, label=f"player {player} on-policy values")
        for player, row in enumerate(values_payload)
    ]
    actor_policy = _validate_action_rows(
        example["oracle_actor_policy"],
        length=3,
        support=support,
        label="oracle actor policy",
        probabilities=True,
    )
    conditional = _validate_action_rows(
        example["oracle_forced_action_conditional_q"],
        length=3,
        support=support,
        label="forced-action conditional Q",
        probabilities=False,
    )
    cfr_values = _validate_action_rows(
        example["oracle_unnormalized_cfr_action_values"],
        length=3,
        support=support,
        label="unnormalized CFR action values",
        probabilities=False,
    )
    mixback_residuals = [
        abs(
            fsum(actor_policy[index][action] * conditional[index][action] for action in support)
            - values[actor][index]
        )
        for index in range(3)
    ]
    if max(mixback_residuals) > 1e-10:
        raise ValueError("forced-action Q does not mix back to private value")
    projected = pbs_input["projected_marginals"]
    expectations = [
        fsum(float(projected[player][index]) * values[player][index] for index in range(3))
        for player in (0, 1)
    ]
    if abs(fsum(expectations)) > 1e-10:
        raise ValueError("projected-marginal-weighted private values are not zero-sum")
    cfr_difference = max(
        abs(cfr_values[index][action] - conditional[index][action])
        for index in range(3)
        for action in support
    )
    coverage = validated_config["coverage_contract"]
    assert isinstance(coverage, dict)
    if cfr_difference < float(coverage["minimum_cfr_q_max_abs_separation"]):
        raise ValueError("CFR CFV and forced-action Q namespaces are not separated")
    diagnostics = example["diagnostics"]
    if not isinstance(diagnostics, dict) or set(diagnostics) != {
        "cfr_vs_forced_q_max_abs_difference",
        "forced_q_mixback_max_abs_residual",
        "projected_marginal_weighted_values",
        "zero_sum_residual",
    }:
        raise ValueError("M5a label diagnostics fields differ")
    recorded = diagnostics["projected_marginal_weighted_values"]
    if not isinstance(recorded, list) or len(recorded) != 2 or any(
        type(value) not in (int, float) or not math.isfinite(float(value)) for value in recorded
    ):
        raise ValueError("M5a projected-marginal diagnostics are invalid")
    if any(abs(float(recorded[index]) - expectations[index]) > 1e-12 for index in (0, 1)):
        raise ValueError("M5a projected-marginal diagnostics were forged")
    scalar_diagnostics = {
        "zero_sum_residual": fsum(expectations),
        "forced_q_mixback_max_abs_residual": max(mixback_residuals),
        "cfr_vs_forced_q_max_abs_difference": cfr_difference,
    }
    for field, expected in scalar_diagnostics.items():
        value = diagnostics[field]
        if type(value) not in (int, float) or not math.isfinite(float(value)) or abs(float(value) - expected) > 1e-12:
            raise ValueError(f"M5a {field} diagnostic was forged")
    optional_target = None
    if example["optional_policy_target"] is not None:
        optional_target = _validate_action_rows(
            example["optional_policy_target"],
            length=3,
            support=support,
            label="optional policy target",
            probabilities=True,
        )
    expected_certificate = _validate_exact_label_content(
        game=game,
        pbs_input=pbs_input,
        provenance=pbs_provenance,
        generator=generator,
        actor=actor,
        support=support,
        actor_policy=actor_policy,
        conditional=conditional,
        cfr_values=cfr_values,
        values=values,
        optional_policy_target=optional_target,
        config=validated_config,
    )
    if (
        not _is_sha256(example["exact_oracle_certificate_sha256"])
        or example["exact_oracle_certificate_sha256"] != expected_certificate
    ):
        raise ValueError("M5a exact-oracle recomputation certificate differs")
    if example["semantics"] != validated_config["value_label_semantics"]:
        raise ValueError("M5a label semantics differ from frozen config")
    expected_split, expected_family = assign_split(game, pbs_input, validated_config)
    if example["split"] != expected_split or example["public_family_id"] != expected_family:
        raise ValueError("M5a public-family split assignment differs")
    body = {key: copy.deepcopy(value) for key, value in example.items() if key != "example_id"}
    if not _is_sha256(example["example_id"]) or example["example_id"] != _digest(body):
        raise ValueError("M5a example digest differs")
    return copy.deepcopy(example)


def build_label_example(
    *,
    game: str,
    pbs_input: Mapping[str, object],
    actor: int,
    action_support: Sequence[str],
    oracle_actor_policy: Sequence[Mapping[str, float]],
    oracle_forced_action_conditional_q: Sequence[Mapping[str, float]],
    oracle_unnormalized_cfr_action_values: Sequence[Mapping[str, float]],
    oracle_on_policy_private_values: Sequence[Sequence[float]],
    pbs_provenance: Mapping[str, object],
    generator: Mapping[str, object],
    config: Mapping[str, object],
    optional_policy_target: Sequence[Mapping[str, float]] | None = None,
) -> dict[str, object]:
    validated = validate_m5a_config(dict(config))
    pbs = validate_small_game_pbs_input(dict(pbs_input))
    provenance = validate_small_game_pbs_provenance(
        dict(pbs_provenance), pbs_input=pbs
    )
    validated_generator = _validate_generator(
        dict(generator), game=game, config=validated
    )
    split, family_id = assign_split(game, pbs, validated)
    projected = pbs["projected_marginals"]
    expectations = [
        fsum(
            float(projected[player][index])
            * float(oracle_on_policy_private_values[player][index])
            for index in range(3)
        )
        for player in (0, 1)
    ]
    support = tuple(action_support)
    mixback_residuals = [
        abs(
            fsum(
                float(oracle_actor_policy[index][action])
                * float(oracle_forced_action_conditional_q[index][action])
                for action in support
            )
            - float(oracle_on_policy_private_values[actor][index])
        )
        for index in range(3)
    ]
    cfr_difference = max(
        abs(
            float(oracle_unnormalized_cfr_action_values[index][action])
            - float(oracle_forced_action_conditional_q[index][action])
        )
        for index in range(3)
        for action in support
    )
    certificate = _validate_exact_label_content(
        game=game,
        pbs_input=pbs,
        provenance=provenance,
        generator=validated_generator,
        actor=actor,
        support=support,
        actor_policy=oracle_actor_policy,
        conditional=oracle_forced_action_conditional_q,
        cfr_values=oracle_unnormalized_cfr_action_values,
        values=oracle_on_policy_private_values,
        optional_policy_target=(
            None
            if optional_policy_target is None
            else [dict(row) for row in optional_policy_target]
        ),
        config=validated,
    )
    body: dict[str, object] = {
        "schema": M5A_LABEL_SCHEMA,
        "game": game,
        "exact_oracle_certificate_sha256": certificate,
        "generator": validated_generator,
        "pbs_input": pbs,
        "pbs_state_id": small_game_pbs_state_id(pbs),
        "pbs_snapshot_sha256": _digest(pbs),
        "pbs_provenance": provenance,
        "pbs_provenance_sha256": _digest(provenance),
        "public_family_id": family_id,
        "split": split,
        "actor": actor,
        "action_support": list(support),
        "oracle_actor_policy": [dict(row) for row in oracle_actor_policy],
        "oracle_forced_action_conditional_q": [
            dict(row) for row in oracle_forced_action_conditional_q
        ],
        "oracle_unnormalized_cfr_action_values": [
            dict(row) for row in oracle_unnormalized_cfr_action_values
        ],
        "oracle_on_policy_private_values": [
            list(row) for row in oracle_on_policy_private_values
        ],
        "optional_policy_target": (
            None
            if optional_policy_target is None
            else [dict(row) for row in optional_policy_target]
        ),
        "semantics": copy.deepcopy(validated["value_label_semantics"]),
        "diagnostics": {
            "projected_marginal_weighted_values": expectations,
            "zero_sum_residual": fsum(expectations),
            "forced_q_mixback_max_abs_residual": max(mixback_residuals),
            "cfr_vs_forced_q_max_abs_difference": cfr_difference,
        },
    }
    body["example_id"] = _digest(body)
    return validate_label_example(body, validated)


def _generator_for_game(
    *,
    game: str,
    config: Mapping[str, object],
    config_file_sha256: str,
    source_snapshot_sha256: str,
) -> dict[str, object]:
    profiles = config["exact_oracle_profiles"]
    assert isinstance(profiles, dict)
    bundle, _, _ = _oracle_runtime(
        game, int(profiles[f"{game}_lcfr_iterations"])
    )
    label_kind = str(profiles["label_profile_kind"])
    belief_kind = str(profiles["belief_policy_kind"])
    label_digest = bundle[
        "average_profile_sha256"
        if label_kind == "average_policy"
        else "current_profile_sha256"
    ]
    belief_digest = bundle[
        "average_profile_sha256"
        if belief_kind == "average_policy"
        else "current_profile_sha256"
    ]
    assert isinstance(label_digest, str) and isinstance(belief_digest, str)
    return exact_oracle_provenance(
        game=game,
        solver_checkpoint_sha256=str(bundle["solver_checkpoint_sha256"]),
        solver_iterations=int(bundle["solver_iterations"]),
        current_profile_sha256=str(bundle["current_profile_sha256"]),
        average_profile_sha256=str(bundle["average_profile_sha256"]),
        label_profile_kind=label_kind,
        label_profile_sha256=label_digest,
        belief_policy_kind=belief_kind,
        belief_profile_sha256=belief_digest,
        config_file_sha256=config_file_sha256,
        source_snapshot_sha256=source_snapshot_sha256,
    )


def _make_complete_example(
    *,
    game: str,
    joint: object,
    factors: Sequence[Sequence[float]],
    trace: Sequence[Mapping[str, object]],
    profile: Mapping[tuple[Any, ...], Mapping[str, float]],
    generator: Mapping[str, object],
    config: Mapping[str, object],
) -> dict[str, object]:
    if game == "kuhn":
        assert isinstance(joint, KuhnPublicBeliefState)
        public_state = {"history": joint.history}
        actor = kuhn_current_player(joint.history)
        support = kuhn_legal_actions(joint.history)
        actor_policy = [profile[(actor, rank, joint.history)] for rank in range(3)]
        values_map = joint.on_policy_infostate_values(profile)
        conditional_map = joint.conditional_deviation_action_values(profile)
        cfr_map = joint.cfr_counterfactual_action_values(profile)
    elif game == "leduc":
        assert isinstance(joint, LeducPublicBeliefState)
        public_state = {
            "history": list(joint.state.history),
            "public_rank": joint.public_rank,
            "street": joint.state.street,
        }
        actor = joint.state.actor
        support = leduc_legal_actions(joint.state)
        actor_policy = _leduc_policy_rows(joint, profile)
        values_map = joint.on_policy_private_values(profile)
        conditional_map = joint.conditional_deviation_action_values(profile)
        cfr_map = joint.cfr_counterfactual_action_values(profile)
    else:
        raise ValueError("complete exact example game differs")
    pbs_input = small_game_pbs_input(
        game=game,
        public_state=public_state,
        private_state_order=M5A_PRIVATE_STATE_ORDER,
        reach_factors=factors,
    )
    provenance = small_game_pbs_provenance(
        pbs_input=pbs_input,
        belief_policy_kind=str(generator["belief_policy_kind"]),
        belief_profile_sha256=str(generator["belief_profile_sha256"]),
        belief_update_trace=trace,
    )
    values = [
        [float(values_map[player][rank]) for rank in range(3)]
        for player in (0, 1)
    ]
    conditional = [dict(conditional_map[rank]) for rank in range(3)]
    cfr_values = [dict(cfr_map[rank]) for rank in range(3)]
    return build_label_example(
        game=game,
        pbs_input=pbs_input,
        pbs_provenance=provenance,
        actor=actor,
        action_support=support,
        oracle_actor_policy=actor_policy,
        oracle_forced_action_conditional_q=conditional,
        oracle_unnormalized_cfr_action_values=cfr_values,
        oracle_on_policy_private_values=values,
        generator=generator,
        config=config,
        optional_policy_target=actor_policy,
    )


def build_complete_exact_label_examples(
    *,
    config: Mapping[str, object],
    config_file_sha256: str,
    source_snapshot_sha256: str,
) -> list[dict[str, object]]:
    """Enumerate every profile-reachable Kuhn/Leduc public decision node."""

    validated = validate_m5a_config(dict(config))
    if not _is_sha256(config_file_sha256) or not _is_sha256(source_snapshot_sha256):
        raise ValueError("complete exact-label binding digest is invalid")
    profiles = validated["exact_oracle_profiles"]
    assert isinstance(profiles, dict)
    examples: list[dict[str, object]] = []

    kuhn_generator = _generator_for_game(
        game="kuhn",
        config=validated,
        config_file_sha256=config_file_sha256,
        source_snapshot_sha256=source_snapshot_sha256,
    )
    _, _, kuhn_average = _oracle_runtime(
        "kuhn", int(profiles["kuhn_lcfr_iterations"])
    )
    kuhn_profile = kuhn_average

    def visit_kuhn(
        joint: KuhnPublicBeliefState,
        factors: list[list[float]],
        trace: list[dict[str, object]],
    ) -> None:
        examples.append(
            _make_complete_example(
                game="kuhn",
                joint=joint,
                factors=factors,
                trace=trace,
                profile=kuhn_profile,
                generator=kuhn_generator,
                config=validated,
            )
        )
        actor = kuhn_current_player(joint.history)
        rows = {rank: kuhn_profile[(actor, rank, joint.history)] for rank in range(3)}
        for action in kuhn_legal_actions(joint.history):
            next_joint = joint.observe(action, rows)
            if kuhn_is_terminal(next_joint.history):
                continue
            next_factors = [list(row) for row in factors]
            _update_factor_row(
                next_factors,
                actor=actor,
                likelihoods=[rows[rank][action] for rank in range(3)],
            )
            next_trace = trace + [
                {
                    "actor": actor,
                    "action": action,
                    "belief_policy_kind": kuhn_generator["belief_policy_kind"],
                    "belief_profile_sha256": kuhn_generator["belief_profile_sha256"],
                }
            ]
            visit_kuhn(next_joint, next_factors, next_trace)

    visit_kuhn(KuhnPublicBeliefState.initial(), [[1.0 / 3.0] * 3 for _ in range(2)], [])

    leduc_generator = _generator_for_game(
        game="leduc",
        config=validated,
        config_file_sha256=config_file_sha256,
        source_snapshot_sha256=source_snapshot_sha256,
    )
    _, _, leduc_average = _oracle_runtime(
        "leduc", int(profiles["leduc_lcfr_iterations"])
    )
    leduc_profile = leduc_average

    def visit_leduc(
        joint: LeducPublicBeliefState,
        factors: list[list[float]],
        trace: list[dict[str, object]],
    ) -> None:
        if joint.state.terminal:
            return
        if joint.chance_pending:
            for public_rank in range(3):
                visit_leduc(
                    joint.observe_public_rank(public_rank),
                    [list(row) for row in factors],
                    list(trace),
                )
            return
        examples.append(
            _make_complete_example(
                game="leduc",
                joint=joint,
                factors=factors,
                trace=trace,
                profile=leduc_profile,
                generator=leduc_generator,
                config=validated,
            )
        )
        actor = joint.state.actor
        rows = _leduc_policy_rows(joint, leduc_profile)
        for action in leduc_legal_actions(joint.state):
            next_factors = [list(row) for row in factors]
            _update_factor_row(
                next_factors,
                actor=actor,
                likelihoods=[rows[rank][action] for rank in range(3)],
            )
            next_trace = trace + [
                {
                    "actor": actor,
                    "action": action,
                    "belief_policy_kind": leduc_generator["belief_policy_kind"],
                    "belief_profile_sha256": leduc_generator["belief_profile_sha256"],
                }
            ]
            visit_leduc(
                joint.observe_action(action, leduc_profile),
                next_factors,
                next_trace,
            )

    visit_leduc(
        LeducPublicBeliefState.initial(),
        [[1.0 / 3.0] * 3 for _ in range(2)],
        [],
    )
    identity_manifest = [
        {
            "game": item["game"],
            "public_family_id": item["public_family_id"],
            "pbs_state_id": item["pbs_state_id"],
            "actor": item["actor"],
            "action_support": item["action_support"],
        }
        for item in examples
    ]
    if len({_digest(item) for item in identity_manifest}) != len(examples):
        raise ValueError("complete exact-label enumeration repeats a public PBS identity")
    coverage = validated["coverage_contract"]
    assert isinstance(coverage, dict)
    game_counts = {
        game: sum(item["game"] == game for item in examples)
        for game in M5A_ALLOWED_GAMES
    }
    split_counts = {
        split: sum(item["split"] == split for item in examples)
        for split in M5A_SPLITS
    }
    if (
        game_counts["kuhn"] != coverage["kuhn_public_decision_nodes"]
        or game_counts["leduc"] != coverage["leduc_public_decision_nodes"]
        or len(examples) != coverage["total_public_decision_nodes"]
        or split_counts != coverage["split_counts"]
        or _digest(identity_manifest) != coverage["identity_manifest_sha256"]
    ):
        raise ValueError("complete exact-label enumeration differs from frozen coverage")
    return examples


_ARTIFACT_BODY_FIELDS = {
    "config",
    "config_file_sha256",
    "example_sha256",
    "examples",
    "games",
    "generator_bindings",
    "generator_bindings_sha256",
    "large_training_authorized",
    "network_training_started",
    "oracle_bundles",
    "oracle_bundles_sha256",
    "online_search_implemented",
    "source_snapshot",
    "source_snapshot_sha256",
    "split_counts",
    "stage",
}


def _validate_source_snapshot(source_snapshot: object) -> dict[str, str]:
    if not isinstance(source_snapshot, dict) or not source_snapshot:
        raise ValueError("M5a source snapshot is invalid")
    if source_snapshot != dict(sorted(source_snapshot.items())):
        raise ValueError("M5a source snapshot is not canonical")
    result: dict[str, str] = {}
    for path, digest in source_snapshot.items():
        relative = Path(path)
        if (
            type(path) is not str
            or relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or not _is_sha256(digest)
        ):
            raise ValueError("M5a source snapshot entry is invalid")
        result[path] = digest
    missing = M5A_CRITICAL_SOURCE_PATHS.difference(result)
    if missing:
        raise ValueError(
            "M5a source snapshot omits critical files: " + ",".join(sorted(missing))
        )
    return result


def build_label_artifact(
    *,
    config: Mapping[str, object],
    config_file_sha256: str,
    source_snapshot: Mapping[str, str],
) -> dict[str, object]:
    validated = validate_m5a_config(dict(config))
    if not _is_sha256(config_file_sha256):
        raise ValueError("M5a config file digest is invalid")
    sources = _validate_source_snapshot(dict(sorted(source_snapshot.items())))
    source_digest = _digest(sources)
    validated_examples = build_complete_exact_label_examples(
        config=validated,
        config_file_sha256=config_file_sha256,
        source_snapshot_sha256=source_digest,
    )
    if len({item["example_id"] for item in validated_examples}) != len(validated_examples):
        raise ValueError("M5a artifact repeats an example")
    bindings: dict[str, dict[str, object]] = {}
    for example in validated_examples:
        game = str(example["game"])
        generator = copy.deepcopy(example["generator"])
        prior = bindings.setdefault(game, generator)
        if prior != generator:
            raise ValueError(f"M5a artifact has multiple {game} generator bindings")
        if generator["config_file_sha256"] != config_file_sha256:
            raise ValueError("M5a example generator config digest differs")
        if generator["source_snapshot_sha256"] != source_digest:
            raise ValueError("M5a example generator source digest differs")
    bindings = dict(sorted(bindings.items()))
    oracle_bundles = {
        game: exact_oracle_bundle(game, validated) for game in M5A_ALLOWED_GAMES
    }
    split_counts = {
        split: sum(item["split"] == split for item in validated_examples)
        for split in M5A_SPLITS
    }
    games = sorted({str(item["game"]) for item in validated_examples})
    body = {
        "stage": validated["stage"],
        "config": validated,
        "config_file_sha256": config_file_sha256,
        "examples": validated_examples,
        "example_sha256": _digest(validated_examples),
        "games": games,
        "split_counts": split_counts,
        "source_snapshot": sources,
        "source_snapshot_sha256": source_digest,
        "generator_bindings": bindings,
        "generator_bindings_sha256": _digest(bindings),
        "oracle_bundles": oracle_bundles,
        "oracle_bundles_sha256": _digest(oracle_bundles),
        "large_training_authorized": False,
        "network_training_started": False,
        "online_search_implemented": False,
    }
    wrapper = {
        "schema": M5A_ARTIFACT_SCHEMA,
        "body": body,
        "body_sha256": _digest(body),
    }
    return validate_label_artifact(wrapper, validated)


def validate_label_artifact(
    payload: object,
    config: Mapping[str, object],
) -> dict[str, object]:
    validated = validate_m5a_config(dict(config))
    if not isinstance(payload, dict) or set(payload) != {"schema", "body", "body_sha256"}:
        raise ValueError("M5a artifact wrapper fields are invalid")
    if payload["schema"] != M5A_ARTIFACT_SCHEMA:
        raise ValueError("M5a artifact schema differs")
    body = payload["body"]
    if not isinstance(body, dict) or set(body) != _ARTIFACT_BODY_FIELDS:
        raise ValueError("M5a artifact body fields are invalid")
    if not _is_sha256(payload["body_sha256"]) or payload["body_sha256"] != _digest(body):
        raise ValueError("M5a artifact body digest differs")
    body_config = validate_m5a_config(body["config"])
    if (
        canonical_bytes(body_config) != canonical_bytes(validated)
        or body["stage"] != validated["stage"]
    ):
        raise ValueError("M5a artifact frozen config/stage differs")
    if not _is_sha256(body["config_file_sha256"]):
        raise ValueError("M5a artifact config file digest is invalid")
    for flag in (
        "large_training_authorized",
        "network_training_started",
        "online_search_implemented",
    ):
        if body[flag] is not False:
            raise ValueError(f"M5a artifact requires {flag}=false")
    examples = body["examples"]
    if not isinstance(examples, list) or not examples:
        raise ValueError("M5a artifact examples are missing")
    validated_examples = [validate_label_example(item, validated) for item in examples]
    if len({item["example_id"] for item in validated_examples}) != len(validated_examples):
        raise ValueError("M5a artifact repeats an example")
    if body["example_sha256"] != _digest(validated_examples):
        raise ValueError("M5a artifact example digest differs")
    expected_counts = {
        split: sum(item["split"] == split for item in validated_examples)
        for split in M5A_SPLITS
    }
    if (
        not isinstance(body["split_counts"], dict)
        or set(body["split_counts"]) != set(M5A_SPLITS)
        or any(type(body["split_counts"][split]) is not int for split in M5A_SPLITS)
        or canonical_bytes(body["split_counts"]) != canonical_bytes(expected_counts)
        or any(count <= 0 for count in expected_counts.values())
    ):
        raise ValueError("M5a artifact must contain every deterministic split")
    expected_games = sorted({str(item["game"]) for item in validated_examples})
    if body["games"] != expected_games or expected_games != list(M5A_ALLOWED_GAMES):
        raise ValueError("M5a artifact exact-oracle game coverage differs")
    sources = _validate_source_snapshot(body["source_snapshot"])
    source_digest = _digest(sources)
    if body["source_snapshot_sha256"] != source_digest:
        raise ValueError("M5a artifact source snapshot digest differs")
    expected_examples = build_complete_exact_label_examples(
        config=validated,
        config_file_sha256=str(body["config_file_sha256"]),
        source_snapshot_sha256=source_digest,
    )
    if validated_examples != expected_examples:
        raise ValueError("M5a artifact does not cover the complete exact public tree")
    bindings: dict[str, dict[str, object]] = {}
    for example in validated_examples:
        game = str(example["game"])
        generator = example["generator"]
        prior = bindings.setdefault(game, generator)
        if prior != generator:
            raise ValueError(f"M5a artifact has multiple {game} generator bindings")
        if generator["config_file_sha256"] != body["config_file_sha256"]:
            raise ValueError("M5a generator/config binding differs")
        if generator["source_snapshot_sha256"] != source_digest:
            raise ValueError("M5a generator/source binding differs")
    bindings = dict(sorted(bindings.items()))
    if canonical_bytes(body["generator_bindings"]) != canonical_bytes(bindings) or body["generator_bindings_sha256"] != _digest(bindings):
        raise ValueError("M5a generator bindings differ")
    expected_bundles = {
        game: exact_oracle_bundle(game, validated) for game in M5A_ALLOWED_GAMES
    }
    if (
        canonical_bytes(body["oracle_bundles"])
        != canonical_bytes(expected_bundles)
        or body["oracle_bundles_sha256"] != _digest(expected_bundles)
    ):
        raise ValueError("M5a embedded exact solver/profile bundles differ")
    return copy.deepcopy(payload)


def load_label_artifact(
    path: str | Path,
    config: Mapping[str, object],
) -> dict[str, object]:
    return validate_label_artifact(strict_json_loads(stable_read_path(path)), config)


def verify_label_artifact_files(
    payload: object,
    *,
    config_path: str | Path,
    source_root: str | Path,
) -> dict[str, object]:
    """Verify the artifact against current config and fd-stable source bytes."""

    config, config_sha256 = load_m5a_config(config_path)
    validated = validate_label_artifact(payload, config)
    body = validated["body"]
    assert isinstance(body, dict)
    if body["config_file_sha256"] != config_sha256:
        raise ValueError("M5a artifact config file changed")
    root = Path(source_root)
    sources = body["source_snapshot"]
    assert isinstance(sources, dict)
    for relative, expected in sources.items():
        actual = sha256_bytes(stable_read_relative(root, Path(relative)))
        if actual != expected:
            raise ValueError(f"M5a artifact source changed: {relative}")
    return validated
