#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地德扑 bot 的启发式学习工作台。"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(ENGINE_DIR)
BOTS_DIR = os.path.join(PROJECT_DIR, "bots")
RUNS_DIR = os.path.join(PROJECT_DIR, "hl_runs")
MANIFEST_PATH = os.path.join(BOTS_DIR, "manifest.json")


class ChineseArgumentParser(argparse.ArgumentParser):
    def format_help(self):
        text = super(ChineseArgumentParser, self).format_help()
        replacements = [
            ("usage:", "用法:"),
            ("positional arguments:", "位置参数:"),
            ("optional arguments:", "可选参数:"),
            ("options:", "选项:"),
            ("show this help message and exit", "显示帮助并退出"),
        ]
        for old, new in replacements:
            text = text.replace(old, new)
        return text

    def format_usage(self):
        return super(ChineseArgumentParser, self).format_usage().replace("usage:", "用法:")

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "参数错误：{}\n".format(message))


def now_ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def safe_tag(value):
    value = value or "run"
    value = re.sub(r"[^0-9A-Za-z._-]+", "_", value.strip())
    return value.strip("_") or "run"


def rel(path):
    try:
        return os.path.relpath(os.path.abspath(path), PROJECT_DIR)
    except ValueError:
        return os.path.abspath(path)


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_text(path, text):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def write_jsonl(path, rows):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest():
    manifest = read_json(MANIFEST_PATH, default=None)
    if not manifest:
        return {"version": 1, "bots": []}
    manifest.setdefault("version", 1)
    manifest.setdefault("bots", [])
    return manifest


def save_manifest(manifest):
    write_json(MANIFEST_PATH, manifest)


def bot_num_from_name(name):
    m = re.match(r"bot(\d+)$", name)
    return int(m.group(1)) if m else None


def discover_existing_bot_numbers():
    nums = []
    if not os.path.isdir(BOTS_DIR):
        return nums
    for name in os.listdir(BOTS_DIR):
        num = bot_num_from_name(name)
        if num is not None:
            nums.append(num)
    return sorted(nums)


def next_bot_number(manifest):
    nums = discover_existing_bot_numbers()
    for entry in manifest.get("bots", []):
        num = entry.get("number")
        if isinstance(num, int):
            nums.append(num)
    return (max(nums) + 1) if nums else 1


def resolve_project_path(path):
    if os.path.isabs(path):
        return path
    return os.path.abspath(os.path.join(PROJECT_DIR, path))


def baseline_name_for_path(path):
    base = os.path.basename(path)
    stem = os.path.splitext(base)[0]
    if base.lower() == "main.py":
        parent = os.path.basename(os.path.dirname(path))
        if parent:
            return parent
    return stem


def snapshot_bot(args):
    source = resolve_project_path(args.source)
    if not os.path.isfile(source):
        raise SystemExit("找不到源 bot：{}".format(source))

    ensure_dir(BOTS_DIR)
    manifest = load_manifest()
    bot_num = args.number if args.number is not None else next_bot_number(manifest)
    bot_name = "bot{:03d}".format(bot_num)
    bot_dir = os.path.join(BOTS_DIR, bot_name)
    target = os.path.join(bot_dir, "main.py")
    if os.path.exists(target) and not args.force:
        raise SystemExit("{} 已存在；如需覆盖请使用 --force".format(target))

    ensure_dir(bot_dir)
    shutil.copy2(source, target)

    entry = {
        "number": bot_num,
        "name": bot_name,
        "path": rel(target),
        "source": rel(source),
        "sha256": file_sha256(target),
        "label": args.label,
        "role": args.role,
        "notes": args.notes or "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }

    manifest["bots"] = [e for e in manifest.get("bots", []) if e.get("name") != bot_name]
    manifest["bots"].append(entry)
    manifest["bots"].sort(key=lambda e: e.get("number", 0))
    save_manifest(manifest)

    print("已创建快照：{}".format(target))
    print("已更新版本清单：{}".format(MANIFEST_PATH))
    return entry


def baseline_entries(args):
    manifest = load_manifest()
    if args.baseline:
        entries = []
        for item in args.baseline:
            path = resolve_project_path(item)
            if not os.path.isfile(path):
                raise SystemExit("找不到 baseline：{}".format(path))
            entries.append({
                "name": baseline_name_for_path(path),
                "path": rel(path),
                "sha256": file_sha256(path),
                "label": item,
                "role": "baseline",
            })
        return entries

    entries = [e for e in manifest.get("bots", []) if e.get("role") == "baseline"]
    if entries:
        for entry in entries:
            path = resolve_project_path(entry["path"])
            if os.path.isfile(path) and not entry.get("sha256"):
                entry["sha256"] = file_sha256(path)
        return entries
    raise SystemExit("没有找到 baseline；请先运行：python 德扑平台\\hl_loop.py snapshot --label baseline")


def scan_action_entry(entry):
    for key, value in entry.items():
        if key != "output":
            return key, value
    return None, None


def compact_trace(trace):
    if not trace:
        return None
    keys = [
        "decision_branch",
        "final_action",
        "raw_action",
        "sanitized",
        "win_rate",
        "to_call",
        "pot",
        "round",
        "anti_lock_pressure",
        "check_probe_signal",
        "small_probe",
        "check_probe",
        "semi_bluff",
        "blocker_bluff",
        "value_tier",
        "made_strength",
        "draw_strength",
        "board_wetness",
    ]
    return {k: trace.get(k) for k in keys if k in trace}


def interesting_from_matchup(matchup_file, matchup_data):
    rows = []
    regression_cases = []
    counters = Counter()
    candidate_slot = str(matchup_data.get("candidate_slot", 0))
    candidate_idx = int(candidate_slot)

    for game in matchup_data.get("games", []):
        logs = game.get("logs", [])
        current_requests = {}
        last_display = None
        for step_idx, entry in enumerate(logs):
            output = entry.get("output")
            if output:
                display = output.get("display", {})
                last_display = display
                content = output.get("content", {})
                if content:
                    current_requests = content

                temp_result = display.get("temp_result")
                if temp_result and len(temp_result) > 0:
                    hand = display.get("matchdata", {}).get("hand")
                    if len(temp_result) <= candidate_idx:
                        continue
                    candidate_result = temp_result[candidate_idx]
                    loss = candidate_result.get("win_chips", 0)
                    if loss <= -800:
                        counters["candidate_high_loss_hand"] += 1
                        rows.append({
                            "kind": "candidate_high_loss_hand",
                            "matchup_file": matchup_file,
                            "game": game.get("game"),
                            "mirror": game.get("mirror", False),
                            "step": step_idx,
                            "hand": max(0, hand - 1) if isinstance(hand, int) else hand,
                            "candidate_win_chips": loss,
                            "last_action": display.get("last_action"),
                            "public_cards": display.get("last_public_cards") or display.get("public_cards"),
                        })
                continue

            player_id, player_log = scan_action_entry(entry)
            if player_id is None:
                continue
            verdict = player_log.get("verdict")
            try:
                action = int(player_log.get("response", -1))
            except (TypeError, ValueError):
                action = -1
            trace = player_log.get("hl_trace")
            actor = "candidate" if player_id == candidate_slot else "baseline"
            req = current_requests.get(player_id)

            if verdict != "OK":
                counters["illegal_or_crash"] += 1
                rows.append({
                    "kind": "illegal_or_crash",
                    "actor": actor,
                    "matchup_file": matchup_file,
                    "game": game.get("game"),
                    "mirror": game.get("mirror", False),
                    "step": step_idx,
                    "verdict": verdict,
                    "action": action,
                    "request": req,
                })

            if trace:
                branch = trace.get("decision_branch")
                if actor == "candidate":
                    counters["branch:" + str(branch)] += 1
                if trace.get("sanitized"):
                    counters["sanitized_action"] += 1
                flags = []
                if trace.get("anti_lock_pressure"):
                    flags.append("anti_lock_pressure")
                if trace.get("check_probe") or trace.get("small_probe") or trace.get("check_probe_signal"):
                    flags.append("probe")
                if trace.get("sanitized"):
                    flags.append("sanitized")
                if action > 0 and branch in ("probe_raise", "semi_bluff_raise", "blocker_bluff_raise"):
                    flags.append("aggressive_bluff_line")
                if flags and actor == "candidate":
                    kind = "trace_" + "_".join(flags[:2])
                    counters[kind] += 1
                    row = {
                        "kind": kind,
                        "actor": actor,
                        "matchup_file": matchup_file,
                        "game": game.get("game"),
                        "mirror": game.get("mirror", False),
                        "step": step_idx,
                        "action": action,
                        "trace": compact_trace(trace),
                        "request": req,
                    }
                    rows.append(row)
                    if req and len(regression_cases) < 80:
                        regression_cases.append({
                            "kind": kind,
                            "matchup_file": matchup_file,
                            "game": game.get("game"),
                            "mirror": game.get("mirror", False),
                            "step": step_idx,
                            "request": req,
                            "action": action,
                            "trace": compact_trace(trace),
                            "watch": flags,
                        })

        if last_display is None:
            counters["missing_display"] += 1

    return rows, regression_cases, counters


def aggregate_matchup_metrics(matchup_data):
    games = matchup_data.get("games", [])
    candidate_slot = int(matchup_data.get("candidate_slot", 0))
    if candidate_slot == 0:
        candidate_wins = matchup_data.get("bot0_wins", 0)
        baseline_wins = matchup_data.get("bot1_wins", 0)
        chip_sum = sum(g.get("bot0_chips", 0) for g in games)
    else:
        candidate_wins = matchup_data.get("bot1_wins", 0)
        baseline_wins = matchup_data.get("bot0_wins", 0)
        chip_sum = sum(g.get("bot1_chips", 0) for g in games)
    logged_games = len(games)
    mirror_pairs = max(1, matchup_data.get("n_games_actual") or matchup_data.get("n_games") or 1)
    return {
        "candidate_slot": candidate_slot,
        "candidate_wins": candidate_wins,
        "baseline_wins": baseline_wins,
        "draws": matchup_data.get("draws", 0),
        "mirror_pairs": mirror_pairs,
        "logged_games": logged_games,
        "chip_sum": chip_sum,
        "avg_logged_chip_diff": round(chip_sum / max(1, logged_games), 1),
        "avg_mirror_chip_diff": round(chip_sum / max(1, mirror_pairs), 1),
    }


def classify_run(avg_chip, counters, control_run=False):
    if counters.get("illegal_or_crash", 0) > 0:
        return "RED"
    if control_run:
        return "YELLOW"
    if avg_chip <= -200:
        return "RED"
    if avg_chip >= 200 and counters.get("sanitized_action", 0) == 0:
        return "GREEN"
    return "YELLOW"


def render_summary(run_data, matchup_metrics, counters, interesting_rows, verdict):
    lines = []
    lines.append("# HL 运行摘要")
    lines.append("")
    lines.append("- 结论：`{}`".format(verdict))
    lines.append("- 候选 bot：`{}`".format(run_data["candidate"]["path"]))
    lines.append("- 随机种子：`{}`".format(run_data["config"]["seed"]))
    lines.append("- 每个 baseline 的镜像组数：`{}`".format(run_data["config"]["games"]))
    if run_data["config"].get("physical_games_per_baseline") is not None:
        lines.append("- 每个 baseline 的实际比赛场数：`{}`".format(
            run_data["config"]["physical_games_per_baseline"]
        ))
    if run_data["config"].get("hands_per_game") is not None:
        lines.append("- 每场手牌数：`{}`".format(run_data["config"]["hands_per_game"]))
    if run_data.get("result", {}).get("control_run"):
        lines.append("- 对照实验：候选和 baseline 哈希一致；此结果只用于校准评测框架，不作为合入策略的证据。")
    lines.append("")
    lines.append("## 对战")
    lines.append("")
    lines.append("| Baseline | 候选座位 | 胜-负-平 | 平均镜像筹码差 | 平均日志筹码差 | 日志 |")
    lines.append("| --- | ---: | ---: | ---: | ---: | --- |")
    for metric in matchup_metrics:
        lines.append("| `{}` | P{} | {}-{}-{} | {} | {} | `{}` |".format(
            metric["baseline"],
            metric["candidate_slot"],
            metric["candidate_wins"],
            metric["baseline_wins"],
            metric["draws"],
            metric["avg_mirror_chip_diff"],
            metric["avg_logged_chip_diff"],
            metric["result_file"],
        ))
    lines.append("")
    lines.append("## 日志信号")
    lines.append("")
    if counters:
        for key, count in counters.most_common(12):
            lines.append("- `{}`: {}".format(key, count))
    else:
        lines.append("- 没有提取到显著 trace 信号。")
    lines.append("")
    lines.append("## 值得复盘的手牌")
    lines.append("")
    for row in interesting_rows[:12]:
        label = row.get("kind")
        game = row.get("game")
        step = row.get("step")
        action = row.get("action", row.get("last_action"))
        lines.append("- `{}` game={} step={} action={}".format(label, game, step, action))
    if not interesting_rows:
        lines.append("- 没有提取到高损失或 trace 高亮信号手牌。")
    lines.append("")
    return "\n".join(lines)


def render_patch_plan(counters, verdict, control_run=False):
    lines = []
    lines.append("# 补丁草案")
    lines.append("")
    lines.append("本文件只给出人工复盘建议，不会自动修改 `1.py`。")
    lines.append("")
    lines.append("## 优先级")
    lines.append("")
    if control_run:
        lines.append("- 这是同代码对照实验。不要把筹码优势当作策略改进证据，应把它用于校准评测框架和回归检查。")
    elif verdict == "GREEN":
        lines.append("- 候选表现较好。合入任何策略修改前，优先补充对应回放/回归用例。")
    elif verdict == "RED":
        lines.append("- 在复盘下方亏损线或非法输出前，不建议合入这个候选。")
    else:
        lines.append("- 将本轮视为观察实验。用提取出的手牌设计一个小范围、有针对性的改动。")
    lines.append("")
    lines.append("## 建议关注点")
    lines.append("")
    suggestions_added = False
    if counters.get("illegal_or_crash", 0):
        lines.append("- 先修复 bot 崩溃或非法输出；在该计数归零前，不应下策略结论。")
        suggestions_added = True
    if counters.get("trace_anti_lock_pressure", 0) or counters.get("trace_anti_lock_pressure_probe", 0):
        lines.append("- 复盘提取出的 anti-lock 手牌，检查 `fold_gives_opponent_lock` 门槛和 `choose_anti_lock_pressure_action` 尺寸。")
        suggestions_added = True
    if counters.get("trace_probe", 0) or counters.get("branch:probe_raise", 0):
        lines.append("- 检查连续 check 后的小探测手牌；只有当亏损集中在这里时，才收紧湿润牌面或弃牌率条件。")
        suggestions_added = True
    if counters.get("candidate_high_loss_hand", 0):
        lines.append("- 从 `interesting_hands.jsonl` 中最高亏损手牌开始，区分亏损来自翻前防守、翻后打光，还是错过价值。")
        suggestions_added = True
    if counters.get("sanitized_action", 0):
        lines.append("- 排查被 `sanitize_action` 修正的动作；这表示候选意图和合法动作边界不一致。")
        suggestions_added = True
    if not suggestions_added:
        lines.append("- 没有出现主导失败模式。下一轮补丁保持小范围，并从 `regression_cases.json` 中补一个回放用例。")
    lines.append("")
    lines.append("## 回归检查清单")
    lines.append("")
    lines.append("- `sanitize_action` 能保证所有返回动作合法。")
    lines.append("- 普通非 anti-lock 场景下，垃圾牌和非 3bet 候选仍保持保守纪律。")
    lines.append("- 当弃牌会让对手进入锁定时，anti-lock 仍作为有边界的例外保留。")
    lines.append("- 连续 check 后的 `100` 小探测/偷池路径仍然可用。")
    lines.append("- 回放用例使用完整 Botzone 请求 payload。")
    lines.append("")
    return "\n".join(lines)


def analyze_run_dir(run_dir):
    run_path = os.path.abspath(run_dir)
    run_data = read_json(os.path.join(run_path, "run.json"), default=None)
    if not run_data:
        raise SystemExit("在目录中找不到 run.json：{}".format(run_path))

    matchups_dir = os.path.join(run_path, "matchups")
    if not os.path.isdir(matchups_dir):
        raise SystemExit("在目录中找不到 matchups 子目录：{}".format(run_path))

    all_rows = []
    all_cases = []
    counters = Counter()
    matchup_metrics = []

    for fname in sorted(os.listdir(matchups_dir)):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(matchups_dir, fname)
        data = read_json(fpath, default={})
        rows, cases, sub_counters = interesting_from_matchup(fname, data)
        all_rows.extend(rows)
        all_cases.extend(cases)
        counters.update(sub_counters)
        metric = aggregate_matchup_metrics(data)
        baseline = data.get("baseline")
        if isinstance(baseline, dict):
            metric["baseline"] = baseline.get("path") or baseline.get("name") or "baseline"
        else:
            metric["baseline"] = baseline or data.get("bot1") or "baseline"
        metric["result_file"] = os.path.join("matchups", fname)
        matchup_metrics.append(metric)

    candidate_hash = run_data.get("candidate", {}).get("sha256")
    candidate_path = run_data.get("candidate", {}).get("path")
    if not candidate_hash and candidate_path:
        resolved_candidate = resolve_project_path(candidate_path)
        if os.path.isfile(resolved_candidate):
            candidate_hash = file_sha256(resolved_candidate)
            run_data["candidate"]["sha256"] = candidate_hash
    baseline_hashes = []
    for entry in run_data.get("baselines", []):
        if isinstance(entry, dict):
            baseline_hash = entry.get("sha256")
            baseline_path = entry.get("path")
            if not baseline_hash and baseline_path:
                resolved_baseline = resolve_project_path(baseline_path)
                if os.path.isfile(resolved_baseline):
                    baseline_hash = file_sha256(resolved_baseline)
                    entry["sha256"] = baseline_hash
            baseline_hashes.append(baseline_hash)
    control_run = bool(candidate_hash and baseline_hashes and all(h == candidate_hash for h in baseline_hashes))

    total_chip = sum(m["chip_sum"] for m in matchup_metrics)
    total_pairs = sum(max(1, m.get("mirror_pairs", 1)) for m in matchup_metrics)
    avg_chip = round(total_chip / max(1, total_pairs), 1)
    verdict = classify_run(avg_chip, counters, control_run=control_run)

    run_data["result"] = {
        "verdict": verdict,
        "avg_mirror_chip_diff": avg_chip,
        "illegal_or_crash_count": counters.get("illegal_or_crash", 0),
        "sanitized_action_count": counters.get("sanitized_action", 0),
        "interesting_count": len(all_rows),
        "control_run": control_run,
    }
    run_data["matchups"] = matchup_metrics
    write_json(os.path.join(run_path, "run.json"), run_data)
    write_jsonl(os.path.join(run_path, "interesting_hands.jsonl"), all_rows)
    write_json(os.path.join(run_path, "regression_cases.json"), {
        "cases": all_cases,
        "count": len(all_cases),
    })
    write_text(os.path.join(run_path, "summary.md"), render_summary(run_data, matchup_metrics, counters, all_rows, verdict))
    write_text(os.path.join(run_path, "patch_plan.md"), render_patch_plan(counters, verdict, control_run=control_run))
    return run_data


def run_hl(args):
    candidate = resolve_project_path(args.candidate)
    if not os.path.isfile(candidate):
        raise SystemExit("找不到候选 bot：{}".format(candidate))

    baselines = baseline_entries(args)
    timestamp = now_ts()
    seed = args.seed if args.seed is not None else int(time.time())
    run_dir = os.path.join(RUNS_DIR, "{}_{}".format(timestamp, safe_tag(args.tag)))
    matchups_dir = os.path.join(run_dir, "matchups")
    ensure_dir(matchups_dir)

    run_data = {
        "type": "hl_run",
        "timestamp": timestamp,
        "candidate": {
            "path": rel(candidate),
            "label": args.candidate_label,
            "sha256": file_sha256(candidate),
        },
        "baselines": baselines,
        "config": {
            "games": args.games,
            "seed": seed,
            "hl_trace": True,
            "bidirectional": not args.single_direction,
        },
        "status": "running",
    }
    write_json(os.path.join(run_dir, "run.json"), run_data)

    if ENGINE_DIR not in sys.path:
        sys.path.insert(0, ENGINE_DIR)
    from battle import mirror_battle

    print("HL 运行目录：{}".format(run_dir))
    for idx, baseline in enumerate(baselines):
        baseline_path = resolve_project_path(baseline["path"])
        if not os.path.isfile(baseline_path):
            raise SystemExit("baseline 文件缺失：{}".format(baseline_path))
        label = safe_tag(baseline.get("name") or baseline.get("label") or "baseline")
        directions = [
            ("forward", candidate, baseline_path, 0),
        ]
        if not args.single_direction:
            directions.append(("reverse", baseline_path, candidate, 1))

        for direction_idx, (direction, bot0_path, bot1_path, candidate_slot) in enumerate(directions):
            result_file = "{}_{}_{}.json".format(safe_tag(args.candidate_label), direction, label)
            result_path = os.path.join(matchups_dir, result_file)
            matchup_seed = int(seed) + idx * 100000 + direction_idx * 50000
            direction_label = "正向" if direction == "forward" else "反向"
            print("正在运行{}对战：候选 vs {}（组数={}，seed={}）".format(
                direction_label, label, args.games, matchup_seed,
            ))
            wins, draws, n_played, logs = mirror_battle(
                bot0_path,
                bot1_path,
                n_games=args.games,
                verbose=args.verbose,
                save_log=True,
                seed=matchup_seed,
                hl_mode=True,
            )
            summary = {
                "timestamp": timestamp,
                "direction": direction,
                "candidate_slot": candidate_slot,
                "candidate": rel(candidate),
                "baseline": baseline,
                "bot0": rel(bot0_path),
                "bot1": rel(bot1_path),
                "n_games": args.games,
                "n_games_actual": n_played,
                "seed": matchup_seed,
                "bot0_wins": wins[0],
                "bot1_wins": wins[1],
                "draws": draws,
                "games": logs,
            }
            write_json(result_path, summary)

    run_data["status"] = "complete"
    write_json(os.path.join(run_dir, "run.json"), run_data)
    analyzed = analyze_run_dir(run_dir)
    print("结论：{}".format(analyzed.get("result", {}).get("verdict")))
    print("摘要：{}".format(os.path.join(run_dir, "summary.md")))
    print("补丁草案：{}".format(os.path.join(run_dir, "patch_plan.md")))
    return run_dir


def analyze_hl(args):
    run_path = resolve_project_path(args.run_dir)
    analyzed = analyze_run_dir(run_path)
    print("已重新生成报告：{}".format(run_path))
    print("结论：{}".format(analyzed.get("result", {}).get("verdict")))
    print("摘要：{}".format(os.path.join(run_path, "summary.md")))
    print("补丁草案：{}".format(os.path.join(run_path, "patch_plan.md")))
    return analyzed


def build_parser():
    parser = ChineseArgumentParser(description="德扑 bot 启发式学习工作台")
    sub = parser.add_subparsers(dest="command", parser_class=ChineseArgumentParser)

    p_snapshot = sub.add_parser("snapshot", help="把 bot 快照到 bots/botNNN/main.py")
    p_snapshot.add_argument("--source", default="1.py", help="源 bot 路径，默认 1.py")
    p_snapshot.add_argument("--label", default="baseline", help="人工标签")
    p_snapshot.add_argument("--role", default="baseline", choices=["baseline", "candidate", "archive"], help="manifest 角色")
    p_snapshot.add_argument("--notes", default="", help="manifest 备注")
    p_snapshot.add_argument("--number", type=int, default=None, help="指定 bot 编号")
    p_snapshot.add_argument("--force", action="store_true", help="覆盖已有 botNNN/main.py")
    p_snapshot.set_defaults(func=snapshot_bot)

    p_run = sub.add_parser("run", help="让候选 bot 对 baseline 池运行评测")
    p_run.add_argument("candidate", nargs="?", default="1.py", help="候选 bot 路径")
    p_run.add_argument("--baseline", action="append", default=None, help="baseline 路径；可重复传入")
    p_run.add_argument("-n", "--games", type=int, default=10, help="每个 baseline 的镜像组数")
    p_run.add_argument("--seed", type=int, default=None, help="固定随机种子")
    p_run.add_argument("--tag", default="candidate", help="运行目录标签")
    p_run.add_argument("--candidate-label", default="candidate", help="报告中的候选标签")
    p_run.add_argument("--single-direction", action="store_true", help="只让候选作为玩家 0")
    p_run.add_argument("-v", "--verbose", action="store_true", help="显示更详细的对战进度")
    p_run.set_defaults(func=run_hl)

    p_analyze = sub.add_parser("analyze", help="重新生成已有运行目录的报告")
    p_analyze.add_argument("run_dir", help="hl_runs 下的运行目录")
    p_analyze.set_defaults(func=analyze_hl)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
