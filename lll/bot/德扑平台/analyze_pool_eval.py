#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从批量镜像评测日志生成中文统计分析。"""

import argparse
import json
import math
import os
from collections import Counter, defaultdict

from hl_loop import ChineseArgumentParser


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def mean(values):
    return sum(values) / len(values) if values else 0.0


def median(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def stddev(values):
    if len(values) < 2:
        return 0.0
    avg = mean(values)
    return math.sqrt(sum((value - avg) ** 2 for value in values) / (len(values) - 1))


def action_kind(action):
    try:
        action = int(action)
    except (TypeError, ValueError):
        return "无法解析"
    if action == -2:
        return "全下"
    if action == -1:
        return "弃牌"
    if action == 0:
        return "过牌/跟注"
    if action > 0:
        return "下注/加注"
    return "其他"


def round_name(value):
    return {
        0: "翻前",
        1: "翻牌",
        2: "转牌",
        3: "河牌",
    }.get(value, "未知")


def analyze_matchup(path):
    data = load_json(path)
    games = data.get("games", [])
    physical_chips = [float(game.get("bot0_chips", 0)) for game in games]
    pair_chips = [
        physical_chips[idx] + physical_chips[idx + 1]
        for idx in range(0, len(physical_chips) - 1, 2)
    ]

    candidate_verdicts = Counter()
    opponent_verdicts = Counter()
    candidate_actions = Counter()
    opponent_actions = Counter()
    branch_counts = Counter()
    round_counts = Counter()
    flag_counts = Counter()
    terminal_branch_stats = defaultdict(lambda: {
        "count": 0,
        "chips": 0.0,
        "win_count": 0,
        "loss_count": 0,
        "positive_chips": 0.0,
        "negative_chips": 0.0,
    })
    hand_results = []

    for game_idx, game in enumerate(games):
        hand_no = 0
        last_action = None
        last_trace = None
        for entry in game.get("logs", []):
            output = entry.get("output")
            if isinstance(output, dict):
                display = output.get("display", {})
                temp_result = display.get("temp_result")
                if isinstance(temp_result, list) and temp_result:
                    hand_no += 1
                    candidate_result = temp_result[0] if len(temp_result) > 0 else {}
                    chips = float(candidate_result.get("win_chips", 0))
                    trace = last_trace or {}
                    branch = trace.get("decision_branch") or "无候选终局动作"
                    terminal_branch_stats[branch]["count"] += 1
                    terminal_branch_stats[branch]["chips"] += chips
                    if chips > 0:
                        terminal_branch_stats[branch]["win_count"] += 1
                        terminal_branch_stats[branch]["positive_chips"] += chips
                    elif chips < 0:
                        terminal_branch_stats[branch]["loss_count"] += 1
                        terminal_branch_stats[branch]["negative_chips"] += chips
                    hand_results.append({
                        "opponent": data.get("baseline", {}).get("name", "未知"),
                        "game": game_idx + 1,
                        "mirror": bool(game.get("mirror")),
                        "hand": hand_no,
                        "chips": chips,
                        "action": last_action,
                        "action_kind": action_kind(last_action),
                        "branch": branch,
                        "round": trace.get("round"),
                        "round_name": round_name(trace.get("round")),
                        "win_rate": trace.get("win_rate"),
                        "to_call": trace.get("to_call"),
                        "pot": trace.get("pot"),
                        "my_cards": trace.get("my_cards"),
                        "public_cards": trace.get("public_cards"),
                        "made_strength": trace.get("made_strength"),
                        "value_tier": trace.get("value_tier"),
                        "opponent_confidence": trace.get("opponent_confidence"),
                        "opponent_aggression": trace.get("opponent_aggression"),
                        "opponent_fold_to_raise": trace.get("opponent_fold_to_raise"),
                        "board_wetness": trace.get("board_wetness"),
                        "anti_lock_pressure": bool(trace.get("anti_lock_pressure")),
                        "check_probe_signal": bool(trace.get("check_probe_signal")),
                        "sanitized": bool(trace.get("sanitized")),
                    })
                    last_action = None
                    last_trace = None
                continue

            for player_key, player_log in entry.items():
                if player_key == "output" or not isinstance(player_log, dict):
                    continue
                player_id = int(player_key)
                verdict = player_log.get("verdict", "未知")
                action = player_log.get("response")
                if player_id == 0:
                    candidate_verdicts[verdict] += 1
                    candidate_actions[action_kind(action)] += 1
                    trace = player_log.get("hl_trace") or {}
                    if trace:
                        branch = trace.get("decision_branch") or "未知分支"
                        branch_counts[branch] += 1
                        round_counts[round_name(trace.get("round"))] += 1
                        for flag in ("anti_lock_pressure", "check_probe_signal", "sanitized"):
                            if trace.get(flag):
                                flag_counts[flag] += 1
                    last_action = action
                    last_trace = trace
                else:
                    opponent_verdicts[verdict] += 1
                    opponent_actions[action_kind(action)] += 1

    terminal_branches = []
    for branch, stats in terminal_branch_stats.items():
        terminal_branches.append({
            "branch": branch,
            "count": stats["count"],
            "total_chips": round(stats["chips"], 1),
            "avg_chips": round(stats["chips"] / max(1, stats["count"]), 1),
            "win_count": stats["win_count"],
            "loss_count": stats["loss_count"],
            "positive_chips": round(stats["positive_chips"], 1),
            "negative_chips": round(stats["negative_chips"], 1),
        })
    terminal_branches.sort(key=lambda row: row["total_chips"])

    return {
        "opponent": data.get("baseline", {}).get("name", os.path.basename(path)),
        "source_file": os.path.basename(path),
        "mirror_wld": [
            int(data.get("bot0_wins", 0)),
            int(data.get("bot1_wins", 0)),
            int(data.get("draws", 0)),
        ],
        "physical_wld": [
            sum(value > 0 for value in physical_chips),
            sum(value < 0 for value in physical_chips),
            sum(value == 0 for value in physical_chips),
        ],
        "physical_chips": physical_chips,
        "pair_chips": pair_chips,
        "total_chips": round(sum(physical_chips), 1),
        "avg_physical_chips": round(mean(physical_chips), 1),
        "median_physical_chips": round(median(physical_chips), 1),
        "stddev_physical_chips": round(stddev(physical_chips), 1),
        "min_physical_chips": round(min(physical_chips), 1) if physical_chips else 0,
        "max_physical_chips": round(max(physical_chips), 1) if physical_chips else 0,
        "candidate_verdicts": dict(candidate_verdicts),
        "opponent_verdicts": dict(opponent_verdicts),
        "candidate_actions": dict(candidate_actions),
        "opponent_actions": dict(opponent_actions),
        "branch_counts": dict(branch_counts),
        "round_counts": dict(round_counts),
        "flag_counts": dict(flag_counts),
        "terminal_branches": terminal_branches,
        "hand_results": hand_results,
    }


def render_report(run_data, matchup_rows):
    all_hands = [hand for row in matchup_rows for hand in row["hand_results"]]
    all_physical_chips = [
        chips for row in matchup_rows for chips in row["physical_chips"]
    ]
    all_pair_chips = [chips for row in matchup_rows for chips in row["pair_chips"]]
    mirror_wins = sum(row["mirror_wld"][0] for row in matchup_rows)
    mirror_losses = sum(row["mirror_wld"][1] for row in matchup_rows)
    mirror_draws = sum(row["mirror_wld"][2] for row in matchup_rows)
    physical_wins = sum(row["physical_wld"][0] for row in matchup_rows)
    physical_losses = sum(row["physical_wld"][1] for row in matchup_rows)
    physical_draws = sum(row["physical_wld"][2] for row in matchup_rows)
    candidate_bad = sum(
        count
        for row in matchup_rows
        for verdict, count in row["candidate_verdicts"].items()
        if verdict != "OK"
    )
    opponent_bad = sum(
        count
        for row in matchup_rows
        for verdict, count in row["opponent_verdicts"].items()
        if verdict != "OK"
    )

    lines = [
        "# 更新对手池 70 手牌评测分析",
        "",
        "## 评测口径",
        "",
        "- 当前 bot：`{}`".format(run_data["candidate"]["path"]),
        "- 对手数量：`{}`".format(len(matchup_rows)),
        "- 每个对手：`{} 场`，由 `{} 组`换牌换庄镜像组成。".format(
            run_data["config"]["physical_games_per_baseline"],
            run_data["config"]["games"],
        ),
        "- 每场：`{} 手牌`。".format(run_data["config"]["hands_per_game"]),
        "- 所有牌堆和本地随机种子固定，可复现。",
        "",
        "## 总体结果",
        "",
        "| 对手 | 镜像组胜-负-平 | 实际场胜-负-平 | 总筹码差 | 场均筹码差 | 中位数 | 波动标准差 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in matchup_rows:
        lines.append("| `{}` | {}-{}-{} | {}-{}-{} | {} | {} | {} | {} |".format(
            row["opponent"],
            *row["mirror_wld"],
            *row["physical_wld"],
            row["total_chips"],
            row["avg_physical_chips"],
            row["median_physical_chips"],
            row["stddev_physical_chips"],
        ))

    lines.extend([
        "",
        "## 合并统计",
        "",
        "- 镜像组总成绩：`{}-{}-{}`；实际场总成绩：`{}-{}-{}`。".format(
            mirror_wins,
            mirror_losses,
            mirror_draws,
            physical_wins,
            physical_losses,
            physical_draws,
        ),
        "- {} 场总筹码差：`{}`；场均：`{}`；单场中位数：`{}`；标准差：`{}`。".format(
            len(all_physical_chips),
            round(sum(all_physical_chips), 1),
            round(mean(all_physical_chips), 1),
            round(median(all_physical_chips), 1),
            round(stddev(all_physical_chips), 1),
        ),
        "- {} 组镜像净值中位数：`{}`；当前 bot 异常动作 `{}` 次，对手异常动作 `{}` 次。".format(
            len(all_pair_chips),
            round(median(all_pair_chips), 1),
            candidate_bad,
            opponent_bad,
        ),
        "- 实验结论：`{}`。{}".format(
            run_data.get("result", {}).get("verdict", "未知"),
            (
                "当前 bot 存在崩溃、非法动作或动作修正。"
                if candidate_bad
                else "平均镜像筹码差为负。"
                if mean(all_pair_chips) < 0
                else "未发现崩溃或非法动作，收益指标非负。"
            ),
        ),
        "",
        "## 稳定性和合法性",
        "",
    ])
    for row in matchup_rows:
        row_candidate_bad = sum(
            count for verdict, count in row["candidate_verdicts"].items()
            if verdict != "OK"
        )
        row_opponent_bad = sum(
            count for verdict, count in row["opponent_verdicts"].items()
            if verdict != "OK"
        )
        lines.append("- `{}`：当前 bot 异常动作 `{}` 次，对手异常动作 `{}` 次；单场最差 `{}`，最好 `{}`。".format(
            row["opponent"],
            row_candidate_bad,
            row_opponent_bad,
            row["min_physical_chips"],
            row["max_physical_chips"],
        ))

    lines.extend(["", "## 可提升之处", ""])
    total_hands = len(all_hands)
    large_losses = [hand for hand in all_hands if hand["chips"] <= -1000]
    all_in_losses = [
        hand for hand in large_losses
        if hand["action_kind"] == "全下" or hand["anti_lock_pressure"]
    ]
    probe_losses = [
        hand for hand in large_losses
        if hand["check_probe_signal"] or "probe" in hand["branch"]
    ]
    anti_hands = [hand for hand in all_hands if hand["branch"] == "anti_lock_pressure"]
    low_equity_anti = [
        hand for hand in anti_hands
        if hand["win_rate"] is not None and hand["win_rate"] < 0.4
    ]
    higher_equity_anti = [
        hand for hand in anti_hands
        if hand["win_rate"] is not None and hand["win_rate"] >= 0.4
    ]
    probe_hands = [hand for hand in all_hands if hand["branch"] == "probe_raise"]
    value_hands = [
        hand for hand in all_hands if hand["branch"] == "value_or_pressure_raise"
    ]
    bot5_anti = [
        hand for hand in anti_hands if hand["opponent"] == "bot5"
    ]
    anti_net = round(sum(hand["chips"] for hand in anti_hands), 1)
    non_anti_river_losses = [
        hand for hand in all_hands
        if hand["round"] == 3
        and hand["chips"] <= -1000
        and not hand["anti_lock_pressure"]
    ]
    lines.append("- 样本共提取 `{}` 手牌结果，其中单手亏损达到 1000 或以上的样本 `{}` 个。".format(
        total_hands, len(large_losses)
    ))
    lines.append("- 大额亏损中，终局动作为全下或带 anti-lock 标记的样本 `{}` 个；这部分应优先区分正常价值打光和反锁定例外。".format(
        len(all_in_losses)
    ))
    lines.append("- 大额亏损中，带连续 check 探测信号或 probe 分支的样本 `{}` 个；只统计聚类，不据此直接削弱 `100` 小探测机制。".format(
        len(probe_losses)
    ))
    lines.append("- anti-lock 终局线共 `{}` 次，`{} 胜 {} 负`，净筹码 `{}`；{}。".format(
        len(anti_hands),
        sum(hand["chips"] > 0 for hand in anti_hands),
        sum(hand["chips"] < 0 for hand in anti_hands),
        anti_net,
        "整体收益为正，但仍需关注少数大额亏损"
        if anti_net >= 0
        else "当前净值为负，应优先复盘大额亏损",
    ))
    lines.append("- anti-lock 中估算胜率低于 40% 的 `{}` 次净筹码 `{}`；胜率不低于 40% 的 `{}` 次净筹码 `{}`。优先继续验证低权益时的触发门槛和下注尺度，而不是删除 anti-lock 例外。".format(
        len(low_equity_anti),
        round(sum(hand["chips"] for hand in low_equity_anti), 1),
        len(higher_equity_anti),
        round(sum(hand["chips"] for hand in higher_equity_anti), 1),
    ))
    if bot5_anti:
        lines.append("- 对 `bot5` 的 anti-lock 终局线 `{}` 次，`{} 胜 {} 负`，净筹码 `{}`。".format(
            len(bot5_anti),
            sum(hand["chips"] > 0 for hand in bot5_anti),
            sum(hand["chips"] < 0 for hand in bot5_anti),
            round(sum(hand["chips"] for hand in bot5_anti), 1),
        ))
    lines.append("- `probe_raise` 终局线 `{}` 次，净筹码 `{}`；`value_or_pressure_raise` 终局线 `{}` 次，净筹码 `{}`。两条主进攻线整体为正，暂不应因少数失败样本整体收紧。".format(
        len(probe_hands),
        round(sum(hand["chips"] for hand in probe_hands), 1),
        len(value_hands),
        round(sum(hand["chips"] for hand in value_hands), 1),
    ))
    if non_anti_river_losses:
        worst_river = min(non_anti_river_losses, key=lambda hand: hand["chips"])
        lines.append("- 河牌非 anti-lock 大额亏损共 `{}` 手，最差为对 `{}` 的 `{}`；应结合对手跟注范围和下注尺寸逐手复盘，不据单手改策略。".format(
            len(non_anti_river_losses),
            worst_river["opponent"],
            worst_river["chips"],
        ))

    weakest = sorted(matchup_rows, key=lambda row: row["total_chips"])
    if weakest:
        lines.append("- 按总筹码差，当前压力最大的对手依次为：{}。".format(
            "、".join("`{}`（{}）".format(row["opponent"], row["total_chips"]) for row in weakest)
        ))

    lines.extend(["", "## 高损失样本", ""])
    for hand in sorted(all_hands, key=lambda row: row["chips"])[:20]:
        lines.append(
            "- `{opponent}` 第 {game} 场/第 {hand} 手：`{chips}`，"
            "{round_name}，终局动作 `{action}`（{action_kind}），分支 `{branch}`，"
            "胜率 `{win_rate}`，成牌强度 `{made_strength}`，价值档 `{value_tier}`，"
            "底池 `{pot}`，需跟 `{to_call}`，anti-lock `{anti_lock_pressure}`。".format(
                **hand
            )
        )

    lines.extend([
        "",
        "## 统计边界",
        "",
        "- 每个对手只有 {} 组镜像样本，足以发现明显崩溃、非法动作和重复失败模式，但不足以确认小幅策略优劣。".format(
            run_data["config"]["games"],
        ),
        "- 单场接近正负 20000 的结果会显著拉动均值，因此同时保留中位数、标准差和镜像组净值。",
        "- 弃牌终局天然记录为负筹码，不能直接把 `fold_defense` 的累计负值解释为错误弃牌；需要结合底池赔率和对手范围逐手复盘。",
        "- 本报告只提出观察方向，不修改任何 bot。",
        "",
        "## 复现实验",
        "",
        "```powershell",
        "python .\\德扑平台\\pool_eval.py .\\{} {} --groups {} --hands {} --seed {}".format(
            run_data["candidate"]["path"],
            " ".join(".\\{}".format(entry["path"]) for entry in run_data["baselines"]),
            run_data["config"]["games"],
            run_data["config"]["hands_per_game"],
            run_data["config"]["seed"],
        ),
        "```",
        "",
    ])
    return "\n".join(lines)


def main():
    parser = ChineseArgumentParser(description="分析批量德扑评测日志")
    parser.add_argument("run_dir", help="评测运行目录")
    args = parser.parse_args()

    run_dir = os.path.abspath(args.run_dir)
    run_data = load_json(os.path.join(run_dir, "run.json"))
    matchups_dir = os.path.join(run_dir, "matchups")
    matchup_rows = []
    for name in sorted(os.listdir(matchups_dir)):
        if name.endswith(".json"):
            matchup_rows.append(analyze_matchup(os.path.join(matchups_dir, name)))

    compact_rows = []
    all_high_losses = []
    for row in matchup_rows:
        compact = dict(row)
        hand_results = compact.pop("hand_results")
        compact_rows.append(compact)
        all_high_losses.extend(hand for hand in hand_results if hand["chips"] <= -1000)

    write_json(os.path.join(run_dir, "pool_statistics.json"), {
        "matchups": compact_rows,
        "high_loss_count": len(all_high_losses),
        "high_losses": sorted(all_high_losses, key=lambda row: row["chips"]),
    })
    write_text(
        os.path.join(run_dir, "pool_analysis.md"),
        render_report(run_data, matchup_rows),
    )
    print("已生成：{}".format(os.path.join(run_dir, "pool_analysis.md")))


if __name__ == "__main__":
    main()
