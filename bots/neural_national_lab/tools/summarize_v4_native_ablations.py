#!/usr/bin/env python3
"""Summarize four candidate-only diagnostic v4 native-TCP ablation reports.

This is deliberately a JSON-only, stdlib-only evidence reducer.  It reads only
the report paths named on the command line.  It does not discover ratings,
Glicko state, policy roles, manifests, or any other repository data.

The statistical unit is a complete paired seed block: the forward and swapped
70-hand legs stay together in every bootstrap resample.  The primary effect is
the paired change in ``net_chips > 0`` for each 70-hand leg.  Chip delta per
hand is reported separately as a secondary diagnostic and never participates
in the primary direction or ordering.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import random
from typing import Any, Iterable, Mapping, Sequence


SUMMARY_SCHEMA = "opponent_multitask_v4_native_ablation_summary_v1"
SUMMARY_METHOD = "paired_complete_seed_block_cluster_bootstrap_v1"
ABLATION_SCHEMA = "opponent_multitask_v4_native_ablation_v1"
PRIMARY_CRITERION = (
    "full_minus_ablation_70_hand_net_chips_gt_zero_paired_uplift"
)
SECONDARY_CRITERION = "full_minus_ablation_net_chips_delta_per_hand"

ABLATION_MODES = (
    "full",
    "neural_off",
    "cross_hand_off",
    "outcome_uncertainty_match_off",
)
COMPARISON_MODES = ABLATION_MODES[1:]
ENV_KEYS = (
    "POK_V4_DISABLE",
    "POK_V4_DISABLE_CROSS_HAND",
    "POK_V4_DISABLE_OUTCOME_UNCERTAINTY_MATCH",
)
MODE_ENV_OVERRIDES: dict[str, dict[str, str | None]] = {
    "full": {
        "POK_V4_DISABLE": None,
        "POK_V4_DISABLE_CROSS_HAND": None,
        "POK_V4_DISABLE_OUTCOME_UNCERTAINTY_MATCH": None,
    },
    "neural_off": {
        "POK_V4_DISABLE": "1",
        "POK_V4_DISABLE_CROSS_HAND": None,
        "POK_V4_DISABLE_OUTCOME_UNCERTAINTY_MATCH": None,
    },
    "cross_hand_off": {
        "POK_V4_DISABLE": None,
        "POK_V4_DISABLE_CROSS_HAND": "1",
        "POK_V4_DISABLE_OUTCOME_UNCERTAINTY_MATCH": None,
    },
    "outcome_uncertainty_match_off": {
        "POK_V4_DISABLE": None,
        "POK_V4_DISABLE_CROSS_HAND": None,
        "POK_V4_DISABLE_OUTCOME_UNCERTAINTY_MATCH": "1",
    },
}
OPPONENT_ENV_OVERRIDES = {key: None for key in ENV_KEYS}
ABLATION_CONTRACT_KEYS = {
    "schema",
    "mode",
    "candidate_env_overrides",
    "opponent_env_overrides",
    "diagnostic_only",
    "eligible_as_strength_evidence",
    "protected_data_read",
    "policy_roles_opened",
    "deployment_policy_value",
    "strength_evidence",
}
ZERO_COUNTER_FIELDS = (
    "candidate_illegal",
    "candidate_timeouts",
    "opponent_illegal",
    "opponent_timeouts",
    "adapter_actions_candidate",
    "adapter_actions_opponent",
)
LEG_NAMES = ("forward", "swapped")
HANDS_PER_LEG = 70
DEFAULT_BOOTSTRAP_SAMPLES = 20_000
DEFAULT_BOOTSTRAP_SEED = 20_260_712
INPUT_OPTIONAL_CONTROL_VALUES = {
    "protected_data_read": False,
    "policy_roles_opened": [],
    "diagnostic_only": True,
    "eligible_as_strength_evidence": False,
    "deployment_policy_value": False,
    "deployment_eligible": False,
    "native_strength_evidence": False,
    "official_exe_accepted": False,
    "formal_release_evidence": False,
}
SUMMARY_ROOT_KEYS = {
    "schema",
    "method",
    "input_reports",
    "validated_contract",
    "execution_identity",
    "comparisons",
    "protected_data_read",
    "policy_roles_opened",
    "diagnostic_only",
    "eligible_as_strength_evidence",
    "deployment_policy_value",
    "deployment_eligible",
    "strength_evidence",
    "native_strength_evidence",
    "official_exe_accepted",
    "formal_release_evidence",
    "payload_sha256",
}


@dataclass(frozen=True)
class _LoadedReport:
    mode: str
    source: str
    raw_bytes: bytes
    sha256: str
    payload: dict[str, Any]
    candidate_artifact: tuple[str, str]
    opponent_artifacts: tuple[tuple[str, str], ...]
    plan_signature: tuple[Any, ...]
    clusters: dict[tuple[str, int], dict[str, Any]]


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def summary_payload_sha256(payload: Mapping[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("payload_sha256", None)
    return hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()


def _finite_tree(value: Any, *, field: str = "payload") -> None:
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _finite_tree(item, field=f"{field}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field} contains a non-string JSON key")
            _finite_tree(item, field=f"{field}.{key}")
        return
    raise ValueError(f"{field} contains a non-JSON value")


def _strict_json_loads(raw: bytes, *, source: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{source}: report is not UTF-8") from exc

    def reject_constant(value: str) -> None:
        raise ValueError(f"{source}: non-finite JSON constant {value!r}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{source}: duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source}: invalid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{source}: report root must be an object")
    _finite_tree(payload, field=source)
    return payload


def _object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _list(value: Any, *, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return value


def _integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _nonempty_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _digest(value: Any, *, field: str) -> str:
    digest = _nonempty_string(value, field=field)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{field} must be a lowercase sha256 digest")
    return digest


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, field: str) -> None:
    observed = set(value)
    if observed != expected:
        raise ValueError(
            f"{field} keys differ: missing={sorted(expected - observed)!r} "
            f"extra={sorted(observed - expected)!r}"
        )


def _validate_ablation_contract(raw: Any, *, source: str) -> str:
    contract = _object(raw, field=f"{source}.candidate_ablation")
    _exact_keys(
        contract,
        ABLATION_CONTRACT_KEYS,
        field=f"{source}.candidate_ablation",
    )
    if contract.get("schema") != ABLATION_SCHEMA:
        raise ValueError(f"{source}: unsupported candidate_ablation schema")
    mode = contract.get("mode")
    if mode not in ABLATION_MODES:
        raise ValueError(f"{source}: unsupported candidate_ablation mode {mode!r}")

    candidate_env = _object(
        contract.get("candidate_env_overrides"),
        field=f"{source}.candidate_ablation.candidate_env_overrides",
    )
    opponent_env = _object(
        contract.get("opponent_env_overrides"),
        field=f"{source}.candidate_ablation.opponent_env_overrides",
    )
    _exact_keys(candidate_env, set(ENV_KEYS), field=f"{source}.candidate_env_overrides")
    _exact_keys(opponent_env, set(ENV_KEYS), field=f"{source}.opponent_env_overrides")
    if candidate_env != MODE_ENV_OVERRIDES[mode]:
        raise ValueError(f"{source}: candidate env does not match mode {mode!r}")
    if opponent_env != OPPONENT_ENV_OVERRIDES:
        raise ValueError(f"{source}: opponent env overrides must all be null")

    expected_scalars = {
        "diagnostic_only": mode != "full",
        "eligible_as_strength_evidence": mode == "full",
        "protected_data_read": False,
        "policy_roles_opened": [],
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    for field, expected in expected_scalars.items():
        if contract.get(field) != expected or type(contract.get(field)) is not type(expected):
            raise ValueError(
                f"{source}: candidate_ablation.{field} must equal {expected!r}"
            )
    return str(mode)


def _validate_artifact(raw: Any, *, field: str) -> tuple[str, str]:
    artifact = _object(raw, field=field)
    path = _nonempty_string(artifact.get("path"), field=f"{field}.path")
    before = _digest(artifact.get("sha256_before"), field=f"{field}.sha256_before")
    after = _digest(artifact.get("sha256_after"), field=f"{field}.sha256_after")
    if artifact.get("stable") is not True or before != after:
        raise ValueError(f"{field} is not a stable execution artifact")
    return path, before


def _validate_zero_compliance(value: Mapping[str, Any], *, field: str) -> None:
    if value.get("passed_compliance") is not True:
        raise ValueError(f"{field}.passed_compliance must be true")
    if value.get("wrapper_used") is not False:
        raise ValueError(f"{field}.wrapper_used must be false")
    issues = value.get("issues")
    if not isinstance(issues, list) or issues:
        raise ValueError(f"{field}.issues must be an empty list")
    for counter in ZERO_COUNTER_FIELDS:
        if _integer(value.get(counter), field=f"{field}.{counter}") != 0:
            raise ValueError(f"{field}.{counter} must be zero")


def _validate_native_process(
    value: Any, *, field: str, expected_bot_seed: int
) -> None:
    native = _object(value, field=field)
    if _integer(native.get("returncode"), field=f"{field}.returncode") != 0:
        raise ValueError(f"{field}.returncode must be zero")
    if (
        _integer(native.get("bot_seed"), field=f"{field}.bot_seed")
        != expected_bot_seed
    ):
        raise ValueError(f"{field}.bot_seed drifted")
    for counter in ("process_failures", "json_response_stdout"):
        if _integer(native.get(counter), field=f"{field}.{counter}") != 0:
            raise ValueError(f"{field}.{counter} must be zero")
    if not isinstance(native.get("decision_trace"), list):
        raise ValueError(f"{field}.decision_trace must be a list")


def _integer_vector(value: Any, *, field: str, length: int) -> list[int]:
    raw = _list(value, field=field)
    if len(raw) != length:
        raise ValueError(f"{field} must contain exactly {length} values")
    return [_integer(item, field=f"{field}[{index}]") for index, item in enumerate(raw)]


def _validate_leg(
    raw: Any,
    *,
    field: str,
    expected_leg: str | None,
    candidate: str,
    opponent: str,
    opponent_path: str,
    match_idx: int,
    deck_seed: int,
    bot_seed: int,
) -> dict[str, Any]:
    leg = _object(raw, field=field)
    name = leg.get("leg")
    if name not in LEG_NAMES or (expected_leg is not None and name != expected_leg):
        raise ValueError(f"{field}.leg must be one of {LEG_NAMES!r}")
    if leg.get("candidate") != candidate or leg.get("opponent") != opponent:
        raise ValueError(f"{field}: candidate/opponent identity drift")
    if leg.get("opponent_path") != opponent_path:
        raise ValueError(f"{field}.opponent_path drifted")
    if _integer(leg.get("match_idx"), field=f"{field}.match_idx") != match_idx:
        raise ValueError(f"{field}.match_idx drifted")
    if _integer(leg.get("deck_seed_base"), field=f"{field}.deck_seed_base") != deck_seed:
        raise ValueError(f"{field}.deck_seed_base drifted")
    if _integer(leg.get("bot_seed_base"), field=f"{field}.bot_seed_base") != bot_seed:
        raise ValueError(f"{field}.bot_seed_base drifted")
    if _integer(leg.get("hands_played"), field=f"{field}.hands_played") != HANDS_PER_LEG:
        raise ValueError(f"{field} is not a complete 70-hand leg")
    _validate_zero_compliance(leg, field=field)
    _validate_native_process(
        leg.get("candidate_native"),
        field=f"{field}.candidate_native",
        expected_bot_seed=bot_seed if name == "forward" else bot_seed + 1,
    )
    _validate_native_process(
        leg.get("opponent_native"),
        field=f"{field}.opponent_native",
        expected_bot_seed=bot_seed + 1 if name == "forward" else bot_seed,
    )
    hand_chips = _integer_vector(
        leg.get("hand_net_chips"),
        field=f"{field}.hand_net_chips",
        length=HANDS_PER_LEG,
    )
    net_chips = _integer(leg.get("net_chips"), field=f"{field}.net_chips")
    if sum(hand_chips) != net_chips:
        raise ValueError(f"{field}: hand chip accounting does not equal net_chips")
    return {
        "leg": str(name),
        "net_chips": net_chips,
        "hand_net_chips": hand_chips,
        "deck_seed_base": deck_seed,
        "bot_seed_base": bot_seed,
    }


def _validate_report(raw: bytes, *, source: str) -> _LoadedReport:
    payload = _strict_json_loads(raw, source=source)
    mode = _validate_ablation_contract(payload.get("candidate_ablation"), source=source)
    if payload.get("format") != "native_tcp_evaluation_v2":
        raise ValueError(f"{source}: format must be native_tcp_evaluation_v2")
    if payload.get("execution_mode") != "native_tcp":
        raise ValueError(f"{source}: execution_mode must be native_tcp")
    if payload.get("paired") is not True:
        raise ValueError(f"{source}: paired must be true")
    if _integer(payload.get("hands_per_match"), field=f"{source}.hands_per_match") != HANDS_PER_LEG:
        raise ValueError(f"{source}: hands_per_match must be 70")
    if payload.get("requires_native_opponents") is not True:
        raise ValueError(f"{source}: native opponents must be required")
    if payload.get("legacy_debug_wrapper_enabled") is not False:
        raise ValueError(f"{source}: legacy wrapper must be disabled")
    if payload.get("wrapper_used") is not False:
        raise ValueError(f"{source}: wrapper_used must be false")

    strength = _object(
        payload.get("strength_evidence"), field=f"{source}.strength_evidence"
    )
    _exact_keys(
        strength,
        {"requested", "passed", "request_errors", "result_errors"},
        field=f"{source}.strength_evidence",
    )
    requested = strength.get("requested")
    passed = strength.get("passed")
    if not isinstance(requested, bool) or not isinstance(passed, bool):
        raise ValueError(f"{source}: strength evidence flags must be booleans")
    for field in ("request_errors", "result_errors"):
        errors = strength.get(field)
        if not isinstance(errors, list) or any(
            not isinstance(error, str) for error in errors
        ):
            raise ValueError(
                f"{source}.strength_evidence.{field} must be a string list"
            )
    strength_errors = [
        *strength["request_errors"],
        *strength["result_errors"],
    ]
    if mode != "full":
        if requested or passed or strength_errors:
            raise ValueError(
                f"{source}: non-full ablation must not claim strength evidence"
            )
    elif requested:
        if not passed or strength_errors:
            raise ValueError(
                f"{source}: requested full strength evidence must have passed cleanly"
            )
    elif passed or strength_errors:
        raise ValueError(
            f"{source}: unrequested full strength evidence must be false and clean"
        )
    for field, expected in INPUT_OPTIONAL_CONTROL_VALUES.items():
        if field in payload and (
            payload.get(field) != expected
            or type(payload.get(field)) is not type(expected)
        ):
            raise ValueError(
                f"{source}.{field} must equal {expected!r} when present"
            )

    force = _object(payload.get("force"), field=f"{source}.force")
    _exact_keys(force, {"hand", "decision", "action"}, field=f"{source}.force")
    if force != {"hand": None, "decision": None, "action": None}:
        raise ValueError(f"{source}: forced actions are forbidden")

    candidate_path = _nonempty_string(
        payload.get("candidate_path"), field=f"{source}.candidate_path"
    )
    opponent_paths_raw = _list(
        payload.get("opponent_paths"), field=f"{source}.opponent_paths"
    )
    opponent_paths = tuple(
        _nonempty_string(path, field=f"{source}.opponent_paths[{index}]")
        for index, path in enumerate(opponent_paths_raw)
    )
    if not opponent_paths or len(set(opponent_paths)) != len(opponent_paths):
        raise ValueError(f"{source}: opponent_paths must be non-empty and unique")

    artifacts = _object(
        payload.get("execution_artifacts"), field=f"{source}.execution_artifacts"
    )
    candidate_artifact = _validate_artifact(
        artifacts.get("candidate"), field=f"{source}.execution_artifacts.candidate"
    )
    if candidate_artifact[0] != candidate_path:
        raise ValueError(f"{source}: candidate artifact path drifted")
    opponent_artifact_rows = _list(
        artifacts.get("opponents"), field=f"{source}.execution_artifacts.opponents"
    )
    opponent_artifacts = tuple(
        _validate_artifact(item, field=f"{source}.execution_artifacts.opponents[{index}]")
        for index, item in enumerate(opponent_artifact_rows)
    )
    if tuple(path for path, _ in opponent_artifacts) != opponent_paths:
        raise ValueError(f"{source}: opponent artifact paths drifted")

    seeds = tuple(
        _integer(seed, field=f"{source}.seeds[{index}]")
        for index, seed in enumerate(_list(payload.get("seeds"), field=f"{source}.seeds"))
    )
    if len(seeds) < 3 or len(set(seeds)) != len(seeds):
        raise ValueError(f"{source}: at least three unique base seeds are required")
    actual_deck_seeds = tuple(
        _integer(seed, field=f"{source}.actual_deck_seed_bases[{index}]")
        for index, seed in enumerate(
            _list(
                payload.get("actual_deck_seed_bases"),
                field=f"{source}.actual_deck_seed_bases",
            )
        )
    )
    if list(actual_deck_seeds) != sorted(set(actual_deck_seeds)):
        raise ValueError(f"{source}: actual deck seeds must be sorted and unique")
    if payload.get("deck_seed_scheme") != "opponent_disjoint_match_blocks_v1":
        raise ValueError(f"{source}: unsupported deck seed scheme")
    opponent_seed_stride = _integer(
        payload.get("opponent_seed_stride"), field=f"{source}.opponent_seed_stride"
    )
    bot_seed_base = _integer(payload.get("bot_seed_base"), field=f"{source}.bot_seed_base")
    bot_seed_stride = _integer(
        payload.get("bot_seed_stride"), field=f"{source}.bot_seed_stride"
    )
    if opponent_seed_stride <= 0 or bot_seed_stride <= 0:
        raise ValueError(f"{source}: seed strides must be positive")
    workers = _integer(payload.get("workers"), field=f"{source}.workers")
    if not 1 <= workers <= 4:
        raise ValueError(f"{source}: workers must be in the inclusive range 1..4")

    rows = _list(payload.get("rows"), field=f"{source}.rows")
    expected_row_count = len(opponent_paths) * len(seeds)
    if len(rows) != expected_row_count:
        raise ValueError(
            f"{source}: row count {len(rows)} does not equal complete plan {expected_row_count}"
        )
    clusters: dict[tuple[str, int], dict[str, Any]] = {}
    path_to_name: dict[str, str] = {}
    observed_deck_seeds: list[int] = []
    observed_bot_seed_bases: list[int] = []
    row_metadata: list[tuple[Any, ...]] = []
    leg_keys: list[tuple[str, int, str]] = []

    for row_index, row_raw in enumerate(rows):
        field = f"{source}.rows[{row_index}]"
        row = _object(row_raw, field=field)
        if row.get("leg") != "paired":
            raise ValueError(f"{field}.leg must be paired")
        candidate = _nonempty_string(row.get("candidate"), field=f"{field}.candidate")
        opponent = _nonempty_string(row.get("opponent"), field=f"{field}.opponent")
        opponent_path = _nonempty_string(
            row.get("opponent_path"), field=f"{field}.opponent_path"
        )
        if opponent_path not in opponent_paths:
            raise ValueError(f"{field}: opponent_path is not in the evaluation plan")
        prior_name = path_to_name.setdefault(opponent_path, opponent)
        if prior_name != opponent:
            raise ValueError(f"{field}: opponent label drifted within one artifact")
        match_idx = _integer(row.get("match_idx"), field=f"{field}.match_idx")
        if not 0 <= match_idx < len(seeds):
            raise ValueError(f"{field}.match_idx is outside the seed plan")
        opponent_idx = opponent_paths.index(opponent_path)
        expected_deck_seed = seeds[match_idx] + opponent_idx * opponent_seed_stride
        deck_seed = _integer(row.get("deck_seed_base"), field=f"{field}.deck_seed_base")
        if deck_seed != expected_deck_seed:
            raise ValueError(f"{field}.deck_seed_base does not match the seed plan")
        expected_bot_seed = (
            bot_seed_base + match_idx * bot_seed_stride + opponent_idx * 100_000
        )
        bot_seed = _integer(row.get("bot_seed_base"), field=f"{field}.bot_seed_base")
        if bot_seed != expected_bot_seed:
            raise ValueError(f"{field}.bot_seed_base does not match the seed plan")
        if _integer(row.get("hands_played"), field=f"{field}.hands_played") != 140:
            raise ValueError(f"{field} must contain two complete 70-hand legs")
        _validate_zero_compliance(row, field=field)

        nested = _list(row.get("legs"), field=f"{field}.legs")
        if len(nested) != 2:
            raise ValueError(f"{field}.legs must contain exactly two legs")
        legs: dict[str, dict[str, Any]] = {}
        for leg_index, leg_raw in enumerate(nested):
            validated_leg = _validate_leg(
                leg_raw,
                field=f"{field}.legs[{leg_index}]",
                expected_leg=None,
                candidate=candidate,
                opponent=opponent,
                opponent_path=opponent_path,
                match_idx=match_idx,
                deck_seed=deck_seed,
                bot_seed=bot_seed,
            )
            leg_name = str(validated_leg["leg"])
            if leg_name in legs:
                raise ValueError(f"{field}: duplicate {leg_name!r} leg")
            legs[leg_name] = validated_leg
            leg_keys.append((opponent, match_idx, leg_name))
        if set(legs) != set(LEG_NAMES):
            raise ValueError(f"{field}: complete forward/swapped legs are required")

        hand_chips = _integer_vector(
            row.get("hand_net_chips"),
            field=f"{field}.hand_net_chips",
            length=HANDS_PER_LEG,
        )
        expected_hand_chips = [
            legs["forward"]["hand_net_chips"][index]
            + legs["swapped"]["hand_net_chips"][index]
            for index in range(HANDS_PER_LEG)
        ]
        if hand_chips != expected_hand_chips:
            raise ValueError(f"{field}: paired hand vector does not equal its two legs")
        net_chips = _integer(row.get("net_chips"), field=f"{field}.net_chips")
        if net_chips != legs["forward"]["net_chips"] + legs["swapped"]["net_chips"]:
            raise ValueError(f"{field}: paired net_chips does not equal its two legs")
        if sum(hand_chips) != net_chips:
            raise ValueError(f"{field}: paired hand chip accounting is inconsistent")

        key = (opponent, match_idx)
        if key in clusters:
            raise ValueError(f"{source}: duplicate row key {key!r}")
        clusters[key] = {
            "candidate": candidate,
            "opponent": opponent,
            "opponent_path": opponent_path,
            "match_idx": match_idx,
            "deck_seed_base": deck_seed,
            "bot_seed_base": bot_seed,
            "legs": legs,
        }
        observed_deck_seeds.append(deck_seed)
        observed_bot_seed_bases.append(bot_seed)
        row_metadata.append(
            (candidate, opponent, match_idx, opponent_path, deck_seed, bot_seed)
        )

    if len(set(path_to_name.values())) != len(path_to_name):
        raise ValueError(f"{source}: opponent labels must uniquely identify artifacts")
    for path in opponent_paths:
        if path not in path_to_name:
            raise ValueError(f"{source}: opponent artifact {path!r} has no rows")
        indexes = {
            cluster["match_idx"]
            for cluster in clusters.values()
            if cluster["opponent_path"] == path
        }
        if indexes != set(range(len(seeds))):
            raise ValueError(f"{source}: opponent {path!r} has an incomplete seed plan")
    if tuple(sorted(observed_deck_seeds)) != actual_deck_seeds:
        raise ValueError(f"{source}: actual_deck_seed_bases do not bind all rows")
    for index, left in enumerate(actual_deck_seeds):
        for right in actual_deck_seeds[index + 1 :]:
            if max(left, right) <= min(left + HANDS_PER_LEG - 1, right + HANDS_PER_LEG - 1):
                raise ValueError(f"{source}: deck seed windows overlap")
    ordered_bot_seeds = sorted(observed_bot_seed_bases)
    if len(set(ordered_bot_seeds)) != len(ordered_bot_seeds):
        raise ValueError(f"{source}: bot seed bases collide")
    for left, right in zip(ordered_bot_seeds, ordered_bot_seeds[1:]):
        if right <= left + 1:
            raise ValueError(f"{source}: per-player bot seed windows overlap")

    plan_signature = (
        candidate_path,
        candidate_artifact,
        opponent_paths,
        opponent_artifacts,
        seeds,
        actual_deck_seeds,
        payload.get("deck_seed_scheme"),
        opponent_seed_stride,
        bot_seed_base,
        bot_seed_stride,
        tuple(sorted(row_metadata)),
        tuple(sorted(leg_keys)),
    )
    return _LoadedReport(
        mode=mode,
        source=source,
        raw_bytes=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        payload=payload,
        candidate_artifact=candidate_artifact,
        opponent_artifacts=opponent_artifacts,
        plan_signature=plan_signature,
        clusters=clusters,
    )


def _rounded(value: float, digits: int = 9) -> float:
    result = round(float(value), digits)
    return 0.0 if result == 0.0 else result


def _percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(float(value) for value in values)
    position = max(0.0, min(1.0, probability)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _ordinary_cluster_bootstrap(
    clusters: Sequence[Sequence[int]],
    *,
    samples: int,
    seed: int,
    scheme: str = "ordinary_complete_seed_block_cluster_v1",
    scale: float = 1.0,
) -> dict[str, Any]:
    if not clusters or any(len(cluster) != 2 for cluster in clusters):
        raise ValueError("ordinary bootstrap requires complete two-leg clusters")
    rng = random.Random(seed)
    estimates: list[float] = []
    count = len(clusters)
    for _ in range(samples):
        selected = [clusters[rng.randrange(count)] for _ in range(count)]
        values = [value for cluster in selected for value in cluster]
        estimates.append((sum(values) / len(values)) * scale)
    point_values = [value for cluster in clusters for value in cluster]
    return {
        "scheme": scheme,
        "clusters": count,
        "paired_70_hand_legs": len(point_values),
        "resamples": samples,
        "seed": seed,
        "confidence": 0.95,
        "estimate": _rounded((sum(point_values) / len(point_values)) * scale),
        "low": _rounded(_percentile(estimates, 0.025)),
        "high": _rounded(_percentile(estimates, 0.975)),
    }


def _equal_opponent_stratified_cluster_bootstrap(
    groups: Mapping[str, Sequence[Sequence[int]]],
    *,
    samples: int,
    seed: int,
    scheme: str = (
        "equal_opponent_stratified_complete_seed_block_cluster_v1"
    ),
    scale: float = 1.0,
) -> dict[str, Any]:
    if not groups or any(
        not clusters or any(len(cluster) != 2 for cluster in clusters)
        for clusters in groups.values()
    ):
        raise ValueError("stratified bootstrap requires complete opponent clusters")
    ordered_groups = [(opponent, groups[opponent]) for opponent in sorted(groups)]
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(samples):
        opponent_estimates: list[float] = []
        for _, clusters in ordered_groups:
            count = len(clusters)
            selected = [clusters[rng.randrange(count)] for _ in range(count)]
            values = [value for cluster in selected for value in cluster]
            opponent_estimates.append((sum(values) / len(values)) * scale)
        estimates.append(sum(opponent_estimates) / len(opponent_estimates))
    point_estimates = []
    for _, clusters in ordered_groups:
        values = [value for cluster in clusters for value in cluster]
        point_estimates.append((sum(values) / len(values)) * scale)
    return {
        "scheme": scheme,
        "opponents": len(ordered_groups),
        "clusters": sum(len(clusters) for _, clusters in ordered_groups),
        "paired_70_hand_legs": 2 * sum(
            len(clusters) for _, clusters in ordered_groups
        ),
        "resamples": samples,
        "seed": seed,
        "confidence": 0.95,
        "estimate": _rounded(sum(point_estimates) / len(point_estimates)),
        "low": _rounded(_percentile(estimates, 0.025)),
        "high": _rounded(_percentile(estimates, 0.975)),
    }


def _comparison(
    full: _LoadedReport,
    ablation: _LoadedReport,
    *,
    bootstrap_samples: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    if set(full.clusters) != set(ablation.clusters):
        raise ValueError(f"{ablation.mode}: row keys drifted from full")
    uplift_clusters: list[list[int]] = []
    grouped_uplifts: dict[str, list[list[int]]] = {}
    chip_delta_clusters: list[list[int]] = []
    grouped_chip_deltas: dict[str, list[list[int]]] = {}
    chip_deltas: list[int] = []
    per_opponent: dict[str, dict[str, Any]] = {}
    full_positive = 0
    ablation_positive = 0

    for key in sorted(full.clusters):
        full_cluster = full.clusters[key]
        ablation_cluster = ablation.clusters[key]
        cluster_uplifts: list[int] = []
        cluster_chip_deltas: list[int] = []
        for leg_name in LEG_NAMES:
            full_leg = full_cluster["legs"][leg_name]
            ablation_leg = ablation_cluster["legs"][leg_name]
            if (
                full_leg["deck_seed_base"] != ablation_leg["deck_seed_base"]
                or full_leg["bot_seed_base"] != ablation_leg["bot_seed_base"]
            ):
                raise ValueError(f"{ablation.mode}: leg seed drift for {(*key, leg_name)!r}")
            full_outcome = int(full_leg["net_chips"] > 0)
            ablation_outcome = int(ablation_leg["net_chips"] > 0)
            full_positive += full_outcome
            ablation_positive += ablation_outcome
            cluster_uplifts.append(full_outcome - ablation_outcome)
            cluster_chip_deltas.append(
                int(full_leg["net_chips"]) - int(ablation_leg["net_chips"])
            )
        uplift_clusters.append(cluster_uplifts)
        grouped_uplifts.setdefault(key[0], []).append(cluster_uplifts)
        chip_delta_clusters.append(cluster_chip_deltas)
        grouped_chip_deltas.setdefault(key[0], []).append(cluster_chip_deltas)
        chip_deltas.extend(cluster_chip_deltas)
        stats = per_opponent.setdefault(
            key[0],
            {
                "clusters": 0,
                "paired_70_hand_legs": 0,
                "full_positive": 0,
                "ablation_positive": 0,
                "paired_uplift_sum": 0,
                "net_chips_delta": 0,
            },
        )
        stats["clusters"] += 1
        stats["paired_70_hand_legs"] += 2
        stats["full_positive"] += sum(
            int(full_cluster["legs"][leg]["net_chips"] > 0) for leg in LEG_NAMES
        )
        stats["ablation_positive"] += sum(
            int(ablation_cluster["legs"][leg]["net_chips"] > 0) for leg in LEG_NAMES
        )
        stats["paired_uplift_sum"] += sum(cluster_uplifts)
        stats["net_chips_delta"] += sum(cluster_chip_deltas)

    leg_count = 2 * len(uplift_clusters)
    uplift_values = [value for cluster in uplift_clusters for value in cluster]
    uplift_mean = sum(uplift_values) / leg_count
    for stats in per_opponent.values():
        opponent_legs = int(stats["paired_70_hand_legs"])
        stats["full_positive_rate"] = _rounded(stats["full_positive"] / opponent_legs)
        stats["ablation_positive_rate"] = _rounded(
            stats["ablation_positive"] / opponent_legs
        )
        stats["paired_uplift_mean"] = _rounded(
            stats["paired_uplift_sum"] / opponent_legs
        )
        stats["net_chips_delta_per_hand"] = _rounded(
            stats["net_chips_delta"] / (opponent_legs * HANDS_PER_LEG)
        )

    direction = "tie"
    if uplift_mean > 0:
        direction = "full_better"
    elif uplift_mean < 0:
        direction = "ablation_better"
    return {
        "full_mode": "full",
        "ablation_mode": ablation.mode,
        "primary": {
            "priority": 1,
            "criterion": PRIMARY_CRITERION,
            "cluster_unit": "complete_(opponent,match_idx)_two_leg_seed_block",
            "leg_key": ["opponent", "match_idx", "leg"],
            "clusters": len(uplift_clusters),
            "paired_70_hand_legs": leg_count,
            "full_positive": full_positive,
            "ablation_positive": ablation_positive,
            "full_positive_rate": _rounded(full_positive / leg_count),
            "ablation_positive_rate": _rounded(ablation_positive / leg_count),
            "paired_uplift_sum": sum(uplift_values),
            "paired_uplift_mean": _rounded(uplift_mean),
            "direction": direction,
            "ordinary_cluster_bootstrap_ci": _ordinary_cluster_bootstrap(
                uplift_clusters,
                samples=bootstrap_samples,
                seed=bootstrap_seed,
            ),
            "equal_opponent_stratified_cluster_bootstrap_ci": (
                _equal_opponent_stratified_cluster_bootstrap(
                    grouped_uplifts,
                    samples=bootstrap_samples,
                    seed=bootstrap_seed + 1,
                )
            ),
        },
        "secondary": {
            "priority": 2,
            "criterion": SECONDARY_CRITERION,
            "used_for_primary_direction_or_ordering": False,
            "paired_70_hand_legs": leg_count,
            "hands": leg_count * HANDS_PER_LEG,
            "net_chips_delta": sum(chip_deltas),
            "net_chips_delta_per_hand": _rounded(
                sum(chip_deltas) / (leg_count * HANDS_PER_LEG)
            ),
            "ordinary_cluster_bootstrap_ci": _ordinary_cluster_bootstrap(
                chip_delta_clusters,
                samples=bootstrap_samples,
                seed=bootstrap_seed + 2,
                scheme=(
                    "ordinary_complete_seed_block_net_chips_delta_per_hand_v1"
                ),
                scale=1.0 / HANDS_PER_LEG,
            ),
            "equal_opponent_stratified_cluster_bootstrap_ci": (
                _equal_opponent_stratified_cluster_bootstrap(
                    grouped_chip_deltas,
                    samples=bootstrap_samples,
                    seed=bootstrap_seed + 3,
                    scheme=(
                        "equal_opponent_stratified_complete_seed_block_"
                        "net_chips_delta_per_hand_v1"
                    ),
                    scale=1.0 / HANDS_PER_LEG,
                )
            ),
        },
        "opponents": {name: per_opponent[name] for name in sorted(per_opponent)},
    }


def summarize_native_ablation_reports(
    inputs: Iterable[tuple[str, bytes]],
    *,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Validate four raw reports and return a deterministic self-hashed summary."""
    if isinstance(bootstrap_samples, bool) or not isinstance(bootstrap_samples, int) or bootstrap_samples <= 0:
        raise ValueError("bootstrap_samples must be a positive integer")
    if isinstance(bootstrap_seed, bool) or not isinstance(bootstrap_seed, int):
        raise ValueError("bootstrap_seed must be an integer")

    loaded = [
        _validate_report(bytes(raw), source=str(source))
        for source, raw in inputs
    ]
    if len(loaded) != len(ABLATION_MODES):
        raise ValueError(
            f"exactly {len(ABLATION_MODES)} reports are required; got {len(loaded)}"
        )
    by_mode: dict[str, _LoadedReport] = {}
    for report in loaded:
        if report.mode in by_mode:
            raise ValueError(f"duplicate ablation mode {report.mode!r}")
        by_mode[report.mode] = report
    missing = [mode for mode in ABLATION_MODES if mode not in by_mode]
    if missing:
        raise ValueError(f"missing ablation modes: {missing!r}")

    full = by_mode["full"]
    for mode in COMPARISON_MODES:
        if by_mode[mode].plan_signature != full.plan_signature:
            raise ValueError(
                f"{mode}: candidate/opponent artifacts, seeds, rows, or leg keys drifted from full"
            )

    comparisons = {
        mode: _comparison(
            full,
            by_mode[mode],
            bootstrap_samples=bootstrap_samples,
            bootstrap_seed=bootstrap_seed + 100 * index,
        )
        for index, mode in enumerate(COMPARISON_MODES)
    }
    payload: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "method": SUMMARY_METHOD,
        "input_reports": [
            {
                "mode": mode,
                "bytes": len(by_mode[mode].raw_bytes),
                "sha256": by_mode[mode].sha256,
            }
            for mode in ABLATION_MODES
        ],
        "validated_contract": {
            "required_modes": list(ABLATION_MODES),
            "candidate_ablation_schema": ABLATION_SCHEMA,
            "native_evaluation_format": "native_tcp_evaluation_v2",
            "paired": True,
            "hands_per_leg": HANDS_PER_LEG,
            "legs_per_cluster": 2,
            "leg_key": ["opponent", "match_idx", "leg"],
            "cluster_unit": "complete_(opponent,match_idx)_two_leg_seed_block",
            "primary_criterion": PRIMARY_CRITERION,
            "secondary_criterion": SECONDARY_CRITERION,
            "secondary_used_for_primary_direction_or_ordering": False,
            "bootstrap_samples": bootstrap_samples,
            "bootstrap_seed": bootstrap_seed,
        },
        "execution_identity": {
            "candidate": {
                "path": full.candidate_artifact[0],
                "sha256": full.candidate_artifact[1],
                "stable_in_all_modes": True,
            },
            "opponents": [
                {"path": path, "sha256": digest, "stable_in_all_modes": True}
                for path, digest in full.opponent_artifacts
            ],
            "opponents_count": len(full.opponent_artifacts),
            "seed_blocks": len(full.clusters),
            "paired_70_hand_legs_per_mode": 2 * len(full.clusters),
        },
        "comparisons": comparisons,
        "protected_data_read": False,
        "policy_roles_opened": [],
        "diagnostic_only": True,
        "eligible_as_strength_evidence": False,
        "deployment_policy_value": False,
        "deployment_eligible": False,
        "strength_evidence": False,
        "native_strength_evidence": False,
        "official_exe_accepted": False,
        "formal_release_evidence": False,
    }
    payload["payload_sha256"] = summary_payload_sha256(payload)
    return _validate_summary_structure(payload)


def _validate_summary_structure(payload: Any) -> dict[str, Any]:
    """Validate the closed summary schema before raw-report replay."""
    artifact = _object(payload, field="summary")
    _finite_tree(artifact, field="summary")
    _exact_keys(artifact, SUMMARY_ROOT_KEYS, field="summary")
    observed_hash = _digest(
        artifact.get("payload_sha256"), field="summary.payload_sha256"
    )
    if summary_payload_sha256(artifact) != observed_hash:
        raise ValueError("v4 native ablation summary self-hash changed")
    if artifact.get("schema") != SUMMARY_SCHEMA or artifact.get("method") != SUMMARY_METHOD:
        raise ValueError("unsupported v4 native ablation summary contract")
    expected_controls = {
        "protected_data_read": False,
        "policy_roles_opened": [],
        "diagnostic_only": True,
        "eligible_as_strength_evidence": False,
        "deployment_policy_value": False,
        "deployment_eligible": False,
        "strength_evidence": False,
        "native_strength_evidence": False,
        "official_exe_accepted": False,
        "formal_release_evidence": False,
    }
    for field, expected in expected_controls.items():
        if artifact.get(field) != expected or type(artifact.get(field)) is not type(expected):
            raise ValueError(f"summary.{field} must equal {expected!r}")

    inputs = _list(artifact.get("input_reports"), field="summary.input_reports")
    if [item.get("mode") if isinstance(item, dict) else None for item in inputs] != list(ABLATION_MODES):
        raise ValueError("summary input modes are incomplete or out of contract order")
    for index, item_raw in enumerate(inputs):
        item = _object(item_raw, field=f"summary.input_reports[{index}]")
        _exact_keys(item, {"mode", "bytes", "sha256"}, field=f"summary.input_reports[{index}]")
        if _integer(item.get("bytes"), field=f"summary.input_reports[{index}].bytes") <= 0:
            raise ValueError("summary input byte counts must be positive")
        _digest(item.get("sha256"), field=f"summary.input_reports[{index}].sha256")

    comparisons = _object(artifact.get("comparisons"), field="summary.comparisons")
    if set(comparisons) != set(COMPARISON_MODES):
        raise ValueError("summary comparisons do not contain exactly three ablations")
    for mode in COMPARISON_MODES:
        comparison = _object(comparisons[mode], field=f"summary.comparisons.{mode}")
        primary = _object(comparison.get("primary"), field=f"summary.comparisons.{mode}.primary")
        secondary = _object(
            comparison.get("secondary"), field=f"summary.comparisons.{mode}.secondary"
        )
        if (
            comparison.get("ablation_mode") != mode
            or comparison.get("full_mode") != "full"
            or primary.get("priority") != 1
            or primary.get("criterion") != PRIMARY_CRITERION
            or secondary.get("priority") != 2
            or secondary.get("criterion") != SECONDARY_CRITERION
            or secondary.get("used_for_primary_direction_or_ordering") is not False
        ):
            raise ValueError(f"summary comparison {mode!r} changed metric priority")
    return artifact


def validate_summary_artifact(
    payload: Any,
    inputs: Iterable[tuple[str, bytes]] | None = None,
) -> dict[str, Any]:
    """Recompute a summary from its four raw reports and require exact equality.

    A self-hash detects accidental edits but is not an authenticity primitive:
    anyone able to edit JSON can calculate another hash.  Authoritative
    validation therefore fails closed unless the raw evaluator report bytes are
    supplied and reproduce the complete artifact, including every count,
    interval, direction, execution identity, and control flag.
    """
    artifact = _validate_summary_structure(payload)
    if inputs is None:
        raise ValueError(
            "raw native ablation reports are required to validate the summary"
        )
    contract = _object(
        artifact.get("validated_contract"), field="summary.validated_contract"
    )
    samples = _integer(
        contract.get("bootstrap_samples"),
        field="summary.validated_contract.bootstrap_samples",
    )
    seed = _integer(
        contract.get("bootstrap_seed"),
        field="summary.validated_contract.bootstrap_seed",
    )
    expected = summarize_native_ablation_reports(
        inputs,
        bootstrap_samples=samples,
        bootstrap_seed=seed,
    )
    if _canonical_bytes(artifact) != _canonical_bytes(expected):
        raise ValueError(
            "v4 native ablation summary does not match its raw input reports"
        )
    return artifact


def summarize_paths(
    paths: Sequence[Path],
    *,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    inputs = [(str(path), path.read_bytes()) for path in paths]
    return summarize_native_ablation_reports(
        inputs,
        bootstrap_samples=bootstrap_samples,
        bootstrap_seed=bootstrap_seed,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Summarize exactly four candidate-only diagnostic v4 native "
            "ablation JSON reports."
        )
    )
    parser.add_argument(
        "--report",
        action="append",
        type=Path,
        required=True,
        help="One native_tcp_evaluation_v2 JSON report; repeat exactly four times.",
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--bootstrap-samples", type=int, default=DEFAULT_BOOTSTRAP_SAMPLES)
    parser.add_argument("--bootstrap-seed", type=int, default=DEFAULT_BOOTSTRAP_SEED)
    args = parser.parse_args()
    try:
        payload = summarize_paths(
            args.report,
            bootstrap_samples=args.bootstrap_samples,
            bootstrap_seed=args.bootstrap_seed,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
