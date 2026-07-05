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
from paired_evaluate import _fresh_initdata, _label, _main_path, _mirror_initdata, _rel, _resolve, _seeded_initdata  # noqa: E402
from active_divergence_scan import _opponent_seed_base, _pair_seed, _request_summary  # noqa: E402
from seeded_process import SeededPersistentBot, match_bot_seeds  # noqa: E402


def _persistent(bot_path: Path, seed: int | None):
    if seed is None:
        return _PersistentBot(str(bot_path.resolve()))
    return SeededPersistentBot(str(bot_path.resolve()), seed)


def _content_player(result: dict[str, Any]) -> tuple[int | None, dict[str, Any] | None]:
    content = result.get("content", {})
    if not content:
        return None, None
    player_id = int(next(iter(content.keys())))
    return player_id, content[str(player_id)]


def _init_state(initdata: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    result = json.loads(judge_func(json.dumps({"log": [], "initdata": copy.deepcopy(initdata)})))
    game_initdata = copy.deepcopy(result["initdata"])
    return result, [{"output": result}], game_initdata


def _step_game(
    bot_paths: list[str],
    player_id: int,
    request_data: dict[str, Any],
    result: dict[str, Any],
    log: list[dict[str, Any]],
    game_initdata: dict[str, Any],
    bot_requests: list[list[dict[str, Any]]],
    bot_responses: list[list[int]],
    bot_data: list[Any],
    persistent,
) -> tuple[int, str, dict[str, Any]]:
    action, verdict, _ = _call_bot(
        bot_paths,
        player_id,
        request_data,
        bot_requests,
        bot_responses,
        bot_data=bot_data,
        persistent_procs=persistent,
    )
    log.append({str(player_id): {"response": str(action), "verdict": verdict}, "output": None})
    next_result = json.loads(judge_func(json.dumps({"log": log, "initdata": game_initdata})))
    log.append({"output": next_result})
    return int(action), verdict, next_result


def _scan_side_prefilter(
    baseline: Path,
    candidate: Path,
    opponent: Path,
    initdata: dict[str, Any],
    bot_seeds: tuple[int, int] | None,
    max_decisions: int,
) -> dict[str, Any]:
    seed0 = bot_seeds[0] if bot_seeds is not None else None
    seed1 = bot_seeds[1] if bot_seeds is not None else None
    base_paths = [str(baseline.resolve()), str(opponent.resolve())]
    cand_paths = [str(candidate.resolve()), str(opponent.resolve())]
    base_persistent = [_persistent(baseline, seed0), _persistent(opponent, seed1)]
    cand_persistent = [_persistent(candidate, seed0), _persistent(opponent, seed1)]
    try:
        base_result, base_log, base_init = _init_state(initdata)
        cand_result, cand_log, cand_init = _init_state(initdata)
        base_requests: list[list[dict[str, Any]]] = [[], []]
        cand_requests: list[list[dict[str, Any]]] = [[], []]
        base_responses: list[list[int]] = [[], []]
        cand_responses: list[list[int]] = [[], []]
        base_data: list[Any] = [None, None]
        cand_data: list[Any] = [None, None]
        compared_own_decisions = 0
        total_steps = 0
        while base_result.get("command") == "request" and cand_result.get("command") == "request":
            base_player, base_request = _content_player(base_result)
            cand_player, cand_request = _content_player(cand_result)
            if base_player is None or cand_player is None:
                break
            if base_player != cand_player:
                return {
                    "status": "path_mismatch",
                    "has_divergence": True,
                    "steps": total_steps,
                    "compared_own_decisions": compared_own_decisions,
                    "first_divergence": {
                        "kind": "player_to_act_mismatch",
                        "baseline_player": base_player,
                        "candidate_player": cand_player,
                        "baseline_request": _request_summary(base_request or {}),
                        "candidate_request": _request_summary(cand_request or {}),
                    },
                }
            total_steps += 1
            if base_player == 0:
                compared_own_decisions += 1
                base_action, base_verdict, _ = _call_bot(
                    base_paths,
                    0,
                    base_request,
                    base_requests,
                    base_responses,
                    bot_data=base_data,
                    persistent_procs=base_persistent,
                )
                cand_action, cand_verdict, _ = _call_bot(
                    cand_paths,
                    0,
                    cand_request,
                    cand_requests,
                    cand_responses,
                    bot_data=cand_data,
                    persistent_procs=cand_persistent,
                )
                if int(base_action) != int(cand_action):
                    return {
                        "status": "diverged",
                        "has_divergence": True,
                        "steps": total_steps,
                        "compared_own_decisions": compared_own_decisions,
                        "first_divergence": {
                            "kind": "own_action",
                            "decision_index": compared_own_decisions - 1,
                            "baseline_action": int(base_action),
                            "candidate_action": int(cand_action),
                            "baseline_verdict": base_verdict,
                            "candidate_verdict": cand_verdict,
                            "same_request": base_request == cand_request,
                            "baseline_request": _request_summary(base_request or {}),
                            "candidate_request": _request_summary(cand_request or {}),
                        },
                    }
                base_log.append({"0": {"response": str(base_action), "verdict": base_verdict}, "output": None})
                cand_log.append({"0": {"response": str(cand_action), "verdict": cand_verdict}, "output": None})
                base_result = json.loads(judge_func(json.dumps({"log": base_log, "initdata": base_init})))
                cand_result = json.loads(judge_func(json.dumps({"log": cand_log, "initdata": cand_init})))
                base_log.append({"output": base_result})
                cand_log.append({"output": cand_result})
            else:
                base_action, base_verdict, base_result = _step_game(
                    base_paths,
                    1,
                    base_request,
                    base_result,
                    base_log,
                    base_init,
                    base_requests,
                    base_responses,
                    base_data,
                    base_persistent,
                )
                cand_action, cand_verdict, cand_result = _step_game(
                    cand_paths,
                    1,
                    cand_request,
                    cand_result,
                    cand_log,
                    cand_init,
                    cand_requests,
                    cand_responses,
                    cand_data,
                    cand_persistent,
                )
                if int(base_action) != int(cand_action):
                    return {
                        "status": "opponent_mismatch",
                        "has_divergence": False,
                        "steps": total_steps,
                        "compared_own_decisions": compared_own_decisions,
                        "first_divergence": {
                            "kind": "opponent_action_mismatch",
                            "baseline_action": int(base_action),
                            "candidate_action": int(cand_action),
                            "baseline_verdict": base_verdict,
                            "candidate_verdict": cand_verdict,
                            "same_request": base_request == cand_request,
                            "baseline_request": _request_summary(base_request or {}),
                            "candidate_request": _request_summary(cand_request or {}),
                        },
                    }
            if max_decisions > 0 and compared_own_decisions >= max_decisions:
                return {
                    "status": "max_decisions",
                    "has_divergence": False,
                    "steps": total_steps,
                    "compared_own_decisions": compared_own_decisions,
                    "first_divergence": None,
                }
        return {
            "status": "finished",
            "has_divergence": False,
            "steps": total_steps,
            "compared_own_decisions": compared_own_decisions,
            "first_divergence": None,
        }
    finally:
        for proc in [*base_persistent, *cand_persistent]:
            proc.close()


def _scan_pair_prefilter(
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
    normal = _scan_side_prefilter(
        baseline,
        candidate,
        opponent,
        initdata,
        normal_bot_seeds,
        args.max_own_decisions_per_side,
    )
    if normal.get("has_divergence") and args.stop_pair_after_first_side:
        mirror_side = {
            "status": "skipped_after_normal_hit",
            "has_divergence": False,
            "steps": 0,
            "compared_own_decisions": 0,
            "first_divergence": None,
        }
    else:
        mirror_side = _scan_side_prefilter(
            baseline,
            candidate,
            opponent,
            mirror,
            mirror_bot_seeds,
            args.max_own_decisions_per_side,
        )
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
        "has_divergence": bool(normal.get("has_divergence") or mirror_side.get("has_divergence")),
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
    return sum(1 for row in payload.get("pairs", []) if row.get("has_divergence"))


def _stop_reached(payload: dict[str, Any], target: int) -> bool:
    return int(target) > 0 and _hit_count(payload) >= int(target)


def _summarize(payload: dict[str, Any]) -> None:
    pairs = list(payload.get("pairs", []))
    by_opponent: dict[str, list[dict[str, Any]]] = {}
    side_hits = {"normal": 0, "mirror": 0}
    statuses = {}
    for row in pairs:
        by_opponent.setdefault(str(row.get("opponent_label")), []).append(row)
        for side in ("normal", "mirror"):
            side_row = row.get(side, {})
            statuses[side_row.get("status", "unknown")] = statuses.get(side_row.get("status", "unknown"), 0) + 1
            if side_row.get("has_divergence"):
                side_hits[side] += 1
    payload["summary"] = {
        "pairs": len(pairs),
        "divergence_pairs": _hit_count(payload),
        "divergence_rate": _hit_count(payload) / max(1, len(pairs)),
        "side_hits": side_hits,
        "side_statuses": statuses,
        "by_opponent": {
            opponent: {
                "pairs": len(rows),
                "divergence_pairs": sum(1 for row in rows if row.get("has_divergence")),
            }
            for opponent, rows in sorted(by_opponent.items())
        },
    }


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
    parser.add_argument("--max-own-decisions-per-side", type=int, default=0)
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
        "mode": "action_divergence_prefilter_v1",
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
        "max_own_decisions_per_side": args.max_own_decisions_per_side,
        "stop_after_hits": args.stop_after_hits,
        "stop_pair_after_first_side": args.stop_pair_after_first_side,
        "tasks_total": 0,
        "tasks_submitted": 0,
        "tasks_skipped": 0,
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
            f"{row['opponent_label']} idx={row['idx']} divergence={row['has_divergence']} "
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
            _consume(_scan_pair_prefilter(*task, baseline=baseline, candidate=candidate, args=args))
    else:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
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
                futures[executor.submit(_scan_pair_prefilter, *task, baseline, candidate, args)] = task

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
