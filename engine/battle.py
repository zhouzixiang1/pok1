#!/usr/bin/env python3
"""通用 bot 对战脚本 — 支持持久化进程模式以提升对局速度"""
import json
import subprocess
import sys
import os
import argparse
import select
import threading
from datetime import datetime

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(ENGINE_DIR)
RESULTS_DIR = os.path.join(PROJECT_DIR, "results")


def _call_bot_subprocess(bot_path, payload):
    """启动子进程调用 bot，通过 stdin/stdout 通信。返回 (action, verdict, data)。"""
    try:
        proc = subprocess.run(
            [sys.executable, bot_path],
            input=json.dumps(payload, separators=(',', ':')),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            return -1, "EXIT({})".format(proc.returncode), None
        result = json.loads(proc.stdout.strip())
        action = int(result.get("response", -1))
        bot_data = result.get("data")
        return action, "OK", bot_data
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT", None
    except Exception as e:
        return -1, "CRASH: {}".format(e), None


class _PersistentBot:
    """Persistent bot process — one Popen per game, line-delimited JSON communication."""

    def __init__(self, bot_path):
        self.bot_path = bot_path
        self.proc = None
        self._alive = False
        self._start()

    def _start(self):
        try:
            self.proc = subprocess.Popen(
                [sys.executable, self.bot_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self._alive = True
        except Exception:
            self._alive = False

    def call(self, payload):
        if not self._alive:
            self._start()
            if not self._alive:
                return -1, "CRASH: process not started", None
        try:
            line = json.dumps(payload, separators=(',', ':'))
            self.proc.stdin.write(line + '\n')
            self.proc.stdin.flush()
        except Exception:
            self._alive = False
            return -1, "CRASH: stdin write failed", None

        result_line = [None]
        error = [None]

        def _read():
            try:
                result_line[0] = self.proc.stdout.readline()
            except Exception as e:
                error[0] = e

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout=30)

        if t.is_alive():
            self._alive = False
            try:
                self.proc.kill()
            except Exception:
                pass
            return -1, "TIMEOUT", None

        if error[0] is not None:
            self._alive = False
            return -1, "CRASH: {}".format(error[0]), None

        if not result_line[0]:
            self._alive = False
            return -1, "EOF", None

        try:
            result = json.loads(result_line[0].strip())
            action = int(result.get("response", -1))
            bot_data = result.get("data")
            return action, "OK", bot_data
        except Exception as e:
            self._alive = False
            return -1, "CRASH: {}".format(e), None

    def close(self):
        self._alive = False
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    pass


def _call_bot(bot_paths, player_id, request_data, bot_requests, bot_responses,
              debug_bots=None, bot_data=None, persistent_procs=None):
    """构造 payload 并调用 bot。返回 (response, verdict, stderr_output)。
    persistent_procs: list[_PersistentBot|None] — 若非 None 则使用持久化进程。"""
    bot_requests[player_id].append(request_data)
    payload = {
        "requests": list(bot_requests[player_id]),
        "responses": list(bot_responses[player_id]),
    }
    if bot_data is not None and bot_data[player_id] is not None:
        payload["data"] = bot_data[player_id]

    # debug 模式下捕获 stderr — 必须用子进程模式
    capture_stderr = debug_bots is not None and player_id in debug_bots

    _returned_data = None
    stderr_output = ""

    if capture_stderr:
        try:
            proc = subprocess.run(
                [sys.executable, bot_paths[player_id]],
                input=json.dumps(payload, separators=(',', ':')),
                capture_output=True,
                text=True,
                timeout=30,
            )
            stderr_output = proc.stderr or ""
            if proc.returncode != 0:
                action, verdict = -1, "EXIT({})".format(proc.returncode)
            else:
                result = json.loads(proc.stdout.strip())
                action = int(result.get("response", -1))
                verdict = "OK"
                _returned_data = result.get("data")
        except subprocess.TimeoutExpired:
            action, verdict = -1, "TIMEOUT"
        except Exception as e:
            action, verdict = -1, "CRASH: {}".format(e)
    elif persistent_procs and persistent_procs[player_id]:
        action, verdict, _returned_data = persistent_procs[player_id].call(payload)
    else:
        action, verdict, _returned_data = _call_bot_subprocess(bot_paths[player_id], payload)

    bot_responses[player_id].append(action)
    if bot_data is not None and _returned_data is not None:
        bot_data[player_id] = _returned_data
    return action, verdict, stderr_output


def battle(bot0_path, bot1_path, n_games=50, verbose=False, debug_bots=None, save_log=False):
    """两个 bot 对战 n_games 局，每局 50 手牌，按筹码总量判胜负"""
    if ENGINE_DIR not in sys.path:
        sys.path.insert(0, ENGINE_DIR)
    from judge import judge as judge_func

    bot_paths = [os.path.abspath(bot0_path), os.path.abspath(bot1_path)]
    if debug_bots is None:
        debug_bots = set()

    use_persistent = not debug_bots
    match_wins = [0, 0]  # 赢了多少局
    draws = 0
    n_played = 0
    all_logs = []  # 每局日志

    for game in range(n_games):
        # Create persistent bots per game
        persistent = None
        if use_persistent:
            persistent = [_PersistentBot(bot_paths[0]), _PersistentBot(bot_paths[1])]

        result_str = judge_func(json.dumps({"log": []}))
        result = json.loads(result_str)
        log = [{"output": result}]
        initdata = result.get("initdata")
        n_played += 1
        # 每局重置 requests/responses 历史
        bot_requests = [[], []]
        bot_responses = [[], []]
        bot_data = [None, None]

        while result.get("command") == "request":
            content = result.get("content", {})
            if not content:
                break
            player_id = int(next(iter(content.keys())))
            request_data = content[str(player_id)]

            response, _, stderr_out = _call_bot(
                bot_paths, player_id, request_data,
                bot_requests, bot_responses, debug_bots, bot_data,
                persistent_procs=persistent,
            )

            # 打印 debug 输出
            if stderr_out:
                print("[Game {} P{}] {}".format(game + 1, player_id, stderr_out.strip()),
                      file=sys.stderr)

            log.append({str(player_id): {"response": str(response), "verdict": "OK"}, "output": None})

            result_str = judge_func(json.dumps({"log": log, "initdata": initdata}))
            result = json.loads(result_str)
            log.append({"output": result})

            if result.get("command") == "finish":
                break

        # Close persistent bots
        if persistent:
            for p in persistent:
                if p:
                    p.close()

        if result.get("command") == "finish":
            display = result.get("display", {})
            if "final_result" in display:
                chips = [r["win_chips"] for r in display["final_result"]]
                if chips[0] > chips[1]:
                    match_wins[0] += 1
                elif chips[1] > chips[0]:
                    match_wins[1] += 1
                else:
                    draws += 1
        else:
            chips = [0, 0]

        if save_log:
            all_logs.append({
                "game": game,
                "winner": 0 if chips[0] > chips[1] else (1 if chips[1] > chips[0] else -1),
                "bot0_chips": chips[0],
                "bot1_chips": chips[1],
                "logs": log,
            })

        if verbose and (game + 1) % 10 == 0:
            print("  已完成 {}/{} 局".format(game + 1, n_games), file=sys.stderr)

    return match_wins, draws, n_played, all_logs


def mirror_battle(bot0_path, bot1_path, n_games=50, verbose=False, save_log=False):
    """镜像对战：每局先正常打一次，再用交换手牌后的牌堆打一次。
    返回 (bot0_wins, bot1_wins, draws, n_games_actual, all_logs)。
    胜负按镜像对（两局）的筹码差判定：若正局 bot0 赢 3000、镜像局 bot0 输 1000，
    则 bot0 净赢 2000，记为 bot0 胜。
    使用持久化 bot 进程加速（每局游戏一个进程，多决策复用）。"""
    if ENGINE_DIR not in sys.path:
        sys.path.insert(0, ENGINE_DIR)
    from judge import judge as judge_func

    bot_paths = [os.path.abspath(bot0_path), os.path.abspath(bot1_path)]
    match_wins = [0, 0]
    draws = 0
    n_played = 0
    all_logs = []

    for game in range(n_games):
        # Create persistent bots for this game (reused across normal + mirror)
        persistent = [_PersistentBot(bot_paths[0]), _PersistentBot(bot_paths[1])]
        try:
            # ── 正局：正常发牌 ──
            result_str = judge_func(json.dumps({"log": []}))
            result = json.loads(result_str)
            log = [{"output": result}]
            initdata = result.get("initdata")
            bot_requests = [[], []]
            bot_responses = [[], []]
            bot_data = [None, None]

            while result.get("command") == "request":
                content = result.get("content", {})
                if not content:
                    break
                player_id = int(next(iter(content.keys())))
                request_data = content[str(player_id)]
                response, _, _ = _call_bot(bot_paths, player_id, request_data, bot_requests, bot_responses,
                                           bot_data=bot_data, persistent_procs=persistent)
                log.append({str(player_id): {"response": str(response), "verdict": "OK"}, "output": None})
                result_str = judge_func(json.dumps({"log": log, "initdata": initdata}))
                result = json.loads(result_str)
                log.append({"output": result})
                if result.get("command") == "finish":
                    break

            if result.get("command") != "finish":
                continue

            chips_normal = [r["win_chips"] for r in result.get("display", {}).get("final_result", [])]
            if len(chips_normal) < 2:
                continue

            if save_log:
                all_logs.append({
                    "game": game * 2, "mirror": False,
                    "winner": 0 if chips_normal[0] > chips_normal[1] else (1 if chips_normal[1] > chips_normal[0] else -1),
                    "bot0_chips": chips_normal[0], "bot1_chips": chips_normal[1],
                    "logs": log,
                })

            # ── 镜像局：交换手牌的牌堆 ──
            mirror_initdata = {
                "max_hand": initdata["max_hand"],
                "dealer": (initdata["dealer"] + 1) % 2,
                "decks": [],
            }
            for deck in initdata["decks"]:
                # deck[-1],deck[-2] 是 player0 的牌，deck[-3],deck[-4] 是 player1 的牌
                # 交换两组，使 player0 拿到原 player1 的牌，反之亦然
                mirror_deck = deck[:-4] + deck[-2:] + deck[-4:-2]
                mirror_initdata["decks"].append(mirror_deck)

            result_str = judge_func(json.dumps({"log": [], "initdata": mirror_initdata}))
            result = json.loads(result_str)
            log_m = [{"output": result}]
            bot_requests_m = [[], []]
            bot_responses_m = [[], []]
            bot_data_m = [None, None]

            while result.get("command") == "request":
                content = result.get("content", {})
                if not content:
                    break
                player_id = int(next(iter(content.keys())))
                request_data = content[str(player_id)]
                response, _, _ = _call_bot(bot_paths, player_id, request_data, bot_requests_m, bot_responses_m,
                                           bot_data=bot_data_m, persistent_procs=persistent)
                log_m.append({str(player_id): {"response": str(response), "verdict": "OK"}, "output": None})
                result_str = judge_func(json.dumps({"log": log_m, "initdata": mirror_initdata}))
                result = json.loads(result_str)
                log_m.append({"output": result})
                if result.get("command") == "finish":
                    break

            if result.get("command") != "finish":
                continue

            chips_mirror = [r["win_chips"] for r in result.get("display", {}).get("final_result", [])]
            if len(chips_mirror) < 2:
                continue

            if save_log:
                all_logs.append({
                    "game": game * 2 + 1, "mirror": True,
                    "winner": 0 if chips_mirror[0] > chips_mirror[1] else (1 if chips_mirror[1] > chips_mirror[0] else -1),
                    "bot0_chips": chips_mirror[0], "bot1_chips": chips_mirror[1],
                    "logs": log_m,
                })

            # ── 镜像对合计 ──
            n_played += 1
            net_chips_0 = chips_normal[0] + chips_mirror[0]
            if net_chips_0 > 0:
                match_wins[0] += 1
            elif net_chips_0 < 0:
                match_wins[1] += 1
            else:
                draws += 1

            if verbose and (game + 1) % 10 == 0:
                print("  已完成 {}/{} 局(含镜像)".format(game + 1, n_games), file=sys.stderr)
        finally:
            for p in persistent:
                p.close()

    return match_wins, draws, n_played, all_logs


def battle_generator(bot0_path, bot1_path, n_games=1, save_log=True):
    """
    生成器版对战引擎。每步 yield 一个事件字典：
      {"type": "display", "game": int, "step": int, "display": {...}}
      {"type": "game_end", "game": int, "winner": int, "bot0_chips": float, "bot1_chips": float}
      {"type": "match_end", "bot0_wins": int, "bot1_wins": int, "draws": int, "saved_file": str|None}
    """
    if ENGINE_DIR not in sys.path:
        sys.path.insert(0, ENGINE_DIR)
    from judge import judge as judge_func

    bot_paths = [os.path.abspath(bot0_path), os.path.abspath(bot1_path)]
    match_wins = [0, 0]
    draws = 0
    all_logs = []

    for game_idx in range(n_games):
        result_str = judge_func(json.dumps({"log": []}))
        result = json.loads(result_str)
        log = [{"output": result}]
        initdata = result.get("initdata")
        step = 0
        # 每局重置 requests/responses 历史
        bot_requests = [[], []]
        bot_responses = [[], []]
        bot_data = [None, None]

        # yield 初始 display
        display = result.get("display")
        if display:
            yield {"type": "display", "game": game_idx, "step": step, "display": display}
            step += 1

        while result.get("command") == "request":
            content = result.get("content", {})
            if not content:
                break
            player_id = int(next(iter(content.keys())))
            request_data = content[str(player_id)]

            response, verdict, _ = _call_bot(bot_paths, player_id, request_data, bot_requests, bot_responses, bot_data=bot_data)

            log.append({str(player_id): {"response": str(response), "verdict": verdict}, "output": None})

            result_str = judge_func(json.dumps({"log": log, "initdata": initdata}))
            result = json.loads(result_str)
            log.append({"output": result})

            # yield 每步 display
            display = result.get("display")
            if display:
                yield {"type": "display", "game": game_idx, "step": step, "display": display}
                step += 1

            if result.get("command") == "finish":
                break

        # 判定单局胜负
        if result.get("command") == "finish":
            display = result.get("display", {})
            if "final_result" in display:
                chips = [r["win_chips"] for r in display["final_result"]]
            else:
                chips = [0, 0]
        else:
            chips = [0, 0]

        if chips[0] > chips[1]:
            winner = 0
            match_wins[0] += 1
        elif chips[1] > chips[0]:
            winner = 1
            match_wins[1] += 1
        else:
            winner = -1
            draws += 1

        if save_log:
            all_logs.append({
                "game": game_idx,
                "winner": winner,
                "bot0_chips": chips[0],
                "bot1_chips": chips[1],
                "logs": log,
            })

        yield {
            "type": "game_end",
            "game": game_idx,
            "winner": winner,
            "bot0_chips": chips[0],
            "bot1_chips": chips[1],
        }

    # 保存日志
    saved_file = None
    if save_log and all_logs:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        b0 = os.path.splitext(os.path.basename(bot0_path))[0]
        b1 = os.path.splitext(os.path.basename(bot1_path))[0]
        summary = {
            "timestamp": timestamp,
            "bot0": bot0_path,
            "bot1": bot1_path,
            "n_games": n_games,
            "bot0_wins": match_wins[0],
            "bot1_wins": match_wins[1],
            "draws": draws,
            "games": all_logs,
        }
        fname = "{}_{}_vs_{}.json".format(timestamp, b0, b1)
        fpath = os.path.join(RESULTS_DIR, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        saved_file = fname

    yield {
        "type": "match_end",
        "bot0_wins": match_wins[0],
        "bot1_wins": match_wins[1],
        "draws": draws,
        "saved_file": saved_file,
    }


def human_battle_generator(bot_path, n_games=1, save_log=True, human_player_id=0, human_sync=None):
    """
    人机对战生成器。人类玩家通过 human_sync 同步提交操作，Bot 自动决策。
    每步 yield 一个事件字典，与 battle_generator 相同格式，另增：
      {"type": "human_action_request", "game": int, "step": int,
       "request_data": {...}, "display": {...}}
    """
    if ENGINE_DIR not in sys.path:
        sys.path.insert(0, ENGINE_DIR)
    from judge import judge as judge_func

    bot_path_abs = os.path.abspath(bot_path)
    bot_paths = [None, None]
    bot_player_id = 1 - human_player_id
    bot_paths[bot_player_id] = bot_path_abs

    match_wins = [0, 0]
    draws = 0
    all_logs = []

    for game_idx in range(n_games):
        result_str = judge_func(json.dumps({"log": []}))
        result = json.loads(result_str)
        log = [{"output": result}]
        initdata = result.get("initdata")
        step = 0
        # 每局重置 requests/responses 历史
        bot_requests = [[], []]
        bot_responses = [[], []]
        bot_data = [None, None]

        # yield 初始 display
        display = result.get("display")
        if display:
            yield {"type": "display", "game": game_idx, "step": step, "display": display}
            step += 1

        while result.get("command") == "request":
            content = result.get("content", {})
            if not content:
                break
            player_id = int(next(iter(content.keys())))
            request_data = content[str(player_id)]

            if player_id == human_player_id:
                # 人类玩家：发送请求并等待操作
                yield {
                    "type": "human_action_request",
                    "game": game_idx,
                    "step": step,
                    "request_data": request_data,
                    "display": result.get("display"),
                }
                response = human_sync.wait_for_action(timeout=300)
                verdict = "OK"
            else:
                # Bot 玩家：子进程自动决策
                response, verdict, _ = _call_bot(bot_paths, player_id, request_data, bot_requests, bot_responses, bot_data=bot_data)

            log.append({str(player_id): {"response": str(response), "verdict": verdict}, "output": None})

            result_str = judge_func(json.dumps({"log": log, "initdata": initdata}))
            result = json.loads(result_str)
            log.append({"output": result})

            # yield 每步 display
            display = result.get("display")
            if display:
                yield {"type": "display", "game": game_idx, "step": step, "display": display}
                step += 1

            if result.get("command") == "finish":
                break

        # 判定单局胜负
        if result.get("command") == "finish":
            display = result.get("display", {})
            if "final_result" in display:
                chips = [r["win_chips"] for r in display["final_result"]]
            else:
                chips = [0, 0]
        else:
            chips = [0, 0]

        if chips[0] > chips[1]:
            winner = 0
            match_wins[0] += 1
        elif chips[1] > chips[0]:
            winner = 1
            match_wins[1] += 1
        else:
            winner = -1
            draws += 1

        if save_log:
            all_logs.append({
                "game": game_idx,
                "winner": winner,
                "bot0_chips": chips[0],
                "bot1_chips": chips[1],
                "logs": log,
            })

        yield {
            "type": "game_end",
            "game": game_idx,
            "winner": winner,
            "bot0_chips": chips[0],
            "bot1_chips": chips[1],
        }

    # 保存日志
    saved_file = None
    if save_log and all_logs:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        b1 = os.path.splitext(os.path.basename(bot_path))[0]
        summary = {
            "timestamp": timestamp,
            "bot0": "Human" if human_player_id == 0 else bot_path,
            "bot1": bot_path if human_player_id == 0 else "Human",
            "n_games": n_games,
            "bot0_wins": match_wins[0],
            "bot1_wins": match_wins[1],
            "draws": draws,
            "games": all_logs,
        }
        fname = "{}_Human_vs_{}.json".format(timestamp, b1)
        fpath = os.path.join(RESULTS_DIR, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        saved_file = fname

    yield {
        "type": "match_end",
        "bot0_wins": match_wins[0],
        "bot1_wins": match_wins[1],
        "draws": draws,
        "saved_file": saved_file,
    }


def main():
    parser = argparse.ArgumentParser(description="Bot 对战")
    parser.add_argument("bot0", nargs="?", default="bots/bot20/main.py", help="Bot 0 路径 (默认: bots/bot20/main.py)")
    parser.add_argument("bot1", nargs="?", default="bots/bot26/main.py", help="Bot 1 路径 (默认: bots/bot26/main.py)")
    parser.add_argument("-n", "--games", type=int, default=1, help="对战局数")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细输出")
    parser.add_argument("-d", "--debug", action="store_true", help="打印 bot debug 信息(stderr)")
    args = parser.parse_args()

    print("Bot 0: {}".format(args.bot0))
    print("Bot 1: {}".format(args.bot1))
    print("局数: {}".format(args.games))
    print()

    debug_bots = {0, 1} if args.debug else None
    wins, draws, n, all_logs = battle(args.bot0, args.bot1, args.games, args.verbose, debug_bots, save_log=True)

    print("=" * 50)
    print("对战结果:")
    print("  Bot 0: 胜 {} 局".format(wins[0]))
    print("  Bot 1: 胜 {} 局".format(wins[1]))
    print("  平局: {} 局".format(draws))
    if wins[0] > wins[1]:
        print("胜者: Bot 0 ({})".format(args.bot0))
    elif wins[1] > wins[0]:
        print("胜者: Bot 1 ({})".format(args.bot1))
    else:
        print("总平局")

    # 保存日志
    saved_file = None
    if all_logs:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        b0 = os.path.splitext(os.path.basename(args.bot0))[0]
        b1 = os.path.splitext(os.path.basename(args.bot1))[0]
        summary = {
            "timestamp": timestamp,
            "bot0": args.bot0,
            "bot1": args.bot1,
            "n_games": n,
            "bot0_wins": wins[0],
            "bot1_wins": wins[1],
            "draws": draws,
            "games": all_logs,
        }
        fname = "{}_{}_vs_{}.json".format(timestamp, b0, b1)
        fpath = os.path.join(RESULTS_DIR, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        saved_file = fname
        print("日志已保存: {}".format(fpath))


if __name__ == "__main__":
    main()
