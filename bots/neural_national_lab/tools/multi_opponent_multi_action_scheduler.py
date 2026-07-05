#!/usr/bin/env python3
"""Schedule multi-opponent multi-action counterfactual data generation.

This is the actor-side bridge toward a Deep-CFR-style local loop: many isolated
process shards generate legal-mask action-value/regret rows, then an optional
builder step converts the opponent outputs into JSONL training rows.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
SHARD_RUNNER = Path(__file__).resolve().with_name("multi_action_shard_runner.py")
BUILD_DATA = Path(__file__).resolve().with_name("build_multi_action_value_data.py")
LABELS = ("fold", "call", "raise_half", "raise_pot", "raise_2pot", "allin")
TARGET_FIELDS = ("delta_vs_rule", "regret_vs_mean", "action_values")


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return (ROOT / p).resolve() if not p.is_absolute() else p.resolve()


def _safe_label(path: Path) -> str:
    text = path.name or path.stem or str(path)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "opponent"


def _display_cmd(cmd: list[str]) -> str:
    display: list[str] = []
    for idx, part in enumerate(cmd):
        if idx == 0:
            display.append("python")
            continue
        p = Path(part)
        display.append(_rel(p) if p.is_absolute() else part)
    return " ".join(display)


def _tail(text: str, limit: int = 4000) -> str:
    return text[-limit:] if len(text) > limit else text


def _append_repeated(cmd: list[str], flag: str, values: list[str]) -> None:
    for value in values:
        cmd.extend([flag, str(value)])


def _opponent_seed_offset(args: argparse.Namespace, opponent_idx: int) -> int:
    return int(args.seed_offset) + opponent_idx * int(args.opponent_seed_stride)


def _opponent_bot_seed_base(args: argparse.Namespace, opponent_idx: int) -> int | None:
    if args.bot_seed_base is None:
        return None
    return int(args.bot_seed_base) + opponent_idx * int(args.opponent_bot_seed_stride)


def _runner_cmd(args: argparse.Namespace, opponent: Path, opponent_idx: int, output: Path) -> list[str]:
    cmd = [
        sys.executable,
        str(SHARD_RUNNER),
        "--version",
        str(_resolve(args.version)),
        "--opponent",
        str(opponent),
        "--shards",
        str(args.shards_per_opponent),
        "--workers",
        str(args.shard_workers),
        "--games-per-shard",
        str(args.games_per_shard),
        "--max-rows-per-shard",
        str(args.max_rows_per_shard),
        "--max-scan-decisions",
        str(args.max_scan_decisions),
        "--stage",
        args.stage,
        "--branch-scope",
        args.branch_scope,
        "--max-branch-steps",
        str(args.max_branch_steps),
        "--min-unique-actions",
        str(args.min_unique_actions),
        "--active-target",
        args.active_target,
        "--active-min-targets",
        str(args.active_min_targets),
        "--active-min-positive-targets",
        str(args.active_min_positive_targets),
        "--active-min-abs-target",
        str(args.active_min_abs_target),
        "--seed-base",
        str(args.seed_base),
        "--seed-offset",
        str(_opponent_seed_offset(args, opponent_idx)),
        "--seed-stride",
        str(args.seed_stride),
        "--bot-seed-stride",
        str(args.bot_seed_stride),
        "--max-hands",
        str(args.max_hands),
        "--shard-timeout-sec",
        str(args.shard_timeout_sec),
        "--output",
        str(output),
    ]
    bot_seed_base = _opponent_bot_seed_base(args, opponent_idx)
    if bot_seed_base is not None:
        cmd.extend(["--bot-seed-base", str(bot_seed_base)])
    _append_repeated(cmd, "--prefilter-rule-label", args.prefilter_rule_label)
    _append_repeated(cmd, "--prefilter-top-label", args.prefilter_top_label)
    _append_repeated(cmd, "--active-drop-label", args.active_drop_label)
    if args.prefilter_min_top_conf:
        cmd.extend(["--prefilter-min-top-conf", str(args.prefilter_min_top_conf)])
    if args.prefilter_max_top_conf is not None:
        cmd.extend(["--prefilter-max-top-conf", str(args.prefilter_max_top_conf)])
    if args.prefilter_free_action:
        cmd.append("--prefilter-free-action")
    if args.prefilter_max_to_call is not None:
        cmd.extend(["--prefilter-max-to-call", str(args.prefilter_max_to_call)])
    if args.prefilter_min_interaction_score is not None:
        cmd.extend(["--prefilter-min-interaction-score", str(args.prefilter_min_interaction_score)])
    if args.prefilter_max_interaction_score is not None:
        cmd.extend(["--prefilter-max-interaction-score", str(args.prefilter_max_interaction_score)])
    if args.rerun_existing:
        cmd.append("--rerun-existing")
    if args.no_scan_persistent:
        cmd.append("--no-scan-persistent")
    if args.no_mirror:
        cmd.append("--no-mirror")
    return cmd


def _run_runner(cmd: list[str], opponent_idx: int, opponent: str, output: str, timeout: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=timeout or None,
        )
        returncode = proc.returncode
        stdout_tail = _tail(proc.stdout)
        stderr_tail = _tail(proc.stderr)
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout_tail = _tail(str(exc.stdout or ""))
        stderr_tail = _tail(str(exc.stderr or ""))

    out_path = Path(output)
    row_count = 0
    summary: dict[str, Any] = {}
    if out_path.exists():
        try:
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            row_count = len(payload.get("rows", []))
            summary = payload.get("summary", {})
        except Exception as exc:  # pragma: no cover - diagnostic path
            summary = {"read_error": str(exc)}
    return {
        "opponent_index": opponent_idx,
        "opponent": _rel(Path(opponent)),
        "output": _rel(out_path),
        "row_count": row_count,
        "summary": summary,
        "returncode": returncode,
        "command": _display_cmd(cmd),
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }


def _summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [row for row in results if int(row.get("returncode", 1)) == 0]
    failed = [row for row in results if int(row.get("returncode", 1)) != 0]
    total_rows = sum(int(row.get("row_count", 0) or 0) for row in results)
    best_counts: dict[str, int] = {}
    active_best_counts: dict[str, int] = {}
    for row in results:
        summary = row.get("summary") or {}
        for key, target in (("best_label_counts", best_counts), ("active_best_label_counts", active_best_counts)):
            counts = summary.get(key) or {}
            for label, count in counts.items():
                target[str(label)] = target.get(str(label), 0) + int(count)
    return {
        "opponents": len(results),
        "ok_opponents": len(ok),
        "failed_opponents": len(failed),
        "rows": total_rows,
        "best_label_counts": dict(sorted(best_counts.items())),
        "active_best_label_counts": dict(sorted(active_best_counts.items())),
        "failed": [
            {"opponent": row.get("opponent"), "returncode": row.get("returncode"), "stderr_tail": row.get("stderr_tail")}
            for row in failed
        ],
    }


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _build_training(args: argparse.Namespace, input_files: list[Path]) -> dict[str, Any] | None:
    if args.training_output is None:
        return None
    cmd = [
        sys.executable,
        str(BUILD_DATA),
        "--output",
        str(_resolve(args.training_output)),
        "--feature-set",
        args.training_feature_set,
        "--target",
        args.training_target,
    ]
    if args.training_summary is not None:
        cmd.extend(["--summary", str(_resolve(args.training_summary))])
    for path in input_files:
        cmd.extend(["--input", str(path)])
    for label in args.training_drop_label:
        cmd.extend(["--drop-label", label])
    if args.training_allow_incomplete_vector:
        cmd.append("--allow-incomplete-vector")
    if args.training_drop_zero_targets:
        cmd.append("--drop-zero-targets")
    if args.training_clip_target is not None:
        cmd.extend(["--clip-target", str(args.training_clip_target)])
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)
    summary: dict[str, Any] | None = None
    if args.training_summary is not None and _resolve(args.training_summary).exists():
        summary = json.loads(_resolve(args.training_summary).read_text(encoding="utf-8"))
    return {
        "returncode": proc.returncode,
        "command": _display_cmd(cmd),
        "output": _rel(_resolve(args.training_output)),
        "summary": summary,
        "stdout_tail": _tail(proc.stdout),
        "stderr_tail": _tail(proc.stderr),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--opponent", action="append", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--workers", type=int, default=1, help="Parallel opponent runners.")
    parser.add_argument("--shard-workers", type=int, default=1, help="Parallel shards inside each opponent runner.")
    parser.add_argument("--shards-per-opponent", type=int, default=4)
    parser.add_argument("--games-per-shard", type=int, default=2)
    parser.add_argument("--max-rows-per-shard", type=int, default=8)
    parser.add_argument("--max-scan-decisions", type=int, default=400)
    parser.add_argument("--stage", choices=["any", "preflop", "flop", "turn", "river"], default="any")
    parser.add_argument("--branch-scope", choices=["hand", "match"], default="hand")
    parser.add_argument("--max-branch-steps", type=int, default=5000)
    parser.add_argument("--min-unique-actions", type=int, default=2)
    parser.add_argument("--prefilter-rule-label", action="append", choices=LABELS, default=[])
    parser.add_argument("--prefilter-top-label", action="append", choices=LABELS, default=[])
    parser.add_argument("--prefilter-min-top-conf", type=float, default=0.0)
    parser.add_argument("--prefilter-max-top-conf", type=float)
    parser.add_argument("--prefilter-free-action", action="store_true")
    parser.add_argument("--prefilter-max-to-call", type=float)
    parser.add_argument("--prefilter-min-interaction-score", type=float)
    parser.add_argument("--prefilter-max-interaction-score", type=float)
    parser.add_argument("--active-target", choices=TARGET_FIELDS, default="delta_vs_rule")
    parser.add_argument("--active-drop-label", action="append", choices=LABELS, default=[])
    parser.add_argument("--active-min-targets", type=int, default=0)
    parser.add_argument("--active-min-positive-targets", type=int, default=0)
    parser.add_argument("--active-min-abs-target", type=float, default=1e-9)
    parser.add_argument("--seed-base", type=int, default=20261001)
    parser.add_argument("--seed-offset", type=int, default=0)
    parser.add_argument("--seed-stride", type=int, default=1)
    parser.add_argument("--opponent-seed-stride", type=int, default=100000)
    parser.add_argument("--bot-seed-base", type=int)
    parser.add_argument("--bot-seed-stride", type=int, default=10000)
    parser.add_argument("--opponent-bot-seed-stride", type=int, default=100000000)
    parser.add_argument("--max-hands", type=int, default=70)
    parser.add_argument("--runner-timeout-sec", type=int, default=0)
    parser.add_argument("--shard-timeout-sec", type=int, default=0)
    parser.add_argument("--rerun-existing", action="store_true")
    parser.add_argument("--no-scan-persistent", action="store_true")
    parser.add_argument("--no-mirror", action="store_true")
    parser.add_argument("--training-output", type=Path)
    parser.add_argument("--training-summary", type=Path)
    parser.add_argument("--training-feature-set", choices=["advantage", "state"], default="advantage")
    parser.add_argument("--training-target", choices=TARGET_FIELDS, default="delta_vs_rule")
    parser.add_argument("--training-drop-label", action="append", choices=LABELS, default=[])
    parser.add_argument("--training-allow-incomplete-vector", action="store_true")
    parser.add_argument("--training-drop-zero-targets", action="store_true")
    parser.add_argument("--training-clip-target", type=float)
    args = parser.parse_args()

    output = _resolve(args.output)
    output_dir = _resolve(args.output_dir) if args.output_dir else output.with_suffix("")
    output_dir.mkdir(parents=True, exist_ok=True)
    opponents = [_resolve(path) for path in args.opponent]
    opponent_outputs = [output_dir / f"{idx:02d}_{_safe_label(path)}.json" for idx, path in enumerate(opponents)]
    commands = [_runner_cmd(args, opponent, idx, opponent_outputs[idx]) for idx, opponent in enumerate(opponents)]
    payload: dict[str, Any] = {
        "mode": "multi_opponent_multi_action_scheduler_v1",
        "executor": "process_pool" if args.workers > 1 else "serial",
        "version": _rel(_resolve(args.version)),
        "opponents": [_rel(path) for path in opponents],
        "output_dir": _rel(output_dir),
        "settings": {
            "workers": args.workers,
            "shard_workers": args.shard_workers,
            "shards_per_opponent": args.shards_per_opponent,
            "games_per_shard": args.games_per_shard,
            "max_rows_per_shard": args.max_rows_per_shard,
            "max_scan_decisions": args.max_scan_decisions,
            "stage": args.stage,
            "branch_scope": args.branch_scope,
            "active_target": args.active_target,
            "seed_base": args.seed_base,
            "seed_offset": args.seed_offset,
            "seed_stride": args.seed_stride,
            "opponent_seed_stride": args.opponent_seed_stride,
            "bot_seed_base": args.bot_seed_base,
            "bot_seed_stride": args.bot_seed_stride,
            "opponent_bot_seed_stride": args.opponent_bot_seed_stride,
            "max_hands": args.max_hands,
        },
        "results": [],
        "summary": {},
        "training": None,
    }
    _write(output, payload)

    results: list[dict[str, Any] | None] = [None] * len(opponents)
    if args.workers <= 1:
        for idx, opponent in enumerate(opponents):
            print(f"opponent {idx + 1}/{len(opponents)}: {_display_cmd(commands[idx])}")
            results[idx] = _run_runner(
                commands[idx],
                idx,
                str(opponent),
                str(opponent_outputs[idx]),
                int(args.runner_timeout_sec),
            )
            print(f"  rc={results[idx]['returncode']} rows={results[idx]['row_count']}")
    else:
        print(f"running {len(opponents)} opponents with {args.workers} scheduler workers")
        with ProcessPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(
                    _run_runner,
                    commands[idx],
                    idx,
                    str(opponent),
                    str(opponent_outputs[idx]),
                    int(args.runner_timeout_sec),
                ): idx
                for idx, opponent in enumerate(opponents)
            }
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()
                print(f"  opponent {idx + 1}/{len(opponents)} rc={results[idx]['returncode']} rows={results[idx]['row_count']}")

    payload["results"] = [row for row in results if row is not None]
    payload["summary"] = _summarize_results(payload["results"])
    payload["training"] = _build_training(args, [path for path in opponent_outputs if path.exists()])
    _write(output, payload)
    print(json.dumps({"summary": payload["summary"], "training": payload["training"]}, indent=2))
    failed = payload["summary"].get("failed_opponents", 0)
    training = payload.get("training")
    if failed:
        raise SystemExit(1)
    if training is not None and int(training.get("returncode", 1)) != 0:
        raise SystemExit(int(training.get("returncode", 1)) or 1)


if __name__ == "__main__":
    main()
