#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = Path(__file__).resolve().parent
TEMPLATE_PREFILTER = TOOL_DIR / "template_action_prefilter.py"

if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from paired_evaluate import _label, _main_path, _rel, _resolve  # noqa: E402


def _safe(text: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip())
    return clean.strip("_") or "item"


def _seed_bases(args: argparse.Namespace) -> list[int]:
    seeds = [int(seed) for seed in args.seed_base or []]
    if args.seed_start is not None:
        for offset in range(max(0, int(args.windows))):
            seeds.append(int(args.seed_start) + offset * int(args.window_stride))
    return sorted(set(seeds))


def _output_path(args: argparse.Namespace, seed_base: int) -> Path:
    baseline = _safe(_label(_main_path(_resolve(args.baseline))))
    candidate = _safe(_label(_main_path(_resolve(args.candidate))))
    name = (
        f"template_{baseline}_vs_{candidate}_"
        f"{args.baseline_action}_to_{args.candidate_action}_{args.stage or 'any'}_"
        f"g{args.games}_seed{seed_base}.json"
    )
    out_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    return out_dir / name


def _bot_seed_base(args: argparse.Namespace, window_index: int) -> int | None:
    if args.bot_seed_base is None:
        return None
    return int(args.bot_seed_base) + int(window_index) * int(args.bot_seed_window_stride)


def _command(args: argparse.Namespace, seed_base: int, window_index: int, output: Path) -> list[str]:
    cmd = [
        sys.executable,
        str(TEMPLATE_PREFILTER),
        "--baseline",
        args.baseline,
        "--candidate",
        args.candidate,
        "--games",
        str(args.games),
        "--workers",
        str(args.workers),
        "--executor",
        args.executor,
        "--seed-base",
        str(seed_base),
        "--seed-offset",
        str(args.seed_offset),
        "--seed-stride",
        str(args.seed_stride),
        "--opponent-seed-stride",
        str(args.opponent_seed_stride),
        "--bot-seed-stride",
        str(args.bot_seed_stride),
        "--opponent-bot-seed-stride",
        str(args.opponent_bot_seed_stride),
        "--max-hands",
        str(args.max_hands),
        "--max-own-decisions-per-side",
        str(args.max_own_decisions_per_side),
        "--baseline-action",
        str(args.baseline_action),
        "--candidate-action",
        str(args.candidate_action),
        "--max-hits-per-side",
        str(args.max_hits_per_side),
        "--stop-after-hits",
        str(args.stop_after_hits),
        "--output",
        str(output),
    ]
    for opponent in args.opponent:
        cmd.extend(["--opponent", opponent])
    if args.stage:
        cmd.extend(["--stage", args.stage])
    bot_seed_base = _bot_seed_base(args, window_index)
    if bot_seed_base is not None:
        cmd.extend(["--bot-seed-base", str(bot_seed_base)])
    if args.stop_pair_after_first_side:
        cmd.append("--stop-pair-after-first-side")
    if args.resume:
        cmd.append("--resume")
    return cmd


def _read_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _window_summary(seed_base: int, output: Path, payload: dict[str, Any]) -> dict[str, Any]:
    summary = dict(payload.get("summary") or {})
    return {
        "seed_base": seed_base,
        "output": _rel(output),
        "pairs": int(summary.get("pairs", 0) or 0),
        "hit_pairs": int(summary.get("hit_pairs", 0) or 0),
        "hit_rate": float(summary.get("hit_rate", 0.0) or 0.0),
        "side_hits": summary.get("side_hits", {}),
        "by_opponent": summary.get("by_opponent", {}),
        "tasks_submitted": payload.get("tasks_submitted"),
        "tasks_skipped": payload.get("tasks_skipped"),
        "early_stopped": bool(payload.get("early_stopped", False)),
    }


def _aggregate(args: argparse.Namespace, rows: list[dict[str, Any]], commands: list[list[str]]) -> dict[str, Any]:
    total_pairs = sum(int(row.get("pairs", 0) or 0) for row in rows)
    total_hits = sum(int(row.get("hit_pairs", 0) or 0) for row in rows)
    side_hits = {"normal": 0, "mirror": 0}
    by_opponent: dict[str, dict[str, int]] = {}
    for row in rows:
        for side, value in dict(row.get("side_hits") or {}).items():
            side_hits[side] = side_hits.get(side, 0) + int(value or 0)
        for opponent, stats in dict(row.get("by_opponent") or {}).items():
            bucket = by_opponent.setdefault(opponent, {"pairs": 0, "hit_pairs": 0})
            bucket["pairs"] += int(stats.get("pairs", 0) or 0)
            bucket["hit_pairs"] += int(stats.get("hit_pairs", 0) or 0)
    return {
        "mode": "template_window_sweep_v1",
        "baseline": {
            "label": _label(_main_path(_resolve(args.baseline))),
            "path": _rel(_main_path(_resolve(args.baseline))),
        },
        "candidate": {
            "label": _label(_main_path(_resolve(args.candidate))),
            "path": _rel(_main_path(_resolve(args.candidate))),
        },
        "opponents": [
            {"label": _label(_main_path(_resolve(path))), "path": _rel(_main_path(_resolve(path)))}
            for path in args.opponent
        ],
        "parameters": {
            "games": args.games,
            "workers_per_window": args.workers,
            "parallel_windows": args.parallel_windows,
            "executor": args.executor,
            "seed_offset": args.seed_offset,
            "seed_stride": args.seed_stride,
            "opponent_seed_stride": args.opponent_seed_stride,
            "bot_seed_base": args.bot_seed_base,
            "bot_seed_window_stride": args.bot_seed_window_stride,
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
            "resume": args.resume,
            "dry_run": args.dry_run,
        },
        "windows": sorted(rows, key=lambda row: int(row["seed_base"])),
        "commands": [" ".join(cmd) for cmd in commands],
        "summary": {
            "windows": len(rows),
            "pairs": total_pairs,
            "hit_pairs": total_hits,
            "hit_rate": total_hits / max(1, total_pairs),
            "side_hits": side_hits,
            "by_opponent": {
                opponent: {
                    **stats,
                    "hit_rate": stats["hit_pairs"] / max(1, stats["pairs"]),
                }
                for opponent, stats in sorted(by_opponent.items())
            },
            "hit_windows": sorted(
                (
                    {
                        "seed_base": row["seed_base"],
                        "hit_pairs": row["hit_pairs"],
                        "hit_rate": row["hit_rate"],
                        "output": row["output"],
                    }
                    for row in rows
                    if int(row.get("hit_pairs", 0) or 0) > 0
                ),
                key=lambda row: (int(row["hit_pairs"]), float(row["hit_rate"])),
                reverse=True,
            ),
        },
    }


def _write_summary(path: Path, payload: dict[str, Any]) -> None:
    out = path if path.is_absolute() else ROOT / path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _run_window(task: tuple[int, int, Path, list[str]], dry_run: bool) -> tuple[int, Path, dict[str, Any] | None]:
    seed_base, _window_index, output, cmd = task
    print(" ".join(cmd), flush=True)
    if dry_run:
        return seed_base, output, None
    subprocess.run(cmd, cwd=ROOT, check=True)
    return seed_base, output, _read_payload(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--opponent", action="append", required=True)
    parser.add_argument("--seed-base", action="append", type=int)
    parser.add_argument("--seed-start", type=int)
    parser.add_argument("--windows", type=int, default=0)
    parser.add_argument("--window-stride", type=int, default=1000)
    parser.add_argument("--games", type=int, default=48)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--parallel-windows", type=int, default=1)
    parser.add_argument("--executor", choices=["process", "thread"], default="process")
    parser.add_argument("--seed-offset", type=int, default=0)
    parser.add_argument("--seed-stride", type=int, default=1)
    parser.add_argument("--opponent-seed-stride", type=int, default=100000)
    parser.add_argument("--bot-seed-base", type=int)
    parser.add_argument("--bot-seed-window-stride", type=int, default=1000000)
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
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--summary-output", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    seeds = _seed_bases(args)
    if not seeds:
        raise SystemExit("provide --seed-base or --seed-start with --windows")

    tasks: list[tuple[int, int, Path, list[str]]] = []
    commands: list[list[str]] = []
    for window_index, seed_base in enumerate(seeds):
        output = _output_path(args, seed_base)
        cmd = _command(args, seed_base, window_index, output)
        tasks.append((seed_base, window_index, output, cmd))
        commands.append(cmd)

    rows: list[dict[str, Any]] = []
    if args.parallel_windows <= 1:
        for seed_base, _window_index, output, cmd in tasks:
            _, out_path, payload = _run_window((seed_base, _window_index, output, cmd), args.dry_run)
            if payload is not None:
                rows.append(_window_summary(seed_base, out_path, payload))
                _write_summary(args.summary_output, _aggregate(args, rows, commands))
    else:
        with ThreadPoolExecutor(max_workers=max(1, int(args.parallel_windows))) as executor:
            futures = [executor.submit(_run_window, task, args.dry_run) for task in tasks]
            for future in as_completed(futures):
                seed_base, out_path, payload = future.result()
                if payload is not None:
                    rows.append(_window_summary(seed_base, out_path, payload))
                    _write_summary(args.summary_output, _aggregate(args, rows, commands))

    summary = _aggregate(args, rows, commands)
    _write_summary(args.summary_output, summary)
    hit_windows = summary["summary"]["hit_windows"]
    print(
        f"windows={summary['summary']['windows']} pairs={summary['summary']['pairs']} "
        f"hits={summary['summary']['hit_pairs']} hit_windows={len(hit_windows)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
