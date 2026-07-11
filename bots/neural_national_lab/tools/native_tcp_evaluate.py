#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
WEB_CORE = ROOT / "web" / "core"
if str(WEB_CORE) not in sys.path:
    sys.path.insert(0, str(WEB_CORE))

from national_native import (  # noqa: E402
    run_legacy_debug_tcp_pair_with_wrappers,
    run_native_tcp_pair,
)


DEFAULT_DECK_SEED_GUARD = 10
DEFAULT_OPPONENT_SEED_STRIDE = 10_000_000


def _resolve(path: str) -> Path:
    raw = Path(path)
    return raw if raw.is_absolute() else (ROOT / raw).resolve()


def _directory_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for file_path in sorted(
        item
        for item in path.rglob("*")
        if (
            item.is_file()
            and "__pycache__" not in item.parts
            and item.name != ".completed"
            and item.suffix != ".pyc"
        )
    ):
        digest.update(str(file_path.relative_to(path)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


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
    errors = []
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
        if len(set(bot_seeds)) != len(bot_seeds):
            errors.append("bot_seed_collision")
    if any(seed is None for seed in base_seeds):
        errors.append("deterministic_deck_seeds_required")
    if len(base_seeds) < 3:
        errors.append("at_least_three_seed_blocks_required")
    if opponent_count <= 0:
        errors.append("opponent_required")
    if int(args.workers) > 4:
        errors.append("workers_must_not_exceed_4")
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
    if payload.get("format") != "native_tcp_evaluation_v2":
        errors.append("unsupported_evaluation_format")
    if payload.get("execution_mode") != "native_tcp":
        errors.append("execution_mode_not_native_tcp")
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


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
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
        "combined": {
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
            forward = await pair_runner(
                candidate,
                opponent,
                int(args.hands),
                deck_seed_base=deck_seed,
                bot_seed_base=bot_seed_base,
                timeout_sec=float(args.timeout_sec),
                **strict_kwargs,
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
            swapped = await pair_runner(
                opponent,
                candidate,
                int(args.hands),
                deck_seed_base=deck_seed,
                bot_seed_base=bot_seed_base,
                timeout_sec=float(args.timeout_sec),
                **strict_kwargs,
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
    payload.update(_summary(payload["rows"]))
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
    parser.add_argument("--timeout-sec", type=float, default=90.0)
    parser.add_argument("--paired", action="store_true", help="For each seed, run candidate/opponent and opponent/candidate, then sum candidate net chips.")
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
        "--strength-evidence",
        action="store_true",
        help="Fail unless this is an independent, complete, compliant 70-hand paired evaluation.",
    )
    args = parser.parse_args()

    base_seeds = _seeds(args)
    request_errors = _strength_request_errors(
        args, base_seeds, opponent_count=len(args.opponent)
    ) if args.strength_evidence else []
    if request_errors:
        raise SystemExit(
            "strength-evidence request rejected: " + ", ".join(request_errors)
        )

    if args.trace_decisions:
        os.environ["POK_TRACE_DECISIONS"] = "1"
    for env_name, value in (
        ("POK_FORCE_HAND", args.force_hand),
        ("POK_FORCE_DECISION", args.force_decision),
        ("POK_FORCE_ACTION", args.force_action),
    ):
        if value is not None:
            os.environ[env_name] = str(int(value))

    payload = asyncio.run(_run(args))
    result_errors = _strength_result_errors(
        payload,
        expected_rows=len(args.opponent) * len(base_seeds),
        hands_per_leg=int(args.hands),
    ) if args.strength_evidence else []
    payload["strength_evidence"] = {
        "requested": bool(args.strength_evidence),
        "passed": bool(args.strength_evidence and not result_errors),
        "request_errors": request_errors,
        "result_errors": result_errors,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.output:
        output = _resolve(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 2 if result_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
