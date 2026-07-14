#!/usr/bin/env python3
"""Build a fail-closed development classic-pool strength verdict for v4.

The candidate and baseline reports must evaluate the same immutable candidate
tree, opponents, cards, bot seeds, and two-seat 70-hand plan.  The candidate is
the ``full`` mode and the baseline is the candidate's ``neural_off`` mode.
This artifact is development classic-pool evidence only; it never claims
deployment, official-platform, final-release, or repository-wide strength
authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence


TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import summarize_v4_native_ablations as ablations  # noqa: E402
from freeze_v4_native_strength_pool import (  # noqa: E402
    MAXIMUM_BOOTSTRAP_SAMPLES,
    MINIMUM_BOOTSTRAP_SAMPLES,
    validate_v4_native_strength_pool_plan_bytes,
)


POOL_PLAN_SCHEMA = "opponent_multitask_v4_native_strength_pool_plan_v1"
VERDICT_SCHEMA = "opponent_multitask_v4_native_strength_verdict_v1"
VERDICT_METHOD = "outcome_first_complete_seed_block_bootstrap_v1"
PRIMARY_CRITERION = "candidate_net_chips_after_70_hands_gt_zero"
UPLIFT_CRITERION = "full_minus_neural_off_70_hand_positive_outcome_uplift"
SECONDARY_CRITERION = "full_minus_neural_off_paired_net_chips_per_hand"

HANDS_PER_LEG = 70
LEGS_PER_CLUSTER = 2
BOT_OPPONENT_SEED_STRIDE = 100_000
DEFAULT_BOOTSTRAP_SAMPLES = 20_000
MIN_BOOTSTRAP_SAMPLES = MINIMUM_BOOTSTRAP_SAMPLES
MAX_BOOTSTRAP_SAMPLES = MAXIMUM_BOOTSTRAP_SAMPLES
DEFAULT_BOOTSTRAP_SEED = 20_260_712

POSITIVE_RATE_LCB_FLOOR = 0.5
OPPONENT_POSITIVE_RATE_FLOOR = 0.5
PAIRED_EV_CI_LCB_FLOOR = 0.0
PAIRED_EV_POINT_TARGET = 5.0
OPPONENT_DELTA_PER_HAND_FLOOR = 0.0
CANDIDATE_DIRECT_EV_PER_HAND_FLOOR = 0.0

POOL_PLAN_KEYS = {
    "schema",
    "repository",
    "lifecycle",
    "ratings_snapshot",
    "candidate_artifact",
    "opponent_artifacts",
    "seeds",
    "actual_deck_seed_bases",
    "deck_seed_scheme",
    "opponent_seed_stride",
    "bot_seed_base",
    "bot_seed_stride",
    "bot_opponent_seed_stride",
    "hands_per_leg",
    "paired",
    "minimum_seed_blocks_per_opponent",
    "workers",
    "runtime_contract",
    "bootstrap_samples",
    "bootstrap_seed",
    "selection",
    "code_artifacts",
    "protected_data_read",
    "policy_roles_opened",
    "held_out_read",
    "policy_selection_opened",
    "policy_gate_opened",
    "deployment_policy_value",
    "deployment_eligible",
    "strength_evidence",
    "native_strength_evidence",
    "official_exe_accepted",
    "formal_release_evidence",
    "payload_sha256",
}

VERDICT_ROOT_KEYS = {
    "schema",
    "method",
    "authority_scope",
    "inputs",
    "pool_plan",
    "execution_identity",
    "thresholds",
    "primary_outcome",
    "outcome_uplift_diagnostic",
    "secondary_paired_ev",
    "execution_contract_passed",
    "candidate_evaluator_outcome_receipt_passed",
    "development_classic_pool_verdict_passed",
    "protected_data_read",
    "policy_roles_opened",
    "deployment_policy_value",
    "deployment_eligible",
    "strength_evidence",
    "native_strength_evidence",
    "official_exe_accepted",
    "formal_release_evidence",
    "payload_sha256",
}

FALSE_AUTHORITY_CONTROLS = {
    "protected_data_read": False,
    "policy_roles_opened": [],
    "deployment_policy_value": False,
    "deployment_eligible": False,
    "strength_evidence": False,
    "native_strength_evidence": False,
    "official_exe_accepted": False,
    "formal_release_evidence": False,
}


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("payload_sha256", None)
    return _digest_bytes(_canonical_bytes(unsigned))


def pool_plan_payload_sha256(payload: Mapping[str, Any]) -> str:
    """Canonical self-hash shared with the pool-plan producer."""
    return _payload_sha256(payload)


def verdict_payload_sha256(payload: Mapping[str, Any]) -> str:
    return _payload_sha256(payload)


def _object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _list(value: Any, *, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, field: str) -> None:
    observed = set(value)
    if observed != expected:
        raise ValueError(
            f"{field} keys differ: missing={sorted(expected - observed)!r} "
            f"extra={sorted(observed - expected)!r}"
        )


def _integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _digest(value: Any, *, field: str) -> str:
    result = _string(value, field=field)
    if len(result) != 64 or any(ch not in "0123456789abcdef" for ch in result):
        raise ValueError(f"{field} must be a lowercase sha256 digest")
    return result


def _rounded(value: float, digits: int = 9) -> float:
    result = round(float(value), digits)
    return 0.0 if result == 0.0 else result


def _artifact_identity(raw: Any, *, field: str) -> dict[str, str]:
    artifact = _object(raw, field=field)
    path = artifact.get("snapshot_path", artifact.get("path"))
    digest = artifact.get(
        "snapshot_directory_sha256",
        artifact.get("execution_directory_sha256", artifact.get("sha256")),
    )
    return {
        "path": _string(path, field=f"{field}.snapshot_path/path"),
        "sha256": _digest(
            digest,
            field=(
                f"{field}.snapshot_directory_sha256/"
                "execution_directory_sha256/sha256"
            ),
        ),
    }


def _opponent_identity(raw: Any, *, field: str) -> dict[str, str]:
    artifact = _object(raw, field=field)
    label = artifact.get("name", artifact.get("label", artifact.get("bot")))
    return {
        "label": _string(label, field=f"{field}.name/label/bot"),
        **_artifact_identity(artifact, field=field),
    }


def _validate_pool_plan(raw: bytes, *, source: str) -> dict[str, Any]:
    try:
        plan = validate_v4_native_strength_pool_plan_bytes(
            bytes(raw), require_snapshots=True
        )
    except Exception as exc:
        raise ValueError(f"{source}: invalid frozen pool plan: {exc}") from exc
    _exact_keys(plan, POOL_PLAN_KEYS, field=source)
    if plan.get("schema") != POOL_PLAN_SCHEMA:
        raise ValueError(f"{source}: unsupported pool-plan schema")
    observed_hash = _digest(
        plan.get("payload_sha256"), field=f"{source}.payload_sha256"
    )
    if pool_plan_payload_sha256(plan) != observed_hash:
        raise ValueError(f"{source}: pool-plan self-hash changed")

    for field in (
        "repository",
        "lifecycle",
        "ratings_snapshot",
        "selection",
        "code_artifacts",
        "runtime_contract",
    ):
        _object(plan.get(field), field=f"{source}.{field}")

    candidate = _artifact_identity(
        plan.get("candidate_artifact"), field=f"{source}.candidate_artifact"
    )
    opponent_rows = _list(
        plan.get("opponent_artifacts"), field=f"{source}.opponent_artifacts"
    )
    opponents = [
        _opponent_identity(row, field=f"{source}.opponent_artifacts[{index}]")
        for index, row in enumerate(opponent_rows)
    ]
    if not opponents:
        raise ValueError(f"{source}: opponent_artifacts must not be empty")
    if len({row["label"] for row in opponents}) != len(opponents):
        raise ValueError(f"{source}: opponent labels must be unique")
    if len({row["path"] for row in opponents}) != len(opponents):
        raise ValueError(f"{source}: opponent paths must be unique")

    seeds = [
        _integer(seed, field=f"{source}.seeds[{index}]")
        for index, seed in enumerate(_list(plan.get("seeds"), field=f"{source}.seeds"))
    ]
    minimum_blocks = _integer(
        plan.get("minimum_seed_blocks_per_opponent"),
        field=f"{source}.minimum_seed_blocks_per_opponent",
    )
    if minimum_blocks < 3:
        raise ValueError(f"{source}: at least three seed blocks are required")
    if len(seeds) < minimum_blocks or len(set(seeds)) != len(seeds):
        raise ValueError(f"{source}: seeds do not satisfy the minimum unique plan")
    actual_seeds = [
        _integer(seed, field=f"{source}.actual_deck_seed_bases[{index}]")
        for index, seed in enumerate(
            _list(
                plan.get("actual_deck_seed_bases"),
                field=f"{source}.actual_deck_seed_bases",
            )
        )
    ]
    if actual_seeds != sorted(set(actual_seeds)):
        raise ValueError(f"{source}: actual deck seeds must be sorted and unique")
    if plan.get("deck_seed_scheme") != "opponent_disjoint_match_blocks_v1":
        raise ValueError(f"{source}: unsupported deck seed scheme")
    if _integer(plan.get("hands_per_leg"), field=f"{source}.hands_per_leg") != 70:
        raise ValueError(f"{source}: hands_per_leg must be 70")
    if plan.get("paired") is not True:
        raise ValueError(f"{source}: paired must be true")
    for field in (
        "opponent_seed_stride",
        "bot_seed_stride",
        "workers",
    ):
        if _integer(plan.get(field), field=f"{source}.{field}") <= 0:
            raise ValueError(f"{source}.{field} must be positive")
    _integer(plan.get("bot_seed_base"), field=f"{source}.bot_seed_base")
    if (
        _integer(
            plan.get("bot_opponent_seed_stride"),
            field=f"{source}.bot_opponent_seed_stride",
        )
        != BOT_OPPONENT_SEED_STRIDE
    ):
        raise ValueError(f"{source}: bot opponent seed stride changed")
    if not 1 <= int(plan["workers"]) <= 4:
        raise ValueError(f"{source}: workers must be in [1, 4]")
    bootstrap_samples = _integer(
        plan.get("bootstrap_samples"), field=f"{source}.bootstrap_samples"
    )
    if not MIN_BOOTSTRAP_SAMPLES <= bootstrap_samples <= MAX_BOOTSTRAP_SAMPLES:
        raise ValueError(
            f"{source}: bootstrap_samples must be in "
            f"[{MIN_BOOTSTRAP_SAMPLES}, {MAX_BOOTSTRAP_SAMPLES}]"
        )
    if _integer(plan.get("bootstrap_seed"), field=f"{source}.bootstrap_seed") < 0:
        raise ValueError(f"{source}: bootstrap_seed must be non-negative")

    expected_controls = {
        "protected_data_read": False,
        "policy_roles_opened": [],
        "held_out_read": False,
        "policy_selection_opened": False,
        "policy_gate_opened": False,
        "deployment_policy_value": False,
        "deployment_eligible": False,
        "strength_evidence": False,
        "native_strength_evidence": False,
        "official_exe_accepted": False,
        "formal_release_evidence": False,
    }
    for field, expected in expected_controls.items():
        if plan.get(field) != expected or type(plan.get(field)) is not type(expected):
            raise ValueError(f"{source}.{field} must equal {expected!r}")

    return {
        "payload": plan,
        "candidate": candidate,
        "opponents": opponents,
        "seeds": seeds,
        "actual_deck_seed_bases": actual_seeds,
        "minimum_seed_blocks_per_opponent": minimum_blocks,
    }


def _labels_by_path(report: Any) -> dict[str, str]:
    labels: dict[str, str] = {}
    for cluster in report.clusters.values():
        path = str(cluster["opponent_path"])
        label = str(cluster["opponent"])
        previous = labels.setdefault(path, label)
        if previous != label:
            raise ValueError("report opponent label drifted")
    return labels


def _bind_inputs(
    plan: dict[str, Any], candidate: Any, baseline: Any
) -> None:
    if candidate.mode != "full":
        raise ValueError("candidate report must use full mode")
    if baseline.mode != "neural_off":
        raise ValueError("baseline report must use neural_off mode")
    receipt = _object(
        candidate.payload.get("strength_evidence"),
        field="candidate_report.strength_evidence",
    )
    if (
        receipt.get("requested") is not True
        or receipt.get("execution_contract_passed") is not True
        or receipt.get("request_errors") != []
        or receipt.get("result_errors") != []
    ):
        raise ValueError(
            "candidate evaluator execution receipt must have passed cleanly"
        )
    if candidate.candidate_artifact != baseline.candidate_artifact:
        raise ValueError("full and neural_off reports used different candidate trees")
    if candidate.plan_signature != baseline.plan_signature:
        raise ValueError(
            "full and neural_off reports differ on opponents, seeds, rows, or legs"
        )
    candidate_identity = {
        "path": candidate.candidate_artifact[0],
        "sha256": candidate.candidate_artifact[1],
    }
    if candidate_identity != plan["candidate"]:
        raise ValueError("candidate report does not match the frozen pool plan")

    labels = _labels_by_path(candidate)
    report_opponents = [
        {"label": labels[path], "path": path, "sha256": digest}
        for path, digest in candidate.opponent_artifacts
    ]
    if report_opponents != plan["opponents"]:
        raise ValueError("opponent reports do not match the frozen pool plan")

    payload = candidate.payload
    exact_fields = {
        "seeds": plan["seeds"],
        "actual_deck_seed_bases": plan["actual_deck_seed_bases"],
        "deck_seed_scheme": plan["payload"]["deck_seed_scheme"],
        "opponent_seed_stride": plan["payload"]["opponent_seed_stride"],
        "bot_seed_base": plan["payload"]["bot_seed_base"],
        "bot_seed_stride": plan["payload"]["bot_seed_stride"],
        "hands_per_match": plan["payload"]["hands_per_leg"],
        "paired": plan["payload"]["paired"],
        "workers": plan["payload"]["workers"],
        "outcome_bootstrap_samples": plan["payload"]["bootstrap_samples"],
        "outcome_bootstrap_seed": plan["payload"]["bootstrap_seed"],
        "runtime_contract": plan["payload"]["runtime_contract"],
    }
    for field, expected in exact_fields.items():
        if payload.get(field) != expected:
            raise ValueError(f"candidate report {field} drifted from pool plan")
        if baseline.payload.get(field) != expected:
            raise ValueError(f"baseline report {field} drifted from pool plan")

    for opponent in plan["opponents"]:
        count = sum(
            cluster["opponent_path"] == opponent["path"]
            for cluster in candidate.clusters.values()
        )
        if count < plan["minimum_seed_blocks_per_opponent"]:
            raise ValueError(
                f"opponent {opponent['label']!r} has too few seed blocks"
            )


def _ordinary_ci(
    clusters: Sequence[Sequence[int]], *, samples: int, seed: int, scale: float = 1.0
) -> dict[str, Any]:
    return ablations._ordinary_cluster_bootstrap(
        clusters, samples=samples, seed=seed, scale=scale
    )


def _stratified_ci(
    groups: Mapping[str, Sequence[Sequence[int]]],
    *,
    samples: int,
    seed: int,
    scale: float = 1.0,
) -> dict[str, Any]:
    return ablations._equal_opponent_stratified_cluster_bootstrap(
        groups, samples=samples, seed=seed, scale=scale
    )


def _compute_metrics(
    candidate: Any,
    baseline: Any,
    *,
    bootstrap_samples: int,
    bootstrap_seed: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    candidate_clusters: list[list[int]] = []
    candidate_groups: dict[str, list[list[int]]] = {}
    uplift_clusters: list[list[int]] = []
    uplift_groups: dict[str, list[list[int]]] = {}
    chip_clusters: list[list[int]] = []
    chip_groups: dict[str, list[list[int]]] = {}
    opponent_stats: dict[str, dict[str, Any]] = {}

    for key in sorted(candidate.clusters):
        candidate_cluster = candidate.clusters[key]
        baseline_cluster = baseline.clusters[key]
        opponent = str(candidate_cluster["opponent"])
        candidate_outcomes: list[int] = []
        baseline_outcomes: list[int] = []
        chip_deltas: list[int] = []
        for leg in ablations.LEG_NAMES:
            candidate_net = int(candidate_cluster["legs"][leg]["net_chips"])
            baseline_net = int(baseline_cluster["legs"][leg]["net_chips"])
            candidate_outcomes.append(int(candidate_net > 0))
            baseline_outcomes.append(int(baseline_net > 0))
            chip_deltas.append(candidate_net - baseline_net)
        uplifts = [
            candidate_outcomes[index] - baseline_outcomes[index]
            for index in range(LEGS_PER_CLUSTER)
        ]
        candidate_clusters.append(candidate_outcomes)
        candidate_groups.setdefault(opponent, []).append(candidate_outcomes)
        uplift_clusters.append(uplifts)
        uplift_groups.setdefault(opponent, []).append(uplifts)
        chip_clusters.append(chip_deltas)
        chip_groups.setdefault(opponent, []).append(chip_deltas)

        stats = opponent_stats.setdefault(
            opponent,
            {
                "clusters": 0,
                "legs": 0,
                "candidate_positive": 0,
                "baseline_positive": 0,
                "outcome_uplift_sum": 0,
                "candidate_chips": 0,
                "baseline_chips": 0,
                "chip_delta": 0,
            },
        )
        stats["clusters"] += 1
        stats["legs"] += LEGS_PER_CLUSTER
        stats["candidate_positive"] += sum(candidate_outcomes)
        stats["baseline_positive"] += sum(baseline_outcomes)
        stats["outcome_uplift_sum"] += sum(uplifts)
        stats["candidate_chips"] += sum(
            int(candidate_cluster["legs"][leg]["net_chips"])
            for leg in ablations.LEG_NAMES
        )
        stats["baseline_chips"] += sum(
            int(baseline_cluster["legs"][leg]["net_chips"])
            for leg in ablations.LEG_NAMES
        )
        stats["chip_delta"] += sum(chip_deltas)

    for stats in opponent_stats.values():
        legs = int(stats["legs"])
        hands = legs * HANDS_PER_LEG
        stats["candidate_positive_rate"] = _rounded(
            stats["candidate_positive"] / legs
        )
        stats["baseline_positive_rate"] = _rounded(
            stats["baseline_positive"] / legs
        )
        stats["outcome_uplift"] = _rounded(stats["outcome_uplift_sum"] / legs)
        stats["candidate_direct_ev_chips_per_hand"] = _rounded(
            stats["candidate_chips"] / hands
        )
        stats["baseline_direct_ev_chips_per_hand"] = _rounded(
            stats["baseline_chips"] / hands
        )
        stats["delta_chips_per_hand"] = _rounded(stats["chip_delta"] / hands)
        stats["candidate_positive_rate_passed"] = bool(
            stats["candidate_positive_rate"] >= OPPONENT_POSITIVE_RATE_FLOOR
        )
        stats["paired_ev_nemesis_passed"] = bool(
            stats["delta_chips_per_hand"] >= OPPONENT_DELTA_PER_HAND_FLOOR
        )
        stats["candidate_direct_ev_nemesis_passed"] = bool(
            stats["candidate_direct_ev_chips_per_hand"]
            >= CANDIDATE_DIRECT_EV_PER_HAND_FLOOR
        )

    candidate_ordinary = _ordinary_ci(
        candidate_clusters, samples=bootstrap_samples, seed=bootstrap_seed
    )
    candidate_stratified = _stratified_ci(
        candidate_groups, samples=bootstrap_samples, seed=bootstrap_seed + 1
    )
    primary_failed_opponents = sorted(
        opponent
        for opponent, stats in opponent_stats.items()
        if not stats["candidate_positive_rate_passed"]
    )
    primary = {
        "priority": 1,
        "criterion": PRIMARY_CRITERION,
        "positive_rate_lcb_comparison": ">",
        "positive_rate_lcb_floor": POSITIVE_RATE_LCB_FLOOR,
        "opponent_positive_rate_comparison": ">=",
        "opponent_positive_rate_floor": OPPONENT_POSITIVE_RATE_FLOOR,
        "ordinary_cluster_bootstrap_ci": candidate_ordinary,
        "equal_opponent_stratified_cluster_bootstrap_ci": candidate_stratified,
        "opponents": {
            name: {
                key: value
                for key, value in opponent_stats[name].items()
                if key
                in {
                    "clusters",
                    "legs",
                    "candidate_positive",
                    "candidate_positive_rate",
                    "candidate_positive_rate_passed",
                }
            }
            for name in sorted(opponent_stats)
        },
        "failed_opponents": primary_failed_opponents,
        "ordinary_lcb_passed": bool(candidate_ordinary["low"] > 0.5),
        "stratified_lcb_passed": bool(candidate_stratified["low"] > 0.5),
    }
    primary["passed"] = bool(
        primary["ordinary_lcb_passed"]
        and primary["stratified_lcb_passed"]
        and not primary_failed_opponents
    )

    uplift = {
        "criterion": UPLIFT_CRITERION,
        "used_for_verdict": False,
        "ordinary_cluster_bootstrap_ci": _ordinary_ci(
            uplift_clusters,
            samples=bootstrap_samples,
            seed=bootstrap_seed + 2,
        ),
        "equal_opponent_stratified_cluster_bootstrap_ci": _stratified_ci(
            uplift_groups,
            samples=bootstrap_samples,
            seed=bootstrap_seed + 3,
        ),
        "opponents": {
            name: {
                "candidate_positive_rate": opponent_stats[name][
                    "candidate_positive_rate"
                ],
                "baseline_positive_rate": opponent_stats[name][
                    "baseline_positive_rate"
                ],
                "outcome_uplift": opponent_stats[name]["outcome_uplift"],
            }
            for name in sorted(opponent_stats)
        },
    }

    chip_scale = 1.0 / HANDS_PER_LEG
    chip_ordinary = _ordinary_ci(
        chip_clusters,
        samples=bootstrap_samples,
        seed=bootstrap_seed + 4,
        scale=chip_scale,
    )
    chip_stratified = _stratified_ci(
        chip_groups,
        samples=bootstrap_samples,
        seed=bootstrap_seed + 5,
        scale=chip_scale,
    )
    all_chip_deltas = [value for cluster in chip_clusters for value in cluster]
    total_candidate_chips = sum(
        int(cluster["legs"][leg]["net_chips"])
        for cluster in candidate.clusters.values()
        for leg in ablations.LEG_NAMES
    )
    total_baseline_chips = sum(
        int(cluster["legs"][leg]["net_chips"])
        for cluster in baseline.clusters.values()
        for leg in ablations.LEG_NAMES
    )
    total_hands = len(all_chip_deltas) * HANDS_PER_LEG
    point = _rounded(
        sum(all_chip_deltas) / total_hands
    )
    direct_ev_nemesis = sorted(
        opponent
        for opponent, stats in opponent_stats.items()
        if not stats["candidate_direct_ev_nemesis_passed"]
    )
    delta_nemesis = sorted(
        opponent
        for opponent, stats in opponent_stats.items()
        if not stats["paired_ev_nemesis_passed"]
    )
    secondary = {
        "priority": 2,
        "criterion": SECONDARY_CRITERION,
        "cannot_rescue_primary_failure": True,
        "candidate_direct_ev_chips_per_hand": _rounded(
            total_candidate_chips / total_hands
        ),
        "baseline_direct_ev_chips_per_hand": _rounded(
            total_baseline_chips / total_hands
        ),
        "point_estimate_chips_per_hand": point,
        "point_target_chips_per_hand": PAIRED_EV_POINT_TARGET,
        "point_target_comparison": ">=",
        "ci_lcb_floor_chips_per_hand": PAIRED_EV_CI_LCB_FLOOR,
        "ci_lcb_comparison": ">",
        "opponent_delta_floor_chips_per_hand": OPPONENT_DELTA_PER_HAND_FLOOR,
        "opponent_delta_comparison": ">=",
        "candidate_direct_ev_floor_chips_per_hand": (
            CANDIDATE_DIRECT_EV_PER_HAND_FLOOR
        ),
        "candidate_direct_ev_comparison": ">=",
        "ordinary_complete_seed_block_ci_chips_per_hand": chip_ordinary,
        "equal_opponent_stratified_complete_seed_block_ci_chips_per_hand": (
            chip_stratified
        ),
        "opponents": {
            name: {
                "clusters": opponent_stats[name]["clusters"],
                "candidate_direct_ev_chips_per_hand": opponent_stats[name][
                    "candidate_direct_ev_chips_per_hand"
                ],
                "baseline_direct_ev_chips_per_hand": opponent_stats[name][
                    "baseline_direct_ev_chips_per_hand"
                ],
                "delta_chips_per_hand": opponent_stats[name][
                    "delta_chips_per_hand"
                ],
                "candidate_direct_ev_nemesis_passed": opponent_stats[name][
                    "candidate_direct_ev_nemesis_passed"
                ],
                "paired_ev_nemesis_passed": opponent_stats[name][
                    "paired_ev_nemesis_passed"
                ],
            }
            for name in sorted(opponent_stats)
        },
        "failed_direct_ev_opponents": direct_ev_nemesis,
        "failed_delta_opponents": delta_nemesis,
        "ordinary_lcb_passed": bool(chip_ordinary["low"] > 0.0),
        "stratified_lcb_passed": bool(chip_stratified["low"] > 0.0),
        "point_target_passed": bool(point >= PAIRED_EV_POINT_TARGET),
        "opponent_nemesis_passed": bool(
            not direct_ev_nemesis and not delta_nemesis
        ),
    }
    secondary["passed"] = bool(
        secondary["ordinary_lcb_passed"]
        and secondary["stratified_lcb_passed"]
        and secondary["point_target_passed"]
        and secondary["opponent_nemesis_passed"]
    )
    return primary, uplift, secondary


def _thresholds(*, bootstrap_samples: int, bootstrap_seed: int) -> dict[str, Any]:
    return {
        "bootstrap_samples": bootstrap_samples,
        "bootstrap_seed": bootstrap_seed,
        "minimum_bootstrap_samples": MIN_BOOTSTRAP_SAMPLES,
        "candidate_positive_rate_lcb_floor": POSITIVE_RATE_LCB_FLOOR,
        "candidate_positive_rate_lcb_comparison": ">",
        "opponent_positive_rate_floor": OPPONENT_POSITIVE_RATE_FLOOR,
        "opponent_positive_rate_comparison": ">=",
        "paired_ev_ci_lcb_floor_chips_per_hand": PAIRED_EV_CI_LCB_FLOOR,
        "paired_ev_ci_lcb_comparison": ">",
        "paired_ev_point_target_chips_per_hand": PAIRED_EV_POINT_TARGET,
        "paired_ev_point_target_comparison": ">=",
        "opponent_delta_floor_chips_per_hand": OPPONENT_DELTA_PER_HAND_FLOOR,
        "opponent_delta_comparison": ">=",
        "candidate_direct_ev_floor_chips_per_hand": (
            CANDIDATE_DIRECT_EV_PER_HAND_FLOOR
        ),
        "candidate_direct_ev_comparison": ">=",
    }


def _validate_verdict_structure(payload: Any) -> dict[str, Any]:
    artifact = _object(payload, field="verdict")
    ablations._finite_tree(artifact, field="verdict")
    _exact_keys(artifact, VERDICT_ROOT_KEYS, field="verdict")
    if artifact.get("schema") != VERDICT_SCHEMA:
        raise ValueError("unsupported v4 native strength verdict schema")
    if artifact.get("method") != VERDICT_METHOD:
        raise ValueError("unsupported v4 native strength verdict method")
    if artifact.get("authority_scope") != "development_classic_pool_only":
        raise ValueError("v4 native strength verdict authority scope changed")
    digest = _digest(
        artifact.get("payload_sha256"), field="verdict.payload_sha256"
    )
    if verdict_payload_sha256(artifact) != digest:
        raise ValueError("v4 native strength verdict self-hash changed")
    for field, expected in FALSE_AUTHORITY_CONTROLS.items():
        if artifact.get(field) != expected or type(artifact.get(field)) is not type(expected):
            raise ValueError(f"verdict.{field} must equal {expected!r}")
    if artifact.get("execution_contract_passed") is not True:
        raise ValueError("verdict execution contract must have passed")
    if not isinstance(
        artifact.get("candidate_evaluator_outcome_receipt_passed"), bool
    ):
        raise ValueError("candidate evaluator outcome receipt flag is invalid")
    primary = _object(artifact.get("primary_outcome"), field="verdict.primary_outcome")
    secondary = _object(
        artifact.get("secondary_paired_ev"), field="verdict.secondary_paired_ev"
    )
    if primary.get("priority") != 1 or primary.get("criterion") != PRIMARY_CRITERION:
        raise ValueError("verdict primary outcome contract changed")
    if secondary.get("priority") != 2 or secondary.get("criterion") != SECONDARY_CRITERION:
        raise ValueError("verdict secondary EV contract changed")
    expected_pass = bool(
        artifact["candidate_evaluator_outcome_receipt_passed"]
        and primary.get("passed") is True
        and secondary.get("passed") is True
    )
    if artifact.get("development_classic_pool_verdict_passed") is not expected_pass:
        raise ValueError("verdict aggregate pass is inconsistent")
    return artifact


def evaluate_v4_native_strength_verdict(
    pool_plan_raw: bytes,
    candidate_report_raw: bytes,
    baseline_report_raw: bytes,
    *,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    if (
        isinstance(bootstrap_samples, bool)
        or not isinstance(bootstrap_samples, int)
        or bootstrap_samples < MIN_BOOTSTRAP_SAMPLES
        or bootstrap_samples > MAX_BOOTSTRAP_SAMPLES
    ):
        raise ValueError(
            "bootstrap_samples must be an integer in "
            f"[{MIN_BOOTSTRAP_SAMPLES}, {MAX_BOOTSTRAP_SAMPLES}]"
        )
    if (
        isinstance(bootstrap_seed, bool)
        or not isinstance(bootstrap_seed, int)
        or bootstrap_seed < 0
    ):
        raise ValueError("bootstrap_seed must be a non-negative integer")

    plan_raw = bytes(pool_plan_raw)
    candidate_raw = bytes(candidate_report_raw)
    baseline_raw = bytes(baseline_report_raw)
    plan = _validate_pool_plan(plan_raw, source="pool_plan")
    if bootstrap_samples != plan["payload"]["bootstrap_samples"]:
        raise ValueError("bootstrap_samples differs from the frozen pool plan")
    if bootstrap_seed != plan["payload"]["bootstrap_seed"]:
        raise ValueError("bootstrap_seed differs from the frozen pool plan")
    candidate = ablations.validate_native_ablation_report_bytes(
        candidate_raw, source="candidate_full_report"
    )
    baseline = ablations.validate_native_ablation_report_bytes(
        baseline_raw, source="baseline_neural_off_report"
    )
    _bind_inputs(plan, candidate, baseline)
    primary, uplift, secondary = _compute_metrics(
        candidate,
        baseline,
        bootstrap_samples=bootstrap_samples,
        bootstrap_seed=bootstrap_seed,
    )
    candidate_receipt = candidate.payload["strength_evidence"]
    candidate_receipt_passed = bool(
        candidate_receipt["outcome_gate_passed"] is True
        and candidate_receipt["passed"] is True
        and candidate_receipt["statistical_errors"] == []
    )
    passed = bool(
        candidate_receipt_passed and primary["passed"] and secondary["passed"]
    )
    payload: dict[str, Any] = {
        "schema": VERDICT_SCHEMA,
        "method": VERDICT_METHOD,
        "authority_scope": "development_classic_pool_only",
        "inputs": {
            "pool_plan": {"bytes": len(plan_raw), "sha256": _digest_bytes(plan_raw)},
            "candidate_full_report": {
                "bytes": len(candidate_raw),
                "sha256": _digest_bytes(candidate_raw),
            },
            "baseline_neural_off_report": {
                "bytes": len(baseline_raw),
                "sha256": _digest_bytes(baseline_raw),
            },
        },
        "pool_plan": {
            "schema": POOL_PLAN_SCHEMA,
            "payload_sha256": plan["payload"]["payload_sha256"],
            "ratings_snapshot_sha256": _digest_bytes(
                _canonical_bytes(plan["payload"]["ratings_snapshot"])
            ),
            "selection_sha256": _digest_bytes(
                _canonical_bytes(plan["payload"]["selection"])
            ),
        },
        "execution_identity": {
            "candidate": plan["candidate"],
            "candidate_mode": "full",
            "baseline_mode": "neural_off",
            "opponents": plan["opponents"],
            "seeds": plan["seeds"],
            "actual_deck_seed_bases": plan["actual_deck_seed_bases"],
            "clusters": len(candidate.clusters),
            "paired_70_hand_legs_per_report": LEGS_PER_CLUSTER
            * len(candidate.clusters),
        },
        "thresholds": _thresholds(
            bootstrap_samples=bootstrap_samples,
            bootstrap_seed=bootstrap_seed,
        ),
        "primary_outcome": primary,
        "outcome_uplift_diagnostic": uplift,
        "secondary_paired_ev": secondary,
        "execution_contract_passed": True,
        "candidate_evaluator_outcome_receipt_passed": (
            candidate_receipt_passed
        ),
        "development_classic_pool_verdict_passed": passed,
        **FALSE_AUTHORITY_CONTROLS,
    }
    payload["payload_sha256"] = verdict_payload_sha256(payload)
    return _validate_verdict_structure(payload)


def validate_v4_native_strength_verdict(
    payload: Any,
    *,
    pool_plan_raw: bytes,
    candidate_report_raw: bytes,
    baseline_report_raw: bytes,
) -> dict[str, Any]:
    """Replay all three raw inputs and require exact canonical equality."""
    artifact = _validate_verdict_structure(payload)
    thresholds = _object(artifact.get("thresholds"), field="verdict.thresholds")
    samples = _integer(
        thresholds.get("bootstrap_samples"),
        field="verdict.thresholds.bootstrap_samples",
    )
    seed = _integer(
        thresholds.get("bootstrap_seed"), field="verdict.thresholds.bootstrap_seed"
    )
    expected = evaluate_v4_native_strength_verdict(
        pool_plan_raw,
        candidate_report_raw,
        baseline_report_raw,
        bootstrap_samples=samples,
        bootstrap_seed=seed,
    )
    if _canonical_bytes(artifact) != _canonical_bytes(expected):
        raise ValueError(
            "v4 native strength verdict does not match its raw inputs"
        )
    return artifact


def _read(path: Path) -> bytes:
    return path.read_bytes()


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-plan", required=True, type=Path)
    parser.add_argument("--candidate-report", required=True, type=Path)
    parser.add_argument("--baseline-report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--bootstrap-samples", type=int, default=DEFAULT_BOOTSTRAP_SAMPLES
    )
    parser.add_argument("--bootstrap-seed", type=int, default=DEFAULT_BOOTSTRAP_SEED)
    args = parser.parse_args(argv)
    try:
        payload = evaluate_v4_native_strength_verdict(
            _read(args.pool_plan),
            _read(args.candidate_report),
            _read(args.baseline_report),
            bootstrap_samples=args.bootstrap_samples,
            bootstrap_seed=args.bootstrap_seed,
        )
    except (OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n"
    _write_text_atomic(args.output, rendered)
    print(rendered, end="")
    return 0 if payload["development_classic_pool_verdict_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
