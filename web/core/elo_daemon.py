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
import signal
import argparse
import time
from collections import Counter, deque
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime

CORE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CORE_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(CORE_DIR))

from glicko2 import Glicko2Player, update_single_game, decay_rd
from battle import mirror_battle
from evolution_infra import locked_file, pair_key
import logging

log = logging.getLogger("pok.daemon")

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

# Continuous scheduling parameters
SAVE_EVERY_N_GAMES = 20
SAVE_INTERVAL_SEC = 60
POLL_TIMEOUT = 0.5

running = True


def handle_signal(signum, frame):
    global running
    log.warning("Received signal %d, shutting down gracefully...", signum)
    running = False


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
        # Compute H2H avg win rates for history snapshot
        h2h = load_h2h()
        bot_stats = load_bot_stats()
        from tool_helpers import compute_h2h_avg_winrate
        win_rates = {}
        for name in ratings:
            wr = compute_h2h_avg_winrate(name, h2h)
            bs = bot_stats.get(name, {})
            games = bs.get("games", 0)
            if wr is not None:
                win_rates[name] = {"h2h_avg_wr": round(wr, 4), "games": games}
            elif games > 0:
                win_rates[name] = {"games": games}
        snapshot = {
            "period": save_num,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "ratings": {name: {"r": p.r, "rd": p.rd} for name, p in ratings.items()},
            "win_rates": win_rates,
        }
        with locked_file(history_file, "a") as f:
            f.write(json.dumps(snapshot) + "\n")


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


def _opponent_coverage(bot, active_bots, h2h):
    """Fraction of active opponents this bot has H2H data for."""
    n_opponents = 0
    for other in active_bots:
        if other == bot:
            continue
        k = pair_key(bot, other)
        if h2h.get(k, {}).get("games", 0) > 0:
            n_opponents += 1
    total = len(active_bots) - 1
    return n_opponents / total if total > 0 else 1.0


PRIORITY_EVAL_FILE = RESULTS_DIR / "priority_eval.json"


def _load_priority_eval():
    """Load the priority eval bot name if a recent signal exists."""
    if not PRIORITY_EVAL_FILE.exists():
        return None
    try:
        with locked_file(PRIORITY_EVAL_FILE, "r") as f:
            data = json.load(f)
        # Expire after 30 minutes
        if time.time() - data.get("since", 0) > 1800:
            PRIORITY_EVAL_FILE.unlink(missing_ok=True)
            return None
        return data.get("bot")
    except Exception:
        return None


def pick_matches(active_bots, h2h, ratings, n_picks=14):
    """Pick match pairs prioritizing under-evaluated and rating-diverse matchups.

    Bots with low opponent coverage (< 80%) get extra scheduling slots to
    quickly fill in missing matchups. Newly committed bots (priority_eval.json)
    are exempt from per-bot caps.
    """
    pairs = [(a, b) for i, a in enumerate(active_bots) for b in active_bots[i + 1:]]

    coverage = {b: _opponent_coverage(b, active_bots, h2h) for b in active_bots}

    def priority(a, b):
        k = pair_key(a, b)
        h = h2h.get(k, {})
        count = h.get("games", 0)
        rating_gap = abs(ratings.get(a, Glicko2Player()).conservative_rating() - ratings.get(b, Glicko2Player()).conservative_rating())
        under_eval = max(0, UNDER_EVAL_BASELINE - count) / UNDER_EVAL_BASELINE
        diversity = min(rating_gap / RATING_GAP_SCALE, 1.0)
        count_penalty = 1.0 / (1.0 + max(0, count - UNDER_EVAL_BASELINE) / DIVERSITY_COUNT_DECAY)
        # Boost never-played pairs where either bot has low coverage
        new_pair_bonus = 0.0
        if count == 0:
            min_cov = min(coverage[a], coverage[b])
            if min_cov < 0.8:
                new_pair_bonus = 0.3 * (1.0 - min_cov)
        return UNDER_EVAL_WEIGHT * under_eval + DIVERSITY_WEIGHT * diversity * count_penalty + new_pair_bonus

    pairs.sort(key=lambda p: priority(p[0], p[1]), reverse=True)

    n_bots = len(active_bots)
    base_max = max(2, n_picks * 2 // n_bots)
    priority_bot = _load_priority_eval()

    selected = []
    bot_counts = Counter()
    for a, b in pairs:
        if len(selected) >= n_picks:
            break
        # Priority bot is exempt from per-bot caps
        if priority_bot and a == priority_bot:
            max_a = n_picks
        else:
            max_a = base_max * 3 if coverage[a] < 0.8 else base_max
        if priority_bot and b == priority_bot:
            max_b = n_picks
        else:
            max_b = base_max * 3 if coverage[b] < 0.8 else base_max
        if bot_counts[a] < max_a and bot_counts[b] < max_b:
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
            log.error("Error in %s vs %s: %s", a, b, err)
        return 0

    if verbose:
        log.debug("%s vs %s: %d-%d-%d (%d games)", a, b, wins_a, wins_b, draws, total)

    # Save replay (using per-game counts)
    if replay_data:
        try:
            save_match_replay(a, b, wins_a, wins_b, draws, replay_data)
        except Exception as e:
            if verbose:
                log.warning("Error saving replay %s vs %s: %s", a, b, e)

    # Per-game Glicko-2 updates (use live opponent ratings each game)
    _default = Glicko2Player()
    for _ in range(wins_a):
        ratings[a] = update_single_game(ratings[a], ratings.get(b, _default), 1.0)
        ratings[b] = update_single_game(ratings[b], ratings.get(a, _default), 0.0)
    for _ in range(wins_b):
        ratings[a] = update_single_game(ratings[a], ratings.get(b, _default), 0.0)
        ratings[b] = update_single_game(ratings[b], ratings.get(a, _default), 1.0)
    for _ in range(draws):
        ratings[a] = update_single_game(ratings[a], ratings.get(b, _default), 0.5)
        ratings[b] = update_single_game(ratings[b], ratings.get(a, _default), 0.5)

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


def save_cycle(ratings, h2h, bot_stats, stats, save_num, active_bots,
               played_bots=None, verbose=False):
    """Write all data files to disk. Apply RD decay to bots that didn't play."""
    if played_bots is not None:
        for b in active_bots:
            if b not in played_bots and b in ratings:
                ratings[b] = decay_rd(ratings[b])
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
        # Compute H2H avg win rates for leaderboard
        from tool_helpers import compute_h2h_avg_winrate
        bot_wr_map = {b: compute_h2h_avg_winrate(b, h2h_out) or 0.0 for b in active_bots}
        sorted_bots = sorted(active_bots, key=lambda b: bot_wr_map.get(b, 0.0), reverse=True)
        log.info("Leaderboard (save #%d):", save_num)
        for i, b in enumerate(sorted_bots):
            p = ratings[b]
            bs = bot_stats.get(b, {})
            wr = bs.get("win_rate", 0.0)
            g = bs.get("games", 0)
            hwr = bot_wr_map.get(b, 0.0)
            log.info("  %d. %s: h2h_avg_wr=%.2f%% r=%.1f rd=%.1f wr=%.2f%% (%d games)",
                     i + 1, b, hwr * 100, p.r, p.rd, wr * 100, g)


def main():
    parser = argparse.ArgumentParser(description="Background Rating Daemon")
    parser.add_argument("--pairs", type=int, default=5, help="Mirror pairs per match")
    parser.add_argument("--workers", type=int, default=14, help="Parallel workers")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print match results")
    parser.add_argument("--once", action="store_true", help="Run ~14 matches then exit")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    global running

    os.makedirs(RESULTS_DIR, exist_ok=True)

    # Read stored parent PID for robust orphan detection
    _stored_ppid = None
    _daemon_pid_file = RESULTS_DIR / ".daemon_pid"
    if _daemon_pid_file.exists():
        try:
            info = json.loads(_daemon_pid_file.read_text().strip())
            if isinstance(info, dict):
                _stored_ppid = info.get("ppid")
        except (json.JSONDecodeError, KeyError):
            pass

    from logging_config import configure_logging
    configure_logging()

    log.info("Starting rating daemon (workers=%d, pairs=%d)", args.workers, args.pairs)
    log.info("Elo ranking + Head-to-Head matrix + per-game updates")

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
                log.info("New bot: %s (r=1500, rd=350)", b)

    # Remove retired bots
    retired = [b for b in ratings if b not in active_bots]
    for b in retired:
        del ratings[b]
        if b in bot_stats:
            del bot_stats[b]
        if args.verbose:
            log.info("Retired: %s", b)
    for b in retired:
        h2h = {k: v for k, v in h2h.items() if b not in k.split(" vs ")}

    if len(active_bots) < 2:
        log.warning("Less than 2 active bots, exiting.")
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
    last_parent_check = time.time()
    save_num = stats.get("total_games", 0) // SAVE_EVERY_N_GAMES
    total_matches = 0
    MAX_POOL_RECOVERIES = 3
    recovery_count = 0
    played_bots_this_cycle = set()

    try:
        while running and in_flight and recovery_count < MAX_POOL_RECOVERIES:
            try:
                while running and in_flight:
                    done, _ = wait(in_flight.keys(), timeout=POLL_TIMEOUT, return_when=FIRST_COMPLETED)

                    for fut in done:
                        a, b = in_flight.pop(fut)
                        # Skip results for bots that have been reaped
                        if a not in active_bots or b not in active_bots:
                            try:
                                fut.result()
                            except Exception:
                                pass
                            continue
                        result = fut.result()
                        n = process_result(result, ratings, h2h, bot_stats, verbose=args.verbose)
                        games_since_save += n
                        total_matches += 1
                        played_bots_this_cycle.add(a)
                        played_bots_this_cycle.add(b)

                        # Replenish: submit next match
                        if match_queue:
                            m = match_queue.popleft()
                            if m[0] not in active_bots or m[1] not in active_bots:
                                continue
                            new_fut = executor.submit(run_single_match, m)
                            in_flight[new_fut] = (m[0], m[1])
                        else:
                            # Refill queue when empty
                            matches = pick_matches(active_bots, h2h, ratings, n_picks=n_workers * 2)
                            for ma, mb in matches:
                                match_queue.append((ma, mb, bot_path(ma), bot_path(mb), n_pairs))
                            if match_queue:
                                m = match_queue.popleft()
                                if m[0] not in active_bots or m[1] not in active_bots:
                                    continue
                                new_fut = executor.submit(run_single_match, m)
                                in_flight[new_fut] = (m[0], m[1])

                    # Periodic save
                    try:
                        now = time.time()
                        if games_since_save >= SAVE_EVERY_N_GAMES or now - last_save_time >= SAVE_INTERVAL_SEC:
                            if games_since_save > 0:
                                save_num += 1
                                save_cycle(ratings, h2h, bot_stats, stats, save_num, active_bots,
                                           played_bots=played_bots_this_cycle, verbose=args.verbose)
                                games_since_save = 0
                                played_bots_this_cycle = set()
                                last_save_time = now
                    except Exception as e:
                        log.warning("Save error (non-fatal): %s", e)

                    # Parent alive check — exit if orphaned
                    now = time.time()
                    if now - last_parent_check >= 5:
                        last_parent_check = now
                        cur_ppid = os.getppid()
                        if cur_ppid == 1 or (_stored_ppid is not None and cur_ppid != _stored_ppid):
                            log.warning("Parent process died (ppid %d → %d), shutting down...", _stored_ppid, cur_ppid)
                            running = False
                            break

                    # Check for reap signal — immediate bot list refresh
                    try:
                        reap_signal = Path(__file__).parent / "results" / ".reap_signal"
                        reap_fresh = False
                        if reap_signal.exists():
                            try:
                                ts = float(reap_signal.read_text().strip())
                                reap_fresh = time.time() - ts <= 300
                            except (ValueError, OSError):
                                reap_fresh = True  # No timestamp = legacy signal, process anyway
                            reap_signal.unlink(missing_ok=True)
                        if reap_fresh:
                            new_bots = get_active_bots()
                            removed = set(active_bots) - set(new_bots)
                            for b in removed:
                                ratings.pop(b, None)
                                bot_stats.pop(b, None)
                                h2h = {k: v for k, v in h2h.items() if b not in k.split(" vs ")}
                            for b in set(new_bots) - set(active_bots):
                                if b not in ratings:
                                    ratings[b] = Glicko2Player()
                            active_bots = new_bots
                            # Filter match_queue and cancel in-flight futures for reaped bots
                            if removed:
                                match_queue = deque(
                                    m for m in match_queue
                                    if m[0] not in removed and m[1] not in removed
                                )
                                for fut in list(in_flight):
                                    a, b = in_flight[fut]
                                    if a in removed or b in removed:
                                        fut.cancel()
                                        del in_flight[fut]
                            if games_since_save > 0:
                                save_num += 1
                                save_cycle(ratings, h2h, bot_stats, stats, save_num, active_bots,
                                           played_bots=played_bots_this_cycle, verbose=args.verbose)
                                games_since_save = 0
                                played_bots_this_cycle = set()
                                last_save_time = time.time()
                            if args.verbose:
                                log.info("Reap signal processed, active bots: %d", len(active_bots))
                    except Exception as e:
                        log.warning("Reap signal error (non-fatal): %s", e)

                    # Refresh bot list periodically
                    if total_matches % 50 == 0:
                        new_bots = get_active_bots()
                        added = set(new_bots) - set(active_bots)
                        removed = set(active_bots) - set(new_bots)
                        for b in added:
                            ratings[b] = Glicko2Player()
                            if args.verbose:
                                log.info("New bot: %s", b)
                        for b in removed:
                            ratings.pop(b, None)
                            bot_stats.pop(b, None)
                            if args.verbose:
                                log.info("Retired: %s", b)
                        for b in removed:
                            h2h = {k: v for k, v in h2h.items() if b not in k.split(" vs ")}
                        if added or removed:
                            active_bots = new_bots
                            if removed:
                                match_queue = deque(
                                    m for m in match_queue
                                    if m[0] not in removed and m[1] not in removed
                                )
                                for fut in list(in_flight):
                                    fa, fb = in_flight[fut]
                                    if fa in removed or fb in removed:
                                        fut.cancel()
                                        del in_flight[fut]

                    # --once mode: stop after first batch completes
                    if args.once and total_matches >= n_workers:
                        break
                break  # normal exit from inner while

            except BrokenProcessPool as e:
                recovery_count += 1
                log.error("ProcessPool broken (recovery %d/%d): %s", recovery_count, MAX_POOL_RECOVERIES, e)
                for fut in list(in_flight):
                    try:
                        fut.result(timeout=1)
                    except Exception:
                        pass
                in_flight.clear()
                executor.shutdown(wait=False, cancel_futures=True)
                executor = ProcessPoolExecutor(max_workers=n_workers)
                match_queue = deque()
                matches = pick_matches(active_bots, h2h, ratings, n_picks=n_workers * 2)
                for a, b in matches:
                    match_queue.append((a, b, bot_path(a), bot_path(b), n_pairs))
                while len(in_flight) < n_workers and match_queue:
                    m = match_queue.popleft()
                    fut = executor.submit(run_single_match, m)
                    in_flight[fut] = (m[0], m[1])

    except Exception as e:
        import traceback
        crash_log = RESULTS_DIR / "daemon_crash.log"
        try:
            with open(crash_log, "a") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"Crash at {datetime.now().isoformat()}\n")
                f.write(traceback.format_exc())
        except Exception:
            pass
        log.critical("FATAL: %s\n%s", e, traceback.format_exc())
        raise
    finally:
        # Kill entire process group (workers + bot subprocesses)
        try:
            os.killpg(os.getpgrp(), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

        executor.shutdown(wait=False, cancel_futures=True)

        # Final save
        try:
            save_cycle(ratings, h2h, bot_stats, stats, save_num + 1, active_bots,
                       played_bots=played_bots_this_cycle, verbose=args.verbose)
        except Exception as e:
            log.warning("Final save failed: %s", e)
        log.info("Shutdown complete. %d matches processed.", total_matches)


if __name__ == "__main__":
    main()
