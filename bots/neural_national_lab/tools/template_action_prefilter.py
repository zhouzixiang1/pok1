#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
import copy
import json
import os
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
from paired_evaluate import _fresh_initdata, _label, _main_path, _mirror_initdata, _rel, _resolve, _seeded_initdata  # noqa: E402
from active_divergence_scan import _opponent_seed_base, _pair_seed, _request_summary  # noqa: E402
from seeded_process import SeededPersistentBot, match_bot_seeds  # noqa: E402


def _persistent(bot_path: Path, seed: int | None):
    if seed is None:
        return _PersistentBot(str(bot_path.resolve()))
    return SeededPersistentBot(str(bot_path.resolve()), seed)


def _stage(request: dict[str, Any]) -> str:
    return _request_summary(request).get("stage", "preflop")


def _content_player(result: dict[str, Any]) -> tuple[int | None, dict[str, Any] | None]:
    content = result.get("content", {})
    if not content:
        return None, None
    player_id = int(next(iter(content.keys())))
    return player_id, content[str(player_id)]


def _call_candidate_same_state(
    candidate_path: str,
    request_data: dict[str, Any],
    candidate_requests: list[list[dict[str, Any]]],
    candidate_responses: list[list[int]],
    candidate_data: list[Any],
    candidate_proc,
    forced_response: int,
) -> tuple[int, str]:
    action, verdict, _ = _call_bot(
        [candidate_path, ""],
        0,
        request_data,
        candidate_requests,
        candidate_responses,
        bot_data=candidate_data,
        persistent_procs=[candidate_proc, None],
    )
    candidate_responses[0][-1] = int(forced_response)
    return int(action), verdict


def _scan_side_template(
    baseline: Path,
    candidate: Path,
    opponent: Path,
    initdata: dict[str, Any],
    bot_seeds: tuple[int, int] | None,
    args: argparse.Namespace,
) -> dict[str, Any]:
    seed0 = bot_seeds[0] if bot_seeds is not None else None
    seed1 = bot_seeds[1] if bot_seeds is not None else None
    baseline_path = str(baseline.resolve())
    candidate_path = str(candidate.resolve())
    opponent_path = str(opponent.resolve())
    baseline_proc = _persistent(baseline, seed0)
    candidate_proc = _persistent(candidate, seed0)
    opponent_proc = _persistent(opponent, seed1)
    try:
        result = json.loads(judge_func(json.dumps({"log": [], "initdata": copy.deepcopy(initdata)})))
        game_initdata = copy.deepcopy(result["initdata"])
        log: list[dict[str, Any]] = [{"output": result}]
        actual_requests: list[list[dict[str, Any]]] = [[], []]
        actual_responses: list[list[int]] = [[], []]
        actual_data: list[Any] = [None, None]
        candidate_requests: list[list[dict[str, Any]]] = [[], []]
        candidate_responses: list[list[int]] = [[], []]
        candidate_data: list[Any] = [None, None]
        own_decisions = 0
        steps = 0
        hits: list[dict[str, Any]] = []
        while result.get("command") == "request":
            player_id, request_data = _content_player(result)
            if player_id is None or request_data is None:
                break
            steps += 1
            if player_id == 0:
                own_decisions += 1
                baseline_action, baseline_verdict, _ = _call_bot(
                    [baseline_path, opponent_path],
                    0,
                    request_data,
                    actual_requests,
                    actual_responses,
                    bot_data=actual_data,
                    persistent_procs=[baseline_proc, opponent_proc],
                )
                candidate_action, candidate_verdict = _call_candidate_same_state(
                    candidate_path,
                    request_data,
                    candidate_requests,
                    candidate_responses,
                    candidate_data,
                    candidate_proc,
                    int(baseline_action),
                )
                request_stage = _stage(request_data)
                is_hit = (
                    int(baseline_action) == int(args.baseline_action)
                    and int(candidate_action) == int(args.candidate_action)
                    and (not args.stage or request_stage == args.stage)
                )
                if is_hit:
                    hits.append({
                        "decision_index": own_decisions - 1,
                        "baseline_action": int(baseline_action),
                        "candidate_action": int(candidate_action),
                        "baseline_verdict": baseline_verdict,
                        "candidate_verdict": candidate_verdict,
                        "stage": request_stage,
                        "request": _request_summary(request_data),
                    })
                    if len(hits) >= max(1, int(args.max_hits_per_side)):
                        return {
                            "status": "hit",
                            "has_hit": True,
                            "steps": steps,
                            "compared_own_decisions": own_decisions,
                            "hits": hits,
                        }
                log.append({"0": {"response": str(baseline_action), "verdict": baseline_verdict}, "output": None})
            else:
                action, verdict, _ = _call_bot(
                    [baseline_path, opponent_path],
                    1,
                    request_data,
                    actual_requests,
                    actual_responses,
                    bot_data=actual_data,
                    persistent_procs=[baseline_proc, opponent_proc],
                )
                log.append({"1": {"response": str(action), "verdict": verdict}, "output": None})
            result = json.loads(judge_func(json.dumps({"log": log, "initdata": game_initdata})))
            log.append({"output": result})
            if args.max_own_decisions_per_side > 0 and own_decisions >= args.max_own_decisions_per_side:
                return {
                    "status": "max_decisions",
                    "has_hit": bool(hits),
                    "steps": steps,
                    "compared_own_decisions": own_decisions,
                    "hits": hits,
                }
        return {
            "status": "finished",
            "has_hit": bool(hits),
            "steps": steps,
            "compared_own_decisions": own_decisions,
            "hits": hits,
        }
    finally:
        for proc in (baseline_proc, candidate_proc, opponent_proc):
            proc.close()


def _scan_pair_template(
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
    normal = _scan_side_template(baseline, candidate, opponent, initdata, normal_bot_seeds, args)
    if normal.get("has_hit") and args.stop_pair_after_first_side:
        mirror_side = {
            "status": "skipped_after_normal_hit",
            "has_hit": False,
            "steps": 0,
            "compared_own_decisions": 0,
            "hits": [],
        }
    else:
        mirror_side = _scan_side_template(baseline, candidate, opponent, mirror, mirror_bot_seeds, args)
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
        "has_hit": bool(normal.get("has_hit") or mirror_side.get("has_hit")),
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


def _hit_count(payload: dict[str, Any]) -> int:
    return sum(1 for row in payload.get("pairs", []) if row.get("has_hit"))


def _stop_reached(payload: dict[str, Any], target: int) -> bool:
    return int(target) > 0 and _hit_count(payload) >= int(target)


def _summarize(payload: dict[str, Any]) -> None:
    pairs = list(payload.get("pairs", []))
    by_opponent: dict[str, list[dict[str, Any]]] = {}
    side_hits = {"normal": 0, "mirror": 0}
    side_statuses: dict[str, int] = {}
    for row in pairs:
        by_opponent.setdefault(str(row.get("opponent_label")), []).append(row)
        for side in ("normal", "mirror"):
            side_row = row.get(side, {})
            status = str(side_row.get("status", "unknown"))
            side_statuses[status] = side_statuses.get(status, 0) + 1
            if side_row.get("has_hit"):
                side_hits[side] += 1
    payload["summary"] = {
        "pairs": len(pairs),
        "hit_pairs": _hit_count(payload),
        "hit_rate": _hit_count(payload) / max(1, len(pairs)),
        "side_hits": side_hits,
        "side_statuses": side_statuses,
        "by_opponent": {
            opponent: {
                "pairs": len(rows),
                "hit_pairs": sum(1 for row in rows if row.get("has_hit")),
            }
            for opponent, rows in sorted(by_opponent.items())
        },
    }
    payload["tasks_completed"] = len(pairs)
    if "tasks_total" in payload:
        payload["tasks_remaining"] = max(0, int(payload.get("tasks_total") or 0) - len(pairs))


def _write(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    out = path if path.is_absolute() else ROOT / path
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--opponent", action="append", required=True)
    parser.add_argument("--games", type=int, default=4)
    parser.add_argument("--pair-index", action="append", type=int)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--executor", choices=["process", "thread"], default="process")
    parser.add_argument("--seed-base", type=int)
    parser.add_argument("--seed-offset", type=int, default=0)
    parser.add_argument("--seed-stride", type=int, default=1)
    parser.add_argument("--opponent-seed-stride", type=int, default=100000)
    parser.add_argument("--bot-seed-base", type=int)
    parser.add_argument("--bot-seed-stride", type=int, default=10000)
    parser.add_argument("--opponent-bot-seed-stride", type=int, default=10000000)
    parser.add_argument("--max-hands", type=int, default=70)
    parser.add_argument("--max-own-decisions-per-side", type=int, default=0)
    parser.add_argument("--baseline-action", type=int, default=101)
    parser.add_argument("--candidate-action", type=int, default=0)
    parser.add_argument("--stage", default="flop")
    parser.add_argument("--max-hits-per-side", type=int, default=1)
    parser.add_argument("--stop-after-hits", type=int, default=0)
    parser.add_argument("--stop-pair-after-first-side", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    baseline = _main_path(_resolve(args.baseline))
    candidate = _main_path(_resolve(args.candidate))
    opponents = [_main_path(_resolve(path)) for path in args.opponent]
    indices = _task_indices(args)
    payload: dict[str, Any] = {
        "mode": "template_action_prefilter_v1",
        "baseline": {"label": _label(baseline), "path": _rel(baseline)},
        "candidate": {"label": _label(candidate), "path": _rel(candidate)},
        "opponents": [{"label": _label(path), "path": _rel(path)} for path in opponents],
        "indices": indices,
        "workers": args.workers,
        "executor": args.executor,
        "seed_base": args.seed_base,
        "seed_offset": args.seed_offset,
        "seed_stride": args.seed_stride,
        "opponent_seed_stride": args.opponent_seed_stride,
        "bot_seed_base": args.bot_seed_base,
        "bot_seed_stride": args.bot_seed_stride,
        "opponent_bot_seed_stride": args.opponent_bot_seed_stride,
        "max_hands": args.max_hands,
        "max_own_decisions_per_side": args.max_own_decisions_per_side,
        "baseline_action": args.baseline_action,
        "candidate_action": args.candidate_action,
        "stage": args.stage,
        "max_hits_per_side": args.max_hits_per_side,
        "stop_after_hits": args.stop_after_hits,
        "stop_pair_after_first_side": args.stop_pair_after_first_side,
        "tasks_total": 0,
        "tasks_existing": 0,
        "tasks_submitted": 0,
        "tasks_skipped": 0,
        "tasks_completed": 0,
        "tasks_remaining": 0,
        "early_stopped": False,
        "pairs": [],
        "summary": {},
    }
    output_path = args.output if args.output.is_absolute() else ROOT / args.output
    existing: dict[tuple[str, int], dict[str, Any]] = {}
    if args.resume and output_path.exists():
        old = json.loads(output_path.read_text(encoding="utf-8"))
        existing = _existing_rows(old)
        payload["pairs"] = list(existing.values())
        payload["tasks_existing"] = len(existing)

    tasks = []
    for opponent_index, opponent in enumerate(opponents):
        for idx in indices:
            if (_label(opponent), int(idx)) not in existing:
                tasks.append((opponent_index, opponent, int(idx)))
    payload["tasks_total"] = len(tasks) + len(existing)
    _summarize(payload)
    _write(args.output, payload)

    def _consume(row: dict[str, Any]) -> None:
        payload["pairs"].append(row)
        payload["pairs"].sort(key=lambda item: (int(item["opponent_index"]), int(item["idx"])))
        _summarize(payload)
        _write(args.output, payload)
        print(
            f"{row['opponent_label']} idx={row['idx']} hit={row['has_hit']} "
            f"normal={row['normal']['status']} mirror={row['mirror']['status']}",
            flush=True,
        )

    submitted = 0
    if args.workers <= 1:
        for task in tasks:
            if _stop_reached(payload, args.stop_after_hits):
                payload["early_stopped"] = True
                break
            submitted += 1
            payload["tasks_submitted"] = submitted
            _consume(_scan_pair_template(*task, baseline=baseline, candidate=candidate, args=args))
    else:
        executor_cls = ProcessPoolExecutor if args.executor == "process" else ThreadPoolExecutor
        with executor_cls(max_workers=max(1, args.workers)) as executor:
            remaining = list(tasks)
            futures = {}

            def _submit_next() -> None:
                nonlocal submitted
                if not remaining:
                    return
                if _stop_reached(payload, args.stop_after_hits):
                    payload["early_stopped"] = True
                    return
                task = remaining.pop(0)
                submitted += 1
                payload["tasks_submitted"] = submitted
                futures[executor.submit(_scan_pair_template, *task, baseline, candidate, args)] = task

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
                if payload.get("early_stopped") and not futures:
                    break

    payload["tasks_submitted"] = submitted
    payload["tasks_skipped"] = max(0, len(tasks) - submitted)
    if _stop_reached(payload, args.stop_after_hits):
        payload["early_stopped"] = bool(payload["tasks_skipped"])
    _summarize(payload)
    _write(args.output, payload)
    if not tasks:
        print("all requested rows already present", flush=True)


if __name__ == "__main__":
    main()
