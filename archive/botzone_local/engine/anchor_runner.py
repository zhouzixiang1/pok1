#!/usr/bin/env python3
"""Run one anchor bot against every other local bot.

The runner keeps the fast in-process mirror-pair design from the one-off
temp/bot17_anchor-runner.py script, but makes the anchor bot and opponent set
configurable from the command line.
"""

from __future__ import print_function

import argparse
import concurrent.futures
import contextlib
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime

try:
    from tqdm import tqdm
except Exception:
    tqdm = None


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
BOTS_DIR = os.path.join(PROJECT_DIR, "bots")

# Main defaults. Edit this block for the common local benchmark setup.
CONFIG = {
    "default_anchor": "5",
    "default_mirror_pairs": 100,
    "default_workers": min(24, os.cpu_count() or 1),
    "output_root": os.path.join(PROJECT_DIR, "ladder_results"),
}


def _write_json(path, data):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, path)


def _bot_number(path):
    match = re.match(r"bot(\d+)$", os.path.basename(os.path.dirname(path)))
    return int(match.group(1)) if match else None


def _bot_label(path):
    num = _bot_number(path)
    if num is not None:
        return "bot_{}".format(num)
    name = os.path.splitext(os.path.basename(path))[0]
    return _slug(name) or "bot"


def _sort_key(path):
    num = _bot_number(path)
    return (0, num) if num is not None else (1, _bot_label(path))


def _slug(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value))
    value = value.strip("._-")
    return value


def _display_path(path):
    try:
        return os.path.relpath(path, PROJECT_DIR)
    except ValueError:
        return path


def _resolve_bot(token):
    token = str(token)
    candidates = []
    match = re.match(r"^(?:bot_?|#)?(\d+)$", token)
    if match:
        candidates.append(os.path.join(BOTS_DIR, "bot{}".format(int(match.group(1))), "main.py"))

    if os.path.isabs(token):
        candidates.append(token)
    else:
        candidates.append(os.path.join(PROJECT_DIR, token))
        candidates.append(os.path.abspath(token))

    seen = set()
    for candidate in candidates:
        candidate = os.path.abspath(candidate)
        if candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isfile(candidate):
            return candidate
    raise ValueError("bot not found: {}".format(token))


def _discover_bots():
    bots = []
    if not os.path.isdir(BOTS_DIR):
        return bots
    for fname in os.listdir(BOTS_DIR):
        if re.match(r"bot\d+$", fname):
            main_py = os.path.join(BOTS_DIR, fname, "main.py")
            if os.path.isfile(main_py):
                bots.append(os.path.abspath(main_py))
    bots.sort(key=_sort_key)
    return bots


def _worker_log_name(anchor_path, opponent_path):
    return "{}_vs_{}.worker.log".format(_bot_label(anchor_path), _bot_label(opponent_path))


def _match_json_name(anchor_path, opponent_path):
    return "{}_vs_{}.json".format(_bot_label(anchor_path), _bot_label(opponent_path))


def _summary_json_name(anchor_path, opponent_path):
    return "{}_vs_{}.summary.json".format(_bot_label(anchor_path), _bot_label(opponent_path))


def _mirror_initdata(initdata):
    if not initdata or "decks" not in initdata:
        raise ValueError("missing initdata/decks for mirror match")
    return {
        "max_hand": initdata["max_hand"],
        "dealer": (initdata["dealer"] + 1) % 2,
        "decks": [
            deck[:-4] + deck[-2:] + deck[-4:-2]
            for deck in initdata["decks"]
        ],
    }


def _play_match(bot_paths, initdata=None):
    from battle import _call_bot
    from judge import judge as judge_func

    if initdata is None:
        result_str = judge_func(json.dumps({"log": []}))
    else:
        result_str = judge_func(json.dumps({"log": [], "initdata": initdata}))
    result = json.loads(result_str)
    log = [{"output": result}]
    match_initdata = result.get("initdata")
    bot_requests = [[], []]
    bot_responses = [[], []]
    bot_data = [None, None]

    while result.get("command") == "request":
        content = result.get("content", {})
        if not content:
            break
        player_id = int(next(iter(content.keys())))
        request_data = content[str(player_id)]

        response, verdict, _ = _call_bot(
            bot_paths, player_id, request_data, bot_requests, bot_responses, bot_data=bot_data
        )
        log.append({
            str(player_id): {"response": str(response), "verdict": verdict},
            "output": None,
        })

        result_str = judge_func(json.dumps({"log": log, "initdata": match_initdata}))
        result = json.loads(result_str)
        log.append({"output": result})
        if result.get("command") == "finish":
            break

    chips = [0, 0]
    if result.get("command") == "finish":
        final_result = result.get("display", {}).get("final_result", [])
        if len(final_result) >= 2:
            chips = [final_result[0]["win_chips"], final_result[1]["win_chips"]]

    return {
        "chips": chips,
        "logs": log,
        "initdata": match_initdata,
        "finished": result.get("command") == "finish",
    }


def _run_mirror_pair(project_dir, anchor_path, opponent_path, pair_index, output_dir):
    started = time.time()
    os.chdir(project_dir)
    engine_dir = os.path.join(project_dir, "engine")
    if engine_dir not in sys.path:
        sys.path.insert(0, engine_dir)

    worker_log_path = os.path.join(output_dir, _worker_log_name(anchor_path, opponent_path))
    bot_paths = [os.path.abspath(anchor_path), os.path.abspath(opponent_path)]
    with open(worker_log_path, "a", encoding="utf-8") as worker_log:
        worker_log.write(
            "[{}] pair {} started\n".format(
                datetime.now().isoformat(timespec="seconds"), pair_index
            )
        )
        worker_log.flush()
        with contextlib.redirect_stdout(worker_log), contextlib.redirect_stderr(worker_log):
            normal = _play_match(bot_paths)
            mirror = _play_match(bot_paths, _mirror_initdata(normal["initdata"]))

    normal_chips = normal["chips"]
    mirror_chips = mirror["chips"]
    net_chips_0 = normal_chips[0] + mirror_chips[0]
    if net_chips_0 > 0:
        pair_winner = 0
    elif net_chips_0 < 0:
        pair_winner = 1
    else:
        pair_winner = -1

    game_base = pair_index * 2
    games = [
        {
            "game": game_base,
            "mirror": False,
            "pair_index": pair_index,
            "winner": 0 if normal_chips[0] > normal_chips[1] else (
                1 if normal_chips[1] > normal_chips[0] else -1
            ),
            "bot0_chips": normal_chips[0],
            "bot1_chips": normal_chips[1],
            "logs": normal["logs"],
        },
        {
            "game": game_base + 1,
            "mirror": True,
            "pair_index": pair_index,
            "winner": 0 if mirror_chips[0] > mirror_chips[1] else (
                1 if mirror_chips[1] > mirror_chips[0] else -1
            ),
            "bot0_chips": mirror_chips[0],
            "bot1_chips": mirror_chips[1],
            "logs": mirror["logs"],
        },
    ]
    return {
        "anchor": _bot_label(anchor_path),
        "opponent": _bot_label(opponent_path),
        "pair_index": pair_index,
        "pair_winner": pair_winner,
        "anchor_pair_wins": 1 if pair_winner == 0 else 0,
        "opponent_pair_wins": 1 if pair_winner == 1 else 0,
        "pair_draws": 1 if pair_winner == -1 else 0,
        "chip_sum": net_chips_0,
        "games": games,
        "elapsed_seconds": round(time.time() - started, 3),
    }


def _write_opponent_outputs(output_dir, anchor_path, opponent_path, pair_results, n_mirror_pairs):
    pair_results = sorted(pair_results, key=lambda x: x["pair_index"])
    games = []
    for item in pair_results:
        games.extend(item["games"])
    games.sort(key=lambda x: x["game"])

    anchor_pair_wins = sum(item["anchor_pair_wins"] for item in pair_results)
    opponent_pair_wins = sum(item["opponent_pair_wins"] for item in pair_results)
    pair_draws = sum(item["pair_draws"] for item in pair_results)
    chip_sum = sum(item["chip_sum"] for item in pair_results)
    actual_logged_games = len(games)
    avg_chip = chip_sum / max(actual_logged_games, 1)
    anchor_actual_wins = sum(1 for game in games if game.get("winner") == 0)
    opponent_actual_wins = sum(1 for game in games if game.get("winner") == 1)
    actual_draws = sum(1 for game in games if game.get("winner") == -1)

    log_file = _match_json_name(anchor_path, opponent_path)
    payload = {
        "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "mode": "anchor_parallel_mirror_pairs",
        "anchor": _bot_label(anchor_path),
        "opponent": _bot_label(opponent_path),
        "bot0": anchor_path,
        "bot1": opponent_path,
        "n_mirror_pairs": n_mirror_pairs,
        "actual_logged_games": actual_logged_games,
        "bot0_wins": anchor_actual_wins,
        "bot1_wins": opponent_actual_wins,
        "draws": actual_draws,
        "bot0_pair_wins": anchor_pair_wins,
        "bot1_pair_wins": opponent_pair_wins,
        "pair_draws": pair_draws,
        "games": games,
    }
    _write_json(os.path.join(output_dir, log_file), payload)

    total_games = anchor_actual_wins + opponent_actual_wins + actual_draws
    total_pairs = anchor_pair_wins + opponent_pair_wins + pair_draws
    summary = {
        "anchor": _bot_label(anchor_path),
        "anchor_num": _bot_number(anchor_path),
        "anchor_path": anchor_path,
        "opponent": _bot_label(opponent_path),
        "opponent_num": _bot_number(opponent_path),
        "opponent_path": opponent_path,
        "anchor_wins": anchor_actual_wins,
        "opponent_wins": opponent_actual_wins,
        "draws": actual_draws,
        "anchor_pair_wins": anchor_pair_wins,
        "opponent_pair_wins": opponent_pair_wins,
        "pair_draws": pair_draws,
        "n_mirror_pairs": n_mirror_pairs,
        "actual_logged_games": actual_logged_games,
        "anchor_win_rate": anchor_actual_wins / max(total_games, 1),
        "anchor_pair_win_rate": anchor_pair_wins / max(total_pairs, 1),
        "anchor_avg_chip_diff": avg_chip,
        "chip_sum": chip_sum,
        "log_file": log_file,
        "worker_log_file": _worker_log_name(anchor_path, opponent_path),
        "elapsed_seconds": round(sum(item["elapsed_seconds"] for item in pair_results), 3),
    }
    _write_json(os.path.join(output_dir, _summary_json_name(anchor_path, opponent_path)), summary)
    return summary


def _write_master_summary(output_dir, manifest, results, errors):
    ranked = sorted(
        results,
        key=lambda x: (x["anchor_pair_win_rate"], x["anchor_avg_chip_diff"], x["anchor_win_rate"]),
        reverse=True,
    )
    data = dict(manifest)
    data["results"] = sorted(results, key=lambda x: (_sort_optional_num(x["opponent_num"]), x["opponent"]))
    data["ranked_results"] = ranked
    data["errors"] = sorted(
        errors,
        key=lambda x: (x.get("opponent", ""), x.get("pair_index", -1) or -1),
    )
    data["finished_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json(os.path.join(output_dir, "summary.json"), data)


def _sort_optional_num(value):
    return value if value is not None else 10 ** 9


def _progress_write(progress, message):
    if progress is not None:
        progress.write(message)
    else:
        print(message, flush=True)


def _progress_update(progress, n):
    if progress is not None:
        progress.update(n)


def _progress_postfix(progress, message):
    if progress is not None:
        progress.set_postfix_str(message)


@contextlib.contextmanager
def _null_progress():
    yield None


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Run one anchor bot against all remaining bots with mirror pairs."
    )
    parser.add_argument(
        "anchor",
        nargs="?",
        default=CONFIG["default_anchor"],
        help=(
            "Anchor bot number, label, or path. Examples: 18, bot5, bots/bot5/main.py. "
            "Default: %(default)s"
        ),
    )
    parser.add_argument(
        "-o", "--opponents",
        nargs="+",
        help="Optional opponent bot numbers/labels/paths. Default: every discovered bot except anchor.",
    )
    parser.add_argument(
        "-x", "--exclude",
        nargs="+",
        default=[],
        help="Bot numbers/labels/paths to exclude from the auto-discovered opponent set.",
    )
    parser.add_argument(
        "-n", "--pairs",
        type=int,
        default=CONFIG["default_mirror_pairs"],
        help="Mirror pairs per opponent. Each pair logs two games. Default: %(default)s",
    )
    parser.add_argument(
        "-j", "--workers",
        type=int,
        default=CONFIG["default_workers"],
        help="Parallel workers per opponent. Default: %(default)s",
    )
    parser.add_argument(
        "--output-root",
        default=CONFIG["output_root"],
        help="Root directory for timestamped runs. Default: %(default)s",
    )
    parser.add_argument(
        "--output-dir",
        help="Exact output directory. Overrides --output-root.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved matchups without running games.",
    )
    return parser.parse_args(argv)


def _resolve_opponents(args, anchor_path):
    if args.opponents:
        opponents = [_resolve_bot(item) for item in args.opponents]
    else:
        opponents = _discover_bots()

    anchor_abs = os.path.abspath(anchor_path)
    excluded = set(os.path.abspath(_resolve_bot(item)) for item in args.exclude)
    result = []
    seen = set()
    for path in opponents:
        path = os.path.abspath(path)
        if path == anchor_abs or path in excluded or path in seen:
            continue
        seen.add(path)
        result.append(path)
    result.sort(key=_sort_key)
    return result


def _default_output_dir(anchor_path, output_root, timestamp):
    label = _bot_label(anchor_path)
    dir_label = label.replace("_", "") if re.match(r"bot_\d+$", label) else label
    return os.path.join(output_root, "{}_anchor_{}".format(dir_label, timestamp))


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.pairs <= 0:
        raise SystemExit("--pairs must be positive")
    if args.workers <= 0:
        raise SystemExit("--workers must be positive")

    try:
        anchor_path = _resolve_bot(args.anchor)
        opponents = _resolve_opponents(args, anchor_path)
    except ValueError as exc:
        raise SystemExit(str(exc))

    if not opponents:
        raise SystemExit("no opponents found")

    print("Anchor: {} ({})".format(_bot_label(anchor_path), _display_path(anchor_path)), flush=True)
    print(
        "Opponents: {}".format(
            ", ".join("{} ({})".format(_bot_label(p), _display_path(p)) for p in opponents)
        ),
        flush=True,
    )
    print(
        "Mirror pairs per opponent: {}; workers per opponent: {}".format(args.pairs, args.workers),
        flush=True,
    )
    if args.dry_run:
        return 0

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = os.path.abspath(
        args.output_dir or _default_output_dir(anchor_path, args.output_root, timestamp)
    )
    os.makedirs(output_dir, exist_ok=True)

    manifest = {
        "type": "anchor_serial_opponents_parallel_pairs",
        "timestamp": timestamp,
        "project_dir": PROJECT_DIR,
        "output_dir": output_dir,
        "anchor": _bot_label(anchor_path),
        "anchor_num": _bot_number(anchor_path),
        "anchor_path": anchor_path,
        "opponents": [
            {
                "label": _bot_label(path),
                "num": _bot_number(path),
                "path": path,
            }
            for path in opponents
        ],
        "n_mirror_pairs_per_opponent": args.pairs,
        "actual_games_per_opponent": args.pairs * 2,
        "match_workers": args.workers,
        "command": " ".join(sys.argv),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "results": [],
        "errors": [],
    }
    _write_json(os.path.join(output_dir, "summary.json"), manifest)

    print("OUTPUT_DIR={}".format(output_dir), flush=True)

    results = []
    errors = []
    total_actual_games = len(opponents) * args.pairs * 2

    progress_context = tqdm(
        total=total_actual_games,
        desc="{} mirror games".format(_bot_label(anchor_path)),
        unit="game",
        dynamic_ncols=True,
    ) if tqdm is not None else _null_progress()

    with progress_context as progress:
        for opponent_path in opponents:
            opponent_label = _bot_label(opponent_path)
            _progress_postfix(
                progress,
                "opponent={}, workers={}".format(opponent_label, args.workers),
            )
            pair_results = []

            with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as executor:
                future_to_pair = {
                    executor.submit(
                        _run_mirror_pair,
                        PROJECT_DIR,
                        anchor_path,
                        opponent_path,
                        pair_index,
                        output_dir,
                    ): pair_index
                    for pair_index in range(args.pairs)
                }
                for future in concurrent.futures.as_completed(future_to_pair):
                    pair_index = future_to_pair[future]
                    try:
                        pair_results.append(future.result())
                    except Exception:
                        error = {
                            "anchor": _bot_label(anchor_path),
                            "opponent": opponent_label,
                            "opponent_num": _bot_number(opponent_path),
                            "pair_index": pair_index,
                            "worker_log_file": _worker_log_name(anchor_path, opponent_path),
                            "traceback": traceback.format_exc(),
                        }
                        errors.append(error)
                        _progress_write(
                            progress,
                            "[ERROR] {} vs {} pair {} failed; worker_log={}".format(
                                _bot_label(anchor_path),
                                opponent_label,
                                pair_index,
                                error["worker_log_file"],
                            ),
                        )
                        _progress_write(progress, error["traceback"])

                    _progress_update(progress, 2)
                    _progress_postfix(
                        progress,
                        "opponent={}, pairs={}/{}, workers={}".format(
                            opponent_label, len(pair_results), args.pairs, args.workers
                        ),
                    )

            if pair_results:
                summary = _write_opponent_outputs(
                    output_dir, anchor_path, opponent_path, pair_results, args.pairs
                )
                results.append(summary)
                _progress_write(
                    progress,
                    "{} vs {}: actual {}-{}-{}, pair {}-{}-{}, avg_chip={:+.1f}, games={}, file={}".format(
                        summary["anchor"],
                        summary["opponent"],
                        summary["anchor_wins"],
                        summary["opponent_wins"],
                        summary["draws"],
                        summary["anchor_pair_wins"],
                        summary["opponent_pair_wins"],
                        summary["pair_draws"],
                        summary["anchor_avg_chip_diff"],
                        summary["actual_logged_games"],
                        summary["log_file"],
                    ),
                )
            else:
                errors.append({
                    "anchor": _bot_label(anchor_path),
                    "opponent": opponent_label,
                    "opponent_num": _bot_number(opponent_path),
                    "pair_index": None,
                    "worker_log_file": _worker_log_name(anchor_path, opponent_path),
                    "traceback": "all pair tasks failed",
                })

            _write_master_summary(output_dir, manifest, results, errors)

    ranked = sorted(
        results,
        key=lambda x: (x["anchor_pair_win_rate"], x["anchor_avg_chip_diff"], x["anchor_win_rate"]),
        reverse=True,
    )
    print("", flush=True)
    print("Final ranking by anchor pair win rate, then avg chip:", flush=True)
    for item in ranked:
        print(
            "{}: actual {}-{}-{}, pair {}-{}-{}, pair_win_rate={:.1%}, avg_chip={:+.1f}".format(
                item["opponent"],
                item["anchor_wins"],
                item["opponent_wins"],
                item["draws"],
                item["anchor_pair_wins"],
                item["opponent_pair_wins"],
                item["pair_draws"],
                item["anchor_pair_win_rate"],
                item["anchor_avg_chip_diff"],
            ),
            flush=True,
        )
    if errors:
        print("Errors: {}".format(len(errors)), flush=True)
    print("SUMMARY={}".format(os.path.join(output_dir, "summary.json")), flush=True)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
