#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import copy
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
ENGINE = ROOT / "engine"
TOOL_DIR = Path(__file__).resolve().parent
for path in (ROOT, ENGINE, TOOL_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from engine.battle import _PersistentBot, _call_bot  # noqa: E402
from judge import judge as judge_func  # noqa: E402
from paired_evaluate import _fresh_initdata, _label, _main_path, _mirror_initdata, _rel, _resolve, _seeded_initdata, _stats  # noqa: E402
from seeded_process import SeededPersistentBot, match_bot_seeds  # noqa: E402


def _pair_seed(args: argparse.Namespace, idx: int, opponent_index: int) -> int | None:
    if args.seed_base is None:
        return None
    return (
        int(args.seed_base)
        + int(args.seed_offset)
        + int(opponent_index) * int(args.opponent_seed_stride)
        + int(idx) * int(args.seed_stride)
    )


def _opponent_seed_base(args: argparse.Namespace, opponent_index: int) -> int | None:
    if args.bot_seed_base is None:
        return None
    return int(args.bot_seed_base) + int(opponent_index) * int(args.opponent_bot_seed_stride)


def _public_stage(public_cards: list[Any]) -> str:
    n = len(public_cards)
    if n >= 5:
        return "river"
    if n == 4:
        return "turn"
    if n >= 3:
        return "flop"
    return "preflop"


def _request_summary(request: dict[str, Any]) -> dict[str, Any]:
    public_cards = list(request.get("public_cards") or [])
    history = list(request.get("history") or [])
    return {
        "hand": request.get("hand"),
        "round": request.get("round"),
        "my_id": request.get("my_id"),
        "dealer_id": request.get("dealer_id"),
        "stage": _public_stage(public_cards),
        "public_cards": public_cards,
        "my_cards": list(request.get("my_cards") or []),
        "pot": request.get("pot"),
        "to_call": request.get("to_call"),
        "my_stage_bet": request.get("my_stage_bet"),
        "opponent_stage_bet": request.get("opponent_stage_bet"),
        "my_chips": request.get("my_chips"),
        "opponent_allin": request.get("opponent_allin"),
        "history_len": len(history),
        "last_history": history[-3:],
    }


def _play_match_record(
    bot0: Path,
    bot1: Path,
    initdata: dict[str, Any],
    bot_seeds: tuple[int, int] | None,
    include_full_requests: bool,
) -> dict[str, Any]:
    bot_paths = [str(bot0.resolve()), str(bot1.resolve())]
    if bot_seeds is None:
        persistent = [_PersistentBot(bot_paths[0]), _PersistentBot(bot_paths[1])]
    else:
        persistent = [
            SeededPersistentBot(bot_paths[0], bot_seeds[0]),
            SeededPersistentBot(bot_paths[1], bot_seeds[1]),
        ]
    try:
        result = json.loads(judge_func(json.dumps({"log": [], "initdata": copy.deepcopy(initdata)})))
        game_initdata = copy.deepcopy(result["initdata"])
        log: list[dict[str, Any]] = [{"output": result}]
        bot_requests: list[list[dict[str, Any]]] = [[], []]
        bot_responses: list[list[int]] = [[], []]
        bot_data: list[Any] = [None, None]
        decisions: list[dict[str, Any]] = []

        while result.get("command") == "request":
            content = result.get("content", {})
            if not content:
                break
            player_id = int(next(iter(content.keys())))
            request_data = content[str(player_id)]
            response, verdict, _ = _call_bot(
                bot_paths,
                player_id,
                request_data,
                bot_requests,
                bot_responses,
                bot_data=bot_data,
                persistent_procs=persistent,
            )
            if player_id == 0:
                row = {
                    "decision_index": len(decisions),
                    "action": int(response),
                    "verdict": verdict,
                    "request": _request_summary(request_data),
                }
                if include_full_requests:
                    row["full_requests"] = copy.deepcopy(bot_requests[player_id])
                decisions.append(row)
            log.append({str(player_id): {"response": str(response), "verdict": verdict}, "output": None})
            result = json.loads(judge_func(json.dumps({"log": log, "initdata": game_initdata})))
            log.append({"output": result})
            if result.get("command") == "finish":
                break

        chips = [0.0, 0.0]
        if result.get("command") == "finish":
            final = result.get("display", {}).get("final_result", [])
            if len(final) >= 2:
                chips = [float(final[0]["win_chips"]), float(final[1]["win_chips"])]
        return {
            "winner": 0 if chips[0] > chips[1] else (1 if chips[1] > chips[0] else -1),
            "bot0_chips": chips[0],
            "bot1_chips": chips[1],
            "decisions": decisions,
        }
    finally:
        for proc in persistent:
            proc.close()


def _compare_decisions(
    baseline: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    max_divergences: int,
    include_full_requests: bool,
) -> dict[str, Any]:
    divergences: list[dict[str, Any]] = []
    comparable = min(len(baseline), len(candidate))
    for pos in range(comparable):
        b = baseline[pos]
        c = candidate[pos]
        if int(b["action"]) == int(c["action"]):
            continue
        row = {
            "decision_index": pos,
            "baseline_action": int(b["action"]),
            "candidate_action": int(c["action"]),
            "same_request": b.get("request") == c.get("request"),
            "baseline_request": b.get("request"),
            "candidate_request": c.get("request"),
        }
        if include_full_requests:
            row["baseline_full_requests"] = copy.deepcopy(b.get("full_requests") or [])
            row["candidate_full_requests"] = copy.deepcopy(c.get("full_requests") or [])
        divergences.append(row)
        if len(divergences) >= max_divergences:
            break
    length_mismatch = len(baseline) != len(candidate)
    return {
        "baseline_decisions": len(baseline),
        "candidate_decisions": len(candidate),
        "compared_decisions": comparable,
        "divergence_count_capped": len(divergences),
        "has_divergence": bool(divergences or length_mismatch),
        "length_mismatch": length_mismatch,
        "first_divergence": divergences[0] if divergences else None,
        "divergences": divergences,
    }


def _scan_side(
    baseline: Path,
    candidate: Path,
    opponent: Path,
    initdata: dict[str, Any],
    bot_seeds: tuple[int, int] | None,
    max_divergences: int,
    include_full_requests: bool,
) -> dict[str, Any]:
    base = _play_match_record(baseline, opponent, initdata, bot_seeds, include_full_requests)
    cand = _play_match_record(candidate, opponent, initdata, bot_seeds, include_full_requests)
    compare = _compare_decisions(base["decisions"], cand["decisions"], max_divergences, include_full_requests)
    return {
        "baseline": {key: base[key] for key in ("winner", "bot0_chips", "bot1_chips")},
        "candidate": {key: cand[key] for key in ("winner", "bot0_chips", "bot1_chips")},
        "delta_chips": float(cand["bot0_chips"]) - float(base["bot0_chips"]),
        "action_compare": compare,
    }


def _scan_pair(
    opponent_index: int,
    opponent: Path,
    idx: int,
    baseline: Path,
    candidate: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    seed = _pair_seed(args, idx, opponent_index)
    initdata = _seeded_initdata(seed, args.max_hands) if seed is not None else _fresh_initdata()
    mirror = _mirror_initdata(initdata)
    bot_seed_base = _opponent_seed_base(args, opponent_index)
    normal_bot_seeds = match_bot_seeds(bot_seed_base, args.bot_seed_stride, idx, "normal")
    mirror_bot_seeds = match_bot_seeds(bot_seed_base, args.bot_seed_stride, idx, "mirror")
    normal = _scan_side(
        baseline,
        candidate,
        opponent,
        initdata,
        normal_bot_seeds,
        args.max_divergences_per_side,
        bool(args.include_full_requests),
    )
    mirror_side = _scan_side(
        baseline,
        candidate,
        opponent,
        mirror,
        mirror_bot_seeds,
        args.max_divergences_per_side,
        bool(args.include_full_requests),
    )
    baseline_net = float(normal["baseline"]["bot0_chips"]) + float(mirror_side["baseline"]["bot0_chips"])
    candidate_net = float(normal["candidate"]["bot0_chips"]) + float(mirror_side["candidate"]["bot0_chips"])
    return {
        "opponent_index": opponent_index,
        "opponent_label": _label(opponent),
        "opponent": _rel(opponent),
        "idx": idx,
        "seed": seed,
        "dealer": initdata["dealer"],
        "bot_seeds": {
            "normal": list(normal_bot_seeds) if normal_bot_seeds is not None else None,
            "mirror": list(mirror_bot_seeds) if mirror_bot_seeds is not None else None,
        },
        "baseline_net_chips": baseline_net,
        "candidate_net_chips": candidate_net,
        "delta_net_chips": candidate_net - baseline_net,
        "has_divergence": bool(
            normal["action_compare"]["has_divergence"]
            or mirror_side["action_compare"]["has_divergence"]
        ),
        "normal": normal,
        "mirror": mirror_side,
    }


def _task_indices(args: argparse.Namespace) -> list[int]:
    if args.pair_index:
        return sorted({int(idx) for idx in args.pair_index})
    return list(range(max(0, int(args.games))))


def _existing_rows(payload: dict[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
    rows: dict[tuple[str, int], dict[str, Any]] = {}
    for row in payload.get("pairs", []):
        try:
            key = (str(row["opponent_label"]), int(row["idx"]))
        except (KeyError, TypeError, ValueError):
            continue
        rows[key] = row
    return rows


def _summarize(payload: dict[str, Any]) -> None:
    rows = payload.get("pairs", [])
    values = [float(row.get("delta_net_chips", 0.0)) for row in rows]
    changed = [row for row in rows if row.get("has_divergence")]
    by_opponent: dict[str, list[float]] = {}
    for row in rows:
        by_opponent.setdefault(str(row.get("opponent_label")), []).append(float(row.get("delta_net_chips", 0.0)))
    payload["summary"] = {
        "pairs": len(rows),
        "divergence_pairs": len(changed),
        "divergence_rate": len(changed) / max(1, len(rows)),
        "positive": sum(1 for value in values if value > 0.0),
        "zero": sum(1 for value in values if value == 0.0),
        "negative": sum(1 for value in values if value < 0.0),
        "delta_stats": _stats(values, 140),
        "by_opponent": {
            opponent: {
                "pairs": len(opp_values),
                "positive": sum(1 for value in opp_values if value > 0.0),
                "zero": sum(1 for value in opp_values if value == 0.0),
                "negative": sum(1 for value in opp_values if value < 0.0),
                "delta_stats": _stats(opp_values, 140),
            }
            for opponent, opp_values in sorted(by_opponent.items())
        },
        "largest_abs_deltas": sorted(
            (
                {
                    "opponent": row.get("opponent_label"),
                    "idx": row.get("idx"),
                    "delta_net_chips": row.get("delta_net_chips"),
                    "has_divergence": row.get("has_divergence"),
                }
                for row in rows
            ),
            key=lambda item: abs(float(item["delta_net_chips"] or 0.0)),
            reverse=True,
        )[:10],
    }


def _divergence_count(payload: dict[str, Any]) -> int:
    return sum(1 for row in payload.get("pairs", []) if row.get("has_divergence"))


def _stop_reached(payload: dict[str, Any], target: int) -> bool:
    return int(target) > 0 and _divergence_count(payload) >= int(target)


def _write(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    out = path if path.is_absolute() else ROOT / path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--opponent", action="append", required=True)
    parser.add_argument("--games", type=int, default=4)
    parser.add_argument("--pair-index", action="append", type=int)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--seed-base", type=int)
    parser.add_argument("--seed-offset", type=int, default=0)
    parser.add_argument("--seed-stride", type=int, default=1)
    parser.add_argument("--opponent-seed-stride", type=int, default=100000)
    parser.add_argument("--bot-seed-base", type=int)
    parser.add_argument("--bot-seed-stride", type=int, default=10000)
    parser.add_argument("--opponent-bot-seed-stride", type=int, default=10000000)
    parser.add_argument("--max-hands", type=int, default=70)
    parser.add_argument("--max-divergences-per-side", type=int, default=3)
    parser.add_argument("--include-full-requests", action="store_true")
    parser.add_argument("--stop-after-divergence-pairs", type=int, default=0)
    parser.add_argument("--no-parallel-early-stop", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    baseline = _main_path(_resolve(args.baseline))
    candidate = _main_path(_resolve(args.candidate))
    opponents = [_main_path(_resolve(path)) for path in args.opponent]
    indices = _task_indices(args)
    payload: dict[str, Any] = {
        "mode": "active_divergence_scan_v1",
        "baseline": {"label": _label(baseline), "path": _rel(baseline)},
        "candidate": {"label": _label(candidate), "path": _rel(candidate)},
        "opponents": [{"label": _label(path), "path": _rel(path)} for path in opponents],
        "indices": indices,
        "workers": args.workers,
        "seed_base": args.seed_base,
        "seed_offset": args.seed_offset,
        "seed_stride": args.seed_stride,
        "opponent_seed_stride": args.opponent_seed_stride,
        "bot_seed_base": args.bot_seed_base,
        "bot_seed_stride": args.bot_seed_stride,
        "opponent_bot_seed_stride": args.opponent_bot_seed_stride,
        "max_hands": args.max_hands,
        "max_divergences_per_side": args.max_divergences_per_side,
        "include_full_requests": bool(args.include_full_requests),
        "stop_after_divergence_pairs": args.stop_after_divergence_pairs,
        "parallel_early_stop": not args.no_parallel_early_stop,
        "tasks_total": 0,
        "tasks_submitted": 0,
        "tasks_skipped": 0,
        "early_stopped": False,
        "pairs": [],
        "summary": {},
    }
    existing: dict[tuple[str, int], dict[str, Any]] = {}
    output_path = args.output if args.output.is_absolute() else ROOT / args.output
    if args.resume and output_path.exists():
        old = json.loads(output_path.read_text(encoding="utf-8"))
        existing = _existing_rows(old)
        payload["pairs"] = list(existing.values())
        _summarize(payload)
        _write(args.output, payload)

    tasks = []
    for opponent_index, opponent in enumerate(opponents):
        for idx in indices:
            if (_label(opponent), int(idx)) not in existing:
                tasks.append((opponent_index, opponent, int(idx)))
    payload["tasks_total"] = len(tasks) + len(existing)

    def _consume(row: dict[str, Any]) -> None:
        payload["pairs"].append(row)
        payload["pairs"].sort(key=lambda item: (int(item["opponent_index"]), int(item["idx"])))
        _summarize(payload)
        _write(args.output, payload)
        print(
            f"{row['opponent_label']} idx={row['idx']} "
            f"delta={row['delta_net_chips']} divergence={row['has_divergence']}",
            flush=True,
        )

    submitted = 0
    if args.workers <= 1:
        for task in tasks:
            if _stop_reached(payload, args.stop_after_divergence_pairs):
                payload["early_stopped"] = True
                break
            submitted += 1
            payload["tasks_submitted"] = submitted
            _consume(_scan_pair(*task, baseline=baseline, candidate=candidate, args=args))
    else:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            remaining = list(tasks)
            futures = {}

            def _submit_next() -> None:
                nonlocal submitted
                if not remaining:
                    return
                if (
                    not args.no_parallel_early_stop
                    and _stop_reached(payload, args.stop_after_divergence_pairs)
                ):
                    payload["early_stopped"] = True
                    return
                task = remaining.pop(0)
                submitted += 1
                payload["tasks_submitted"] = submitted
                futures[executor.submit(_scan_pair, *task, baseline, candidate, args)] = task

            while remaining and len(futures) < max(1, args.workers):
                _submit_next()
            while futures:
                done, _ = wait(set(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    futures.pop(future)
                    _consume(future.result())
                while remaining and len(futures) < max(1, args.workers):
                    before = len(futures)
                    _submit_next()
                    if len(futures) == before:
                        break
                if not futures and remaining and payload.get("early_stopped"):
                    break
    payload["tasks_submitted"] = submitted
    payload["tasks_skipped"] = max(0, len(tasks) - submitted)
    if _stop_reached(payload, args.stop_after_divergence_pairs):
        payload["early_stopped"] = bool(payload["tasks_skipped"])
    _summarize(payload)
    _write(args.output, payload)

    if not tasks:
        _write(args.output, payload)
        print("all requested rows already present", flush=True)


if __name__ == "__main__":
    main()
