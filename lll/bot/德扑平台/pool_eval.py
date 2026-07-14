#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量运行候选 bot 对多个对手的固定手数镜像评测。"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(ENGINE_DIR)
RUNS_DIR = os.path.join(PROJECT_DIR, "hl_runs")

if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)

from battle import mirror_battle
from hl_loop import ChineseArgumentParser, analyze_run_dir


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def append_progress(path, text):
    stamp = datetime.now().strftime("%H:%M:%S")
    with open(path, "a", encoding="utf-8") as f:
        f.write("[{}] {}\n".format(stamp, text))


def rel(path):
    return os.path.relpath(os.path.abspath(path), PROJECT_DIR)


def safe_name(path):
    return os.path.splitext(os.path.basename(path))[0]


def main():
    parser = ChineseArgumentParser(description="德扑 bot 批量镜像评测")
    parser.add_argument("candidate", help="候选 bot 路径")
    parser.add_argument("opponents", nargs="+", help="对手 bot 路径")
    parser.add_argument("--groups", type=int, default=5, help="每个对手的镜像组数")
    parser.add_argument("--hands", type=int, default=70, help="每场手牌数")
    parser.add_argument("--seed", type=int, default=2026060601, help="基础随机种子")
    parser.add_argument("--tag", default="updated_pool_70hands_10games", help="实验标签")
    parser.add_argument("--in-process", action="store_true", help="进程内确定性调用 bot")
    args = parser.parse_args()

    candidate = os.path.abspath(args.candidate)
    opponents = [os.path.abspath(path) for path in args.opponents]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(RUNS_DIR, "{}_{}".format(timestamp, args.tag))
    matchups_dir = os.path.join(run_dir, "matchups")
    os.makedirs(matchups_dir, exist_ok=True)
    progress_path = os.path.join(run_dir, "progress.txt")

    baselines = []
    for path in opponents:
        baselines.append({
            "name": safe_name(path),
            "path": rel(path),
            "sha256": file_sha256(path),
            "label": safe_name(path),
            "role": "opponent",
        })

    run_data = {
        "type": "pool_eval",
        "timestamp": timestamp,
        "candidate": {
            "path": rel(candidate),
            "label": "当前 bot",
            "sha256": file_sha256(candidate),
        },
        "baselines": baselines,
        "config": {
            "games": args.groups,
            "physical_games_per_baseline": args.groups * 2,
            "hands_per_game": args.hands,
            "seed": args.seed,
            "hl_trace": True,
            "mirror": True,
            "bidirectional": False,
            "in_process": args.in_process,
        },
        "status": "running",
    }
    write_json(os.path.join(run_dir, "run.json"), run_data)
    append_progress(
        progress_path,
        "开始评测：{} 个对手，每个 {} 场，每场 {} 手。".format(
            len(opponents), args.groups * 2, args.hands
        ),
    )

    for idx, (opponent, baseline) in enumerate(zip(opponents, baselines)):
        matchup_seed = args.seed + idx * 100000
        name = baseline["name"]
        append_progress(progress_path, "开始对战 {}，seed={}。".format(name, matchup_seed))
        wins, draws, n_played, logs = mirror_battle(
            candidate,
            opponent,
            n_games=args.groups,
            verbose=True,
            save_log=True,
            seed=matchup_seed,
            hl_mode=True,
            max_hand=args.hands,
            in_process=args.in_process,
        )
        summary = {
            "timestamp": timestamp,
            "direction": "mirror",
            "candidate_slot": 0,
            "candidate": rel(candidate),
            "baseline": baseline,
            "bot0": rel(candidate),
            "bot1": rel(opponent),
            "n_games": args.groups,
            "n_games_actual": n_played,
            "physical_games": len(logs),
            "hands_per_game": args.hands,
            "seed": matchup_seed,
            "bot0_wins": wins[0],
            "bot1_wins": wins[1],
            "draws": draws,
            "games": logs,
        }
        result_path = os.path.join(matchups_dir, "current_mirror_{}.json".format(name))
        write_json(result_path, summary)
        append_progress(
            progress_path,
            "完成 {}：镜像组 {}-{}-{}，实际保存 {} 场。".format(
                name, wins[0], wins[1], draws, len(logs)
            ),
        )

    run_data["status"] = "complete"
    write_json(os.path.join(run_dir, "run.json"), run_data)
    analyzed = analyze_run_dir(run_dir)
    append_progress(
        progress_path,
        "全部完成：结论 {}，平均镜像筹码差 {}。".format(
            analyzed.get("result", {}).get("verdict"),
            analyzed.get("result", {}).get("avg_mirror_chip_diff"),
        ),
    )
    print("评测完成：{}".format(run_dir))


if __name__ == "__main__":
    main()
