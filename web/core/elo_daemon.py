"""
Background Rating Daemon for Poker Bot Evolution.

Continuously runs mirror battles between active bots. Uses per-game Elo
updates and maintains a Head-to-Head win/loss matrix. Continuous scheduling
eliminates idle cores.

Usage:
    python web/core/elo_daemon.py --pairs 5 --workers 12 --verbose
"""

import os
import sys
import json
import random
import signal
import argparse
import asyncio
import time
import multiprocessing
import threading
from collections import Counter, deque
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED

# Battle Scheduler integration (optional)
import logging

log = logging.getLogger("pok.daemon")

# Stable identifier for this daemon run, stamped into every rating_history
# snapshot. Lets readers (stagnation_analyzer, etc.) filter to the single
# continuous timeline produced by THIS run, ignoring snapshots from prior runs
# that were concatenated into the same file (which corrupt trend analysis).
# Set once in main(); remains None for ad-hoc save_ratings() calls outside a run.
import uuid as _uuid
daemon_run_id: str | None = None

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
from system_log import log_system_event  # Group B: structured events for SIGTERM/orphan source attribution

CORE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CORE_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(CORE_DIR))

from glicko2 import Glicko2Player, update_rating_period, decay_rd
from engine.battle import mirror_battle
from evolution_infra import (
    pair_key,
    get_active_bots, load_ratings,
    read_locked_json, write_locked_json, append_locked_jsonl,
    update_h2h, update_bot_stats,
)
from bot_action_stats import compute_all_bot_stats, get_global_stats
from eval_rounds import EvalRoundManager
from workflow_profiles import get_workflow_profile

BOTS_DIR = PROJECT_ROOT / "bots"
RESULTS_DIR = CORE_DIR / "results"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
STATS_FILE = RESULTS_DIR / "elo_daemon_stats.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"
BOT_STATS_FILE = RESULTS_DIR / "bot_stats.json"
REPLAY_DIR = RESULTS_DIR / "match_replay"
# A1 (INERTNESS fix, evolution-plan-refresh-jun21): per-bot stderr telemetry.
# The daemon now captures bot stderr (FOLD_GATE_FIRE / SB_OPEN_OPP_SIZE / ...) that
# _PersistentBot previously discarded; grep these files to verify detector firing.
TELEMETRY_DIR = RESULTS_DIR / "bot_telemetry"
MATCH_HISTORY_FILE = RESULTS_DIR / "match_history.jsonl"
MAX_REPLAY_FILES = 2000

# JSONL rotation limits (lines kept after rotation)
MAX_RATING_HISTORY_LINES = 3000
MAX_MATCH_HISTORY_LINES = 15000
MAX_SYSTEM_EVENTS_LINES = 5000

# Match selection priority weights
UNDER_EVAL_WEIGHT = 0.6
DIVERSITY_WEIGHT = 0.4
UNDER_EVAL_BASELINE = 90
RATING_GAP_SCALE = 200
DIVERSITY_COUNT_DECAY = 100

# Continuous scheduling parameters
SAVE_EVERY_N_GAMES = 20
SAVE_INTERVAL_SEC = 60
POLL_TIMEOUT = 0.5
# v193 root-cause-audit (2026-06-26): the daemon's main loop was `while running
# and in_flight` — once in_flight emptied (all mirror battles done / reap canceled
# matches / pool recovered) the loop exited, and any external (precommit) job
# submitted AFTER that point was never drained (stayed queued in battle_jobs.jsonl
# forever). This keep-alive lets the daemon idle-spin and keep draining external
# jobs even with empty in_flight. DAEMON_IDLE_MAX_SEC bounds the idle spin so a
# truly idle daemon (no external jobs arriving) eventually exits and the monitor
# can restart it; any external job submission resets the idle timer. Must exceed
# the longest plausible precommit submit→drain gap (a generation's master+workers
# phase can take ~20-40min before precommit submits jobs).
DAEMON_IDLE_MAX_SEC = 1800  # 30 min idle cap
# Heartbeat freshness: is_daemon_scheduler_capable() treats the daemon as
# unhealthy if the heartbeat written into .daemon_pid is older than this. Must
# exceed the main-loop iteration cadence (POLL_TIMEOUT=0.5s × per-iteration work,
# including periodic save_cycle which can take seconds) with comfortable margin
# so a healthy busy daemon is never misclassified as stalled.
HEARTBEAT_STALE_SEC = 120

running = True

PICK_MATCH_LOG_INTERVAL_SEC = float(os.environ.get("POK_PICK_MATCH_LOG_INTERVAL_SEC", "30"))
ACTION_STATS_REFRESH_INTERVAL_SEC = float(os.environ.get("POK_ACTION_STATS_REFRESH_INTERVAL_SEC", "30"))
_pick_match_log_state: dict[str, object] = {"last_signature": None, "last_ts": 0.0}
OFFICIAL_JOB_RECONCILE_INTERVAL_SEC = float(os.environ.get(
    "POK_OFFICIAL_JOB_INTERVAL_SEC",
    os.environ.get("POK_OFFICIAL_QUEUE_INTERVAL_SEC", "60"),
))
OFFICIAL_JOB_RECONCILE_LIMIT = max(1, int(os.environ.get(
    "POK_OFFICIAL_JOB_LIMIT",
    os.environ.get("POK_OFFICIAL_QUEUE_LIMIT", "1"),
)))


def _write_heartbeat(scheduler_capable=True):
    """Refresh the last_heartbeat field in .daemon_pid atomically.

    v193 root-cause-audit (2026-06-26): the daemon only wrote .daemon_pid once
    at startup, so is_daemon_scheduler_capable() (a static `scheduler_capable`
    flag) could not detect a stalled main loop (process alive but not draining
    jobs). A fresh heartbeat lets the liveness probe treat the daemon as healthy
    only while the main loop is actually iterating. Safe no-op if the pid file
    is missing/invalid (e.g. between restarts).
    """
    try:
        from evolution_infra import RESULTS_DIR
        pid_file = RESULTS_DIR / ".daemon_pid"
        if not pid_file.exists():
            return
        raw = pid_file.read_text().strip()
        try:
            info = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            info = {}
        info["last_heartbeat"] = time.time()
        # C3: use a heartbeat-specific temp name to avoid colliding with
        # start_daemon's ".daemon_pid.tmp" when an orphan-cleanup restart races
        # with a heartbeat write (both used the identical path -> torn JSON ->
        # liveness probe false-negative). Also fsync before atomic replace so a
        # crash/power loss can't leave an empty/torn PID file.
        tmp = pid_file.with_suffix(".hb.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, json.dumps(info).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(pid_file))
    except Exception:
        # Heartbeat is advisory; never let it crash the main loop.
        pass


def start_official_certification_thread():
    """Process official EXE certification jobs without blocking quality gates."""
    enabled = os.environ.get(
        "POK_OFFICIAL_JOB_RECONCILER",
        os.environ.get("POK_OFFICIAL_QUEUE_WORKER", "1"),
    )
    if enabled.strip().lower() in {"0", "false", "off", "no"}:
        log.info("Official certification job reconciler disabled")
        return None

    interval = max(5.0, OFFICIAL_JOB_RECONCILE_INTERVAL_SEC)
    limit = OFFICIAL_JOB_RECONCILE_LIMIT

    def _worker():
        log.info("Official certification job reconciler started (interval=%ss, limit=%s)", interval, limit)
        while running:
            try:
                from official_certification_job import reconcile_jobs

                result = reconcile_jobs(limit=limit)
                if result.get("processed") or result.get("errors"):
                    log.info(
                        "Official certification jobs processed=%s remaining=%s errors=%s lock_busy=%s",
                        result.get("processed"),
                        result.get("remaining"),
                        result.get("errors") or [],
                        result.get("lock_busy"),
                    )
                    try:
                        log_system_event(
                            "official_certification.jobs_reconciled",
                            "warn" if result.get("errors") else "info",
                            "Official certification jobs reconciled in daemon background worker",
                            {
                                "processed": result.get("processed"),
                                "remaining": result.get("remaining"),
                                "lock_busy": result.get("lock_busy"),
                                "errors": result.get("errors") or [],
                                "results": result.get("results") or [],
                            },
                        )
                    except Exception:
                        pass
            except Exception as exc:
                log.warning("Official certification job reconciler failed: %s", exc)
                try:
                    log_system_event(
                        "official_certification.job_reconciler_failed",
                        "warn",
                        f"Official certification job reconciler failed: {type(exc).__name__}",
                        {"error": str(exc)[:500]},
                    )
                except Exception:
                    pass

            deadline = time.time() + interval
            while running and time.time() < deadline:
                time.sleep(min(1.0, deadline - time.time()))

    thread = threading.Thread(target=_worker, name="official-certification-jobs", daemon=True)
    thread.start()
    return thread


def handle_signal(signum, frame):
    global running
    # Group B diagnostic (root-cause-audit follow-up 2026-06-22): record the
    # signal NAME + active thread count so app.log can distinguish a SIGTERM from
    # stop_daemon (clean shutdown) vs an external kill / OOM precursor. A signal
    # carries no sender PID, but the thread snapshot helps tell "stuck in
    # save_cycle" (many threads, fcntl contention) from "normal idle" apart.
    try:
        _sig_name = signal.Signals(signum).name
    except (ValueError, AttributeError):
        _sig_name = str(signum)
    try:
        _threads = threading.active_count()
        log.warning("Received signal %s (%d) — shutting down gracefully. active_threads=%d",
                    _sig_name, signum, _threads)
        log_system_event(
            "daemon.signal_received",
            "warn",
            f"Daemon received {_sig_name} ({signum}); shutting down gracefully",
            {
                "signal": _sig_name,
                "signum": signum,
                "active_threads": _threads,
                "pid": os.getpid(),
                "shutdown_requested": True,
            },
        )
    except Exception:
        log.warning("Received signal %d, shutting down gracefully...", signum)
    running = False


def _handle_pool_break_for_shutdown(exc):
    """Return True when a pool break is an expected side-effect of shutdown."""
    if running:
        return False
    msg = f"ProcessPool interrupted during daemon shutdown; skipping recovery: {exc}"
    log.info(msg)
    try:
        log_system_event(
            "daemon.pool_shutdown_interrupt",
            "info",
            "ProcessPool interrupted during daemon shutdown; skipping recovery",
            {"error": str(exc)[:500]},
        )
    except Exception:
        pass
    return True


def bot_path(bot_name):
    return str(BOTS_DIR / bot_name / "main.py")


def save_ratings(ratings, save_num=None):
    from evaluation_data_identity import current_evaluation_digest

    os.makedirs(RESULTS_DIR, exist_ok=True)
    evaluation_identity_digest = current_evaluation_digest(RESULTS_DIR)
    data = {}
    for name, p in ratings.items():
        d = p.to_dict()
        d["last_period"] = datetime.now().isoformat(timespec="seconds")
        data[name] = d
    write_locked_json(RATINGS_FILE, data)

    if save_num is not None:
        history_file = RESULTS_DIR / "rating_history.jsonl"
        # Compute H2H avg win rates for history snapshot
        h2h = load_h2h()
        try:
            from rating_snapshot import choose_h2h_source
            h2h = choose_h2h_source(list(ratings.keys()), h2h, MATCH_HISTORY_FILE)["h2h"]
        except Exception:
            pass
        bot_stats = load_bot_stats()
        from tool_helpers import compute_h2h_avg_winrate
        try:
            from rating_snapshot import build_strength_rows
            strength_rows = {
                row["name"]: row
                for row in build_strength_rows(
                    ratings,
                    bot_stats,
                    h2h,
                    active_bots=list(ratings.keys()),
                    match_history_path=MATCH_HISTORY_FILE,
                )
            }
        except Exception:
            strength_rows = {}
        win_rates = {}
        for name in ratings:
            wr = compute_h2h_avg_winrate(name, h2h)
            bs = bot_stats.get(name, {})
            games = bs.get("games", 0)
            strength = strength_rows.get(name, {})
            if wr is not None:
                win_rates[name] = {
                    "h2h_avg_wr": round(wr, 4),
                    "games": games,
                    "leaderboard_score": strength.get("leaderboard_score"),
                    "h2h_coverage": strength.get("h2h_coverage"),
                    "h2h_source": strength.get("h2h_source"),
                }
            elif games > 0:
                win_rates[name] = {
                    "games": games,
                    "leaderboard_score": strength.get("leaderboard_score"),
                    "h2h_coverage": strength.get("h2h_coverage"),
                    "h2h_source": strength.get("h2h_source"),
                }
        snapshot = {
            "period": save_num,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "daemon_run_id": daemon_run_id,  # None for ad-hoc calls; set in main()
            "evaluation_identity_digest": evaluation_identity_digest,
            "ratings": {name: {"r": p.r, "rd": p.rd, "sigma": p.sigma} for name, p in ratings.items()},
            "win_rates": win_rates,
        }
        append_locked_jsonl(history_file, snapshot)


def load_stats():
    return read_locked_json(STATS_FILE, default={"pairs": {}, "total_games": 0})


def save_stats(stats):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    write_locked_json(STATS_FILE, stats)


def load_h2h():
    from evaluation_data_identity import ensure_evaluation_data_identity

    ensure_evaluation_data_identity(RESULTS_DIR)
    return read_locked_json(H2H_FILE, default={})


def save_h2h(h2h):
    from evaluation_data_identity import ensure_evaluation_data_identity

    os.makedirs(RESULTS_DIR, exist_ok=True)
    ensure_evaluation_data_identity(RESULTS_DIR)
    # Prune low-sample entries (games < 2 have no statistical value)
    h2h = {k: v for k, v in h2h.items() if v.get("games", 0) >= 2}
    write_locked_json(H2H_FILE, h2h)


def _h2h_with_win_rates(h2h):
    out = {}
    for k, v in (h2h or {}).items():
        entry = dict(v)
        g = int(entry.get("games", 0) or 0)
        if g > 0:
            entry["win_rate"] = round((entry.get("a_wins", 0) + 0.5 * entry.get("draws", 0)) / g, 4)
        else:
            entry["win_rate"] = 0.5
        out[k] = entry
    return out


def load_bot_stats():
    return read_locked_json(BOT_STATS_FILE, default={})


def save_bot_stats(bot_stats):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    write_locked_json(BOT_STATS_FILE, bot_stats)


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
    try:
        data = read_locked_json(PRIORITY_EVAL_FILE)
        if not data:
            return None
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


def _log_pick_matches(selected_count: int, candidate_count: int, priority_bot, bot_count: int) -> None:
    """Keep daemon match-selection logs useful without flooding app.log.

    The daemon can refill the queue several times per second when the active
    pool is tiny and matches are fast. A per-call INFO line drowns out
    orchestrator, quality-gate, and official-platform signals. Log immediately
    when the scheduling shape changes, otherwise emit one heartbeat per
    interval and leave repeated details at DEBUG.
    """

    signature = (selected_count, candidate_count, priority_bot, bot_count)
    now = time.time()
    last_signature = _pick_match_log_state.get("last_signature")
    last_ts = float(_pick_match_log_state.get("last_ts") or 0.0)
    should_info = signature != last_signature or now - last_ts >= PICK_MATCH_LOG_INTERVAL_SEC
    log_fn = log.info if should_info else log.debug
    log_fn(
        "pick_matches: %d pairs from %d candidates (priority=%s, bots=%d)",
        selected_count,
        candidate_count,
        priority_bot,
        bot_count,
    )
    if should_info:
        _pick_match_log_state["last_signature"] = signature
        _pick_match_log_state["last_ts"] = now


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
    _log_pick_matches(len(selected), len(pairs), priority_bot, len(active_bots))
    return selected


def save_match_replay(
    a,
    b,
    wins_a,
    wins_b,
    draws,
    replay_data,
    net_chips_samples=None,
    strength_sample_unit=None,
):
    from evaluation_data_identity import current_evaluation_digest

    evaluation_identity_digest = current_evaluation_digest(RESULTS_DIR)
    os.makedirs(REPLAY_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    fname = f"{timestamp}_{a}_vs_{b}.json"
    net_chips_values = [int(value) for value in (net_chips_samples or [])]
    strength_summary = None
    if strength_sample_unit == "70_hand_match":
        from strength_order import summarize_70_hand_net_chips

        if not net_chips_values:
            raise ValueError("70-hand strength replay must contain at least one sample")
        if len(replay_data or []) != len(net_chips_values):
            raise ValueError("70-hand strength replay rows disagree with sample count")
        for index, replay in enumerate(replay_data or []):
            if not isinstance(replay, dict):
                raise ValueError(f"70-hand strength replay {index} is not an object")
            if int(replay.get("hands_played", 0) or 0) != 70:
                raise ValueError(f"70-hand strength replay {index} is incomplete")
            if replay.get("passed_compliance") is not True:
                raise ValueError(f"70-hand strength replay {index} failed compliance")
            if replay.get("wrapper_used") is True:
                raise ValueError(f"70-hand strength replay {index} used a legacy wrapper")
        strength_summary = summarize_70_hand_net_chips(net_chips_values)
        if (
            strength_summary["positive_matches"] != int(wins_a)
            or strength_summary["negative_matches"] != int(wins_b)
            or strength_summary["zero_matches"] != int(draws)
        ):
            raise ValueError("70-hand net-chip samples disagree with recorded match outcomes")
    match_data = {
        "id": fname,
        "timestamp": timestamp,
        "bot0": a,
        "bot1": b,
        "bot0_wins": wins_a,
        "bot1_wins": wins_b,
        "draws": draws,
        "evaluation_identity_digest": evaluation_identity_digest,
        "strength_sample_unit": strength_sample_unit,
        "hands_per_strength_sample": 70 if strength_summary is not None else None,
        "strength_admitted": strength_summary is not None,
        "strength_complete": strength_summary is not None,
        "strength_compliance_passed": strength_summary is not None,
        "strength_sample_count": strength_summary.get("samples", 0) if strength_summary else 0,
        "net_chips_bot0": net_chips_values,
        "strength_order": strength_summary,
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
            "evaluation_identity_digest": evaluation_identity_digest,
            "strength_sample_unit": strength_sample_unit,
            "hands_per_strength_sample": 70 if strength_summary is not None else None,
            "strength_admitted": strength_summary is not None,
            "strength_complete": strength_summary is not None,
            "strength_compliance_passed": strength_summary is not None,
            "strength_sample_count": strength_summary.get("samples", 0) if strength_summary else 0,
            "net_chips_bot0": net_chips_values,
            "strength_order": strength_summary,
        }
        append_locked_jsonl(MATCH_HISTORY_FILE, summary)
    except Exception as e:
        log.warning("Match history write failed: %s", e)
        try:
            replay_path.unlink()
        except OSError:
            pass
        raise

    return fname


def save_bot_telemetry(bot_a_name, bot_b_name, all_logs):
    """A1 (INERTNESS fix, evolution-plan-refresh-jun21): extract per-bot stderr from
    match logs and append to results/bot_telemetry/{bot}.jsonl so detector firing
    (FOLD_GATE_FIRE / SB_OPEN_OPP_SIZE / PROTECT_FLOOR / ...) can be grep-verified.

    The daemon path previously piped bot stderr to /dev/null, making every detector's
    runtime firing UNVERIFIABLE for 6+ generations. _PersistentBot now drains stderr
    into the per-decision log entry; this function aggregates it per match per bot.
    Player key "0" -> bot_a, "1" -> bot_b (bot_paths order in mirror_battle)."""
    try:
        stderr_by_bot = {bot_a_name: [], bot_b_name: []}
        key_to_bot = {"0": bot_a_name, "1": bot_b_name}
        for game in all_logs:
            logs = game.get("logs") if isinstance(game, dict) else None
            if not isinstance(logs, list):
                continue
            for entry in logs:
                if not isinstance(entry, dict):
                    continue
                for pkey, bot_name in key_to_bot.items():
                    pinfo = entry.get(pkey)
                    if isinstance(pinfo, dict) and pinfo.get("stderr"):
                        stderr_by_bot[bot_name].append(pinfo["stderr"])
        os.makedirs(TELEMETRY_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        opponent_of = {bot_a_name: bot_b_name, bot_b_name: bot_a_name}
        for bot_name, chunks in stderr_by_bot.items():
            joined = "".join(chunks).strip()
            if not joined:
                continue
            entry = {
                "ts": ts,
                "opponent": opponent_of.get(bot_name, ""),
                "stderr": joined,
            }
            try:
                append_locked_jsonl(TELEMETRY_DIR / f"{bot_name}.jsonl", entry)
            except Exception as e:
                log.debug("Telemetry write failed for %s: %s", bot_name, e)
    except Exception as e:
        log.debug("Telemetry extraction failed (%s vs %s): %s", bot_a_name, bot_b_name, e)


def cleanup_old_replays():
    if not REPLAY_DIR.exists():
        return
    files = sorted(REPLAY_DIR.iterdir(), key=lambda f: f.name)
    if len(files) > MAX_REPLAY_FILES:
        for old_file in files[: len(files) - MAX_REPLAY_FILES]:
            old_file.unlink()


def _rating_protocol_config(n_pairs=None):
    profile = get_workflow_profile()
    protocol = os.environ.get(
        "POK_RATING_PROTOCOL",
        getattr(profile, "rating_protocol", "local_json"),
    )
    protocol = protocol if protocol in {"local_json", "national"} else "local_json"
    national_execution_mode = getattr(profile, "national_execution_mode", "adapter")
    if national_execution_mode == "native_tcp":
        # Production strength identity is immutable: an environment variable
        # may change sample count, but cannot switch backend or shorten a match.
        protocol = "national"
        national_hands = 70
    else:
        national_hands = int(os.environ.get(
            "POK_NATIONAL_RATING_HANDS",
            str(getattr(profile, "national_rating_hands", 70)),
        ))
    matches_override = "POK_NATIONAL_RATING_MATCHES" in os.environ
    national_matches = int(os.environ.get(
        "POK_NATIONAL_RATING_MATCHES",
        str(getattr(profile, "national_rating_matches", 1)),
    ))
    if n_pairs is not None and not matches_override:
        national_matches = max(national_matches, int(n_pairs))
    national_hands = max(1, min(70, national_hands))
    national_matches = max(1, min(8, national_matches))
    strict = os.environ.get("POK_NATIONAL_RATING_STRICT")
    if strict is None:
        strict_bool = bool(getattr(profile, "national_acceptance_hard", True))
    else:
        strict_bool = strict not in {"0", "false", "False", "no", "NO"}
    config = {
        "profile_id": getattr(profile, "profile_id", "default"),
        "protocol": protocol,
        "national_execution_mode": national_execution_mode,
        "national_hands": national_hands,
        "national_matches": national_matches,
        "strict": strict_bool,
    }
    if national_execution_mode == "native_tcp":
        from national_native import current_strength_runtime_overlay_identity

        config["native_strength_runtime_overlay"] = (
            current_strength_runtime_overlay_identity()
        )
    return config


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


def _run_local_json_match(bot_a_name, bot_b_name, bot_a_path, bot_b_path, n_pairs):
    """Run the legacy local JSON mirror-battle rating backend."""
    _match_wins, _draws, _n_played, all_logs, net_chips_list = mirror_battle(
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
        save_match_replay(
            bot_a_name,
            bot_b_name,
            games_a,
            games_b,
            games_draw,
            all_logs,
            list(net_chips_list or []),
            "legacy_mirror_pair",
        )
    except Exception as e:
        log.debug("Replay save failed: %s", e)

    # A1 (INERTNESS fix): capture bot stderr telemetry for detector firing
    # verification. Best-effort — never blocks match result processing.
    try:
        save_bot_telemetry(bot_a_name, bot_b_name, all_logs)
    except Exception as e:
        log.debug("Telemetry save failed: %s", e)

    return (bot_a_name, bot_b_name, games_a, games_b, games_draw, total, None, list(net_chips_list or []))


def _run_national_rating_match(bot_a_name, bot_b_name, bot_a_path, bot_b_path, config):
    """Run the national GameEngine rating backend and return daemon result shape."""
    hands = int(config["national_hands"])
    matches = int(config["national_matches"])
    strict = bool(config["strict"])
    native_tcp_mode = config.get("national_execution_mode") == "native_tcp"
    if native_tcp_mode and hands != 70:
        return (
            bot_a_name,
            bot_b_name,
            0,
            0,
            0,
            0,
            f"native_rating_contract: expected exactly 70 hands, got {hands}",
            [],
        )
    if native_tcp_mode:
        from national_native import run_current_runtime_native_strength_pair
    else:
        from national_acceptance import resolve_bot, run_pair
        bot_a = resolve_bot(bot_a_path)
        bot_b = resolve_bot(bot_b_path)
    wins_a = wins_b = draws = 0
    net_chips_list: list[int] = []
    replays: list[dict] = []
    issues: list[str] = []

    for repeat in range(matches):
        if native_tcp_mode:
            result = asyncio.run(run_current_runtime_native_strength_pair(
                bot_a_path,
                bot_b_path,
                hands,
                require_native_a=True,
                require_native_b=True,
            ))
        else:
            result = asyncio.run(run_pair(bot_a, bot_b, hands, strict=strict))
        replay = dict(result)
        replay["rating_protocol"] = "national_native_tcp" if native_tcp_mode else "national"
        replay["repeat"] = repeat + 1
        replays.append(replay)
        hands_played = int(result.get("hands_played", 0) or 0)
        if hands_played != hands:
            issues.append(f"repeat={repeat + 1}: hands_played={hands_played}/{hands}")
        if result.get("passed_compliance") is not True:
            reported = [str(item) for item in (result.get("issues") or [])]
            issues.extend(reported or [f"repeat={repeat + 1}: compliance_failed"])
        if native_tcp_mode and result.get("wrapper_used") is True:
            issues.append(f"repeat={repeat + 1}: native_wrapper_used")
        if native_tcp_mode:
            overlay = result.get("runtime_overlay") or {}
            if not (
                overlay.get("enabled") is True
                and overlay.get("both_sides") is True
                and overlay.get("mode") == "current_system_wrapper_bilateral"
            ):
                issues.append(f"repeat={repeat + 1}: native_runtime_overlay_missing")
        if issues:
            continue
        net = int(result.get("net_chips_a", 0) or 0)
        net_chips_list.append(net)
        if net > 0:
            wins_a += 1
        elif net < 0:
            wins_b += 1
        else:
            draws += 1

    total = wins_a + wins_b + draws
    if issues:
        # Fail the whole daemon batch closed.  In particular, do not write a
        # replay/history row that reconstruction could later mistake for
        # authoritative H2H evidence.
        return (
            bot_a_name,
            bot_b_name,
            0,
            0,
            0,
            0,
            ("native_rating_contract: " if native_tcp_mode else "national_rating_contract: ")
            + "; ".join(issues[:8]),
            [],
        )
    try:
        save_match_replay(
            bot_a_name,
            bot_b_name,
            wins_a,
            wins_b,
            draws,
            replays,
            net_chips_list,
            f"{hands}_hand_match",
        )
    except Exception as e:
        log.debug("National replay save failed: %s", e)

    return (bot_a_name, bot_b_name, wins_a, wins_b, draws, total, None, net_chips_list)


def run_single_match(args):
    """Run the configured rating backend and return lightweight daemon result."""
    bot_a_name, bot_b_name, bot_a_path, bot_b_path, n_pairs = args
    try:
        config = _rating_protocol_config(n_pairs=n_pairs)
        if (
            config["protocol"] == "national"
            and config.get("national_execution_mode") == "native_tcp"
        ):
            # The native runner is the single capacity owner for every caller
            # (daemon, quality gate, and precommit). Acquiring here as well can
            # deadlock all workers when every process holds an outer lease.
            return _run_national_rating_match(
                bot_a_name, bot_b_name, bot_a_path, bot_b_path, config
            )

        from runtime_capacity import acquire_match_slots

        with acquire_match_slots(
            f"daemon:{bot_a_name}:{bot_b_name}:{os.getpid()}",
            count=1,
        ):
            if config["protocol"] == "national":
                return _run_national_rating_match(
                    bot_a_name, bot_b_name, bot_a_path, bot_b_path, config
                )
            return _run_local_json_match(
                bot_a_name, bot_b_name, bot_a_path, bot_b_path, n_pairs
            )
    except Exception as e:
        return (bot_a_name, bot_b_name, 0, 0, 0, 0, str(e), [])


def process_result(result, ratings, h2h, bot_stats, verbose=False):
    """Process one completed match: update Elo, H2H, bot_stats."""
    a, b, wins_a, wins_b, draws, total, err, *_extra = result
    if err is not None:
        log.error("Error in %s vs %s: %s", a, b, err)
        return 0
    if total == 0:
        log.warning("Skipping 0-game match %s vs %s (all mirror games skipped) — no rating update", a, b)
        return 0

    log.debug("%s vs %s: %d-%d-%d (%d games)", a, b, wins_a, wins_b, draws, total)

    # Batch Glicko-2 update: snapshot pre-match ratings to avoid live-rating contamination
    _default = Glicko2Player()
    player_a = ratings.get(a, _default)
    player_b = ratings.get(b, _default)
    # Snapshot opponent ratings as they were before this match (carry last_play_period)
    opp_a_snapshot = Glicko2Player(player_a.r, player_a.rd, player_a.sigma,
                                   last_play_period=getattr(player_a, 'last_play_period', None))
    opp_b_snapshot = Glicko2Player(player_b.r, player_b.rd, player_b.sigma,
                                   last_play_period=getattr(player_b, 'last_play_period', None))

    # Build results lists for the rating period (all games in this match)
    results_a = []
    results_b = []
    for _ in range(wins_a):
        results_a.append((opp_b_snapshot, 1.0))
        results_b.append((opp_a_snapshot, 0.0))
    for _ in range(wins_b):
        results_a.append((opp_b_snapshot, 0.0))
        results_b.append((opp_a_snapshot, 1.0))
    for _ in range(draws):
        results_a.append((opp_b_snapshot, 0.5))
        results_b.append((opp_a_snapshot, 0.5))

    ratings[a] = update_rating_period(player_a, results_a)
    ratings[b] = update_rating_period(player_b, results_b)

    # Update H2H (one call per individual game)
    for _ in range(wins_a):
        update_h2h(h2h, a, b, wins_a=1, wins_b=0)
    for _ in range(wins_b):
        update_h2h(h2h, a, b, wins_a=0, wins_b=1)
    for _ in range(draws):
        update_h2h(h2h, a, b, wins_a=0, wins_b=0, draws=1)

    # Update bot stats (one call per individual game)
    for _ in range(wins_a):
        update_bot_stats(bot_stats, a, wins=1, losses=0)
        update_bot_stats(bot_stats, b, wins=0, losses=1)
    for _ in range(wins_b):
        update_bot_stats(bot_stats, a, wins=0, losses=1)
        update_bot_stats(bot_stats, b, wins=1, losses=0)
    for _ in range(draws):
        update_bot_stats(bot_stats, a, wins=0, losses=0, draws=1)
        update_bot_stats(bot_stats, b, wins=0, losses=0, draws=1)

    return total


# Phase 0 follow-up: bot_action_stats scan runs ~260s for 2000 replays and would
# block the daemon main scheduling loop if called synchronously in save_cycle (observed
# stalling match cadence from ~10s to ~15min). Stats feed only the Master-prompt
# injection — no commit gate depends on them — so an async background refresh with a
# one-cycle-stale read is acceptable.
_action_stats_thread = None
_last_action_stats_refresh_start = 0.0


def _refresh_action_stats_async(active_bots):
    """Kick off a background bot_action_stats refresh; non-blocking.

    Skips if a previous scan is still running (avoids thread pile-up when
    save_cycle fires faster than the scan completes). The on-disk
    bot_action_stats.json is updated in the worker via write_locked_json
    (fcntl-protected), so concurrent readers stay safe.
    """
    global _action_stats_thread, _last_action_stats_refresh_start
    if _action_stats_thread is not None and _action_stats_thread.is_alive():
        return
    now = time.monotonic()
    if now - _last_action_stats_refresh_start < ACTION_STATS_REFRESH_INTERVAL_SEC:
        return
    _last_action_stats_refresh_start = now

    bots_snapshot = list(active_bots)

    def _worker():
        try:
            t0 = time.perf_counter()
            etag_path = REPLAY_DIR / ".stats_etag.json"
            per_opp = compute_all_bot_stats(
                bots_snapshot, REPLAY_DIR, force_full=False, etag_path=etag_path
            )
            flat = {
                bot: get_global_stats(per_opp, bot)
                for bot in bots_snapshot
                if bot in per_opp
            }
            write_locked_json(RESULTS_DIR / "bot_action_stats.json", flat)
            # Phase 3: also persist the per-opponent breakdown (nested shape).
            # The flat file above is unchanged to keep every legacy reader
            # working; this new file is consumed ONLY by the Master
            # opponent-profile injection (tool_planning.run_master). Advisory:
            # a write failure here is caught by the surrounding try/except and
            # only degrades the Master prompt (no gate depends on it).
            write_locked_json(RESULTS_DIR / "bot_action_stats_per_opp.json", per_opp)
            log.info(
                "bot_action_stats scan: %.2fs, %d bots (async incremental etag @ %s)",
                time.perf_counter() - t0, len(flat), etag_path.name,
            )

            # Phase 3: MAP-Elites behavior archive (advisory diversity signal).
            # Re-scans replays for per-bot behavior fingerprints, discretizes
            # into a 5x5 aggression x looseness grid, keeps the max-fitness bot
            # per niche (fitness = h2h avg win_rate). No gate/reap reads this
            # yet; it is a write-only MVP for population-diversity telemetry.
            # Wrapped in its own try/except so a failure here does not abort
            # the stats refresh mid-loop.
            # Behavior archive (MAP-Elites, advisory diversity telemetry).
            # RE-ENABLED 2026-06-17: _scan_behavior_fingerprints is now
            # streaming+incremental (etag-tracked accumulator in
            # .behavior_acc.json), so peak memory = one replay file (~2-8MB)
            # instead of the full 4.2GB history. The old full-read version OOM-
            # killed the daemon (rc=-9 every 2-3 min); the incremental fix
            # resolves it. Still advisory-only (no reap/gate reads the archive).
            try:
                from map_elites import write_behavior_archive
                _wr_map = None
                try:
                    from tool_helpers import compute_h2h_avg_winrate, _load_h2h_data
                    _h2h_snap = _load_h2h_data()
                    _wr_map = {
                        b: (compute_h2h_avg_winrate(b, _h2h_snap) or 0.5)
                        for b in bots_snapshot
                    }
                except Exception:
                    pass
                write_behavior_archive(REPLAY_DIR, bots_snapshot, h2h_winrates=_wr_map)
            except Exception as _me:
                log.warning("Behavior archive write failed (non-fatal): %s", _me)
        except Exception as e:
            log.warning("Bot action stats computation failed (non-fatal): %s", e)

    _action_stats_thread = threading.Thread(
        target=_worker, daemon=True, name="action-stats-refresh"
    )
    _action_stats_thread.start()


def save_cycle(ratings, h2h, bot_stats, stats, save_num, active_bots,
               played_bots=None, verbose=False):
    """Write all data files to disk. Apply RD decay to bots that didn't play.

    RD decay now uses the TRUE number of rating periods elapsed since each idle
    bot last competed (Glickman Step 1b: phi* = sqrt(phi^2 + sigma^2 * t)),
    rather than a flat one-period hit per save cycle. A bot idle for N cycles
    receives N periods of uncertainty growth at once. The per-save-cycle sawtooth
    (play -> RD drops -> idle one cycle -> RD snaps back) is eliminated.

    Bots that played this cycle are "refreshed": their last_play_period is stamped
    to the current save_num so future idle spans are measured from now.
    """
    if played_bots is not None:
        for b in active_bots:
            if b not in ratings:
                continue
            p = ratings[b]
            if b in played_bots:
                # Refreshed this cycle — stamp last_play_period so idle spans reset.
                ratings[b] = Glicko2Player(p.r, p.rd, p.sigma, last_play_period=save_num)
            else:
                # Idle: grow RD by the number of periods actually elapsed since
                # the bot last played. last_play_period is None only for a
                # never-played bot (still at default rd=350); treat as one period.
                last_per = getattr(p, 'last_play_period', None)
                elapsed = (save_num - last_per) if last_per is not None else 1
                # Clamp to guard against corrupted/gapped period counters
                # (e.g. a daemon restart that jumps save_num). 50 periods of idle
                # already pushes RD well past DEFAULT_RD, so this only bounds abuse.
                elapsed = max(1, min(elapsed, 50))
                ratings[b] = decay_rd(p, elapsed)
    # Recompute win rates for H2H. Prefer a match_history rebuild when the
    # append-only history covers more active-pool pairs than the in-memory/file
    # matrix. This prevents sparse H2H snapshots from driving the leaderboard
    # and evolution choices after daemon restarts or partial saves.
    h2h_out = _h2h_with_win_rates(h2h)
    try:
        from rating_snapshot import choose_h2h_source
        h2h_selection = choose_h2h_source(active_bots, h2h_out, MATCH_HISTORY_FILE)
        if h2h_selection["source"] == "match_history_rebuilt":
            rebuilt = _h2h_with_win_rates(h2h_selection["h2h"])
            stored_cov = h2h_selection["stored_coverage"]
            rebuilt_cov = h2h_selection["rebuilt_coverage"]
            h2h_out = rebuilt
            h2h.clear()
            h2h.update(rebuilt)
            log_system_event(
                "rating.h2h_rebuilt_from_history",
                "warn",
                "Rebuilt active H2H matrix from match_history because stored coverage was lower",
                {
                    "save_num": save_num,
                    "stored_pairs": stored_cov.get("covered_pairs"),
                    "rebuilt_pairs": rebuilt_cov.get("covered_pairs"),
                    "total_pairs": rebuilt_cov.get("total_pairs"),
                    "stored_coverage": round(stored_cov.get("coverage", 0.0), 4),
                    "rebuilt_coverage": round(rebuilt_cov.get("coverage", 0.0), 4),
                },
            )
    except Exception as e:
        log.debug("H2H rebuild check failed (non-fatal): %s", e)
    save_h2h(h2h_out)

    save_bot_stats(bot_stats)

    # Write ratings/history after the H2H and bot_stats snapshots so
    # rating_history.win_rates reflects the same save cycle, not the previous
    # on-disk matrix.
    save_ratings(ratings, save_num=save_num)

    # Update legacy stats for backward compat
    stats["total_games"] = sum(v["games"] for v in bot_stats.values()) // 2
    stats["pairs"] = {k: v["games"] for k, v in h2h_out.items()}
    save_stats(stats)

    cleanup_old_replays()

    # Rotate growing JSONL files to prevent unbounded growth
    _rotate_jsonl(RESULTS_DIR / "rating_history.jsonl", MAX_RATING_HISTORY_LINES)
    _rotate_jsonl(MATCH_HISTORY_FILE, MAX_MATCH_HISTORY_LINES)
    # Note: system_events.jsonl is written by web process, rotated by system_log.py

    # fix-2: Backfill real rating_delta into critic_calibration.jsonl for bots
    # whose ratings have converged. Non-blocking best-effort.
    try:
        from agent_review import reconcile_critic_calibration
        reconcile_critic_calibration(ratings, bot_stats)
    except Exception as e:
        log.debug("Calibration reconcile failed (non-fatal): %s", e)

    # fix-12: Backfill rating_delta outcomes into experience_attribution sidecar
    # for lessons whose source-gen bot has converged. Feeds Ratchet retire so
    # repeatedly-tried lessons get retired. Hurt signal = rating_delta < 0
    # (continuous), NOT precommit_passed (always True at commit → would be INERT).
    try:
        from experience_attribution import reconcile_lesson_outcomes
        reconcile_lesson_outcomes(ratings, bot_stats)
    except Exception as e:
        log.debug("Lesson attribution reconcile failed (non-fatal): %s", e)

    # Compute bot_action_stats ASYNCHRONOUSLY (Phase 0 follow-up): the full replay
    # scan is expensive (~260s for 2000 replays) and blocks the main scheduling loop
    # when run synchronously. Stats feed only the Master-prompt injection (no commit
    # gate depends on them), so a one-cycle-stale read is acceptable. Non-blocking.
    _refresh_action_stats_async(active_bots)

    if verbose:
        # Compute H2H avg win rates for leaderboard
        from tool_helpers import compute_h2h_avg_winrate, load_strength_scores
        strength_scores = load_strength_scores()
        bot_wr_map = {b: compute_h2h_avg_winrate(b, h2h_out) or 0.0 for b in active_bots}
        sorted_bots = sorted(active_bots, key=lambda b: strength_scores.get(b, 0.0), reverse=True)
        log.info("Leaderboard (save #%d):", save_num)
        for i, b in enumerate(sorted_bots):
            p = ratings[b]
            bs = bot_stats.get(b, {})
            wr = bs.get("win_rate", 0.0)
            g = bs.get("games", 0)
            hwr = bot_wr_map.get(b, 0.0)
            score = strength_scores.get(b, 0.0)
            log.info("  %d. %s: score=%.4f h2h_avg_wr=%.2f%% r=%.1f rd=%.1f wr=%.2f%% (%d games)",
                     i + 1, b, score, hwr * 100, p.r, p.rd, wr * 100, g)


def _is_external(m):
    """A queued job is an external (precommit/eval) job iff it's the external tuple form.

    External job tuple shape: ("external", job_id, a, b, path_a, path_b, n_pairs[, priority]).
    The trailing `priority` element (Phase 0 F) is optional for back-compat.
    """
    return (isinstance(m, tuple) and len(m) in (7, 8) and m[0] == "external")


def _external_priority(m):
    """Effective priority for an external job tuple.

    External job tuple shape: ("external", job_id, a, b, path_a, path_b, n_pairs[, priority])
    New submissions carry an 8th element = BattleJob.priority (higher = more urgent).
    Legacy/unknown external jobs default to priority 0. precommit_eval jobs always rank
    strictly above any daemon full-pool (internal) match.
    """
    if len(m) >= 8:
        try:
            return int(m[7])
        except (TypeError, ValueError):
            return 0
    return 0


def _pop_next_job(match_queue):
    """Pop the highest-priority queued job, external-jobs-first.

    Scheduler priority (Phase 0 F): precommit external jobs preempt daemon full-pool
    matches. Among external jobs, higher `priority` (BattleJob.priority) wins. Internal
    matches are only popped when no external job remains. This keeps the daemon's full
    pool evaluation healthy while guaranteeing precommit jobs never starve behind a
    wall of daemon-generated matches.

    Returns the job tuple, or None if the queue is empty.
    """
    if not match_queue:
        return None
    # Fast path: most iterations have at most one external job (the one just drained).
    # Find the best external job if any exist; else pop the first internal match.
    best_ext_idx = None
    best_ext_pri = None
    for idx, m in enumerate(match_queue):
        if not _is_external(m):
            continue
        pri = _external_priority(m)
        if best_ext_idx is None or pri > best_ext_pri:
            best_ext_idx = idx
            best_ext_pri = pri
    if best_ext_idx is not None:
        # deque has no O(1) index-delete; rotate to pop. The queue is short (≤ a few
        # hundred entries in pathological cases, usually <20), so this is fine.
        m = match_queue[best_ext_idx]
        del match_queue[best_ext_idx]
        return m
    return match_queue.popleft()


def _log_external_dispatch(job_id, bot_a, bot_b, source, in_flight_count, queue_count):
    try:
        log_system_event(
            "daemon.external_job_dispatched", "info",
            f"Dispatched external job {job_id}: {bot_a} vs {bot_b} ({source})",
            {
                "job_id": job_id,
                "bot_a": bot_a,
                "bot_b": bot_b,
                "source": source,
                "in_flight": in_flight_count,
                "queue": queue_count,
            },
        )
    except Exception:
        pass


def _log_external_result(job_id, bot_a, bot_b, result=None, error=None):
    try:
        payload = {"job_id": job_id, "bot_a": bot_a, "bot_b": bot_b}
        severity = "info"
        message = f"External job {job_id} completed: {bot_a} vs {bot_b}"
        if error is not None:
            severity = "warn"
            message = f"External job {job_id} failed: {bot_a} vs {bot_b}"
            payload["error"] = str(error)[:500]
        elif result is not None:
            payload.update({
                "wins_a": result[2],
                "wins_b": result[3],
                "draws": result[4],
                "total": result[5],
                "error": result[6] if len(result) > 6 and result[6] else None,
                "net_chips_samples": len(result[7]) if len(result) > 7 and result[7] else 0,
            })
        log_system_event("daemon.external_job_result", severity, message, payload)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Background Rating Daemon")
    parser.add_argument("--pairs", type=int, default=5, help="Mirror pairs per match")
    parser.add_argument("--workers", type=int, default=max(1, min(12, int(multiprocessing.cpu_count() * 28 / 32))), help="Parallel workers (capped at 12 to avoid OOM; see MAX_SAFE_DAEMON_WORKERS)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print match results")
    parser.add_argument("--once", action="store_true", help="Run ~14 matches then exit")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    global running, daemon_run_id
    # Stamp this run with a unique id so rating_history readers can isolate the
    # single continuous timeline produced here, ignoring stale concatenated runs.
    daemon_run_id = str(_uuid.uuid4())[:8]
    log.info("Daemon run id: %s", daemon_run_id)

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
    _backend_config = _rating_protocol_config(n_pairs=args.pairs)
    from evaluation_data_identity import ensure_evaluation_data_identity

    ensure_evaluation_data_identity(
        RESULTS_DIR,
        runtime_profile=_backend_config,
    )
    log.info(
        "Rating backend: profile=%s protocol=%s execution=%s national_hands=%s national_matches=%s strict=%s",
        _backend_config["profile_id"],
        _backend_config["protocol"],
        _backend_config["national_execution_mode"],
        _backend_config["national_hands"],
        _backend_config["national_matches"],
        _backend_config["strict"],
    )
    try:
        log_system_event(
            "daemon.rating_backend",
            "info",
            f"Rating daemon backend: {_backend_config['protocol']}",
            _backend_config,
        )
    except Exception:
        pass

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
    # External (precommit) job drain capacity: how many external jobs we keep staged in
    # the match_queue per poll cycle. With priority-aware dispatch (_pop_next_job),
    # queued external jobs always grab the next freed worker slot before any daemon
    # full-pool match, so a higher buffer lowers precommit queueing latency without
    # starving the full pool (internal matches still fill all remaining slots).
    _capacity = max(2, n_workers // 2)

    # Start battle experience background thread (non-fatal if unavailable)
    try:
        from battle_experience import start_experience_thread
        start_experience_thread()
        log.info("Battle experience background thread started")
    except Exception as e:
        log.warning("Battle experience thread failed to start (non-fatal): %s", e)

    try:
        start_official_certification_thread()
    except Exception as e:
        log.warning("Official certification job reconciler failed to start (non-fatal): %s", e)

    # 预留 worker slot 给 external(precommit) job。root-cause-audit 2026-06-21: daemon
    # 启动即用 internal matches 填满全部 n_workers 槽 → 外部 precommit job 只能在某个
    # in-flight internal match 完成后才拿到 slot（app.log "Drained 3 pending" → "Collected
    # 0/3" 数十分钟），precommit 永走慢路径 ~22min/代。预留 + _pop_next_job external-first
    # 让 external job 立即占用预留 slot。steady-state replenish(L898/L913) 是"完成一个补
    # 一个"维持平衡，不会突破此上限。
    _ext_reserved = min(2, max(0, n_workers - 2))
    # Fill initial pool (priority-aware: external jobs first)
    while len(in_flight) < n_workers - _ext_reserved and match_queue:
        m = _pop_next_job(match_queue)
        if m is None:
            break
        # Detect external jobs: ("external", job_id, a, b, path_a, path_b, n_pairs[, priority])
        is_external = _is_external(m)
        if is_external:
            exec_args = m[2:7]
            ext_job_id = m[1]
            fut = executor.submit(run_single_match, exec_args)
            in_flight[fut] = (exec_args[0], exec_args[1], ext_job_id)
            _log_external_dispatch(
                ext_job_id, exec_args[0], exec_args[1],
                "initial_fill", len(in_flight), len(match_queue),
            )
        else:
            if m[0] not in active_bots or m[1] not in active_bots:
                continue
            fut = executor.submit(run_single_match, m)
            in_flight[fut] = (m[0], m[1])

    games_since_save = 0
    last_save_time = time.time()
    last_parent_check = time.time()
    # v193 keep-alive: track when the daemon last had real work (in_flight non-empty
    # OR an external job to drain). Bounds the idle spin so a truly idle daemon
    # exits after DAEMON_IDLE_MAX_SEC; any external-job submission resets it.
    last_busy_time = time.time()
    last_heartbeat_time = 0.0
    save_num = stats.get("total_games", 0) // SAVE_EVERY_N_GAMES
    total_matches = 0
    MAX_POOL_RECOVERIES = 3
    recovery_count = 0
    played_bots_this_cycle = set()
    last_bot_refresh_time = time.time()

    try:
        # H4 (2026-06-29): track priority_eval.json mtime so a newly-committed bot's
        # priority signal takes effect without waiting for the current match_queue
        # (potentially hundreds of daemon matches) to drain. When the file's mtime
        # changes, the daemon-internal matches in match_queue are cleared so the
        # next refill (pick_matches) re-reads the updated priority. External
        # (precommit) jobs are preserved — they have deadlines and are priority-
        # dispatched anyway. Initialized to the current mtime so a fresh start does
        # not immediately nuke the seed queue.
        _priority_eval_mtime = 0.0
        try:
            if PRIORITY_EVAL_FILE.exists():
                _priority_eval_mtime = os.path.getmtime(PRIORITY_EVAL_FILE)
        except OSError:
            pass

        while running and recovery_count < MAX_POOL_RECOVERIES:
            try:
                while running:
                    # H4: hot-reload priority signal. If priority_eval.json was
                    # rewritten (new commit), drop daemon-internal queued matches so
                    # the next pick_matches picks up the new priority bot. External
                    # jobs are kept (they are deadline-bound + priority-dispatched).
                    try:
                        if PRIORITY_EVAL_FILE.exists():
                            _mt = os.path.getmtime(PRIORITY_EVAL_FILE)
                            if _mt != _priority_eval_mtime:
                                _kept = [m for m in match_queue if _is_external(m)]
                                _dropped = len(match_queue) - len(_kept)
                                if _dropped > 0:
                                    match_queue.clear()
                                    match_queue.extend(_kept)
                                    _priority_bot_now = _load_priority_eval()
                                    log.info(
                                        "H4: priority_eval.json changed (mtime %.0f→%.0f); "
                                        "dropped %d internal queued match(es), kept %d external; "
                                        "priority_bot=%s",
                                        _priority_eval_mtime, _mt, _dropped, len(_kept),
                                        _priority_bot_now,
                                    )
                                _priority_eval_mtime = _mt
                    except OSError:
                        pass

                    # Poll external job queue
                    if _SCHEDULER_AVAILABLE:
                        ext_in_queue = sum(1 for m in match_queue if _is_external(m))
                        if ext_in_queue < _capacity:
                            if _first_iteration:
                                recovered = requeue_unclaimed_on_startup()
                                for job in recovered:
                                    match_queue.append((
                                        "external", job["job_id"],
                                        job["bot_a_name"], job["bot_b_name"],
                                        job["bot_a_path"], job["bot_b_path"],
                                        job["n_pairs"], job.get("priority", 0),
                                    ))
                            pending = drain_pending_jobs()
                            if pending:
                                log.info(
                                    "[dispatch] drained %d external job(s); "
                                    "match_queue=%d (ext=%d), in_flight=%d",
                                    len(pending), len(match_queue),
                                    sum(1 for m in match_queue if _is_external(m)),
                                    len(in_flight),
                                )
                            for job in pending:
                                match_queue.append((
                                    "external", job["job_id"],
                                    job["bot_a_name"], job["bot_b_name"],
                                    job["bot_a_path"], job["bot_b_path"],
                                    job["n_pairs"], job.get("priority", 0),
                                ))
                            _first_iteration = False

                    # OPT-1' (precommit-stall fix 2026-06-24): proactively fill the
                    # reserved empty slots with queued external (precommit) jobs.
                    # Steady-state replenish (:992-1006) only fires on `for fut in
                    # done` (1:1 per completed match), so without this the 2 reserved
                    # slots stay structurally empty while external jobs wait for an
                    # in-flight internal match to complete — root cause of the 640s
                    # scheduler_stall (collected=0/N) then 23-43min serial fallback.
                    # Only external jobs may claim the reserved slots; internal still
                    # flows 1:1 through replenish. In_flight rises to n_workers only
                    # while external jobs are queued, else stays at n_workers-2.
                    while len(in_flight) < n_workers and match_queue:
                        _next_ext = None
                        for _q_idx, _q_m in enumerate(match_queue):
                            if _is_external(_q_m):
                                _next_ext = _q_idx
                                break
                        if _next_ext is None:
                            break
                        _q_m = match_queue[_next_ext]
                        del match_queue[_next_ext]
                        _exec_args = _q_m[2:7]
                        _ext_job_id = _q_m[1]
                        new_fut = executor.submit(run_single_match, _exec_args)
                        in_flight[new_fut] = (_exec_args[0], _exec_args[1], _ext_job_id)
                        _log_external_dispatch(
                            _ext_job_id, _exec_args[0], _exec_args[1],
                            "proactive_reserved_slot", len(in_flight), len(match_queue),
                        )
                        log.info(
                            "[dispatch] proactively submitted external job %s into "
                            "reserved slot (in_flight=%d, queue=%d)",
                            _ext_job_id, len(in_flight), len(match_queue),
                        )

                    # v193 root-cause-audit (2026-06-26) keep-alive path: with the
                    # main loop now `while running` (not `while running and in_flight`),
                    # we reach here even with empty in_flight — which lets external
                    # (precommit) jobs submitted after the last internal batch be
                    # drained instead of stranded forever. Two cases:
                    #  (a) in_flight non-empty: normal — wait on futures as before.
                    #  (b) in_flight empty: idle-spin. If there's a queued external
                    #      job it was just submitted above (proactive fill), so loop
                    #      back immediately; otherwise sleep POLL_TIMEOUT and keep
                    #      polling the external queue. DAEMON_IDLE_MAX_SEC bounds the
                    #      spin so a truly idle daemon exits cleanly (rc=0) for the
                    #      monitor to restart. The empty-set wait() below would raise,
                    #      so we skip it entirely when in_flight is empty.
                    _has_external = any(_is_external(m) for m in match_queue)
                    if in_flight:
                        last_busy_time = time.time()
                    elif _has_external:
                        # External job just got submitted into in_flight (or still
                        # queued) — reset idle timer and loop back to dispatch it.
                        last_busy_time = time.time()
                    if not in_flight:
                        # Periodic heartbeat even while idle, so the liveness probe
                        # sees a live main loop (not a stalled one).
                        if time.time() - last_heartbeat_time >= 5:
                            _write_heartbeat()
                            last_heartbeat_time = time.time()
                        if not _has_external:
                            # Genuinely idle: bound the spin. rc=0 idle exit lets the
                            # monitor restart without burning the restart budget.
                            idle_for = time.time() - last_busy_time
                            if idle_for >= DAEMON_IDLE_MAX_SEC:
                                log.info(
                                    "[dispatch] idle for %.0fs with no in_flight / external "
                                    "jobs — exiting keep-alive (rc=0).", idle_for,
                                )
                                try:
                                    log_system_event(
                                        "daemon.idle_exit", "info",
                                        f"Daemon idle-exit after {idle_for:.0f}s "
                                        f"(no in_flight/external jobs).",
                                        {"idle_sec": round(idle_for, 1)},
                                    )
                                except Exception:
                                    pass
                                running = False
                                break
                            time.sleep(POLL_TIMEOUT)
                            continue
                        # External job queued but not yet in in_flight (capacity full
                        # of externals, or reserved slots saturated) — brief sleep,
                        # loop back; replenish on the next completed future.
                        time.sleep(POLL_TIMEOUT)
                        continue

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
                                            net_chips=list(result[7]) if len(result) > 7 and result[7] else [],
                                            error=result[6] if len(result) > 6 and result[6] else None,
                                            completed_at=time.time(),
                                            source="scheduler",
                                        ))
                                        _log_external_result(ext_job_id, a, b, result=result)
                                    except Exception as wr_err:
                                        log.warning("write_result failed for %s: %s", ext_job_id, wr_err)
                            except Exception as e:
                                if _SCHEDULER_AVAILABLE:
                                    try:
                                        write_result(BattleResult(
                                            job_id=ext_job_id,
                                            wins_a=0, wins_b=0, draws=0, total=0,
                                            net_chips=[],
                                            error=str(e),
                                            completed_at=time.time(),
                                            source="scheduler",
                                        ))
                                        _log_external_result(ext_job_id, a, b, error=e)
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

                        # Replenish: submit next match (priority-aware: external jobs first)
                        if match_queue and executor is not None:
                            m = _pop_next_job(match_queue)
                            if m is not None:
                                is_ext = _is_external(m)
                                if is_ext:
                                    exec_args = m[2:7]
                                    ext_job_id = m[1]
                                    new_fut = executor.submit(run_single_match, exec_args)
                                    in_flight[new_fut] = (exec_args[0], exec_args[1], ext_job_id)
                                    _log_external_dispatch(
                                        ext_job_id, exec_args[0], exec_args[1],
                                        "replenish_after_done", len(in_flight), len(match_queue),
                                    )
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
                                m = _pop_next_job(match_queue)
                                if m is not None:
                                    is_ext = _is_external(m)
                                    if is_ext:
                                        exec_args = m[2:7]
                                        ext_job_id = m[1]
                                        new_fut = executor.submit(run_single_match, exec_args)
                                        in_flight[new_fut] = (exec_args[0], exec_args[1], ext_job_id)
                                        _log_external_dispatch(
                                            ext_job_id, exec_args[0], exec_args[1],
                                            "replenish_after_refill", len(in_flight), len(match_queue),
                                        )
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

                    # v193: refresh heartbeat while busy so the liveness probe sees
                    # a live main loop (throttled to ~every 5s; aligns with the idle
                    # path's cadence). last_busy_time already reset above when
                    # in_flight was non-empty.
                    now_hb = time.time()
                    if now_hb - last_heartbeat_time >= 5:
                        _write_heartbeat()
                        last_heartbeat_time = now_hb

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
                            # Group B: tag orphan exits distinctly from SIGTERM-driven exits
                            # (both yield rc=0). grep daemon.orphan_exit vs "Received signal"
                            # to attribute the rc=0 source.
                            log.warning("Parent process died (ppid %d → %d), orphan exit", _stored_ppid, cur_ppid)
                            log_system_event(
                                "daemon.orphan_exit", "warn",
                                f"Daemon orphaned (ppid {_stored_ppid} → {cur_ppid}), exiting cleanly (rc=0)",
                                {"stored_ppid": _stored_ppid, "cur_ppid": cur_ppid},
                            )
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
                            _added_bots = set(new_bots) - set(active_bots)  # reap-bug fix: track newly-added bots
                            removed = set(active_bots) - set(new_bots)
                            for b in removed:
                                ratings.pop(b, None)
                                bot_stats.pop(b, None)
                                h2h = {k: v for k, v in h2h.items() if b not in k.split(" vs ")}
                            for b in set(new_bots) - set(active_bots):
                                if b not in ratings:
                                    ratings[b] = Glicko2Player()
                            active_bots = new_bots
                            # Filter match_queue: preserve external jobs (priority-aware form)
                            if removed:
                                match_queue = deque(
                                    m for m in match_queue
                                    if _is_external(m)
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
                            # Reap-bug fix: prepend pairs for newly-added bots so they're
                            # scheduled immediately instead of waiting for queue to drain.
                            # (was the v86/v87 eval deadlock — match_queue only re-picked when empty,
                            # so a newly committed bot never entered the queue → 600s eval timeout loop.)
                            if _added_bots:
                                try:
                                    _fresh = pick_matches(active_bots, h2h, ratings, n_picks=n_workers * 2)
                                    _prepended = 0
                                    for _a, _b in _fresh:
                                        if _a in _added_bots or _b in _added_bots:
                                            match_queue.appendleft((_a, _b, bot_path(_a), bot_path(_b), n_pairs))
                                            _prepended += 1
                                    if _prepended:
                                        log.info("Reap refresh: %d new-bot pairs prepended to queue (reap-bug fix)", _prepended)
                                except Exception as _rp_err:
                                    log.warning("Reap re-pick failed (non-fatal): %s", _rp_err)
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
                                    if _is_external(m)
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
                                    if _is_external(m)
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
                if _handle_pool_break_for_shutdown(e):
                    for fut in list(in_flight):
                        try:
                            fut.cancel()
                        except Exception:
                            pass
                    in_flight.clear()
                    break
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
                                    net_chips=[],
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
                    mp_ctx = _mp.get_context("spawn")
                    executor = ProcessPoolExecutor(max_workers=n_workers, mp_context=mp_ctx)
                    # Preserve external jobs from old queue before discarding
                    old_external = [m for m in match_queue if _is_external(m)]
                    match_queue = deque(old_external)
                    # Rebuild internal matches on top of preserved externals
                    matches = pick_matches(active_bots, h2h, ratings, n_picks=n_workers * 2)
                    for a, b in matches:
                        match_queue.append((a, b, bot_path(a), bot_path(b), n_pairs))
                    while len(in_flight) < n_workers - _ext_reserved and match_queue:
                        m = _pop_next_job(match_queue)
                        if m is None:
                            break
                        is_ext = _is_external(m)
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
            try:
                log_system_event(
                    "daemon.final_save_failed",
                    "error",
                    f"Daemon final save failed during shutdown: {str(e)[:180]}",
                    {
                        "error": str(e)[:500],
                        "save_num": save_num + 1,
                        "active_bot_count": len(active_bots) if active_bots is not None else None,
                        "total_matches": total_matches,
                    },
                )
            except Exception:
                pass
        log.info("Shutdown complete. %d matches processed.", total_matches)


if __name__ == "__main__":
    main()
