#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent
WEB_CORE = ROOT / "web" / "core"
for import_root in (ROOT, TOOLS, WEB_CORE):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from national_native import (  # noqa: E402
    run_legacy_debug_tcp_pair_with_wrappers,
    run_native_tcp_pair,
)

try:  # package import in tests; direct import for the CLI
    from .v4_native_strength_artifacts import tree_digest as _artifact_tree_digest
    from .v4_native_strength_runtime import (
        DEFAULT_MATCH_TIMEOUT_SEC,
        RUNTIME_ENVIRONMENT_OVERRIDES,
        native_strength_runtime_contract,
        validate_native_strength_runtime_contract,
    )
except ImportError:  # pragma: no cover - direct CLI execution path
    from v4_native_strength_artifacts import (  # type: ignore[no-redef]
        tree_digest as _artifact_tree_digest,
    )
    from v4_native_strength_runtime import (  # type: ignore[no-redef]
        DEFAULT_MATCH_TIMEOUT_SEC,
        RUNTIME_ENVIRONMENT_OVERRIDES,
        native_strength_runtime_contract,
        validate_native_strength_runtime_contract,
    )


DEFAULT_DECK_SEED_GUARD = 10
DEFAULT_OPPONENT_SEED_STRIDE = 10_000_000
DEFAULT_OUTCOME_BOOTSTRAP_SAMPLES = 20_000
DEFAULT_OUTCOME_BOOTSTRAP_SEED = 20_260_712
PRIMARY_OUTCOME_CRITERION = "net_chips_after_70_hands_gt_zero"
STRENGTH_EVIDENCE_SCHEMA = "native_tcp_strength_evidence_v2_outcome_first"
CANDIDATE_ABLATION_SCHEMA = "opponent_multitask_v4_native_ablation_v1"
CANDIDATE_ABLATION_ENV_NAMES = (
    "POK_V4_DISABLE",
    "POK_V4_DISABLE_CROSS_HAND",
    "POK_V4_DISABLE_OUTCOME_UNCERTAINTY_MATCH",
)
LEGACY_NEURAL_ABLATION_ENV_NAMES = (
    "POK_V3_DISABLE",
    "POK_V3_DISABLE_CROSS_HAND",
    "POK_V3_DISABLE_RISK_MATCH",
)
EVALUATION_CONTROL_ENV_NAMES = (
    "POK_TRACE_DECISIONS",
    "POK_FORCE_HAND",
    "POK_FORCE_DECISION",
    "POK_FORCE_ACTION",
)
CANDIDATE_ABLATION_MODES = (
    "full",
    "neural_off",
    "cross_hand_off",
    "outcome_uncertainty_match_off",
)


def _candidate_ablation_contract(mode: str) -> dict[str, Any]:
    if mode not in CANDIDATE_ABLATION_MODES:
        raise ValueError(f"unknown candidate ablation mode: {mode!r}")
    candidate_env = {name: None for name in CANDIDATE_ABLATION_ENV_NAMES}
    enabled_env = {
        "neural_off": "POK_V4_DISABLE",
        "cross_hand_off": "POK_V4_DISABLE_CROSS_HAND",
        "outcome_uncertainty_match_off": (
            "POK_V4_DISABLE_OUTCOME_UNCERTAINTY_MATCH"
        ),
    }.get(mode)
    if enabled_env is not None:
        candidate_env[enabled_env] = "1"
    diagnostic_only = mode != "full"
    return {
        "schema": CANDIDATE_ABLATION_SCHEMA,
        "mode": mode,
        "candidate_env_overrides": candidate_env,
        "opponent_env_overrides": {
            name: None for name in CANDIDATE_ABLATION_ENV_NAMES
        },
        "diagnostic_only": diagnostic_only,
        "eligible_as_strength_evidence": not diagnostic_only,
        "protected_data_read": False,
        "policy_roles_opened": [],
        "deployment_policy_value": False,
        "strength_evidence": False,
    }


def _candidate_ablation_contract_errors(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["candidate_ablation_contract_missing"]
    try:
        expected = _candidate_ablation_contract(str(payload.get("mode") or ""))
    except ValueError:
        return ["candidate_ablation_mode_unknown"]
    if payload != expected:
        return ["candidate_ablation_contract_mismatch"]
    return []


def _candidate_ablation_request_errors(args: argparse.Namespace) -> list[str]:
    mode = str(getattr(args, "candidate_ablation", "full"))
    try:
        _candidate_ablation_contract(mode)
    except ValueError:
        return ["candidate_ablation_mode_unknown"]
    errors = []
    if mode != "full" and bool(args.allow_generated_opponent_entry):
        errors.append("candidate_ablation_requires_native_opponents")
    if mode != "full" and bool(getattr(args, "strength_evidence", False)):
        errors.append("candidate_ablation_is_diagnostic_only")
    return errors


def _native_process_env_overrides(
    args: argparse.Namespace,
    ablation_contract: dict[str, Any],
    *,
    candidate: bool,
) -> dict[str, str | None]:
    """Return the complete evaluator-owned environment for one seat.

    Native runners inherit the parent process environment.  Explicit nulls are
    therefore evidence controls: they prevent an unrelated shell probe from
    silently changing either policy while the report still claims no force or
    ablation was active.
    """
    force_values = {
        "POK_FORCE_HAND": getattr(args, "force_hand", None),
        "POK_FORCE_DECISION": getattr(args, "force_decision", None),
        "POK_FORCE_ACTION": getattr(args, "force_action", None),
    }
    overrides: dict[str, str | None] = {
        **RUNTIME_ENVIRONMENT_OVERRIDES,
        "POK_TRACE_DECISIONS": (
            "1" if bool(getattr(args, "trace_decisions", False)) else None
        ),
        **{
            name: None if value is None else str(int(value))
            for name, value in force_values.items()
        },
        **{name: None for name in LEGACY_NEURAL_ABLATION_ENV_NAMES},
    }
    overrides.update(
        ablation_contract[
            "candidate_env_overrides" if candidate else "opponent_env_overrides"
        ]
    )
    return overrides


def _resolve(path: str) -> Path:
    raw = Path(path)
    return raw if raw.is_absolute() else (ROOT / raw).resolve()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{time.monotonic_ns()}"
    )
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _directory_digest(path: Path) -> str:
    return _artifact_tree_digest(path)


def _seeds(args: argparse.Namespace) -> list[int | None]:
    if args.seeds:
        return [int(seed) for seed in args.seeds.split(",") if seed.strip()]
    if args.seed_base is None:
        return [None for _ in range(args.matches)]
    stride = (
        int(args.seed_stride)
        if args.seed_stride is not None
        else int(args.hands) + DEFAULT_DECK_SEED_GUARD
    )
    return [int(args.seed_base) + idx * stride for idx in range(args.matches)]


def _opponent_deck_seed(
    base_seed: int | None,
    opponent_idx: int,
    opponent_seed_stride: int,
) -> int | None:
    if base_seed is None:
        return None
    return int(base_seed) + int(opponent_idx) * int(opponent_seed_stride)


def _seed_window_overlaps(
    seeds: list[int | None], *, hands: int
) -> list[tuple[int, int]]:
    numeric = [int(seed) for seed in seeds if seed is not None]
    overlaps = []
    for left_index, left in enumerate(numeric):
        left_last = left + int(hands) - 1
        for right in numeric[left_index + 1:]:
            right_last = right + int(hands) - 1
            if max(left, right) <= min(left_last, right_last):
                overlaps.append((left, right))
    return overlaps


def _strength_request_errors(
    args: argparse.Namespace,
    base_seeds: list[int | None],
    *,
    opponent_count: int,
) -> list[str]:
    errors = _candidate_ablation_request_errors(args)
    if not args.paired:
        errors.append("paired_required")
    if int(args.hands) != 70:
        errors.append("hands_per_leg_must_equal_70")
    if args.allow_generated_opponent_entry:
        errors.append("legacy_wrapper_must_be_disabled")
    if args.bot_seed_base is None:
        errors.append("bot_seed_base_required")
    else:
        bot_seeds = [
            _bot_seed(args, match_idx, opponent_idx)
            for opponent_idx in range(opponent_count)
            for match_idx in range(len(base_seeds))
        ]
        ordered_bot_seeds = sorted(bot_seeds)
        if len(set(bot_seeds)) != len(bot_seeds):
            errors.append("bot_seed_collision")
        elif any(
            right <= left + 1
            for left, right in zip(ordered_bot_seeds, ordered_bot_seeds[1:])
        ):
            errors.append("per_player_bot_seed_window_overlap")
    if any(seed is None for seed in base_seeds):
        errors.append("deterministic_deck_seeds_required")
    if len(base_seeds) < 3:
        errors.append("at_least_three_seed_blocks_required")
    if opponent_count <= 0:
        errors.append("opponent_required")
    if not 1 <= int(args.workers) <= 4:
        errors.append("workers_must_be_between_1_and_4")
    if int(args.opponent_seed_stride) <= 0:
        errors.append("opponent_seed_stride_must_be_positive")
    if int(args.bot_seed_stride) <= 0:
        errors.append("bot_seed_stride_must_be_positive")
    if int(args.outcome_bootstrap_samples) < 2_000:
        errors.append("outcome_bootstrap_samples_must_be_at_least_2000")
    if float(args.timeout_sec) != DEFAULT_MATCH_TIMEOUT_SEC:
        errors.append("match_timeout_must_equal_frozen_default")
    if bool(args.trace_decisions):
        errors.append("decision_trace_forbidden")
    if any(
        value is not None
        for value in (args.force_hand, args.force_decision, args.force_action)
    ):
        errors.append("forced_actions_forbidden")
    actual_seeds = [
        _opponent_deck_seed(seed, opponent_idx, args.opponent_seed_stride)
        for opponent_idx in range(opponent_count)
        for seed in base_seeds
    ]
    overlaps = _seed_window_overlaps(actual_seeds, hands=int(args.hands))
    if overlaps:
        errors.append(f"overlapping_deck_windows:{overlaps[:5]}")
    return errors


def _strength_result_errors(
    payload: dict[str, Any],
    *,
    expected_rows: int,
    hands_per_leg: int,
) -> list[str]:
    errors = []
    errors.extend(
        _candidate_ablation_contract_errors(payload.get("candidate_ablation"))
    )
    ablation = payload.get("candidate_ablation")
    if isinstance(ablation, dict) and not ablation.get(
        "eligible_as_strength_evidence"
    ):
        errors.append("candidate_ablation_not_strength_eligible")
    if payload.get("format") != "native_tcp_evaluation_v2":
        errors.append("unsupported_evaluation_format")
    if payload.get("execution_mode") != "native_tcp":
        errors.append("execution_mode_not_native_tcp")
    try:
        validate_native_strength_runtime_contract(
            payload.get("runtime_contract"), require_default_timeout=True
        )
    except ValueError:
        errors.append("runtime_contract_invalid")
    if not payload.get("paired"):
        errors.append("payload_not_paired")
    if not payload.get("requires_native_opponents"):
        errors.append("native_opponents_not_required")
    if payload.get("legacy_debug_wrapper_enabled") or payload.get("wrapper_used"):
        errors.append("payload_wrapper_enabled_or_used")
    artifacts = payload.get("execution_artifacts") or {}
    candidate_artifact = artifacts.get("candidate") or {}
    opponent_artifacts = list(artifacts.get("opponents") or [])
    if not _valid_artifact(candidate_artifact):
        errors.append("candidate_artifact_not_stable")
    if not opponent_artifacts or any(
        not _valid_artifact(artifact) for artifact in opponent_artifacts
    ):
        errors.append("opponent_artifact_not_stable")
    rows = list(payload.get("rows") or [])
    if len(rows) != expected_rows:
        errors.append(f"row_count:{len(rows)}!={expected_rows}")
    for index, row in enumerate(rows):
        prefix = f"row[{index}]"
        if row.get("leg") != "paired":
            errors.append(f"{prefix}:not_paired")
        if int(row.get("hands_played", 0) or 0) != 2 * hands_per_leg:
            errors.append(f"{prefix}:short_match")
        if len(row.get("hand_net_chips") or []) != hands_per_leg:
            errors.append(f"{prefix}:incomplete_hand_vector")
        if not row.get("passed_compliance"):
            errors.append(f"{prefix}:compliance_failed")
        if row.get("wrapper_used"):
            errors.append(f"{prefix}:wrapper_used")
        if row.get("issues"):
            errors.append(f"{prefix}:issues_present")
        legs = list(row.get("legs") or [])
        if len(legs) != 2:
            errors.append(f"{prefix}:paired_legs_missing")
        for leg_index, leg in enumerate(legs):
            if int(leg.get("hands_played", 0) or 0) != hands_per_leg:
                errors.append(f"{prefix}:leg[{leg_index}]:short_match")
            if not leg.get("passed_compliance"):
                errors.append(f"{prefix}:leg[{leg_index}]:compliance_failed")
        for field in (
            "candidate_illegal",
            "candidate_timeouts",
            "opponent_illegal",
            "opponent_timeouts",
            "adapter_actions_candidate",
            "adapter_actions_opponent",
        ):
            if int(row.get(field, 0) or 0) != 0:
                errors.append(f"{prefix}:{field}")
    deck_seeds = [row.get("deck_seed_base") for row in rows]
    overlaps = _seed_window_overlaps(deck_seeds, hands=hands_per_leg)
    if overlaps:
        errors.append(f"overlapping_result_deck_windows:{overlaps[:5]}")
    return errors


def _strength_outcome_errors(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    outcome = payload.get("seventy_hand_outcomes")
    if not isinstance(outcome, dict):
        return ["seventy_hand_outcomes_missing"]
    if outcome.get("criterion") != PRIMARY_OUTCOME_CRITERION:
        errors.append("primary_outcome_criterion_mismatch")
    combined = outcome.get("combined")
    if not isinstance(combined, dict):
        return [*errors, "combined_outcome_summary_missing"]

    def require_lcb(field: str, label: str) -> None:
        interval = combined.get(field)
        if not isinstance(interval, dict):
            errors.append(f"{label}_missing")
            return
        low = interval.get("low")
        if (
            isinstance(low, bool)
            or not isinstance(low, (int, float))
            or not math.isfinite(float(low))
        ):
            errors.append(f"{label}_invalid")
        elif float(low) <= 0.5:
            errors.append(f"{label}_not_above_half")

    require_lcb(
        "cluster_bootstrap_positive_rate_ci",
        "ordinary_positive_rate_lcb",
    )
    require_lcb(
        "opponent_stratified_cluster_bootstrap_positive_rate_ci",
        "opponent_stratified_positive_rate_lcb",
    )
    opponents = outcome.get("opponents")
    if not isinstance(opponents, dict) or not opponents:
        errors.append("per_opponent_outcome_summary_missing")
    else:
        for opponent, stats in sorted(opponents.items()):
            if not isinstance(stats, dict):
                errors.append(f"opponent:{opponent}:summary_invalid")
                continue
            rate = stats.get("positive_rate")
            if (
                isinstance(rate, bool)
                or not isinstance(rate, (int, float))
                or not math.isfinite(float(rate))
            ):
                errors.append(f"opponent:{opponent}:positive_rate_invalid")
            elif float(rate) < 0.5:
                errors.append(f"opponent:{opponent}:positive_rate_below_half")
    if combined.get("win_rate_evidence_passed") is not (not errors):
        errors.append("win_rate_evidence_passed_inconsistent")
    return errors


def _strength_evidence_payload(
    *,
    requested: bool,
    request_errors: list[str],
    result_errors: list[str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    statistical_errors = _strength_outcome_errors(payload) if requested else []
    execution_contract_passed = bool(
        requested and not request_errors and not result_errors
    )
    outcome_gate_passed = bool(
        execution_contract_passed and not statistical_errors
    )
    return {
        "schema": STRENGTH_EVIDENCE_SCHEMA,
        "criterion": PRIMARY_OUTCOME_CRITERION,
        "requested": bool(requested),
        "execution_contract_passed": execution_contract_passed,
        "outcome_gate_passed": outcome_gate_passed,
        "passed": outcome_gate_passed,
        "request_errors": list(request_errors),
        "result_errors": list(result_errors),
        "statistical_errors": statistical_errors,
    }


def _valid_artifact(artifact: dict[str, Any]) -> bool:
    before = artifact.get("sha256_before")
    after = artifact.get("sha256_after")
    return bool(
        artifact.get("stable")
        and artifact.get("path")
        and isinstance(before, str)
        and len(before) == 64
        and before == after
    )


def _bot_seed(args: argparse.Namespace, match_idx: int, opponent_idx: int) -> int | None:
    if args.bot_seed_base is None:
        return None
    return int(args.bot_seed_base) + match_idx * int(args.bot_seed_stride) + opponent_idx * 100_000


def _percentile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0.0, min(1.0, probability)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _seventy_hand_legs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    legs: list[dict[str, Any]] = []
    for row in rows:
        nested = row.get("legs")
        if row.get("leg") == "paired" and isinstance(nested, list):
            legs.extend(dict(leg) for leg in nested if isinstance(leg, dict))
        else:
            legs.append(row)
    return legs


def _outcome_stats(legs: list[dict[str, Any]]) -> dict[str, Any]:
    values = [int(leg["net_chips"]) for leg in legs]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    count = len(values)
    return {
        "criterion": PRIMARY_OUTCOME_CRITERION,
        "matches_70_hand": count,
        "wins": len(wins),
        "losses": len(losses),
        "draws": count - len(wins) - len(losses),
        "positive_rate": round(len(wins) / count, 6) if count else 0.0,
        "total_net_chips": sum(values),
        "mean_net_chips_per_match": (
            round(statistics.mean(values), 3) if values else 0.0
        ),
        "median_net_chips_per_match": statistics.median(values) if values else 0.0,
        "mean_chips_when_positive": (
            round(statistics.mean(wins), 3) if wins else 0.0
        ),
        "mean_chips_when_negative": (
            round(statistics.mean(losses), 3) if losses else 0.0
        ),
        "samples": values,
    }


def _row_outcomes(row: dict[str, Any]) -> list[int]:
    return [
        1 if int(leg["net_chips"]) > 0 else 0
        for leg in _seventy_hand_legs([row])
    ]


def _cluster_bootstrap_positive_rate_ci(
    rows: list[dict[str, Any]], *, samples: int, seed: int
) -> dict[str, Any]:
    clusters = [_row_outcomes(row) for row in rows]
    clusters = [cluster for cluster in clusters if cluster]
    resamples = max(1, int(samples))
    if not clusters:
        return {
            "clusters": 0,
            "matches_70_hand": 0,
            "resamples": resamples,
            "seed": int(seed),
            "confidence": 0.95,
            "low": 0.0,
            "high": 0.0,
        }
    rng = random.Random(seed)
    rates = []
    for _ in range(resamples):
        selected = [clusters[rng.randrange(len(clusters))] for _ in clusters]
        outcomes = [outcome for cluster in selected for outcome in cluster]
        rates.append(sum(outcomes) / len(outcomes))
    return {
        "clusters": len(clusters),
        "matches_70_hand": sum(len(cluster) for cluster in clusters),
        "resamples": resamples,
        "seed": int(seed),
        "confidence": 0.95,
        "low": round(_percentile(rates, 0.025), 6),
        "high": round(_percentile(rates, 0.975), 6),
    }


def _stratified_cluster_bootstrap_positive_rate_ci(
    groups: dict[str, list[dict[str, Any]]], *, samples: int, seed: int
) -> dict[str, Any]:
    clusters = {
        opponent: [_row_outcomes(row) for row in rows]
        for opponent, rows in groups.items()
    }
    clusters = {
        opponent: [cluster for cluster in group if cluster]
        for opponent, group in clusters.items()
    }
    clusters = {opponent: group for opponent, group in clusters.items() if group}
    resamples = max(1, int(samples))
    if not clusters:
        return {
            "opponents": 0,
            "clusters": 0,
            "matches_70_hand": 0,
            "resamples": resamples,
            "seed": int(seed),
            "confidence": 0.95,
            "low": 0.0,
            "high": 0.0,
        }
    rng = random.Random(seed)
    rates = []
    for _ in range(resamples):
        opponent_rates = []
        for group in clusters.values():
            selected = [group[rng.randrange(len(group))] for _ in group]
            outcomes = [outcome for cluster in selected for outcome in cluster]
            opponent_rates.append(sum(outcomes) / len(outcomes))
        rates.append(sum(opponent_rates) / len(opponent_rates))
    return {
        "opponents": len(clusters),
        "clusters": sum(len(group) for group in clusters.values()),
        "matches_70_hand": sum(
            len(cluster) for group in clusters.values() for cluster in group
        ),
        "resamples": resamples,
        "seed": int(seed),
        "confidence": 0.95,
        "low": round(_percentile(rates, 0.025), 6),
        "high": round(_percentile(rates, 0.975), 6),
    }


def _seventy_hand_outcome_summary(
    rows: list[dict[str, Any]], *, bootstrap_samples: int, bootstrap_seed: int
) -> dict[str, Any]:
    grouped = {
        opponent: [row for row in rows if row["opponent"] == opponent]
        for opponent in sorted({str(row["opponent"]) for row in rows})
    }
    by_opponent = {}
    for index, (opponent, subset) in enumerate(grouped.items()):
        stats = _outcome_stats(_seventy_hand_legs(subset))
        stats["cluster_bootstrap_positive_rate_ci"] = (
            _cluster_bootstrap_positive_rate_ci(
                subset,
                samples=bootstrap_samples,
                seed=bootstrap_seed + 100 + index,
            )
        )
        by_opponent[opponent] = stats
    combined = _outcome_stats(_seventy_hand_legs(rows))
    ordinary = _cluster_bootstrap_positive_rate_ci(
        rows, samples=bootstrap_samples, seed=bootstrap_seed
    )
    stratified = _stratified_cluster_bootstrap_positive_rate_ci(
        grouped, samples=bootstrap_samples, seed=bootstrap_seed + 1
    )
    combined.update({
        "cluster_bootstrap_positive_rate_ci": ordinary,
        "opponent_stratified_cluster_bootstrap_positive_rate_ci": stratified,
        "opponents_below_half": [
            opponent
            for opponent, stats in by_opponent.items()
            if stats["positive_rate"] < 0.5
        ],
        "win_rate_evidence_passed": bool(
            ordinary["low"] > 0.5
            and stratified["low"] > 0.5
            and all(stats["positive_rate"] >= 0.5 for stats in by_opponent.values())
        ),
    })
    return {
        "priority": 1,
        "criterion": PRIMARY_OUTCOME_CRITERION,
        "combined": combined,
        "opponents": by_opponent,
    }


def _summary(
    rows: list[dict[str, Any]],
    *,
    outcome_bootstrap_samples: int = DEFAULT_OUTCOME_BOOTSTRAP_SAMPLES,
    outcome_bootstrap_seed: int = DEFAULT_OUTCOME_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    by_opponent: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_opponent.setdefault(row["opponent"], {"rows": []})["rows"].append(row)
    for opponent, payload in by_opponent.items():
        subset = payload.pop("rows")
        values = [int(row["net_chips"]) for row in subset]
        hands = sum(int(row["hands_played"]) for row in subset)
        payload.update({
            "matches": len(subset),
            "hands": hands,
            "compliant_matches": sum(1 for row in subset if row["passed_compliance"]),
            "total_net_chips": sum(values),
            "mean_net_chips": round(statistics.mean(values), 3) if values else 0.0,
            "median_net_chips": statistics.median(values) if values else 0,
            "mean_per_hand": round(sum(values) / max(1, hands), 3),
            "wins": sum(1 for value in values if value > 0),
            "losses": sum(1 for value in values if value < 0),
            "draws": sum(1 for value in values if value == 0),
            "samples": values,
            "issues": [row for row in subset if row["issues"]],
            "candidate_illegal_total": sum(row["candidate_illegal"] for row in subset),
            "candidate_timeouts_total": sum(row["candidate_timeouts"] for row in subset),
            "opponent_illegal_total": sum(row["opponent_illegal"] for row in subset),
            "opponent_timeouts_total": sum(row["opponent_timeouts"] for row in subset),
            "adapter_actions_candidate_total": sum(row["adapter_actions_candidate"] for row in subset),
            "adapter_actions_opponent_total": sum(row["adapter_actions_opponent"] for row in subset),
        })
    combined = [int(row["net_chips"]) for row in rows]
    combined_hands = sum(int(row["hands_played"]) for row in rows)
    return {
        "seventy_hand_outcomes": _seventy_hand_outcome_summary(
            rows,
            bootstrap_samples=outcome_bootstrap_samples,
            bootstrap_seed=outcome_bootstrap_seed,
        ),
        "combined": {
            "unit": "paired_seed_block" if any(row.get("leg") == "paired" for row in rows) else "single_match",
            "matches": len(rows),
            "hands": combined_hands,
            "compliant_matches": sum(1 for row in rows if row["passed_compliance"]),
            "total_net_chips": sum(combined),
            "mean_per_hand": round(sum(combined) / max(1, combined_hands), 3),
            "wins": sum(1 for value in combined if value > 0),
            "losses": sum(1 for value in combined if value < 0),
            "draws": sum(1 for value in combined if value == 0),
        },
        "opponents": by_opponent,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    ablation_errors = _candidate_ablation_request_errors(args)
    if ablation_errors:
        raise ValueError(
            "candidate-ablation request rejected: " + ", ".join(ablation_errors)
        )
    ablation_contract = _candidate_ablation_contract(
        str(getattr(args, "candidate_ablation", "full"))
    )
    candidate = _resolve(args.candidate)
    opponents = [_resolve(item) for item in args.opponent]
    candidate_digest_before = _directory_digest(candidate)
    opponent_digests_before = [_directory_digest(path) for path in opponents]
    base_seed_values = _seeds(args)
    semaphore = asyncio.Semaphore(max(1, int(args.workers)))
    started = time.time()

    def result_row(
        result: dict[str, Any],
        opponent: Path,
        match_idx: int,
        deck_seed: int | None,
        *,
        candidate_is_a: bool,
        leg: str,
    ) -> dict[str, Any]:
        bot_a = result["bot_a"]
        bot_b = result["bot_b"]
        candidate_label = bot_a if candidate_is_a else bot_b
        opponent_label = bot_b if candidate_is_a else bot_a
        candidate_key = bot_a if candidate_is_a else bot_b
        opponent_key = bot_b if candidate_is_a else bot_a
        candidate_idx = 0 if candidate_is_a else 1
        net_chips = int(result["net_chips_a"] if candidate_is_a else result["net_chips_b"])
        hand_net_chips = [
            int(row["earnings"][candidate_idx])
            for row in result.get("settlements", [])
            if isinstance(row.get("earnings"), list) and len(row["earnings"]) >= 2
        ]
        return {
            "candidate": candidate_label,
            "opponent": opponent_label,
            "opponent_path": str(opponent),
            "match_idx": match_idx,
            "leg": leg,
            "deck_seed_base": deck_seed,
            "bot_seed_base": result.get("bot_seed_base"),
            "hands_played": int(result["hands_played"]),
            "net_chips": net_chips,
            "net_chips_per_hand": round(net_chips / max(1, int(result["hands_played"])), 3),
            "hand_net_chips": hand_net_chips,
            "passed_compliance": bool(result["passed_compliance"]),
            "wrapper_used": bool(result.get("wrapper_used")),
            "issues": result["issues"],
            "candidate_illegal": result["per_player"][candidate_key]["illegal_actions"],
            "candidate_timeouts": result["per_player"][candidate_key]["timeouts"],
            "opponent_illegal": result["per_player"][opponent_key]["illegal_actions"],
            "opponent_timeouts": result["per_player"][opponent_key]["timeouts"],
            "adapter_actions_candidate": result["per_player"][candidate_key]["adapter"]["actions_sent"],
            "adapter_actions_opponent": result["per_player"][opponent_key]["adapter"]["actions_sent"],
            "candidate_native": result["per_player"][candidate_key]["native"],
            "opponent_native": result["per_player"][opponent_key]["native"],
            "candidate_runtime_telemetry": result["per_player"][candidate_key].get(
                "runtime_telemetry", {}
            ),
            "opponent_runtime_telemetry": result["per_player"][opponent_key].get(
                "runtime_telemetry", {}
            ),
        }

    async def one(opponent_idx: int, opponent: Path, match_idx: int, deck_seed: int | None) -> dict[str, Any]:
        async with semaphore:
            bot_seed_base = _bot_seed(args, match_idx, opponent_idx)
            pair_runner = (
                run_legacy_debug_tcp_pair_with_wrappers
                if args.allow_generated_opponent_entry
                else run_native_tcp_pair
            )
            strict_kwargs = {} if args.allow_generated_opponent_entry else {
                "require_native_a": True,
                "require_native_b": True,
            }
            strict_kwargs["sanitize_parent_environment"] = True
            forward_env_kwargs = {
                "bot_a_env_overrides": dict(
                    _native_process_env_overrides(
                        args, ablation_contract, candidate=True
                    )
                ),
                "bot_b_env_overrides": dict(
                    _native_process_env_overrides(
                        args, ablation_contract, candidate=False
                    )
                ),
            }
            forward = await pair_runner(
                candidate,
                opponent,
                int(args.hands),
                deck_seed_base=deck_seed,
                bot_seed_base=bot_seed_base,
                timeout_sec=float(args.timeout_sec),
                **strict_kwargs,
                **forward_env_kwargs,
            )
            forward_row = result_row(
                forward,
                opponent,
                match_idx,
                deck_seed,
                candidate_is_a=True,
                leg="forward",
            )
            if not args.paired:
                return forward_row
            swapped_env_kwargs = {
                "bot_a_env_overrides": dict(
                    _native_process_env_overrides(
                        args, ablation_contract, candidate=False
                    )
                ),
                "bot_b_env_overrides": dict(
                    _native_process_env_overrides(
                        args, ablation_contract, candidate=True
                    )
                ),
            }
            swapped = await pair_runner(
                opponent,
                candidate,
                int(args.hands),
                deck_seed_base=deck_seed,
                bot_seed_base=bot_seed_base,
                timeout_sec=float(args.timeout_sec),
                **strict_kwargs,
                **swapped_env_kwargs,
            )
            swapped_row = result_row(
                swapped,
                opponent,
                match_idx,
                deck_seed,
                candidate_is_a=False,
                leg="swapped",
            )
            hands_played = int(forward_row["hands_played"]) + int(swapped_row["hands_played"])
            net_chips = int(forward_row["net_chips"]) + int(swapped_row["net_chips"])
            forward_hands = list(forward_row.get("hand_net_chips", []))
            swapped_hands = list(swapped_row.get("hand_net_chips", []))
            paired_hand_net_chips = [
                int(forward_hands[idx]) + int(swapped_hands[idx])
                for idx in range(min(len(forward_hands), len(swapped_hands)))
            ]
            issues = (
                [f"forward:{issue}" for issue in forward_row["issues"]]
                + [f"swapped:{issue}" for issue in swapped_row["issues"]]
            )
            return {
                "candidate": forward_row["candidate"],
                "opponent": forward_row["opponent"],
                "opponent_path": str(opponent),
                "match_idx": match_idx,
                "leg": "paired",
                "deck_seed_base": deck_seed,
                "bot_seed_base": bot_seed_base,
                "hands_played": hands_played,
                "net_chips": net_chips,
                "net_chips_per_hand": round(net_chips / max(1, hands_played), 3),
                "hand_net_chips": paired_hand_net_chips,
                "passed_compliance": bool(forward_row["passed_compliance"] and swapped_row["passed_compliance"]),
                "wrapper_used": bool(forward_row["wrapper_used"] or swapped_row["wrapper_used"]),
                "issues": issues,
                "candidate_illegal": int(forward_row["candidate_illegal"]) + int(swapped_row["candidate_illegal"]),
                "candidate_timeouts": int(forward_row["candidate_timeouts"]) + int(swapped_row["candidate_timeouts"]),
                "opponent_illegal": int(forward_row["opponent_illegal"]) + int(swapped_row["opponent_illegal"]),
                "opponent_timeouts": int(forward_row["opponent_timeouts"]) + int(swapped_row["opponent_timeouts"]),
                "adapter_actions_candidate": int(forward_row["adapter_actions_candidate"]) + int(swapped_row["adapter_actions_candidate"]),
                "adapter_actions_opponent": int(forward_row["adapter_actions_opponent"]) + int(swapped_row["adapter_actions_opponent"]),
                "legs": [forward_row, swapped_row],
            }

    tasks = [
        one(opponent_idx, opponent, match_idx, deck_seed)
        for opponent_idx, opponent in enumerate(opponents)
        for match_idx, base_seed in enumerate(base_seed_values)
        for deck_seed in [
            _opponent_deck_seed(
                base_seed, opponent_idx, args.opponent_seed_stride
            )
        ]
    ]
    rows = []
    for coro in asyncio.as_completed(tasks):
        row = await coro
        rows.append(row)
        if args.print_rows:
            print(json.dumps(row, ensure_ascii=False), flush=True)
    payload = {
        "format": "native_tcp_evaluation_v2",
        "execution_mode": "native_tcp",
        "candidate_ablation": ablation_contract,
        "runtime_contract": native_strength_runtime_contract(
            args.timeout_sec,
            trace_decisions=bool(args.trace_decisions),
            force_hand=args.force_hand,
            force_decision=args.force_decision,
            force_action=args.force_action,
        ),
        "candidate_path": str(candidate),
        "opponent_paths": [str(path) for path in opponents],
        "hands_per_match": int(args.hands),
        "seeds": base_seed_values,
        "deck_seed_scheme": "opponent_disjoint_match_blocks_v1",
        "opponent_seed_stride": int(args.opponent_seed_stride),
        "actual_deck_seed_bases": sorted({
            int(row["deck_seed_base"])
            for row in rows
            if row.get("deck_seed_base") is not None
        }),
        "execution_artifacts": {
            "candidate": {
                "path": str(candidate),
                "sha256_before": candidate_digest_before,
                "sha256_after": _directory_digest(candidate),
            },
            "opponents": [
                {
                    "path": str(path),
                    "sha256_before": opponent_digests_before[index],
                    "sha256_after": _directory_digest(path),
                }
                for index, path in enumerate(opponents)
            ],
        },
        "workers": int(args.workers),
        "paired": bool(args.paired),
        "requires_native_opponents": not args.allow_generated_opponent_entry,
        "legacy_debug_wrapper_enabled": bool(args.allow_generated_opponent_entry),
        "wrapper_used": any(bool(row.get("wrapper_used")) for row in rows),
        "bot_seed_base": args.bot_seed_base,
        "bot_seed_stride": int(args.bot_seed_stride),
        "outcome_bootstrap_samples": int(args.outcome_bootstrap_samples),
        "outcome_bootstrap_seed": int(args.outcome_bootstrap_seed),
        "trace_decisions": bool(args.trace_decisions),
        "force": {
            "hand": args.force_hand,
            "decision": args.force_decision,
            "action": args.force_action,
        },
        "elapsed_sec": round(time.time() - started, 3),
        "rows": sorted(rows, key=lambda row: (row["opponent"], row["match_idx"])),
    }
    payload["execution_artifacts"]["candidate"]["stable"] = (
        payload["execution_artifacts"]["candidate"]["sha256_before"]
        == payload["execution_artifacts"]["candidate"]["sha256_after"]
    )
    for artifact in payload["execution_artifacts"]["opponents"]:
        artifact["stable"] = (
            artifact["sha256_before"] == artifact["sha256_after"]
        )
    payload.update(_summary(
        payload["rows"],
        outcome_bootstrap_samples=int(args.outcome_bootstrap_samples),
        outcome_bootstrap_seed=int(args.outcome_bootstrap_seed),
    ))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate neural lab bots through native national TCP matches.")
    parser.add_argument("--candidate", required=True, help="Candidate bot directory containing native national_bot.py.")
    parser.add_argument("--opponent", action="append", required=True, help="Opponent bot directory. Repeat for multiple opponents.")
    parser.add_argument("--hands", type=int, default=10, help="Hands per match, capped by the native runner at 70.")
    parser.add_argument("--matches", type=int, default=10, help="Number of matches per opponent when --seeds is not provided.")
    parser.add_argument("--seed-base", type=int, default=None, help="Deck seed base for deterministic decks.")
    parser.add_argument(
        "--seed-stride",
        type=int,
        default=None,
        help="Deck-base stride. Defaults to hands + 10 to avoid overlap.",
    )
    parser.add_argument(
        "--opponent-seed-stride",
        type=int,
        default=DEFAULT_OPPONENT_SEED_STRIDE,
        help="Additional deck-base offset per opponent.",
    )
    parser.add_argument("--seeds", default="", help="Comma-separated deck seeds. Overrides --matches and --seed-base.")
    parser.add_argument("--bot-seed-base", type=int, default=None, help="Seed Python random in each native bot process.")
    parser.add_argument("--bot-seed-stride", type=int, default=10)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--timeout-sec", type=float, default=DEFAULT_MATCH_TIMEOUT_SEC
    )
    parser.add_argument("--paired", action="store_true", help="For each seed, run candidate/opponent and opponent/candidate, then sum candidate net chips.")
    parser.add_argument(
        "--candidate-ablation",
        choices=CANDIDATE_ABLATION_MODES,
        default="full",
        help=(
            "Apply one diagnostic v4 ablation to the candidate process only; "
            "the opponent environment is always cleared."
        ),
    )
    parser.add_argument("--trace-decisions", action="store_true", help="Set POK_TRACE_DECISIONS=1 for native bot subprocesses that support structured decision traces.")
    parser.add_argument("--force-hand", type=int, default=None, help="Set POK_FORCE_HAND for native bot subprocesses that support force probes.")
    parser.add_argument("--force-decision", type=int, default=None, help="Set POK_FORCE_DECISION for native bot subprocesses that support force probes.")
    parser.add_argument("--force-action", type=int, default=None, help="Set POK_FORCE_ACTION for native bot subprocesses that support force probes.")
    parser.add_argument(
        "--allow-generated-opponent-entry",
        action="store_true",
        help="Use the legacy/debug wrapper API for missing or invalid native entries. Off by default.",
    )
    parser.add_argument("--print-rows", action="store_true")
    parser.add_argument("--output", default="", help="Optional JSON output path.")
    parser.add_argument(
        "--outcome-bootstrap-samples",
        type=int,
        default=DEFAULT_OUTCOME_BOOTSTRAP_SAMPLES,
        help="Cluster bootstrap resamples for the primary 70-hand positive-rate metric.",
    )
    parser.add_argument(
        "--outcome-bootstrap-seed",
        type=int,
        default=DEFAULT_OUTCOME_BOOTSTRAP_SEED,
    )
    parser.add_argument(
        "--strength-evidence",
        action="store_true",
        help="Fail unless this is an independent, complete, compliant 70-hand paired evaluation.",
    )
    args = parser.parse_args()

    ablation_errors = _candidate_ablation_request_errors(args)
    if ablation_errors:
        raise SystemExit(
            "candidate-ablation request rejected: "
            + ", ".join(ablation_errors)
        )
    base_seeds = _seeds(args)
    request_errors = _strength_request_errors(
        args, base_seeds, opponent_count=len(args.opponent)
    ) if args.strength_evidence else []
    if request_errors:
        raise SystemExit(
            "strength-evidence request rejected: " + ", ".join(request_errors)
        )

    payload = asyncio.run(_run(args))
    result_errors = _strength_result_errors(
        payload,
        expected_rows=len(args.opponent) * len(base_seeds),
        hands_per_leg=int(args.hands),
    ) if args.strength_evidence else []
    payload["strength_evidence"] = _strength_evidence_payload(
        requested=bool(args.strength_evidence),
        request_errors=request_errors,
        result_errors=result_errors,
        payload=payload,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.output:
        output = _resolve(args.output)
        _atomic_write_json(output, payload)
    return 2 if (
        args.strength_evidence and not payload["strength_evidence"]["passed"]
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())
