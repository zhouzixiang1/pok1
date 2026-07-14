#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from blueprint_contract import CONTRACT_VERSION  # noqa: E402
from collect_outcome_teacher_data import _extract  # noqa: E402
from archive.botzone_local.engine.battle import battle, mirror_battle  # noqa: E402
from feature_spec import LABELS  # noqa: E402


def _resolve(path: Path) -> Path:
    return (ROOT / path).resolve() if not path.is_absolute() else path


def _main_path(path: Path) -> Path:
    return path / "main.py" if path.is_dir() else path


def _label(path: Path) -> str:
    return path.parent.name if path.name == "main.py" else path.name


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _run_job(job: dict[str, Any]) -> dict[str, Any]:
    teacher_main = Path(job["teacher_main"])
    opponent_main = Path(job["opponent_main"])
    if job["mode"] == "mirror":
        _, _, _, logs, _ = mirror_battle(
            str(teacher_main),
            str(opponent_main),
            n_games=int(job["games_per_shard"]),
            save_log=True,
        )
    else:
        _, _, _, logs = battle(
            str(teacher_main),
            str(opponent_main),
            n_games=int(job["games_per_shard"]),
            save_log=True,
        )

    samples: list[dict[str, Any]] = []
    for game_log in logs:
        samples.extend(
            _extract(
                game_log,
                job["teacher_label"],
                job["opponent_label"],
                positive_scale=float(job["positive_scale"]),
                negative_scale=float(job["negative_scale"]),
                min_weight=float(job["min_weight"]),
                max_weight=float(job["max_weight"]),
                min_abs_delta=float(job["min_abs_delta"]),
            )
        )
    for sample in samples:
        meta = sample.setdefault("meta", {})
        meta["shard"] = int(job["shard"])
        meta["job"] = int(job["job"])
    return {
        "job": int(job["job"]),
        "shard": int(job["shard"]),
        "pair": job["pair"],
        "samples": samples,
    }


def _counts(samples: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, float]]:
    counts = {name: 0 for name in LABELS}
    weighted = {name: 0.0 for name in LABELS}
    for sample in samples:
        label = int(sample["label"])
        counts[LABELS[label]] += 1
        weighted[LABELS[label]] += float(sample.get("weight", 1.0))
    return counts, weighted


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parallel outcome-weighted teacher-data sampler for the blueprint contract."
    )
    parser.add_argument("--teacher", action="append", required=True, type=Path)
    parser.add_argument("--opponent", action="append", required=True, type=Path)
    parser.add_argument("--games-per-shard", type=int, default=1)
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--mode", choices=["battle", "mirror"], default="mirror")
    parser.add_argument("--positive-scale", type=float, default=600.0)
    parser.add_argument("--negative-scale", type=float, default=0.30)
    parser.add_argument("--min-weight", type=float, default=0.08)
    parser.add_argument("--max-weight", type=float, default=3.0)
    parser.add_argument("--min-abs-delta", type=float, default=0.0)
    parser.add_argument("--max-samples", type=int, default=0)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    jobs: list[dict[str, Any]] = []
    for teacher_arg in args.teacher:
        teacher_main = _main_path(_resolve(teacher_arg))
        for opponent_arg in args.opponent:
            opponent_main = _main_path(_resolve(opponent_arg))
            if teacher_main.resolve() == opponent_main.resolve():
                continue
            pair = f"{_label(teacher_main)} vs {_label(opponent_main)}"
            for shard in range(args.shards):
                jobs.append(
                    {
                        "job": len(jobs),
                        "shard": shard,
                        "teacher_main": str(teacher_main),
                        "opponent_main": str(opponent_main),
                        "teacher_label": _label(teacher_main),
                        "opponent_label": _label(opponent_main),
                        "pair": pair,
                        "mode": args.mode,
                        "games_per_shard": args.games_per_shard,
                        "positive_scale": args.positive_scale,
                        "negative_scale": args.negative_scale,
                        "min_weight": args.min_weight,
                        "max_weight": args.max_weight,
                        "min_abs_delta": args.min_abs_delta,
                    }
                )

    if not jobs:
        raise SystemExit("no teacher/opponent jobs to run")

    samples: list[dict[str, Any]] = []
    pair_counts: dict[str, int] = {}
    shard_counts: dict[str, int] = {}
    max_workers = max(1, min(args.workers, len(jobs)))
    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(_run_job, job): job for job in jobs}
        for future in as_completed(future_map):
            result = future.result()
            rows = result["samples"]
            samples.extend(rows)
            pair_counts[result["pair"]] = pair_counts.get(result["pair"], 0) + len(rows)
            shard_key = f"{result['pair']} shard {result['shard']}"
            shard_counts[shard_key] = len(rows)
            print(
                f"job {result['job'] + 1}/{len(jobs)} {shard_key}: {len(rows)} samples",
                file=sys.stderr,
            )
            if args.max_samples > 0 and len(samples) >= args.max_samples:
                break

    if args.max_samples > 0:
        samples = samples[: args.max_samples]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as fh:
        for sample in samples:
            fh.write(json.dumps(sample, separators=(",", ":")) + "\n")

    label_counts, weighted_label_counts = _counts(samples)
    summary = {
        "samples": len(samples),
        "jobs": len(jobs),
        "completed_jobs": len(shard_counts),
        "shards": args.shards,
        "games_per_shard": args.games_per_shard,
        "workers": max_workers,
        "mode": args.mode,
        "teachers": [_rel(_main_path(_resolve(p))) for p in args.teacher],
        "opponents": [_rel(_main_path(_resolve(p))) for p in args.opponent],
        "pair_counts": pair_counts,
        "shard_counts": shard_counts,
        "label_counts": label_counts,
        "weighted_label_counts": weighted_label_counts,
        "contract": CONTRACT_VERSION,
        "target": "teacher_action_outcome_weighted_sharded",
    }
    args.output.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
