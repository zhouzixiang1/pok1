"""
Background Glicko-2 Rating Daemon for Poker Bot Evolution.

Continuously runs mirror battles between active bots, updates Glicko-2
ratings after each rating period. Prioritizes under-evaluated pairs.

Usage:
    python web/core/elo_daemon.py --pairs 5 --workers 14 --verbose
"""

import os
import sys
import json
import fcntl
import signal
import random
import argparse
import time
from contextlib import contextmanager
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime

# Add project root and core dir to sys.path
CORE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CORE_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(CORE_DIR))

from glicko2 import Glicko2Player, update_rating_period, decay_rd
from battle import mirror_battle

BOTS_DIR = PROJECT_ROOT / "bots"
RESULTS_DIR = CORE_DIR / "results"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
STATS_FILE = RESULTS_DIR / "elo_daemon_stats.json"
REPLAY_DIR = RESULTS_DIR / "match_replay"
MATCH_HISTORY_FILE = RESULTS_DIR / "match_history.jsonl"
MAX_REPLAY_FILES = 200

MIN_PERIOD_GAMES = 10  # Min matches per bot before rating period closes

# Match selection priority weights
UNDER_EVAL_WEIGHT = 0.6
DIVERSITY_WEIGHT = 0.4
UNDER_EVAL_BASELINE = 50
RATING_GAP_SCALE = 200
MAX_HISTORY_LINES = 200
HISTORY_KEEP_LINES = 100

running = True


@contextmanager
def locked_file(path, mode='r', lock_type=None, encoding=None):
    """Context manager for file operations with fcntl locking."""
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
    # Kill all child processes (ProcessPoolExecutor workers + battle subprocesses)
    try:
        os.killpg(os.getpgid(os.getpid()), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def get_active_bots():
    """Scan bots/ for completed claude_v* directories."""
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
    """Load Glicko-2 ratings with shared lock."""
    if not RATINGS_FILE.exists():
        return {}
    with locked_file(RATINGS_FILE, "r") as f:
        data = json.load(f)
    return {name: Glicko2Player.from_dict(d) for name, d in data.items()}


def save_ratings(ratings, period=None):
    """Save Glicko-2 ratings with exclusive lock. Optionally append to history."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    data = {}
    for name, p in ratings.items():
        d = p.to_dict()
        d["last_period"] = datetime.now().isoformat(timespec="seconds")
        data[name] = d
    with locked_file(RATINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)

    # Append snapshot to history log (atomic under LOCK_EX)
    if period is not None:
        history_file = RESULTS_DIR / "rating_history.jsonl"
        snapshot = {
            "period": period,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "ratings": {name: {"r": p.r, "rd": p.rd} for name, p in ratings.items()},
        }
        with locked_file(history_file, "a+") as f:
            f.write(json.dumps(snapshot) + "\n")
            f.flush()
            # Trim history file to prevent unbounded growth
            f.seek(0)
            lines = f.readlines()
            if len(lines) > MAX_HISTORY_LINES:
                f.seek(0)
                f.truncate()
                f.writelines(lines[-HISTORY_KEEP_LINES:])


def load_stats():
    if not STATS_FILE.exists():
        return {"pairs": {}, "total_periods": 0}
    with locked_file(STATS_FILE, "r") as f:
        data = json.load(f)
    return data


def save_stats(stats):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with locked_file(STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2)


def pair_key(a, b):
    """Canonical pair key (alphabetical order)."""
    return f"{a} vs {b}" if a < b else f"{b} vs {a}"


def pick_matches(active_bots, stats, ratings, n_picks=14):
    """Pick n_picks match pairs, balancing under-evaluation and rating diversity."""
    pairs = []
    for i, a in enumerate(active_bots):
        for b in active_bots[i + 1:]:
            pairs.append((a, b))

    def priority(a, b):
        count = stats.get("pairs", {}).get(pair_key(a, b), 0)
        rating_gap = abs(ratings.get(a, Glicko2Player()).r - ratings.get(b, Glicko2Player()).r)

        # Under-evaluation score: high when few games played
        under_eval = max(0, UNDER_EVAL_BASELINE - count) / UNDER_EVAL_BASELINE  # 0-1
        # Diversity score: high when rating gap is large (different strategies)
        diversity = min(rating_gap / RATING_GAP_SCALE, 1.0)  # 0-1

        return UNDER_EVAL_WEIGHT * under_eval + DIVERSITY_WEIGHT * diversity

    pairs.sort(key=lambda p: priority(p[0], p[1]), reverse=True)
    return pairs[:n_picks]


def save_match_replay(a, b, wins_a, wins_b, draws, replay_data):
    """Save a match replay JSON and append summary to match_history.jsonl."""
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

    # Step 1: Write replay JSON
    replay_path = REPLAY_DIR / fname
    try:
        with open(replay_path, "w", encoding="utf-8") as f:
            json.dump(match_data, f, ensure_ascii=False)
    except OSError:
        raise

    # Step 2: Append summary to permanent JSONL (with lock)
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
        # JSONL append failed — remove orphaned replay file
        try:
            replay_path.unlink()
        except OSError:
            pass
        raise

    return fname


def cleanup_old_replays():
    """Keep only the most recent MAX_REPLAY_FILES replay files."""
    if not REPLAY_DIR.exists():
        return
    files = sorted(REPLAY_DIR.iterdir(), key=lambda f: f.name)
    if len(files) > MAX_REPLAY_FILES:
        for old_file in files[: len(files) - MAX_REPLAY_FILES]:
            old_file.unlink()


def run_single_match(args):
    """Run a mirror_battle in a subprocess. Called by ProcessPoolExecutor."""
    bot_a_name, bot_b_name, bot_a_path, bot_b_path, n_pairs = args
    try:
        match_wins, draws, n_played, all_logs = mirror_battle(
            bot_a_path, bot_b_path, n_games=n_pairs, verbose=False, save_log=True
        )
        return (bot_a_name, bot_b_name, match_wins[0], match_wins[1], draws, n_played, None, all_logs)
    except Exception as e:
        return (bot_a_name, bot_b_name, 0, 0, 0, 0, str(e), [])


def run_rating_period(active_bots, ratings, stats, n_pairs, n_workers, verbose=False):
    """
    Run one rating period:
    1. Schedule matches prioritizing under-evaluated pairs
    2. Execute in parallel
    3. Collect results
    4. Glicko-2 batch update
    """
    # Ensure all active bots have rating entries
    for b in active_bots:
        if b not in ratings:
            ratings[b] = Glicko2Player()

    # Decide how many rounds needed so each bot gets enough games
    n_bots = len(active_bots)
    if n_bots < 2:
        if verbose:
            print("[DAEMON] Less than 2 active bots, nothing to do.")
        return ratings, stats

    # Schedule matches
    matches = pick_matches(active_bots, stats, ratings, n_picks=n_workers)
    if not matches:
        return ratings, stats

    # Build match args
    match_args = [
        (a, b, bot_path(a), bot_path(b), n_pairs)
        for a, b in matches
    ]

    if verbose:
        print(f"[DAEMON] Rating period: {len(match_args)} matches, {n_workers} workers")

    # Execute matches in parallel
    results_by_bot = {b: [] for b in active_bots}  # bot -> list of (opponent_player, score)
    match_results = []

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(run_single_match, arg): arg for arg in match_args}
        for future in as_completed(futures):
            if not running:
                executor.shutdown(wait=False, cancel_futures=True)
                return ratings, stats  # Early return, don't wait for executor
            result = future.result()
            a, b, wins_a, wins_b, draws, n_played, err, replay_data = result
            if err is not None:
                if verbose:
                    print(f"[DAEMON] Error in {a} vs {b}: {err}")
                continue

            match_results.append((a, b, wins_a, wins_b, draws))

            # Save replay data
            if replay_data:
                try:
                    save_match_replay(a, b, wins_a, wins_b, draws, replay_data)
                except Exception as e:
                    if verbose:
                        print(f"[DAEMON] Error saving replay {a} vs {b}: {e}")

            if verbose:
                print(f"[DAEMON] {a} vs {b}: {wins_a}-{wins_b}-{draws}")

            # Convert to per-game scores for Glicko-2
            for _ in range(wins_a):
                results_by_bot[a].append((ratings[b], 1.0))
                results_by_bot[b].append((ratings[a], 0.0))
            for _ in range(wins_b):
                results_by_bot[a].append((ratings[b], 0.0))
                results_by_bot[b].append((ratings[a], 1.0))
            for _ in range(draws):
                results_by_bot[a].append((ratings[b], 0.5))
                results_by_bot[b].append((ratings[a], 0.5))

    # Glicko-2 batch update
    for b in active_bots:
        if results_by_bot[b]:
            ratings[b] = update_rating_period(ratings[b], results_by_bot[b])

    # Update stats
    for a, b, wins_a, wins_b, draws in match_results:
        k = pair_key(a, b)
        stats.setdefault("pairs", {})
        stats["pairs"][k] = stats["pairs"].get(k, 0) + wins_a + wins_b + draws

    stats["total_periods"] = stats.get("total_periods", 0) + 1

    # Cleanup old replay files
    cleanup_old_replays()

    if verbose:
        # Print current leaderboard
        sorted_bots = sorted(active_bots, key=lambda b: ratings[b].r, reverse=True)
        print(f"\n[DAEMON] Leaderboard after period {stats['total_periods']}:")
        for i, b in enumerate(sorted_bots):
            p = ratings[b]
            print(f"  {i+1}. {b}: r={p.r:.1f} rd={p.rd:.1f} (conservative={p.conservative_rating():.1f})")
        print()

    return ratings, stats


def main():
    parser = argparse.ArgumentParser(description="Background Glicko-2 Rating Daemon")
    parser.add_argument("--pairs", type=int, default=5, help="Mirror pairs per match")
    parser.add_argument("--workers", type=int, default=14, help="Parallel workers")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print match results")
    parser.add_argument("--once", action="store_true", help="Run one rating period then exit")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    os.makedirs(RESULTS_DIR, exist_ok=True)

    print(f"[DAEMON] Starting Glicko-2 daemon (workers={args.workers}, pairs={args.pairs})")

    while running:
        active_bots = get_active_bots()
        if len(active_bots) < 2:
            if args.verbose:
                print(f"[DAEMON] Waiting for bots... ({len(active_bots)} active)")
            if args.once:
                break
            time.sleep(10)
            continue

        ratings = load_ratings()
        stats = load_stats()

        # Ensure new bots are in ratings
        for b in active_bots:
            if b not in ratings:
                ratings[b] = Glicko2Player()
                if args.verbose:
                    print(f"[DAEMON] New bot discovered: {b} (r=1500, rd=350)")

        # Remove retired bots from ratings
        retired = [b for b in ratings if b not in active_bots]
        for b in retired:
            del ratings[b]
            if args.verbose:
                print(f"[DAEMON] Retired bot removed: {b}")

        ratings, stats = run_rating_period(
            active_bots, ratings, stats,
            n_pairs=args.pairs, n_workers=args.workers, verbose=args.verbose
        )

        save_ratings(ratings, period=stats.get("total_periods", 0))
        save_stats(stats)

        if args.once:
            break

        # Brief pause between periods
        time.sleep(1)

    print("[DAEMON] Shutdown complete.")


if __name__ == "__main__":
    main()
