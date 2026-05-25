#!/usr/bin/env python3
"""
德州扑克 Bot 天梯循环赛脚本

自动扫描所有 Bot，进行双向循环赛，计算 ELO 排名。
支持多进程并行执行，默认 10 个 worker。
输出全部放在 ladder_results/ 目录，不影响 results/。

用法:
    python engine/ladder.py -v                          # 全部 Bot，50 局/场，10 并行
    python engine/ladder.py -b 1 4 7 11 -n 20 -v       # 指定 Bot，20 局/场
    python engine/ladder.py -j 4 -v                     # 4 个并行 worker
    python engine/ladder.py --continue ladder_results/ladder_XXX/checkpoint.json -v
"""

from __future__ import print_function

import argparse
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
from datetime import datetime

# ── 项目路径 ─────────────────────────────────────────────────────────────────

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
LADDER_RESULTS_DIR = os.path.join(PROJECT_DIR, "ladder_results")

# ── ELO 参数 ────────────────────────────────────────────────────────────────────

INITIAL_RATING = 1200
K_NEW = 40
K_STABLE = 20
K_THRESHOLD = 30
EXPECTED_DENOM = 400

RANK_THRESHOLDS = [
    (2000, "王者"),
    (1800, "大师"),
    (1600, "钻石"),
    (1400, "铂金"),
    (1200, "黄金"),
    (1000, "白银"),
]


def get_rank_title(rating):
    for threshold, title in RANK_THRESHOLDS:
        if rating >= threshold:
            return title
    return "青铜"


def get_k_factor(games_played):
    return K_NEW if games_played < K_THRESHOLD else K_STABLE


def compute_elo(rating_a, rating_b, score_a, k_a, k_b):
    expected_a = 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / EXPECTED_DENOM))
    expected_b = 1.0 - expected_a
    score_b = 1.0 - score_a
    new_a = round(rating_a + k_a * (score_a - expected_a))
    new_b = round(rating_b + k_b * (score_b - expected_b))
    return new_a, new_b


# ── Bot 发现 ─────────────────────────────────────────────────────────────────

def discover_bots(bot_numbers=None):
    """扫描 bots/ 目录，返回 [(bot_number, bot_path), ...] 按编号排序"""
    bots_dir = os.path.join(PROJECT_DIR, "bots")
    found = []
    for fname in os.listdir(bots_dir):
        m = re.match(r"bot(\d+)$", fname)
        if m:
            num = int(m.group(1))
            main_py = os.path.join(bots_dir, fname, "main.py")
            if os.path.isfile(main_py) and (bot_numbers is None or num in bot_numbers):
                found.append((num, main_py))
    found.sort(key=lambda x: x[0])
    return found


# ── 循环赛配对 ────────────────────────────────────────────────────────────────

def generate_pairings(bots):
    """生成双向配对：N * (N-1) 场，每对 (i, j) 和 (j, i) 各一次"""
    pairings = []
    for i in range(len(bots)):
        for j in range(len(bots)):
            if i != j:
                pairings.append((i, j))
    return pairings


# ── 多进程 Worker ────────────────────────────────────────────────────────────

def _init_worker():
    """兼容串行模式：确保 ENGINE_DIR 在 sys.path 中"""
    if ENGINE_DIR not in sys.path:
        sys.path.insert(0, ENGINE_DIR)


def _run_matchup_subprocess(bot0_path, bot1_path, n_games, output_dir, timestamp):
    """启动独立子进程执行单场对战，子进程退出后内存自动回收。
    返回 (bot0_wins, bot1_wins, draws, chip_sum, n_games_actual, result_file) 或 None。"""
    # 写一个临时脚本让子进程执行
    script = (
        "import sys, os, json, gc\n"
        "sys.path.insert(0, {edir!r})\n"
        "from battle import mirror_battle as battle_func\n"
        "bot0_path, bot1_path, n_games, output_dir, timestamp = sys.argv[1:6]\n"
        "n_games = int(n_games)\n"
        "# 静默输出\n"
        "devnull = open(os.devnull, 'w')\n"
        "old_out, old_err = sys.stdout, sys.stderr\n"
        "sys.stdout, sys.stderr = devnull, devnull\n"
        "try:\n"
        "    match_wins, draws, n_played, all_logs = battle_func(bot0_path, bot1_path, n_games=n_games, verbose=False, save_log=True)\n"
        "finally:\n"
        "    sys.stdout, sys.stderr = old_out, old_err\n"
        "    devnull.close()\n"
        "b0w, b1w = match_wins[0], match_wins[1]\n"
        "# 保存日志\n"
        "import re\n"
        "def _bn(p):\n"
        "    m = re.search(r'bot(\\d+)', os.path.basename(os.path.dirname(p)))\n"
        "    return int(m.group(1)) if m else 0\n"
        "bot0_num, bot1_num = _bn(bot0_path), _bn(bot1_path)\n"
        "summary = dict(timestamp=timestamp, bot0=bot0_path, bot1=bot1_path, n_games=n_games, bot0_wins=b0w, bot1_wins=b1w, draws=draws, games=all_logs)\n"
        "fname = 'bot_{{}}_vs_bot_{{}}.json'.format(bot0_num, bot1_num)\n"
        "with open(os.path.join(output_dir, fname), 'w') as f:\n"
        "    json.dump(summary, f, ensure_ascii=False, indent=2)\n"
        "chip_sum = sum(g.get('bot0_chips', 0) for g in all_logs) if all_logs else 0\n"
        "n_actual = len(all_logs) if all_logs else 0\n"
        "del all_logs\n"
        "gc.collect()\n"
        "# 输出摘要到 stdout，主进程读取\n"
        "result = dict(b0w=b0w, b1w=b1w, draws=draws, chip_sum=chip_sum, n_actual=n_actual, fname=fname, bot0_num=bot0_num, bot1_num=bot1_num)\n"
        "print(json.dumps(result))\n"
    ).format(edir=ENGINE_DIR)

    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", script, bot0_path, bot1_path, str(n_games), output_dir, timestamp],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = proc.communicate(timeout=3600)  # 1h timeout
        if proc.returncode != 0:
            return None
        result = json.loads(stdout.decode().strip())
        return (result["b0w"], result["b1w"], result["draws"],
                result["chip_sum"], result["n_actual"], result["fname"],
                result["bot0_num"], result["bot1_num"])
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        return None


# ── 进程管理 ─────────────────────────────────────────────────────────────────

_pool_ref = [None]  # 全局引用，供信号处理器使用


def _kill_stale_ladder():
    """清理上次残留的 ladder.py 进程，防止孤儿 worker 占用资源。"""
    current_pid = os.getpid()
    my_sid = os.getsid(0)
    try:
        out = subprocess.Popen(
            ["pgrep", "-f", "ladder\\.py"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, _ = out.communicate(timeout=5)
        if out.returncode != 0:
            return
        pids = []
        for line in stdout.decode().strip().split('\n'):
            line = line.strip()
            if line:
                pid = int(line)
                # 跳过自己、同 session 的进程（自己刚启动的 worker）
                if pid == current_pid:
                    continue
                try:
                    if os.getsid(pid) == my_sid:
                        continue
                except (ProcessLookupError, PermissionError):
                    continue
                pids.append(pid)
        if not pids:
            return

        # 先 SIGTERM，再 SIGKILL
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(1)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        print("已清理 {} 个残留的 ladder 进程".format(len(pids)), flush=True)
    except Exception:
        pass


def _emergency_cleanup(signum, frame):
    """收到 SIGTERM/SIGINT 时立即清理子进程。"""
    if _pool_ref[0] is not None:
        try:
            _pool_ref[0].shutdown(wait=False)
        except Exception:
            pass
    # 杀掉所有子进程
    try:
        children = subprocess.Popen(
            ["pgrep", "-P", str(os.getpid())],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, _ = children.communicate(timeout=3)
        if children.returncode == 0:
            for line in stdout.decode().strip().split('\n'):
                line = line.strip()
                if line:
                    try:
                        os.kill(int(line), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
    except Exception:
        pass
    sys.exit(1)


# ── ELO 逐局更新 ─────────────────────────────────────────────────────────────

def update_elo_for_matchup(elo_ratings, games_played, bot0_num, bot1_num,
                           bot0_wins, bot1_wins, draws):
    """逐局更新 ELO，返回 (elo_before_0, elo_before_1, elo_after_0, elo_after_1)"""
    ra = elo_ratings[bot0_num]
    rb = elo_ratings[bot1_num]
    ga = games_played[bot0_num]
    gb = games_played[bot1_num]
    elo_before = (ra, rb)

    results = []
    for _ in range(bot0_wins):
        results.append("win")
    for _ in range(bot1_wins):
        results.append("loss")
    for _ in range(draws):
        results.append("draw")

    rng = random.Random(bot0_num * 1000 + bot1_num)
    rng.shuffle(results)

    for result in results:
        k_a = get_k_factor(ga)
        k_b = get_k_factor(gb)
        if result == "win":
            score_a = 1.0
        elif result == "loss":
            score_a = 0.0
        else:
            score_a = 0.5
        ra, rb = compute_elo(ra, rb, score_a, k_a, k_b)
        ga += 1
        gb += 1

    elo_ratings[bot0_num] = ra
    elo_ratings[bot1_num] = rb
    games_played[bot0_num] = ga
    games_played[bot1_num] = gb
    return elo_before[0], elo_before[1], ra, rb


# ── 统计累积 ─────────────────────────────────────────────────────────────────

def init_stats(bots):
    """初始化统计结构"""
    stats = {}
    for num, _ in bots:
        stats[num] = {
            "wins": 0, "losses": 0, "draws": 0,
            "total_chip_diff": 0.0, "games_played": 0,
            "opponents": {},
        }
    return stats


def update_stats(stats, bot0_num, bot1_num, bot0_wins, bot1_wins, draws, all_logs):
    """更新统计数据"""
    s0 = stats[bot0_num]
    s1 = stats[bot1_num]

    s0["wins"] += bot0_wins
    s0["losses"] += bot1_wins
    s0["draws"] += draws
    s0["games_played"] += bot0_wins + bot1_wins + draws

    s1["wins"] += bot1_wins
    s1["losses"] += bot0_wins
    s1["draws"] += draws
    s1["games_played"] += bot0_wins + bot1_wins + draws

    if all_logs:
        chip_sum = sum(g.get("bot0_chips", 0) for g in all_logs)
        s0["total_chip_diff"] += chip_sum
        s1["total_chip_diff"] -= chip_sum

    opp_key = str(bot1_num)
    if opp_key not in s0["opponents"]:
        s0["opponents"][opp_key] = {"wins": 0, "losses": 0, "draws": 0, "total_chip_diff": 0.0}
    s0["opponents"][opp_key]["wins"] += bot0_wins
    s0["opponents"][opp_key]["losses"] += bot1_wins
    s0["opponents"][opp_key]["draws"] += draws
    if all_logs:
        s0["opponents"][opp_key]["total_chip_diff"] += sum(
            g.get("bot0_chips", 0) for g in all_logs
        )

    opp_key2 = str(bot0_num)
    if opp_key2 not in s1["opponents"]:
        s1["opponents"][opp_key2] = {"wins": 0, "losses": 0, "draws": 0, "total_chip_diff": 0.0}
    s1["opponents"][opp_key2]["wins"] += bot1_wins
    s1["opponents"][opp_key2]["losses"] += bot0_wins
    s1["opponents"][opp_key2]["draws"] += draws
    if all_logs:
        s1["opponents"][opp_key2]["total_chip_diff"] -= sum(
            g.get("bot0_chips", 0) for g in all_logs
        )


# ── 存档 ─────────────────────────────────────────────────────────────────────

def save_battle_log(output_dir, bot0_num, bot1_num, bot0_path, bot1_path,
                    n_games, bot0_wins, bot1_wins, draws, all_logs, timestamp):
    """保存单场对战日志（battle 格式）"""
    summary = {
        "timestamp": timestamp,
        "bot0": bot0_path,
        "bot1": bot1_path,
        "n_games": n_games,
        "bot0_wins": bot0_wins,
        "bot1_wins": bot1_wins,
        "draws": draws,
        "games": all_logs,
    }
    fname = "bot_{}_vs_bot_{}.json".format(bot0_num, bot1_num)
    fpath = os.path.join(output_dir, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return fname


def save_checkpoint(output_dir, completed_matchups, elo_ratings, games_played,
                    stats, bots, n_games, pairings, timestamp, elo_history):
    """保存检查点"""
    checkpoint = {
        "timestamp": timestamp,
        "n_games": n_games,
        "completed": completed_matchups,
        "elo_ratings": elo_ratings,
        "games_played": games_played,
        "stats": stats,
        "pairings": [
            {"bot0_idx": i, "bot1_idx": j, "bot0_num": bots[i][0], "bot1_num": bots[j][0]}
            for i, j in pairings
        ],
        "elo_history": elo_history,
    }
    fpath = os.path.join(output_dir, "checkpoint.json")
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, ensure_ascii=False, indent=2)


def load_checkpoint(checkpoint_path):
    """加载检查点"""
    with open(checkpoint_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── 控制台输出 ───────────────────────────────────────────────────────────────

def print_progress(current, total, bot0_num, bot1_num, bot0_wins, bot1_wins, draws, n_games):
    """打印逐场进度"""
    line = "[{}/{}] bot_{} vs bot_{}: {}-{}-{} ({})".format(
        current, total, bot0_num, bot1_num,
        bot0_wins, bot1_wins, draws, n_games
    )
    print(line, flush=True)


def print_ladder(bots, elo_ratings, stats):
    """打印天梯排行榜"""
    print("=" * 72, flush=True)
    print("             德州扑克 Bot 天梯排行榜", flush=True)
    print("=" * 72, flush=True)
    print(
        "{:>4} | {:<8} | {:<4} | {:>4} | {:>3} | {:>3} | {:>3} | {:>6} | {:>8}".format(
            "排名", "Bot", "段位", "ELO", "胜", "负", "平", "胜率", "均筹码"
        ),
        flush=True,
    )
    print("-" * 72, flush=True)

    ranking = []
    for num, _ in bots:
        s = stats.get(num, {})
        elo = elo_ratings.get(num, INITIAL_RATING)
        wins = s.get("wins", 0)
        losses = s.get("losses", 0)
        draws_count = s.get("draws", 0)
        total = wins + losses + draws_count
        win_rate = wins / total if total > 0 else 0.0
        avg_chip = s.get("total_chip_diff", 0.0) / s.get("games_played", 1) if s.get("games_played", 0) > 0 else 0.0
        ranking.append((num, elo, wins, losses, draws_count, win_rate, avg_chip))
    ranking.sort(key=lambda x: -x[1])

    for rank, (num, elo, wins, losses, draws_count, win_rate, avg_chip) in enumerate(ranking, 1):
        title = get_rank_title(elo)
        chip_str = "{:+.0f}".format(avg_chip) if avg_chip != 0 else "0"
        print(
            " {:>2}  | bot_{:<4} | {:<4} | {:>4} | {:>3} | {:>3} | {:>3} | {:>5.1f}% | {:>8}".format(
                rank, num, title, elo, wins, losses, draws_count,
                win_rate * 100, chip_str
            ),
            flush=True,
        )
    print("=" * 72, flush=True)


def print_matchup_matrix(bots, matchup_results):
    """打印对战矩阵"""
    bot_nums = [num for num, _ in bots]
    n = len(bot_nums)
    if n == 0:
        return

    col_w = max(9, max(len("bot_{}".format(num)) for num in bot_nums) + 2)
    header_w = max(6, max(len("bot_{}".format(num)) for num in bot_nums) + 2)

    print(flush=True)
    print("对战矩阵（行主 vs 列客，格式: 胜-负-平）:", flush=True)

    header = " " * header_w
    for num in bot_nums:
        label = "bot_{}".format(num)
        header += " {:>{}}".format(label, col_w)
    print(header, flush=True)

    for row_num in bot_nums:
        row_label = "bot_{}".format(row_num)
        line = "{:<{}}".format(row_label, header_w)
        for col_num in bot_nums:
            if row_num == col_num:
                cell = "--"
            else:
                key = (row_num, col_num)
                res = matchup_results.get(key)
                if res:
                    cell = "{}-{}-{}".format(res[0], res[1], res[2])
                else:
                    cell = "?"
            line += " {:>{}}".format(cell, col_w)
        print(line, flush=True)
    print(flush=True)


# ── 最终报告 ─────────────────────────────────────────────────────────────────

def save_final_report(output_dir, bots, n_games, elo_ratings, games_played,
                      stats, matchup_details, elo_history, timestamp):
    """保存最终总结报告"""
    bot_numbers = [num for num, _ in bots]
    total_matchups = len(bot_numbers) * (len(bot_numbers) - 1)

    total_games = sum(
        d["bot0_wins"] + d["bot1_wins"] + d["draws"]
        for d in matchup_details
    )

    ranking = []
    for num, _ in bots:
        s = stats.get(num, {})
        elo = elo_ratings.get(num, INITIAL_RATING)
        wins = s.get("wins", 0)
        losses = s.get("losses", 0)
        draws_count = s.get("draws", 0)
        total = wins + losses + draws_count
        win_rate = wins / total if total > 0 else 0.0
        avg_chip = s.get("total_chip_diff", 0.0) / s.get("games_played", 1) if s.get("games_played", 0) > 0 else 0.0

        opp_data = {}
        for opp_str, opp_stats in s.get("opponents", {}).items():
            opp_total = opp_stats["wins"] + opp_stats["losses"] + opp_stats["draws"]
            opp_avg_chip = opp_stats["total_chip_diff"] / opp_total if opp_total > 0 else 0.0
            opp_data[opp_str] = {
                "wins": opp_stats["wins"],
                "losses": opp_stats["losses"],
                "draws": opp_stats["draws"],
                "avg_chip_diff": round(opp_avg_chip, 1),
            }

        ranking.append({
            "bot_number": num,
            "elo": elo,
            "rank_title": get_rank_title(elo),
            "wins": wins,
            "losses": losses,
            "draws": draws_count,
            "win_rate": round(win_rate, 3),
            "avg_chip_diff": round(avg_chip, 1),
            "games_played": s.get("games_played", 0),
            "opponents": opp_data,
        })
    ranking.sort(key=lambda x: -x["elo"])
    for i, r in enumerate(ranking):
        r["rank"] = i + 1

    report = {
        "type": "ladder_report",
        "timestamp": timestamp,
        "config": {
            "games_per_matchup": n_games,
            "bot_numbers": bot_numbers,
            "total_matchups": total_matchups,
            "total_games_played": total_games,
        },
        "ladder": ranking,
        "matchups": matchup_details,
        "elo_history": elo_history,
    }

    fpath = os.path.join(output_dir, "ladder_report.json")
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return fpath


# ── 单场结果处理（提取为函数供串行/并行共用）───────────────────────────────────

def _process_result(idx, bots, pairings, n_games, output_dir, timestamp,
                    bot0_wins, bot1_wins, draws, chip_sum, n_games_actual, result_file,
                    elo_ratings, games_played, stats, elo_history,
                    completed_matchups, matchup_results, matchup_details):
    """处理一场对战结果：更新 ELO、更新统计、保存检查点"""
    i, j = pairings[idx]
    bot0_num, bot0_path = bots[i]
    bot1_num, bot1_path = bots[j]

    # 更新 ELO
    elo_b0, elo_b1, elo_a0, elo_a1 = update_elo_for_matchup(
        elo_ratings, games_played, bot0_num, bot1_num,
        bot0_wins, bot1_wins, draws
    )

    # 记录 ELO 历史
    elo_history[bot0_num].append(elo_ratings[bot0_num])
    elo_history[bot1_num].append(elo_ratings[bot1_num])

    # 更新统计（用 chip_sum 替代遍历 all_logs）
    s0 = stats[bot0_num]
    s1 = stats[bot1_num]
    total = bot0_wins + bot1_wins + draws

    s0["wins"] += bot0_wins
    s0["losses"] += bot1_wins
    s0["draws"] += draws
    s0["games_played"] += total
    s0["total_chip_diff"] += chip_sum

    s1["wins"] += bot1_wins
    s1["losses"] += bot0_wins
    s1["draws"] += draws
    s1["games_played"] += total
    s1["total_chip_diff"] -= chip_sum

    opp_key = str(bot1_num)
    if opp_key not in s0["opponents"]:
        s0["opponents"][opp_key] = {"wins": 0, "losses": 0, "draws": 0, "total_chip_diff": 0.0}
    s0["opponents"][opp_key]["wins"] += bot0_wins
    s0["opponents"][opp_key]["losses"] += bot1_wins
    s0["opponents"][opp_key]["draws"] += draws
    s0["opponents"][opp_key]["total_chip_diff"] += chip_sum

    opp_key2 = str(bot0_num)
    if opp_key2 not in s1["opponents"]:
        s1["opponents"][opp_key2] = {"wins": 0, "losses": 0, "draws": 0, "total_chip_diff": 0.0}
    s1["opponents"][opp_key2]["wins"] += bot1_wins
    s1["opponents"][opp_key2]["losses"] += bot0_wins
    s1["opponents"][opp_key2]["draws"] += draws
    s1["opponents"][opp_key2]["total_chip_diff"] -= chip_sum

    # 记录对战结果
    matchup_results[(bot0_num, bot1_num)] = (bot0_wins, bot1_wins, draws)

    # 平均筹码
    avg_chip_0 = chip_sum / max(n_games_actual, 1) if n_games_actual > 0 else 0.0

    matchup_details.append({
        "bot0": bot0_path,
        "bot1": bot1_path,
        "bot0_wins": bot0_wins,
        "bot1_wins": bot1_wins,
        "draws": draws,
        "bot0_avg_chips": round(avg_chip_0, 1),
        "bot1_avg_chips": round(-avg_chip_0, 1),
        "result_file": result_file,
        "elo_before": [elo_b0, elo_b1],
        "elo_after": [elo_a0, elo_a1],
    })

    # 标记完成
    key = "{}_vs_{}".format(bot0_num, bot1_num)
    completed_matchups[key] = True

    return bot0_num, bot1_num


# ── 主流程 ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="德州扑克 Bot 天梯循环赛")
    parser.add_argument("-n", "--games", type=int, default=50,
                        help="每场对战局数（默认 50）")
    parser.add_argument("-b", "--bots", type=int, nargs="+", default=None,
                        help="指定 Bot 编号，如 -b 1 4 7")
    parser.add_argument("--continue", dest="continue_from", default=None,
                        help="检查点文件路径，用于断点续跑")
    parser.add_argument("-j", "--jobs", type=int, default=8,
                        help="并行 worker 数量（默认 8，设为 1 则串行）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="打印逐场进度")
    args = parser.parse_args()

    n_jobs = max(1, args.jobs)

    # 清理残留的 ladder 进程（防止孤儿 worker）
    _kill_stale_ladder()

    # 注册信号处理，确保子进程随主进程一起退出
    signal.signal(signal.SIGTERM, _emergency_cleanup)
    signal.signal(signal.SIGINT, _emergency_cleanup)

    # 发现 Bot
    bots = discover_bots(args.bots)
    if not bots:
        print("未发现任何 Bot", flush=True)
        sys.exit(1)

    n_bots = len(bots)
    if n_bots < 2:
        print("至少需要 2 个 Bot 进行循环赛", flush=True)
        sys.exit(1)

    n_games = args.games
    pairings = generate_pairings(bots)
    total_matchups = len(pairings)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = os.path.join(LADDER_RESULTS_DIR, "ladder_" + timestamp)

    # 初始化状态
    completed_matchups = {}
    elo_ratings = {}
    games_played = {}
    stats = init_stats(bots)
    matchup_details = []
    matchup_results = {}
    elo_history = {}

    for num, _ in bots:
        elo_ratings[num] = INITIAL_RATING
        games_played[num] = 0
        elo_history[num] = [INITIAL_RATING]

    start_from_idx = 0

    if args.continue_from:
        print("从检查点恢复: {}".format(args.continue_from), flush=True)
        ckpt = load_checkpoint(args.continue_from)
        output_dir = os.path.dirname(args.continue_from)
        timestamp = ckpt.get("timestamp", timestamp)

        elo_ratings = {int(k): v for k, v in ckpt.get("elo_ratings", {}).items()}
        games_played = {int(k): v for k, v in ckpt.get("games_played", {}).items()}
        completed = ckpt.get("completed", {})
        completed_matchups = {k: True for k in completed}
        stats = {}
        for k, v in ckpt.get("stats", {}).items():
            stats[int(k)] = v
        for num, _ in bots:
            if num not in stats:
                stats[num] = {"wins": 0, "losses": 0, "draws": 0,
                              "total_chip_diff": 0.0, "games_played": 0,
                              "opponents": {}}

        elo_history = {}
        for k, v in ckpt.get("elo_history", {}).items():
            elo_history[int(k)] = v

        # 为新加入的 Bot 补齐默认值
        for num, _ in bots:
            if num not in elo_ratings:
                elo_ratings[num] = INITIAL_RATING
            if num not in games_played:
                games_played[num] = 0
            if num not in elo_history:
                elo_history[num] = [INITIAL_RATING]

        for idx, (i, j) in enumerate(pairings):
            key = "{}_vs_{}".format(bots[i][0], bots[j][0])
            if key not in completed_matchups:
                start_from_idx = idx
                break
        else:
            start_from_idx = total_matchups
            print("所有对战已完成", flush=True)

        for key in completed_matchups:
            parts = key.split("_vs_")
            b0, b1 = int(parts[0]), int(parts[1])
            fname = "bot_{}_vs_bot_{}.json".format(b0, b1)
            fpath = os.path.join(output_dir, fname)
            if os.path.exists(fpath):
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    matchup_details.append({
                        "bot0": "bots/bot{}/main.py".format(b0),
                        "bot1": "bots/bot{}/main.py".format(b1),
                        "bot0_wins": data.get("bot0_wins", 0),
                        "bot1_wins": data.get("bot1_wins", 0),
                        "draws": data.get("draws", 0),
                        "bot0_avg_chips": round(
                            sum(g.get("bot0_chips", 0) for g in data.get("games", []))
                            / max(len(data.get("games", [])), 1), 1
                        ),
                        "bot1_avg_chips": round(
                            sum(g.get("bot1_chips", 0) for g in data.get("games", []))
                            / max(len(data.get("games", [])), 1), 1
                        ),
                        "result_file": fname,
                        "elo_before": [elo_ratings.get(b0, INITIAL_RATING),
                                       elo_ratings.get(b1, INITIAL_RATING)],
                        "elo_after": [elo_ratings.get(b0, INITIAL_RATING),
                                      elo_ratings.get(b1, INITIAL_RATING)],
                    })
                    matchup_results[(b0, b1)] = (
                        data.get("bot0_wins", 0),
                        data.get("bot1_wins", 0),
                        data.get("draws", 0),
                    )

    # 创建输出目录
    os.makedirs(output_dir, exist_ok=True)

    # 收集待执行的配对
    pending_indices = []
    for idx in range(start_from_idx, total_matchups):
        i, j = pairings[idx]
        key = "{}_vs_{}".format(bots[i][0], bots[j][0])
        if key not in completed_matchups:
            pending_indices.append(idx)

    n_pending = len(pending_indices)
    n_done = total_matchups - n_pending

    print("=" * 72, flush=True)
    print("德州扑克 Bot 天梯循环赛", flush=True)
    print("=" * 72, flush=True)
    print("Bot 数量: {}".format(n_bots), flush=True)
    print("每场局数: {}".format(n_games), flush=True)
    print("总对战场次: {}".format(total_matchups), flush=True)
    print("并行 worker: {}".format(n_jobs), flush=True)
    print("输出目录: {}".format(output_dir), flush=True)
    if args.continue_from:
        print("已恢复完成: {} 场".format(n_done), flush=True)
        print("待进行: {} 场".format(n_pending), flush=True)
    print(flush=True)

    if n_pending == 0:
        print("所有对战已完成，直接输出结果", flush=True)
    elif n_jobs == 1:
        # ── 串行模式 ─────────────────────────────────────────────────────
        for count, idx in enumerate(pending_indices):
            i, j = pairings[idx]
            bot0_num, bot0_path = bots[i]
            bot1_num, bot1_path = bots[j]

            if args.verbose:
                print("[{}/{}] bot_{} vs bot_{} ...".format(
                    count + 1, n_pending, bot0_num, bot1_num
                ), end="", flush=True)

            sys.path.insert(0, ENGINE_DIR)
            try:
                from battle import mirror_battle as battle_func
                _wins, dr, _played, logs = battle_func(
                    bot0_path, bot1_path, n_games=n_games,
                    verbose=False, debug_bots=None, save_log=True
                )
                b0w, b1w = _wins[0], _wins[1]
                chip_sum = sum(g.get("bot0_chips", 0) for g in logs) if logs else 0
                n_actual = len(logs) if logs else 0
                # 串行模式也保存日志文件
                result_file = save_battle_log(
                    output_dir, bot0_num, bot1_num, bot0_path, bot1_path,
                    n_games, b0w, b1w, dr, logs, datetime.now().strftime("%Y%m%d_%H%M%S")
                )
                del logs
            finally:
                if PROJECT_DIR in sys.path:
                    sys.path.remove(PROJECT_DIR)

            if args.verbose:
                print(" {}-{}-{}".format(b0w, b1w, dr), flush=True)
            else:
                print_progress(count + 1, n_pending, bot0_num, bot1_num,
                               b0w, b1w, dr, n_games)

            _process_result(
                idx, bots, pairings, n_games, output_dir, timestamp,
                b0w, b1w, dr, chip_sum, n_actual, result_file,
                elo_ratings, games_played, stats, elo_history,
                completed_matchups, matchup_results, matchup_details
            )
            save_checkpoint(
                output_dir, completed_matchups, elo_ratings, games_played,
                stats, bots, n_games, pairings, timestamp, elo_history
            )
    else:
        # ── 并行模式：用独立子进程，每场打完进程退出，内存自动回收 ─────
        import threading
        import gc

        result_queue = []  # [(pairing_idx, result_tuple)]
        queue_lock = threading.Lock()
        processed_count = 0

        def _worker(pair_idx, bot0_path, bot1_path):
            """线程函数：启动子进程跑一场对战"""
            res = _run_matchup_subprocess(bot0_path, bot1_path, n_games, output_dir, timestamp)
            with queue_lock:
                result_queue.append((pair_idx, res))

        # 启动 worker 线程（每个线程管理一个子进程）
        active = {}  # {pairing_idx: thread}
        pending_iter = iter(pending_indices)
        consume_set = set()

        # 初始提交 n_jobs 个任务
        for _ in range(min(n_jobs, n_pending)):
            idx = next(pending_iter, None)
            if idx is None:
                break
            i, j = pairings[idx]
            _, bot0_path = bots[i]
            _, bot1_path = bots[j]
            t = threading.Thread(target=_worker, args=(idx, bot0_path, bot1_path))
            t.daemon = True
            active[idx] = t
            t.start()

        while processed_count < n_pending:
            # 收集已完成的结果
            with queue_lock:
                ready = [(idx, res) for idx, res in result_queue if idx not in consume_set]
                for idx, _ in ready:
                    consume_set.add(idx)
                result_queue[:] = [(idx, res) for idx, res in result_queue if idx not in consume_set]

            for pair_idx, res in ready:
                # 清理对应线程
                if pair_idx in active:
                    active[pair_idx].join(timeout=1)
                    del active[pair_idx]

                # 启动下一个任务补位
                next_idx = next(pending_iter, None)
                if next_idx is not None:
                    i, j = pairings[next_idx]
                    _, b0p = bots[i]
                    _, b1p = bots[j]
                    t = threading.Thread(target=_worker, args=(next_idx, b0p, b1p))
                    t.daemon = True
                    active[next_idx] = t
                    t.start()

                ii, jj = pairings[pair_idx]
                bot0_num = bots[ii][0]
                bot1_num = bots[jj][0]
                processed_count += 1

                if res is None:
                    print("[!] bot_{} vs bot_{} 执行失败".format(bot0_num, bot1_num), flush=True)
                    # 标记为跳过
                    key = "{}_vs_{}".format(bot0_num, bot1_num)
                    completed_matchups[key] = True
                    save_checkpoint(
                        output_dir, completed_matchups, elo_ratings, games_played,
                        stats, bots, n_games, pairings, timestamp, elo_history
                    )
                    continue

                b0w, b1w, dr, chip_sum, n_actual, fname, _, _ = res

                print_progress(processed_count, n_pending, bot0_num, bot1_num,
                               b0w, b1w, dr, n_games)

                _process_result(
                    pair_idx, bots, pairings, n_games, output_dir, timestamp,
                    b0w, b1w, dr, chip_sum, n_actual, fname,
                    elo_ratings, games_played, stats, elo_history,
                    completed_matchups, matchup_results, matchup_details
                )
                save_checkpoint(
                    output_dir, completed_matchups, elo_ratings, games_played,
                    stats, bots, n_games, pairings, timestamp, elo_history
                )

                gc.collect()

            if not ready:
                time.sleep(0.5)

    # 打印结果
    print(flush=True)
    print_ladder(bots, elo_ratings, stats)
    print_matchup_matrix(bots, matchup_results)

    # 保存最终报告
    report_path = save_final_report(
        output_dir, bots, n_games, elo_ratings, games_played,
        stats, matchup_details, elo_history, timestamp
    )
    print("最终报告已保存: {}".format(report_path), flush=True)
    print("所有输出目录: {}".format(output_dir), flush=True)


if __name__ == "__main__":
    main()
