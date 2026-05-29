"""
Background Rating Daemon for Poker Bot Evolution.

Continuously runs mirror battles between active bots. Uses per-game Elo
updates and maintains a Head-to-Head win/loss matrix. Continuous scheduling
eliminates idle cores.

Usage:
    python web/core/elo_daemon.py --pairs 5 --workers 14 --verbose
"""

import os
import sys
import json
import fcntl
import signal
import argparse
import time
from collections import Counter, deque
from contextlib import contextmanager
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime

CORE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CORE_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(CORE_DIR))

from glicko2 import Glicko2Player, update_single_game, decay_rd
from battle import mirror_battle

BOTS_DIR = PROJECT_ROOT / "bots"
RESULTS_DIR = CORE_DIR / "results"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
STATS_FILE = RESULTS_DIR / "elo_daemon_stats.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"
BOT_STATS_FILE = RESULTS_DIR / "bot_stats.json"
REPLAY_DIR = RESULTS_DIR / "match_replay"
MATCH_HISTORY_FILE = RESULTS_DIR / "match_history.jsonl"
MAX_REPLAY_FILES = 200

# Match selection priority weights
UNDER_EVAL_WEIGHT = 0.6
DIVERSITY_WEIGHT = 0.4
UNDER_EVAL_BASELINE = 50
RATING_GAP_SCALE = 200
DIVERSITY_COUNT_DECAY = 100
MAX_HISTORY_LINES = 200
HISTORY_KEEP_LINES = 100

# Continuous scheduling parameters
SAVE_EVERY_N_GAMES = 20
SAVE_INTERVAL_SEC = 60
POLL_TIMEOUT = 0.5

running = True


@contextmanager
def locked_file(path, mode='r', lock_type=None, encoding=None):
    if lock_type is None:
        lock_type = fcntl.LOCK_EX if ('w' in mode or 'a' in mode or '+' in mode) else fcntl.LOCK_SH
    open_kwargs = {}
    if encoding is not None:
        open_kwargs["encoding"] = encoding
    with open(path, mode, **open_kwargs) as f:
        fcntl.flock(f, lock_type)
        try:
            yield f
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def handle_signal(signum, frame):
    global running
    print(f"\n[DAEMON] Received signal {signum}, shutting down gracefully...")
    running = False
    try:
        os.killpg(os.getpgid(os.getpid()), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def get_active_bots():
    bots = []
    if BOTS_DIR.exists():
        for d in sorted(os.listdir(BOTS_DIR)):
            if d.startswith("claude_v") and os.path.isdir(BOTS_DIR / d):
                if (BOTS_DIR / d / ".completed").exists():
                    bots.append(d)
    return bots


def bot_path(bot_name):
    return str(BOTS_DIR / bot_name / "main.py")


def load_ratings():
    if not RATINGS_FILE.exists():
        return {}
    with locked_file(RATINGS_FILE, "r") as f:
        data = json.load(f)
    return {name: Glicko2Player.from_dict(d) for name, d in data.items()}


def save_ratings(ratings, save_num=None):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    data = {}
    for name, p in ratings.items():
        d = p.to_dict()
        d["last_period"] = datetime.now().isoformat(timespec="seconds")
        data[name] = d
    with locked_file(RATINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)

    if save_num is not None:
        history_file = RESULTS_DIR / "rating_history.jsonl"
        snapshot = {
            "period": save_num,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "ratings": {name: {"r": p.r, "rd": p.rd} for name, p in ratings.items()},
        }
        with locked_file(history_file, "a+") as f:
            f.write(json.dumps(snapshot) + "\n")
            f.flush()
            f.seek(0)
            lines = f.readlines()
            if len(lines) > MAX_HISTORY_LINES:
                f.seek(0)
                f.truncate()
                f.writelines(lines[-HISTORY_KEEP_LINES:])


def load_stats():
    if not STATS_FILE.exists():
        return {"pairs": {}, "total_games": 0}
    with locked_file(STATS_FILE, "r") as f:
        data = json.load(f)
    return data


def save_stats(stats):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with locked_file(STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2)


def pair_key(a, b):
    return f"{a} vs {b}" if a < b else f"{b} vs {a}"


def load_h2h():
    if not H2H_FILE.exists():
        return {}
    with locked_file(H2H_FILE, "r") as f:
        return json.load(f)


def save_h2h(h2h):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with locked_file(H2H_FILE, "w") as f:
        json.dump(h2h, f, indent=2)


def load_bot_stats():
    if not BOT_STATS_FILE.exists():
        return {}
    with locked_file(BOT_STATS_FILE, "r") as f:
        return json.load(f)


def save_bot_stats(bot_stats):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with locked_file(BOT_STATS_FILE, "w") as f:
        json.dump(bot_stats, f, indent=2)


def pick_matches(active_bots, h2h, ratings, n_picks=14):
    """Pick match pairs prioritizing under-evaluated and rating-diverse matchups."""
    pairs = [(a, b) for i, a in enumerate(active_bots) for b in active_bots[i + 1:]]

    def priority(a, b):
        k = pair_key(a, b)
        h = h2h.get(k, {})
        count = h.get("games", 0)
        rating_gap = abs(ratings.get(a, Glicko2Player()).r - ratings.get(b, Glicko2Player()).r)
        under_eval = max(0, UNDER_EVAL_BASELINE - count) / UNDER_EVAL_BASELINE
        diversity = min(rating_gap / RATING_GAP_SCALE, 1.0)
        count_penalty = 1.0 / (1.0 + max(0, count - UNDER_EVAL_BASELINE) / DIVERSITY_COUNT_DECAY)
        return UNDER_EVAL_WEIGHT * under_eval + DIVERSITY_WEIGHT * diversity * count_penalty

    pairs.sort(key=lambda p: priority(p[0], p[1]), reverse=True)

    n_bots = len(active_bots)
    max_per_bot = max(1, n_picks * 2 // n_bots)
    selected = []
    bot_counts = Counter()
    for a, b in pairs:
        if len(selected) >= n_picks:
            break
        if bot_counts[a] < max_per_bot and bot_counts[b] < max_per_bot:
            selected.append((a, b))
            bot_counts[a] += 1
            bot_counts[b] += 1
    return selected


def save_match_replay(a, b, wins_a, wins_b, draws, replay_data):
    os.makedirs(REPLAY_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    fname = f"{timestamp}_{a}_vs_{b}.json"
    match_data = {
        "id": fname,
        "timestamp": timestamp,
        "bot0": a,
        "bot1": b,
        "bot0_wins": wins_a,
        "bot1_wins": wins_b,
        "draws": draws,
        "games": replay_data,
    }

    replay_path = REPLAY_DIR / fname
    try:
        with open(replay_path, "w", encoding="utf-8") as f:
            json.dump(match_data, f, ensure_ascii=False)
    except OSError:
        raise

    try:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        summary = {
            "id": fname,
            "timestamp": timestamp,
            "bot0": a,
            "bot1": b,
            "bot0_wins": wins_a,
            "bot1_wins": wins_b,
            "draws": draws,
        }
        with locked_file(MATCH_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    except Exception:
        try:
            replay_path.unlink()
        except OSError:
            pass
        raise

    return fname


def cleanup_old_replays():
    if not REPLAY_DIR.exists():
        return
    files = sorted(REPLAY_DIR.iterdir(), key=lambda f: f.name)
    if len(files) > MAX_REPLAY_FILES:
        for old_file in files[: len(files) - MAX_REPLAY_FILES]:
            old_file.unlink()


def run_single_match(args):
    """Run mirror_battle, return per-game independent results from all_logs."""
    bot_a_name, bot_b_name, bot_a_path, bot_b_path, n_pairs = args
    try:
        match_wins, draws, n_played, all_logs = mirror_battle(
            bot_a_path, bot_b_path, n_games=n_pairs, verbose=False, save_log=True
        )
        # Count each game (normal + mirror) independently by winner
        games_a, games_b, games_draw = 0, 0, 0
        for game in all_logs:
            w = game.get("winner", -1)
            if w == 0:
                games_a += 1
            elif w == 1:
                games_b += 1
            else:
                games_draw += 1
        total = games_a + games_b + games_draw
        return (bot_a_name, bot_b_name, games_a, games_b, games_draw, total, None, all_logs)
    except Exception as e:
        return (bot_a_name, bot_b_name, 0, 0, 0, 0, str(e), [])


def process_result(result, ratings, h2h, bot_stats, verbose=False):
    """Process one completed match: update Elo, H2H, bot_stats, save replay."""
    a, b, wins_a, wins_b, draws, total, err, replay_data = result
    if err is not None:
        if verbose:
            print(f"[DAEMON] Error in {a} vs {b}: {err}")
        return 0

    if verbose:
        print(f"[DAEMON] {a} vs {b}: {wins_a}-{wins_b}-{draws} ({total} games)")

    # Save replay (using per-game counts)
    if replay_data:
        try:
            save_match_replay(a, b, wins_a, wins_b, draws, replay_data)
        except Exception as e:
            if verbose:
                print(f"[DAEMON] Error saving replay {a} vs {b}: {e}")

    # Snapshot opponent ratings for Elo updates
    opp_b = Glicko2Player(r=ratings[b].r, rd=ratings[b].rd, sigma=ratings[b].sigma)
    opp_a = Glicko2Player(r=ratings[a].r, rd=ratings[a].rd, sigma=ratings[a].sigma)

    # Per-game Elo updates
    for _ in range(wins_a):
        ratings[a] = update_single_game(ratings[a], opp_b, 1.0)
        ratings[b] = update_single_game(ratings[b], opp_a, 0.0)
    for _ in range(wins_b):
        ratings[a] = update_single_game(ratings[a], opp_b, 0.0)
        ratings[b] = update_single_game(ratings[b], opp_a, 1.0)
    for _ in range(draws):
        ratings[a] = update_single_game(ratings[a], opp_b, 0.5)
        ratings[b] = update_single_game(ratings[b], opp_a, 0.5)

    # Update H2H
    k = pair_key(a, b)
    h2h.setdefault(k, {"games": 0, "a_wins": 0, "b_wins": 0, "draws": 0})
    h2h[k]["games"] += total
    # a is bot0, b is bot1; key is lexical, so track by position
    if a < b:
        h2h[k]["a_wins"] += wins_a
        h2h[k]["b_wins"] += wins_b
    else:
        h2h[k]["a_wins"] += wins_b
        h2h[k]["b_wins"] += wins_a
    h2h[k]["draws"] += draws

    # Update bot stats
    for name, w, l in [(a, wins_a, wins_b), (b, wins_b, wins_a)]:
        if name not in bot_stats:
            bot_stats[name] = {"wins": 0, "losses": 0, "draws": 0, "games": 0}
        bot_stats[name]["wins"] += w
        bot_stats[name]["losses"] += l
        bot_stats[name]["draws"] += draws
        bot_stats[name]["games"] += w + l + draws
        g = bot_stats[name]["games"]
        bot_stats[name]["win_rate"] = round(bot_stats[name]["wins"] / g, 4) if g > 0 else 0.0

    return total


def save_cycle(ratings, h2h, bot_stats, stats, save_num, active_bots, verbose=False):
    """Write all data files to disk."""
    save_ratings(ratings, save_num=save_num)

    # Recompute win rates for H2H
    h2h_out = {}
    for k, v in h2h.items():
        entry = dict(v)
        g = entry["games"]
        entry["win_rate"] = round(entry["a_wins"] / g, 4) if g > 0 else 0.5
        h2h_out[k] = entry
    save_h2h(h2h_out)

    save_bot_stats(bot_stats)

    # Update legacy stats for backward compat
    stats["total_games"] = sum(v["games"] for v in bot_stats.values()) // 2
    stats["pairs"] = {k: v["games"] for k, v in h2h_out.items()}
    save_stats(stats)

    cleanup_old_replays()

    if verbose:
        sorted_bots = sorted(active_bots, key=lambda b: ratings[b].r, reverse=True)
        print(f"\n[DAEMON] Leaderboard (save #{save_num}):")
        for i, b in enumerate(sorted_bots):
            p = ratings[b]
            bs = bot_stats.get(b, {})
            wr = bs.get("win_rate", 0.0)
            g = bs.get("games", 0)
            print(f"  {i+1}. {b}: r={p.r:.1f} rd={p.rd:.1f} wr={wr:.2%} ({g} games)")
        print()


def main():
    parser = argparse.ArgumentParser(description="Background Rating Daemon")
    parser.add_argument("--pairs", type=int, default=5, help="Mirror pairs per match")
    parser.add_argument("--workers", type=int, default=14, help="Parallel workers")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print match results")
    parser.add_argument("--once", action="store_true", help="Run ~14 matches then exit")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    os.makedirs(RESULTS_DIR, exist_ok=True)

    print(f"[DAEMON] Starting rating daemon (workers={args.workers}, pairs={args.pairs})")
    print(f"[DAEMON] Elo ranking + Head-to-Head matrix + per-game updates")

    # Load persisted state
    ratings = load_ratings()
    h2h = load_h2h()
    bot_stats = load_bot_stats()
    stats = load_stats()

    active_bots = get_active_bots()
    n_workers = args.workers
    n_pairs = args.pairs

    # Ensure new bots have entries
    for b in active_bots:
        if b not in ratings:
            ratings[b] = Glicko2Player()
            if args.verbose:
                print(f"[DAEMON] New bot: {b} (r=1500, rd=350)")

    # Remove retired bots
    retired = [b for b in ratings if b not in active_bots]
    for b in retired:
        del ratings[b]
        if b in bot_stats:
            del bot_stats[b]
        if args.verbose:
            print(f"[DAEMON] Retired: {b}")

    if len(active_bots) < 2:
        print("[DAEMON] Less than 2 active bots, exiting.")
        return

    # Build initial match queue
    match_queue = deque()
    matches = pick_matches(active_bots, h2h, ratings, n_picks=n_workers * 2)
    for a, b in matches:
        match_queue.append((a, b, bot_path(a), bot_path(b), n_pairs))

    executor = ProcessPoolExecutor(max_workers=n_workers)
    in_flight = {}  # future -> (bot_a, bot_b)

    # Fill initial pool
    while len(in_flight) < n_workers and match_queue:
        m = match_queue.popleft()
        fut = executor.submit(run_single_match, m)
        in_flight[fut] = (m[0], m[1])

    games_since_save = 0
    last_save_time = time.time()
    save_num = stats.get("total_games", 0) // SAVE_EVERY_N_GAMES
    total_matches = 0

    try:
        while running and in_flight:
            done, _ = wait(in_flight.keys(), timeout=POLL_TIMEOUT, return_when=FIRST_COMPLETED)

            for fut in done:
                a, b = in_flight.pop(fut)
                result = fut.result()
                n = process_result(result, ratings, h2h, bot_stats, verbose=args.verbose)
                games_since_save += n
                total_matches += 1

                # Replenish: submit next match
                if match_queue:
                    m = match_queue.popleft()
                    new_fut = executor.submit(run_single_match, m)
                    in_flight[new_fut] = (m[0], m[1])
                else:
                    # Refill queue when empty
                    matches = pick_matches(active_bots, h2h, ratings, n_picks=n_workers * 2)
                    for ma, mb in matches:
                        match_queue.append((ma, mb, bot_path(ma), bot_path(mb), n_pairs))
                    if match_queue:
                        m = match_queue.popleft()
                        new_fut = executor.submit(run_single_match, m)
                        in_flight[new_fut] = (m[0], m[1])

            # Periodic save
            now = time.time()
            if games_since_save >= SAVE_EVERY_N_GAMES or now - last_save_time >= SAVE_INTERVAL_SEC:
                if games_since_save > 0:
                    save_num += 1
                    save_cycle(ratings, h2h, bot_stats, stats, save_num, active_bots, verbose=args.verbose)
                    games_since_save = 0
                    last_save_time = now

            # Refresh bot list periodically
            if total_matches % 50 == 0:
                new_bots = get_active_bots()
                added = set(new_bots) - set(active_bots)
                removed = set(active_bots) - set(new_bots)
                for b in added:
                    ratings[b] = Glicko2Player()
                    if args.verbose:
                        print(f"[DAEMON] New bot: {b}")
                for b in removed:
                    ratings.pop(b, None)
                    bot_stats.pop(b, None)
                    if args.verbose:
                        print(f"[DAEMON] Retired: {b}")
                if added or removed:
                    active_bots = new_bots

            # --once mode: stop after first batch completes
            if args.once and total_matches >= n_workers:
                break

    finally:
        # Graceful shutdown: wait briefly for in-flight, then final save
        print(f"[DAEMON] Draining {len(in_flight)} in-flight matches...")
        for fut in in_flight:
            try:
                result = fut.result(timeout=10)
                process_result(result, ratings, h2h, bot_stats, verbose=args.verbose)
            except Exception:
                pass
        executor.shutdown(wait=False)

        # Final save
        save_cycle(ratings, h2h, bot_stats, stats, save_num + 1, active_bots, verbose=args.verbose)
        print(f"[DAEMON] Shutdown complete. {total_matches} matches processed.")


if __name__ == "__main__":
    main()
