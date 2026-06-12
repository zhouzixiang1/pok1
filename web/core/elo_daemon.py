"""
Background Rating Daemon for Poker Bot Evolution.

Continuously runs mirror battles between active bots. Uses per-game Elo
updates and maintains a Head-to-Head win/loss matrix. Continuous scheduling
eliminates idle cores.

Usage:
    python web/core/elo_daemon.py --pairs 5 --workers 28 --verbose
"""

import os
import sys
import json
import random
import signal
import argparse
import time
import multiprocessing
from collections import Counter, deque
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED

# Battle Scheduler integration (optional)
import logging

log = logging.getLogger("pok.daemon")

try:
    from battle_scheduler import (
        BattleResult,
        drain_pending_jobs,
        requeue_unclaimed_on_startup,
        write_result,
    )
    _SCHEDULER_AVAILABLE = True
except Exception as e:
    log.debug("Scheduler module not available: %s", e)
    _SCHEDULER_AVAILABLE = False
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime

CORE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CORE_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(CORE_DIR))

from glicko2 import Glicko2Player, update_single_game, decay_rd
from engine.battle import mirror_battle
from evolution_infra import locked_file, pair_key
from bot_action_stats import compute_bot_action_stats, compute_all_bot_stats
from eval_rounds import EvalRoundManager

BOTS_DIR = PROJECT_ROOT / "bots"
RESULTS_DIR = CORE_DIR / "results"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
STATS_FILE = RESULTS_DIR / "elo_daemon_stats.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"
BOT_STATS_FILE = RESULTS_DIR / "bot_stats.json"
REPLAY_DIR = RESULTS_DIR / "match_replay"
MATCH_HISTORY_FILE = RESULTS_DIR / "match_history.jsonl"
MAX_REPLAY_FILES = 200

# JSONL rotation limits (lines kept after rotation)
MAX_RATING_HISTORY_LINES = 3000
MAX_MATCH_HISTORY_LINES = 15000
MAX_SYSTEM_EVENTS_LINES = 5000

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
    # Atomic write: tmp + fsync + rename (crash-safe)
    tmp = RATINGS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(RATINGS_FILE))

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
            "ratings": {name: {"r": p.r, "rd": p.rd, "sigma": p.sigma} for name, p in ratings.items()},
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
    # Prune low-sample entries (games < 2 have no statistical value)
    h2h = {k: v for k, v in h2h.items() if v.get("games", 0) >= 2}
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
    """Load the priority eval bot name. Expires when bot reaches min_games."""
    if not PRIORITY_EVAL_FILE.exists():
        return None
    try:
        with locked_file(PRIORITY_EVAL_FILE, "r") as f:
            data = json.load(f)
        bot = data.get("bot")
        if not bot:
            return None
        # Expire when bot has reached min_games (not by timeout — daemon may be stopped/restarted)
        min_games = data.get("min_games", 100)
        stats = load_bot_stats()
        if stats.get(bot, {}).get("games", 0) >= min_games:
            PRIORITY_EVAL_FILE.unlink(missing_ok=True)
            return None
        return bot
    except Exception as e:
        log.debug("Priority eval load failed: %s", e)
        return None


def pick_matches(active_bots, h2h, ratings, n_picks=None):
    if n_picks is None:
        n_picks = multiprocessing.cpu_count()
    """Pick match pairs prioritizing under-evaluated and rating-diverse matchups.

    Bots with low opponent coverage (< 80%) get extra scheduling slots to
    quickly fill in missing matchups. Newly committed bots (priority_eval.json)
    get a strong priority boost and are exempt from per-bot caps.
    """
    pairs = [(a, b) for i, a in enumerate(active_bots) for b in active_bots[i + 1:]]
    # Shuffle before sorting to break alphabetical ordering — prevents systematic
    # starvation of high-version bots when priority values cluster tightly
    random.shuffle(pairs)

    coverage = {b: _opponent_coverage(b, active_bots, h2h) for b in active_bots}
    priority_bot = _load_priority_eval()

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
        score = UNDER_EVAL_WEIGHT * under_eval + DIVERSITY_WEIGHT * diversity * count_penalty + new_pair_bonus
        # Strong boost for priority bot pairs — ensures newly committed bots get scheduled
        if priority_bot and (a == priority_bot or b == priority_bot):
            score += 2.0
        return score

    pairs.sort(key=lambda p: priority(p[0], p[1]), reverse=True)

    n_bots = len(active_bots)
    base_max = max(2, n_picks * 2 // n_bots)

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
    log.info("pick_matches: %d pairs from %d candidates (priority=%s, bots=%d)",
             len(selected), len(pairs), priority_bot, len(active_bots))
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
    except Exception as e:
        log.warning("Match history write failed: %s", e)
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


def _rotate_jsonl(filepath, max_lines):
    """Trim a JSONL file to keep only the last `max_lines` lines.

    Uses fcntl LOCK_EX to serialize with concurrent writers (workers, web process)
    who also use locked_file() with LOCK_EX for appends.
    Only rotates files OWNED by the daemon (written in save_cycle).
    """
    if not filepath.exists():
        return
    try:
        # Quick size check — skip if small
        if filepath.stat().st_size < 1_000_000:  # < 1MB
            return
        # Acquire exclusive lock to prevent concurrent writers from losing data
        fd = open(filepath, "r")
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            content = fd.read()
            lines = content.splitlines() if content else []
            if len(lines) <= max_lines:
                return
            trimmed = lines[-max_lines:]
            tmp = filepath.with_suffix(".tmp")
            tmp.write_text("\n".join(trimmed) + "\n", encoding="utf-8")
            os.replace(str(tmp), str(filepath))
            log.debug("Rotated %s: %d → %d lines", filepath.name, len(lines), max_lines)
        finally:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
        # Clean up stale .tmp if present from a previous crash
        stale_tmp = filepath.with_suffix(".tmp")
        if stale_tmp.exists():
            stale_tmp.unlink(missing_ok=True)
    except Exception as e:
        log.debug("JSONL rotation failed for %s: %s", filepath.name, e)


def run_single_match(args):
    """Run mirror_battle, save replay in-worker, return lightweight result."""
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

        # Save replay inside worker to avoid ~2MB cross-process transfer
        try:
            save_match_replay(bot_a_name, bot_b_name, games_a, games_b, games_draw, all_logs)
        except Exception as e:
            log.debug("Replay save failed: %s", e)

        return (bot_a_name, bot_b_name, games_a, games_b, games_draw, total, None)
    except Exception as e:
        return (bot_a_name, bot_b_name, 0, 0, 0, 0, str(e))


def process_result(result, ratings, h2h, bot_stats, verbose=False):
    """Process one completed match: update Elo, H2H, bot_stats."""
    a, b, wins_a, wins_b, draws, total, err = result
    if err is not None:
        log.error("Error in %s vs %s: %s", a, b, err)
        return 0

    log.debug("%s vs %s: %d-%d-%d (%d games)", a, b, wins_a, wins_b, draws, total)

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

    # Rotate growing JSONL files to prevent unbounded growth
    _rotate_jsonl(RESULTS_DIR / "rating_history.jsonl", MAX_RATING_HISTORY_LINES)
    _rotate_jsonl(MATCH_HISTORY_FILE, MAX_MATCH_HISTORY_LINES)
    # Note: system_events.jsonl is written by web process, rotated by system_log.py

    # Compute and write bot action stats from replay files (single-pass)
    try:
        bot_action_stats = compute_all_bot_stats(active_bots, REPLAY_DIR)
        action_stats_file = RESULTS_DIR / "bot_action_stats.json"
        tmp = action_stats_file.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(bot_action_stats, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(action_stats_file))
    except Exception as e:
        log.warning("Bot action stats computation failed (non-fatal): %s", e)

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
    parser.add_argument("--workers", type=int, default=max(1, int(multiprocessing.cpu_count() * 28 / 32)), help="Parallel workers")
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

    # Eval round manager for deterministic evaluation cycles
    eval_round_mgr = EvalRoundManager()

    import multiprocessing as _mp
    mp_ctx = _mp.get_context("spawn")
    executor = ProcessPoolExecutor(max_workers=n_workers, mp_context=mp_ctx)
    in_flight = {}  # future -> (bot_a, bot_b) or (bot_a, bot_b, ext_job_id)

    _first_iteration = True
    _capacity = max(1, n_workers // 4)

    # Fill initial pool
    while len(in_flight) < n_workers and match_queue:
        m = match_queue.popleft()
        # Detect external jobs: ("external", job_id, a, b, path_a, path_b, n_pairs)
        is_external = len(m) == 7 and m[0] == "external"
        if is_external:
            exec_args = m[2:7]
            ext_job_id = m[1]
            fut = executor.submit(run_single_match, exec_args)
            in_flight[fut] = (exec_args[0], exec_args[1], ext_job_id)
        else:
            if m[0] not in active_bots or m[1] not in active_bots:
                continue
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
    last_bot_refresh_time = time.time()

    try:
        while running and in_flight and recovery_count < MAX_POOL_RECOVERIES:
            try:
                while running and in_flight:
                    # Poll external job queue
                    if _SCHEDULER_AVAILABLE:
                        ext_in_queue = sum(
                            1 for m in match_queue if len(m) == 7 and m[0] == "external"
                        )
                        if ext_in_queue < _capacity:
                            if _first_iteration:
                                recovered = requeue_unclaimed_on_startup()
                                for job in recovered:
                                    match_queue.appendleft((
                                        "external", job["job_id"],
                                        job["bot_a_name"], job["bot_b_name"],
                                        job["bot_a_path"], job["bot_b_path"],
                                        job["n_pairs"],
                                    ))
                            pending = drain_pending_jobs()
                            for job in pending:
                                match_queue.appendleft((
                                    "external", job["job_id"],
                                    job["bot_a_name"], job["bot_b_name"],
                                    job["bot_a_path"], job["bot_b_path"],
                                    job["n_pairs"],
                                ))
                            _first_iteration = False

                    done, _ = wait(in_flight.keys(), timeout=POLL_TIMEOUT, return_when=FIRST_COMPLETED)

                    for fut in done:
                        entry = in_flight.pop(fut)
                        is_external = len(entry) == 3
                        if is_external:
                            a, b, ext_job_id = entry
                            try:
                                result = fut.result()
                                if _SCHEDULER_AVAILABLE:
                                    try:
                                        write_result(BattleResult(
                                            job_id=ext_job_id,
                                            wins_a=result[2], wins_b=result[3],
                                            draws=result[4], total=result[5],
                                            error=result[6] if len(result) > 6 and result[6] else None,
                                            completed_at=time.time(),
                                            source="scheduler",
                                        ))
                                    except Exception as wr_err:
                                        log.warning("write_result failed for %s: %s", ext_job_id, wr_err)
                            except Exception as e:
                                if _SCHEDULER_AVAILABLE:
                                    try:
                                        write_result(BattleResult(
                                            job_id=ext_job_id,
                                            wins_a=0, wins_b=0, draws=0, total=0,
                                            error=str(e),
                                            completed_at=time.time(),
                                            source="scheduler",
                                        ))
                                    except Exception as wr_err:
                                        log.warning("write_result(error) failed for %s: %s", ext_job_id, wr_err)
                            continue

                        a, b = entry
                        # Skip results for bots that have been reaped
                        if a not in active_bots or b not in active_bots:
                            try:
                                fut.result()
                            except Exception as e:
                                log.debug("Reaped bot result error: %s", e)
                            continue
                        result = fut.result()
                        n = process_result(result, ratings, h2h, bot_stats, verbose=args.verbose)
                        games_since_save += n
                        total_matches += 1
                        played_bots_this_cycle.add(a)
                        played_bots_this_cycle.add(b)

                        # Eval round tracking
                        try:
                            if eval_round_mgr.is_active:
                                eval_round_mgr.record_result(
                                    result[0], result[1],
                                    result[2], result[3], result[4],
                                )
                            else:
                                trigger = eval_round_mgr.count_game(n)
                                if trigger and len(active_bots) >= 2:
                                    eval_pairs = eval_round_mgr.start_round(active_bots)
                                    for ea, eb in eval_pairs:
                                        match_queue.append((ea, eb, bot_path(ea), bot_path(eb), n_pairs))
                                    if args.verbose:
                                        log.info("Eval round triggered: %d pairs queued", len(eval_pairs))
                        except Exception as er_err:
                            log.warning("Eval round tracking error (non-fatal): %s", er_err)

                        # Replenish: submit next match
                        if match_queue and executor is not None:
                            m = match_queue.popleft()
                            is_ext = len(m) == 7 and m[0] == "external"
                            if is_ext:
                                exec_args = m[2:7]
                                ext_job_id = m[1]
                                new_fut = executor.submit(run_single_match, exec_args)
                                in_flight[new_fut] = (exec_args[0], exec_args[1], ext_job_id)
                            else:
                                if m[0] not in active_bots or m[1] not in active_bots:
                                    continue
                                new_fut = executor.submit(run_single_match, m)
                                in_flight[new_fut] = (m[0], m[1])
                        elif executor is not None:
                            # Refill queue when empty
                            matches = pick_matches(active_bots, h2h, ratings, n_picks=n_workers * 2)
                            for ma, mb in matches:
                                match_queue.append((ma, mb, bot_path(ma), bot_path(mb), n_pairs))
                            if match_queue:
                                m = match_queue.popleft()
                                is_ext = len(m) == 7 and m[0] == "external"
                                if is_ext:
                                    exec_args = m[2:7]
                                    ext_job_id = m[1]
                                    new_fut = executor.submit(run_single_match, exec_args)
                                    in_flight[new_fut] = (exec_args[0], exec_args[1], ext_job_id)
                                else:
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

                    # Eval round finalization check
                    try:
                        if eval_round_mgr.is_active and eval_round_mgr.is_round_complete():
                            eval_round_mgr.finish_round(h2h_data=h2h)
                    except Exception as er_err:
                        if args.verbose:
                            log.warning("Eval round finalization error (non-fatal): %s", er_err)

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
                            last_bot_refresh_time = time.time()  # Reset timer since we just refreshed
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
                            # Filter match_queue: preserve external jobs (len==7, m[0]=="external")
                            if removed:
                                match_queue = deque(
                                    m for m in match_queue
                                    if (len(m) == 7 and m[0] == "external")
                                    or (m[0] not in removed and m[1] not in removed)
                                )
                                for fut in list(in_flight):
                                    entry = in_flight[fut]
                                    is_ext = len(entry) == 3
                                    if is_ext:
                                        a, b, _ = entry
                                    else:
                                        a, b = entry
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

                    # Time-based bot list refresh (every 30s safety net)
                    now = time.time()
                    if now - last_bot_refresh_time >= 30:
                        last_bot_refresh_time = now
                        new_bots = get_active_bots()
                        added = set(new_bots) - set(active_bots)
                        removed = set(active_bots) - set(new_bots)
                        if added or removed:
                            for b in removed:
                                ratings.pop(b, None)
                                bot_stats.pop(b, None)
                                h2h = {k: v for k, v in h2h.items() if b not in k.split(" vs ")}
                            for b in added:
                                if b not in ratings:
                                    ratings[b] = Glicko2Player()
                            active_bots = new_bots
                            if removed:
                                match_queue = deque(
                                    m for m in match_queue
                                    if (len(m) == 7 and m[0] == "external")
                                    or (m[0] not in removed and m[1] not in removed)
                                )
                                for fut in list(in_flight):
                                    entry = in_flight[fut]
                                    is_ext = len(entry) == 3
                                    if is_ext:
                                        fa, fb, _ = entry
                                    else:
                                        fa, fb = entry
                                    if fa in removed or fb in removed:
                                        fut.cancel()
                                        del in_flight[fut]
                            log.info("Time-based refresh: +%d -%d bots (total %d)", len(added), len(removed), len(active_bots))

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
                                    if (len(m) == 7 and m[0] == "external")
                                    or (m[0] not in removed and m[1] not in removed)
                                )
                                for fut in list(in_flight):
                                    entry = in_flight[fut]
                                    is_ext = len(entry) == 3
                                    if is_ext:
                                        fa, fb, _ = entry
                                    else:
                                        fa, fb = entry
                                    if fa in removed or fb in removed:
                                        fut.cancel()
                                        del in_flight[fut]

                    # --once mode: stop after first batch completes
                    if args.once and total_matches >= n_workers:
                        break
                break  # normal exit from inner while

            except (BrokenProcessPool, ConnectionRefusedError, OSError) as e:
                recovery_count += 1
                log.error("ProcessPool broken (recovery %d/%d): %s", recovery_count, MAX_POOL_RECOVERIES, e)
                # Write error results for any external jobs before clearing
                for fut in list(in_flight):
                    entry = in_flight[fut]
                    if len(entry) == 3:
                        a, b, ext_job_id = entry
                        if _SCHEDULER_AVAILABLE:
                            try:
                                write_result(BattleResult(
                                    job_id=ext_job_id,
                                    wins_a=0, wins_b=0, draws=0, total=0,
                                    error="daemon_pool_broken",
                                    completed_at=time.time(),
                                    source="scheduler",
                                ))
                            except Exception as wr_err:
                                log.warning("write_result(recovery) failed for %s: %s", ext_job_id, wr_err)
                    try:
                        fut.result(timeout=1)
                    except Exception:
                        pass
                in_flight.clear()
                try:
                    executor.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
                try:
                    import multiprocessing as _mp
                    mp_ctx = _mp.get_context("spawn")
                    executor = ProcessPoolExecutor(max_workers=n_workers, mp_context=mp_ctx)
                    # Preserve external jobs from old queue before discarding
                    old_external = [m for m in match_queue if isinstance(m, tuple) and len(m) == 7 and m[0] == "external"]
                    match_queue = deque(old_external)
                    # Rebuild internal matches on top of preserved externals
                    matches = pick_matches(active_bots, h2h, ratings, n_picks=n_workers * 2)
                    for a, b in matches:
                        match_queue.append((a, b, bot_path(a), bot_path(b), n_pairs))
                    while len(in_flight) < n_workers and match_queue:
                        m = match_queue.popleft()
                        is_ext = len(m) == 7 and m[0] == "external"
                        if is_ext:
                            exec_args = m[2:7]
                            ext_job_id = m[1]
                            fut = executor.submit(run_single_match, exec_args)
                            in_flight[fut] = (exec_args[0], exec_args[1], ext_job_id)
                        else:
                            fut = executor.submit(run_single_match, m)
                            in_flight[fut] = (m[0], m[1])
                except (ConnectionRefusedError, OSError) as recover_exc:
                    log.error("Failed to create new process pool after break: %s. Will retry next cycle.", recover_exc)
                    # Don't re-raise — let the daemon continue and retry on next save cycle
                    executor = None
                    in_flight.clear()  # Prevent next loop iteration from accessing None executor

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
        # Shutdown executor first (workers + their bot subprocesses)
        try:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

        # Cancel any in-progress eval round
        try:
            eval_round_mgr.cancel_round()
        except Exception:
            pass

        # Kill entire process group (stray bot subprocesses)
        try:
            os.killpg(os.getpgrp(), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

        # Final save
        try:
            save_cycle(ratings, h2h, bot_stats, stats, save_num + 1, active_bots,
                       played_bots=played_bots_this_cycle, verbose=args.verbose)
        except Exception as e:
            log.warning("Final save failed: %s", e)
        log.info("Shutdown complete. %d matches processed.", total_matches)


if __name__ == "__main__":
    main()
