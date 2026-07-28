"""Current-epoch national TCP Glicko-2 rating daemon.

Each admitted strength sample is one complete 70-hand local native TCP match.
The sign of final net chips supplies win/loss/draw; magnitude is retained only
as a secondary tie-breaker. Official EXE and Arena results are never admitted.

Usage:
    python web/core/elo_daemon.py --pairs 5 --workers 12 --verbose
"""

import os
import sys
import hashlib
import json
import math
import random
import signal
import stat
import argparse
import asyncio
import time
import multiprocessing
import threading
from collections import Counter, deque
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
import elo_daemon_replay_store as _edrs  # noqa: E402,F401  (replay-store cluster)
import elo_daemon_persistence as _edp  # noqa: E402,F401  (persistence/state-IO cluster)

# Battle Scheduler integration (optional)
import logging

log = logging.getLogger("pok.daemon")

# Stable identifier for this daemon run, stamped into every rating-history
# snapshot. Generation planning only consumes the bounded tail published in an
# immutable evaluation cycle; the id prevents cross-run rows entering that tail.
# Set once in main(); remains None for ad-hoc save_ratings() calls outside a run.
import uuid as _uuid
daemon_run_id: str | None = None
# Evaluation-data identity observed at daemon startup.  A migration creates a
# new identity; an old still-running process must never publish its stale
# in-memory ratings into that new epoch.
daemon_evaluation_identity_digest: str | None = None
daemon_last_cycle_manifest_digest: str | None = None
daemon_last_cycle_save_num: int | None = None
_daemon_writer_lease_fd: int | None = None

from concurrent.futures.process import BrokenProcessPool
from datetime import datetime
from system_log import log_system_event  # Group B: structured events for SIGTERM/orphan source attribution

CORE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CORE_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(CORE_DIR))

from glicko2 import Glicko2Player, update_rating_period, decay_rd
from evolution_infra import (
    pair_key,
    get_active_bots,
    read_locked_json, write_locked_json, append_locked_jsonl, locked_file,
    read_and_maybe_unlink_locked_text,
    update_h2h, update_bot_stats,
)
from bot_action_stats import (
    MAX_ACTION_STATS_CYCLE_LAG,
    compute_all_bot_stats,
    get_global_stats,
)
from eval_rounds import EvalRoundManager
from workflow_profiles import get_workflow_profile

BOTS_DIR = PROJECT_ROOT / "bots"
RESULTS_DIR = CORE_DIR / "results"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
STATS_FILE = RESULTS_DIR / "elo_daemon_stats.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"
BOT_STATS_FILE = RESULTS_DIR / "bot_stats.json"
SELECTION_SNAPSHOT_FILE = RESULTS_DIR / "selection_snapshot.json"
REPLAY_DIR = RESULTS_DIR / "match_replay"
# A1 (INERTNESS fix, evolution-plan-refresh-jun21): per-bot stderr telemetry.
# The daemon now captures bot stderr (FOLD_GATE_FIRE / SB_OPEN_OPP_SIZE / ...) that
# _PersistentBot previously discarded; grep these files to verify detector firing.
MATCH_HISTORY_FILE = RESULTS_DIR / "match_history.jsonl"
MAX_REPLAY_FILES = 2000
# Maximum complete 70-hand samples scheduled for one bot pairing in a rating
# period.  More samples can accumulate across immutable cycles; this per-cycle
# budget is not itself a strength verdict.
MAX_NATIONAL_RATING_MATCHES = 8

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
# Heartbeat freshness for process observability. This exceeds the main-loop
# iteration cadence (including periodic cycle publication) with ample margin.
HEARTBEAT_STALE_SEC = 120
MIN_RATING_POOL_BOTS = 2
RATING_POOL_IDLE_POLL_SEC = 2.0

running = True

PICK_MATCH_LOG_INTERVAL_SEC = float(os.environ.get("POK_PICK_MATCH_LOG_INTERVAL_SEC", "30"))
ACTION_STATS_REFRESH_INTERVAL_SEC = float(os.environ.get("POK_ACTION_STATS_REFRESH_INTERVAL_SEC", "30"))
_pick_match_log_state: dict[str, object] = {"last_signature": None, "last_ts": 0.0}
OFFICIAL_JOB_RECONCILE_INTERVAL_SEC = float(os.environ.get(
    "POK_OFFICIAL_JOB_INTERVAL_SEC",
    "60",
))
OFFICIAL_JOB_RECONCILE_LIMIT = max(1, int(os.environ.get(
    "POK_OFFICIAL_JOB_LIMIT",
    "1",
)))


def _acquire_daemon_writer_lease():
    """Delegate to elo_daemon_persistence."""
    return _edp._acquire_daemon_writer_lease()


def _release_daemon_writer_lease():
    """Delegate to elo_daemon_persistence."""
    return _edp._release_daemon_writer_lease()


def _single_writer_daemon(func):
    """Delegate to elo_daemon_persistence."""
    return _edp._single_writer_daemon(func)


def _write_heartbeat(
    *,
    activity_state: str = "scheduling_matches",
    active_bot_count: int | None = None,
):
    """Delegate to elo_daemon_persistence."""
    return _edp._write_heartbeat(
        activity_state=activity_state,
        active_bot_count=active_bot_count,
    )


def _reconcile_rating_pool_membership(
    previous_active_bots,
    ratings,
    h2h,
    bot_stats,
    *,
    save_num=0,
    verbose=False,
):
    """Delegate to elo_daemon_persistence."""
    return _edp._reconcile_rating_pool_membership(
        previous_active_bots,
        ratings,
        h2h,
        bot_stats,
        save_num=save_num,
        verbose=verbose,
    )


def _wait_for_minimum_rating_pool(
    active_bots,
    ratings,
    h2h,
    bot_stats,
    *,
    save_num=0,
    verbose=False,
    once=False,
):
    """Remain live and observable until two strict bots can form a match.

    Zero/one-bot policy epochs are normal bootstrap states, not daemon exits.
    The loop intentionally publishes only process heartbeat metadata; ratings
    stay unavailable until an immutable cycle for the exact published pool is
    committed by real complete matches.
    """

    announced = None
    while running and len(active_bots) < MIN_RATING_POOL_BOTS:
        state = (
            "waiting_for_first_published_bot"
            if not active_bots
            else "waiting_for_second_published_bot"
        )
        if state != announced:
            log.info(
                "Rating daemon idle: %s (active=%d required=%d)",
                state,
                len(active_bots),
                MIN_RATING_POOL_BOTS,
            )
            log_system_event(
                "daemon.rating_pool_idle",
                "info",
                "Rating daemon is waiting for a schedulable strict pool",
                {
                    "activity_state": state,
                    "active_bot_count": len(active_bots),
                    "minimum_rating_pool_bots": MIN_RATING_POOL_BOTS,
                },
            )
            announced = state
        _write_heartbeat(
            activity_state=state,
            active_bot_count=len(active_bots),
        )
        if once:
            return active_bots, h2h
        deadline = time.time() + RATING_POOL_IDLE_POLL_SEC
        while running and time.time() < deadline:
            time.sleep(min(0.25, max(0.0, deadline - time.time())))
        if not running:
            break
        active_bots, h2h, added, removed = _reconcile_rating_pool_membership(
            active_bots,
            ratings,
            h2h,
            bot_stats,
            save_num=save_num,
            verbose=verbose,
        )
        if added or removed:
            log.info(
                "Rating pool changed while idle: +%s -%s (total=%d)",
                added,
                removed,
                len(active_bots),
            )
    if running:
        _write_heartbeat(
            activity_state="scheduling_matches",
            active_bot_count=len(active_bots),
        )
    return active_bots, h2h


def start_official_certification_thread():
    """Process official EXE certification jobs without blocking quality gates."""
    enabled = os.environ.get("POK_OFFICIAL_JOB_RECONCILER", "1")
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
    """Delegate to elo_daemon_persistence."""
    return _edp.bot_path(bot_name)


def save_ratings(
    ratings,
    save_num=None,
    *,
    h2h_snapshot=None,
    bot_stats_snapshot=None,
):
    """Delegate to elo_daemon_persistence."""
    return _edp.save_ratings(
        ratings,
        save_num=save_num,
        h2h_snapshot=h2h_snapshot,
        bot_stats_snapshot=bot_stats_snapshot,
    )


def load_stats():
    """Delegate to elo_daemon_persistence."""
    return _edp.load_stats()


def save_stats(stats):
    """Delegate to elo_daemon_persistence."""
    return _edp.save_stats(stats)


def load_h2h():
    """Delegate to elo_daemon_persistence."""
    return _edp.load_h2h()


def save_h2h(h2h):
    """Delegate to elo_daemon_persistence."""
    return _edp.save_h2h(h2h)


def _persistable_h2h(h2h):
    """Delegate to elo_daemon_persistence."""
    return _edp._persistable_h2h(h2h)


def _h2h_with_win_rates(h2h):
    """Delegate to elo_daemon_persistence."""
    return _edp._h2h_with_win_rates(h2h)


def load_bot_stats():
    """Delegate to elo_daemon_persistence."""
    return _edp.load_bot_stats()


def save_bot_stats(bot_stats):
    """Delegate to elo_daemon_persistence."""
    return _edp.save_bot_stats(bot_stats)


def _opponent_coverage(bot, active_bots, h2h):
    """Delegate to elo_daemon_persistence."""
    return _edp._opponent_coverage(bot, active_bots, h2h)


PRIORITY_EVAL_FILE = RESULTS_DIR / "priority_eval.json"


def _load_priority_eval():
    """Load the priority eval bot name. Expires when bot reaches min_games."""
    try:
        priority_bot = None

        def expire_if_satisfied(raw):
            nonlocal priority_bot
            data = json.loads(raw)
            if not isinstance(data, dict):
                return False
            bot = data.get("bot")
            if not bot:
                return False
            # Expire when the bot reaches min_games (not by timeout — the
            # daemon may be stopped/restarted).  Both this criterion and the
            # exact-inode unlink run under priority_eval.json's EX sidecar.
            min_games = data.get("min_games", 100)
            stats = load_bot_stats()
            if stats.get(bot, {}).get("games", 0) >= min_games:
                return True
            priority_bot = bot
            return False

        read_and_maybe_unlink_locked_text(
            PRIORITY_EVAL_FILE,
            expire_if_satisfied,
        )
        return priority_bot
    except Exception as e:
        log.debug("Priority eval load failed: %s", e)
        return None


def _consume_reap_signal(path=None):
    """Consume one daemon-refresh signal without racing its atomic writer."""

    signal_path = Path(path) if path is not None else RESULTS_DIR / ".reap_signal"
    refresh_requested = False

    def consume(raw):
        nonlocal refresh_requested
        try:
            ts = float(raw.strip())
            # The file is a one-shot capability, not a freshness cache.  A
            # crash-safe handoff must be able to reissue its exact frozen bytes
            # hours later and still force the daemon to reload the active pool.
            refresh_requested = math.isfinite(ts)
        except ValueError:
            # Empty/non-numeric files are the legacy refresh capability.  They
            # are still consumed, but filesystem/authenticity errors above are
            # never downgraded to this compatibility path.
            refresh_requested = True
        return True

    _raw, consumed = read_and_maybe_unlink_locked_text(signal_path, consume)
    return refresh_requested if consumed else False


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
    expected_evaluation_identity_digest=None,
    expected_native_match_timing_plan=None,
    stage_only=False,
):
    """Delegate to elo_daemon_replay_store."""
    return _edrs.save_match_replay(a, b, wins_a, wins_b, draws, replay_data, net_chips_samples, strength_sample_unit, expected_evaluation_identity_digest, expected_native_match_timing_plan, stage_only)


def _ensure_safe_replay_directory(path: Path) -> Path:
    """Delegate to elo_daemon_replay_store."""
    return _edrs._ensure_safe_replay_directory(path)


def _save_match_replay_under_cycle_lock(
    a,
    b,
    wins_a,
    wins_b,
    draws,
    replay_data,
    net_chips_samples=None,
    strength_sample_unit=None,
    expected_evaluation_identity_digest=None,
    expected_native_match_timing_plan=None,
    stage_only=False,
):
    """Delegate to elo_daemon_replay_store."""
    return _edrs._save_match_replay_under_cycle_lock(a, b, wins_a, wins_b, draws, replay_data, net_chips_samples, strength_sample_unit, expected_evaluation_identity_digest, expected_native_match_timing_plan, stage_only)


def cleanup_old_replays():
    """Delegate to elo_daemon_replay_store."""
    return _edrs.cleanup_old_replays()


def _rating_protocol_config(n_pairs=None):
    profile = get_workflow_profile()
    if (
        getattr(profile, "rating_protocol", None) != "national"
        or getattr(profile, "national_execution_mode", None) != "native_tcp"
    ):
        raise ValueError("rating daemon supports only national native_tcp matches")
    if os.environ.get("POK_RATING_PROTOCOL", "national") != "national":
        raise ValueError("POK_RATING_PROTOCOL cannot override national strength")
    national_hands = 70
    matches_override = "POK_NATIONAL_RATING_MATCHES" in os.environ
    national_matches = int(os.environ.get(
        "POK_NATIONAL_RATING_MATCHES",
        str(getattr(profile, "national_rating_matches", 1)),
    ))
    if n_pairs is not None and not matches_override:
        national_matches = max(national_matches, int(n_pairs))
    national_hands = max(1, min(70, national_hands))
    national_matches = max(1, min(MAX_NATIONAL_RATING_MATCHES, national_matches))
    from national_native import build_native_match_timing_plan

    native_match_timing_plan = build_native_match_timing_plan(
        hands=national_hands,
        requested_timeout_sec=None,
    )
    config = {
        "profile_id": getattr(profile, "profile_id", "default"),
        "protocol": "national",
        "national_execution_mode": "native_tcp",
        "national_hands": national_hands,
        "national_matches": national_matches,
        "artifact_execution_mode": "direct_content_bound_policy_artifact",
        "native_match_timing_plan": native_match_timing_plan.snapshot(),
        "native_match_timing_plan_digest": native_match_timing_plan.digest(),
    }
    return config


def _rotate_jsonl(filepath, max_lines):
    """Retain append-only authority; cold-range archiving owns compaction.

    Replacing a data path while another process may already be waiting on that
    data inode splits one logical lock into two.  The daemon therefore never
    destructively trims authoritative JSONL.  The post-publication archival
    journal may copy digest-bound cold ranges under the shared stable sidecar,
    while the live source remains append-only.
    """

    return {
        "rotated": False,
        "reason": "append_only_authority_preserved",
        "path": str(filepath),
        "requested_max_lines": int(max_lines),
    }


def _run_national_rating_match(
    bot_a_name,
    bot_b_name,
    bot_a_path,
    bot_b_path,
    config,
    *,
    persist_strength=True,
    expected_identity=None,
):
    """Run the national GameEngine rating backend and return daemon result shape."""
    hands = int(config["national_hands"])
    matches = int(config["national_matches"])
    if config.get("national_execution_mode") != "native_tcp" or hands != 70:
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
    from bot_artifact import hash_path
    from national_native import (
        _artifact_execution_is_valid,
        require_native_match_timing_plan,
        run_native_strength_pair,
        validate_native_match_timing_evidence,
    )
    expected_artifacts = {
        bot_a_name: hash_path(Path(bot_a_path).parent),
        bot_b_name: hash_path(Path(bot_b_path).parent),
    }
    wins_a = wins_b = draws = 0
    net_chips_list: list[int] = []
    replays: list[dict] = []
    issues: list[str] = []
    rating_timing_plan = require_native_match_timing_plan(
        config.get("native_match_timing_plan"),
        hands=hands,
        requested_timeout_sec=None,
    )
    if config.get("native_match_timing_plan_digest") != rating_timing_plan.digest():
        raise ValueError("native rating timing plan digest mismatch")

    for repeat in range(matches):
        result = asyncio.run(run_native_strength_pair(
            bot_a_path,
            bot_b_path,
            hands,
            timeout_sec=None,
            timing_plan=rating_timing_plan,
        ))
        replay = dict(result)
        replay["rating_protocol"] = "national_native_tcp"
        replay["repeat"] = repeat + 1
        replay["rating_liveness_budget"] = rating_timing_plan.liveness_budget_snapshot()
        replay["rating_match_timing_plan"] = rating_timing_plan.snapshot()
        replay["rating_match_timing_plan_digest"] = rating_timing_plan.digest()
        replays.append(replay)
        hands_played = int(result.get("hands_played", 0) or 0)
        if hands_played != hands:
            issues.append(f"repeat={repeat + 1}: hands_played={hands_played}/{hands}")
        if result.get("passed_compliance") is not True:
            reported = [str(item) for item in (result.get("issues") or [])]
            issues.extend(reported or [f"repeat={repeat + 1}: compliance_failed"])
        timing_issues = validate_native_match_timing_evidence(
            result,
            timing_plan=rating_timing_plan,
        )
        if timing_issues:
            issues.extend(
                f"repeat={repeat + 1}: {item}" for item in timing_issues
            )
        if not _artifact_execution_is_valid(
            result.get("artifact_execution"), expected_artifacts
        ):
            issues.append(
                f"repeat={repeat + 1}: native_artifact_execution_identity_invalid"
            )
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
            "native_rating_contract: " + "; ".join(issues[:8]),
            [],
        )
    admission = None
    if persist_strength:
        try:
            admission = save_match_replay(
                bot_a_name,
                bot_b_name,
                wins_a,
                wins_b,
                draws,
                replays,
                net_chips_list,
                f"{hands}_hand_match",
                expected_evaluation_identity_digest=expected_identity,
                expected_native_match_timing_plan=rating_timing_plan.snapshot(),
                stage_only=True,
            )
        except Exception as e:
            return (
                bot_a_name,
                bot_b_name,
                0,
                0,
                0,
                0,
                f"strength_admission_failed: {e}",
                [],
                None,
            )

    return (
        bot_a_name,
        bot_b_name,
        wins_a,
        wins_b,
        draws,
        total,
        None,
        net_chips_list,
        admission,
    )


def run_single_match(args):
    """Run the configured rating backend and return lightweight daemon result."""
    bot_a_name, bot_b_name, bot_a_path, bot_b_path, n_pairs = args[:5]
    authority = args[5] if len(args) > 5 and isinstance(args[5], dict) else {}
    persist_strength = bool(authority.get("admit_strength", False))
    expected_identity = authority.get("evaluation_identity_digest")
    if persist_strength and not expected_identity:
        return (
            bot_a_name,
            bot_b_name,
            0,
            0,
            0,
            0,
            "strength_admission_authority_missing",
            [],
        )
    try:
        config = _rating_protocol_config(n_pairs=n_pairs)
        # The native runner is the single capacity owner for every caller.
        return _run_national_rating_match(
            bot_a_name,
            bot_b_name,
            bot_a_path,
            bot_b_path,
            config,
            persist_strength=persist_strength,
            expected_identity=expected_identity,
        )
    except Exception as e:
        return (bot_a_name, bot_b_name, 0, 0, 0, 0, str(e), [])


def process_result(result, ratings, h2h, bot_stats, verbose=False):
    """Process complete native matches into Glicko-2, H2H, and bot stats."""
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


def _discard_staged_match(result):
    try:
        admission = result[8] if len(result) > 8 else None
        path = Path((admission or {}).get("pending_path", ""))
        replay_root = _ensure_safe_replay_directory(REPLAY_DIR)
        pending_root = _ensure_safe_replay_directory(replay_root / ".pending")
        if pending_root.parent != replay_root:
            return
        if path.is_file() and not path.is_symlink() and path.resolve().parent == pending_root:
            path.unlink(missing_ok=True)
    except Exception:
        pass


def admit_internal_match_result(result, ratings, h2h, bot_stats, *, verbose=False):
    """Serialize rating mutation and authoritative history/replay publication."""
    if result[6] is not None or int(result[5] or 0) <= 0:
        _discard_staged_match(result)
        return process_result(result, ratings, h2h, bot_stats, verbose=verbose)
    admission = result[8] if len(result) > 8 else None
    if not isinstance(admission, dict):
        raise RuntimeError("successful internal match has no staged admission receipt")

    from evaluation_bundle import evaluation_cycle_lock
    from evaluation_data_identity import current_evaluation_digest
    import hashlib

    with evaluation_cycle_lock(RESULTS_DIR, exclusive=False):
        expected_identity = str(admission.get("evaluation_identity_digest") or "")
        current_identity = current_evaluation_digest(RESULTS_DIR)
        if (
            not expected_identity
            or expected_identity != daemon_evaluation_identity_digest
            or current_identity != expected_identity
        ):
            _discard_staged_match(result)
            raise RuntimeError(
                "staged match identity no longer matches the daemon evaluation epoch"
            )
        replay_root = _ensure_safe_replay_directory(REPLAY_DIR)
        pending_root = _ensure_safe_replay_directory(replay_root / ".pending")
        if pending_root.parent != replay_root:
            raise RuntimeError("staged replay directory escapes replay root")
        pending = Path(str(admission.get("pending_path") or ""))
        if (
            pending.is_symlink()
            or not pending.is_file()
            or pending.resolve().parent != pending_root
        ):
            raise RuntimeError("staged match replay path is missing or unsafe")
        payload = pending.read_bytes()
        if len(payload) != int(admission.get("replay_bytes", -1)):
            raise RuntimeError("staged match replay size mismatch")
        if hashlib.sha256(payload).hexdigest() != admission.get("replay_sha256"):
            raise RuntimeError("staged match replay digest mismatch")
        parsed = json.loads(payload.decode("utf-8"))
        summary = admission.get("summary")
        if not isinstance(parsed, dict) or not isinstance(summary, dict):
            raise RuntimeError("staged match admission payload is invalid")
        if parsed.get("evaluation_identity_digest") != expected_identity:
            raise RuntimeError("staged replay identity mismatch")
        if parsed.get("evaluation_epoch") != "national_tcp_policy_v1":
            raise RuntimeError("staged replay evaluation epoch mismatch")
        if parsed.get("execution_mode") != "native_tcp":
            raise RuntimeError("staged replay execution mode mismatch")
        # This is the sole mutation boundary for native rating/H2H.  A
        # successful worker result is insufficient: only a complete strict
        # 70-hand envelope with raw replay, artifact and timing proof may be
        # admitted.  Diagnostic/non-strength staged receipts stay outside this
        # API rather than becoming a back door into Glicko.
        if (
            parsed.get("strength_sample_unit") != "70_hand_match"
            or int(parsed.get("hands_per_strength_sample", 0) or 0) != 70
            or parsed.get("strength_admitted") is not True
            or parsed.get("strength_complete") is not True
            or parsed.get("strength_compliance_passed") is not True
        ):
            raise RuntimeError("staged match is not an admitted 70-hand strength sample")
        try:
            from bot_artifact import hash_path
            from national_native import (
                _artifact_execution_is_valid,
                require_native_match_timing_plan,
                validate_native_match_timing_evidence,
            )
            from replay_analysis import validate_native_replay

            replay_validation = validate_native_replay(
                parsed,
                expected_evaluation_identity_digest=expected_identity,
                expected_replay_id=str(admission.get("filename") or ""),
            )
            if not replay_validation.accepted:
                raise RuntimeError(
                    "staged replay strict validation failed:"
                    + str(replay_validation.reason)
                )
            staged_timing_plan = require_native_match_timing_plan(
                parsed.get("native_match_timing_plan"),
                hands=70,
                requested_timeout_sec=None,
            )
            if parsed.get("native_match_timing_plan_digest") != (
                staged_timing_plan.digest()
            ):
                raise RuntimeError("staged replay timing plan digest mismatch")
            expected_artifacts = {
                str(parsed["bot0"]): hash_path(BOTS_DIR / str(parsed["bot0"])),
                str(parsed["bot1"]): hash_path(BOTS_DIR / str(parsed["bot1"])),
            }
            if dict(replay_validation.artifact_hashes) != expected_artifacts:
                raise RuntimeError(
                    "staged replay artifact identity does not match current bot bytes"
                )
            for index, replay in enumerate(parsed.get("games") or []):
                timing_issues = validate_native_match_timing_evidence(
                    replay,
                    timing_plan=staged_timing_plan,
                )
                if timing_issues:
                    raise RuntimeError(
                        f"staged replay {index} timing evidence invalid:"
                        + ";".join(timing_issues)
                    )
                if not _artifact_execution_is_valid(
                    replay.get("artifact_execution"),
                    expected_artifacts,
                ):
                    raise RuntimeError(
                        f"staged replay {index} artifact identity invalid"
                    )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "staged replay strength evidence invalid:"
                f"{type(exc).__name__}"
            ) from exc
        summary_fields = (
            "id", "timestamp", "execution_mode", "evaluation_epoch", "bot0",
            "bot1", "bot0_wins", "bot1_wins", "draws",
            "evaluation_identity_digest", "strength_sample_unit",
            "hands_per_strength_sample", "strength_admitted", "strength_complete",
            "strength_compliance_passed", "strength_sample_count",
            "net_chips_bot0", "strength_order",
            "native_match_timing_plan", "native_match_timing_plan_digest",
        )
        derived_summary = {field: parsed.get(field) for field in summary_fields}
        derived_summary["replay_sha256"] = hashlib.sha256(payload).hexdigest()
        if summary != derived_summary:
            raise RuntimeError("staged match summary is not canonical replay projection")
        if (
            str(derived_summary.get("bot0")) != str(result[0])
            or str(derived_summary.get("bot1")) != str(result[1])
            or int(derived_summary.get("bot0_wins", -1)) != int(result[2])
            or int(derived_summary.get("bot1_wins", -1)) != int(result[3])
            or int(derived_summary.get("draws", -1)) != int(result[4])
        ):
            raise RuntimeError("staged match receipt disagrees with worker result")

        # Main-thread order is the transaction: mutate in-memory ratings/H2H,
        # append the matching history row, then expose the replay. Any failure
        # is fatal to this daemon run; restart hydrates the previous pointer and
        # discards/truncates this uncommitted work.
        admitted = process_result(
            result,
            ratings,
            h2h,
            bot_stats,
            verbose=verbose,
        )
        if admitted <= 0:
            raise RuntimeError("successful staged match produced no rating admission")
        append_locked_jsonl(MATCH_HISTORY_FILE, summary)
        final_path = replay_root / str(admission.get("filename") or "")
        if (
            final_path.parent != replay_root
            or final_path.exists()
            or final_path.is_symlink()
        ):
            raise RuntimeError("staged match final replay path collision")
        os.replace(pending, final_path)
        return admitted


# Phase 0 follow-up: bot_action_stats scan runs ~260s for 2000 replays and would
# block the daemon main scheduling loop if called synchronously in save_cycle (observed
# stalling match cadence from ~10s to ~15min). Stats feed only the Master-prompt
# injection — no commit gate depends on them — so an async background refresh with a
# one-cycle-stale read is acceptable. The implementation and its private thread
# handles live in elo_daemon_persistence; this delegate preserves monkeypatch
# compatibility (tests setattr elo_daemon._refresh_action_stats_async).
def _refresh_action_stats_async(active_bots):
    """Delegate to elo_daemon_persistence."""
    return _edp._refresh_action_stats_async(active_bots)


def _current_rating_history_tail(max_rows=10):
    """Delegate to elo_daemon_persistence."""
    return _edp._current_rating_history_tail(max_rows=max_rows)


def _project_rating_history_tail(
    active_bots,
    save_num,
    *,
    expected_evaluation_identity_digest,
    max_rows=10,
):
    """Delegate to elo_daemon_persistence."""
    return _edp._project_rating_history_tail(
        active_bots,
        save_num,
        expected_evaluation_identity_digest=expected_evaluation_identity_digest,
        max_rows=max_rows,
    )


def _save_authoritative_evaluation_cycle(
    ratings,
    h2h_out,
    bot_stats,
    stats,
    save_num,
    active_bots,
    *,
    _test_only_allow_unleased=False,
):
    """Delegate to elo_daemon_persistence.

    The companion writes the ``daemon_last_cycle_*`` globals back through the
    parent module namespace, so no ``global`` declaration is needed here.
    """
    return _edp._save_authoritative_evaluation_cycle(
        ratings,
        h2h_out,
        bot_stats,
        stats,
        save_num,
        active_bots,
        _test_only_allow_unleased=_test_only_allow_unleased,
    )


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
    # Recompute active-pool H2H from verified raw history.  The persisted
    # matrix is a cache only; exact W/L/D disagreement halts publication.
    h2h_out = _h2h_with_win_rates(h2h)
    try:
        from rating_snapshot import choose_h2h_source
        h2h_selection = choose_h2h_source(
            active_bots,
            h2h_out,
            MATCH_HISTORY_FILE,
            expected_evaluation_identity_digest=(
                daemon_evaluation_identity_digest or ""
            ),
            replay_dir=REPLAY_DIR,
        )
        if h2h_selection.get("integrity_ok") is not True:
            raise RuntimeError(
                "stored H2H does not exactly match verified raw match history:"
                + ";".join(
                    str(issue)
                    for issue in (h2h_selection.get("integrity_issues") or [])[:8]
                )
            )
        selected_h2h = _h2h_with_win_rates(h2h_selection["h2h"])
        if h2h_selection.get("stored_h2h") != h2h_selection["h2h"]:
            stored_cov = h2h_selection["stored_coverage"]
            rebuilt_cov = h2h_selection["rebuilt_coverage"]
            log_system_event(
                "rating.h2h_rebuilt_from_history",
                "warn",
                "Rebuilt active H2H matrix from verified raw match history",
                {
                    "save_num": save_num,
                    "stored_pairs": stored_cov.get("covered_pairs"),
                    "rebuilt_pairs": rebuilt_cov.get("covered_pairs"),
                    "total_pairs": rebuilt_cov.get("total_pairs"),
                    "stored_coverage": round(stored_cov.get("coverage", 0.0), 4),
                    "rebuilt_coverage": round(rebuilt_cov.get("coverage", 0.0), 4),
                },
            )
        # Keep only raw-rebuilt rows whose two endpoints are in the frozen
        # active pool. Historical inactive matchups remain append-only context,
        # never current selection authority.
        h2h_out = _persistable_h2h(selected_h2h)
        h2h.clear()
        h2h.update(h2h_out)
    except Exception as e:
        raise RuntimeError(
            "cannot canonicalize H2H for coherent evaluation publication"
        ) from e
    cycle_manifest = _save_authoritative_evaluation_cycle(
        ratings,
        h2h_out,
        bot_stats,
        stats,
        save_num,
        active_bots,
    )
    # `_save_authoritative_evaluation_cycle` rebuilds H2H again after bounded
    # history retention.  Keep the long-lived scheduler state on that exact
    # retained projection; otherwise the next period would reintroduce W/L/D
    # whose raw replay rows were intentionally rotated away.
    h2h.clear()
    h2h.update(h2h_out)
    log_system_event(
        "rating.evaluation_cycle_published",
        "info",
        f"Published coherent evaluation cycle {save_num}",
        {
            "save_num": int(save_num),
            "daemon_run_id": str(daemon_run_id or "adhoc"),
            "manifest_digest": cycle_manifest.get("manifest_digest"),
            "active_bot_count": len(active_bots),
        },
    )

    # Keep the caller-owned dict aligned with the committed daemon-stats alias.
    stats.clear()
    stats.update(load_stats())

    cleanup_old_replays()

    # JSONL rotation happens before immutable publication so its byte cutoffs
    # remain valid for restart recovery.
    # The canonical events.jsonl ledger is rotated by evolution_infra.

    # Compute native action evidence asynchronously.  The writer publishes its
    # source-cycle identity; generation preparation only admits a bounded-stale
    # copy inside the immutable evidence snapshot.
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


def _load_committed_daemon_state():
    """Delegate to elo_daemon_persistence.

    The companion writes the ``daemon_last_cycle_*`` globals back through the
    parent module namespace, so no ``global`` declaration is needed here.
    """
    return _edp._load_committed_daemon_state()


def _internal_match_job(bot_a, bot_b, path_a, path_b, n_pairs):
    return (
        bot_a,
        bot_b,
        path_a,
        path_b,
        n_pairs,
        {
            "admit_strength": True,
            "evaluation_identity_digest": daemon_evaluation_identity_digest,
        },
    )


@_single_writer_daemon
def main():
    parser = argparse.ArgumentParser(
        description=(
            "national_tcp_policy_v1 Glicko-2 daemon; one sample is one complete "
            "70-hand local native TCP match"
        )
    )
    parser.add_argument(
        "--pairs",
        type=int,
        default=5,
        help=(
            "Complete 70-hand native matches per scheduled bot pairing "
            f"(1..{MAX_NATIONAL_RATING_MATCHES})"
        ),
    )
    parser.add_argument("--workers", type=int, default=max(1, min(12, int(multiprocessing.cpu_count() * 28 / 32))), help="Parallel workers (capped at 12 to avoid OOM; see MAX_SAFE_DAEMON_WORKERS)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print match results")
    parser.add_argument("--once", action="store_true", help="Run ~14 matches then exit")
    args = parser.parse_args()
    if not 1 <= args.pairs <= MAX_NATIONAL_RATING_MATCHES:
        parser.error(
            "--pairs must be an integer in "
            f"[1, {MAX_NATIONAL_RATING_MATCHES}]"
        )

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    global running, daemon_run_id, daemon_evaluation_identity_digest
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
    log.info("Glicko-2 + H2H from complete 70-hand local native TCP matches")
    _backend_config = _rating_protocol_config(n_pairs=args.pairs)
    from evaluation_data_identity import ensure_evaluation_data_identity

    identity_manifest = ensure_evaluation_data_identity(
        RESULTS_DIR,
        runtime_profile=_backend_config,
    )
    daemon_evaluation_identity_digest = str(
        identity_manifest.get("manifest_digest") or ""
    )
    log.info(
        "Rating backend: profile=%s protocol=%s execution=%s national_hands=%s national_matches=%s artifact_execution=%s",
        _backend_config["profile_id"],
        _backend_config["protocol"],
        _backend_config["national_execution_mode"],
        _backend_config["national_hands"],
        _backend_config["national_matches"],
        _backend_config["artifact_execution_mode"],
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

    # Load persisted state from the immutable commit pointer. Compatibility
    # aliases are repaired from it before any new match can be scheduled.
    ratings, h2h, bot_stats, stats, _recovered_save_num = (
        _load_committed_daemon_state()
    )

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

    # Establish a recoverable empty baseline before scheduling even one match.
    # This closes the first-cycle crash window after an identity reset.
    from evaluation_bundle import MANIFEST_FILENAME as _CYCLE_MANIFEST_FILENAME

    if not (RESULTS_DIR / _CYCLE_MANIFEST_FILENAME).exists():
        _save_authoritative_evaluation_cycle(
            ratings,
            {},
            bot_stats,
            stats,
            0,
            active_bots,
        )

    try:
        start_official_certification_thread()
    except Exception as e:
        log.warning("Official certification job reconciler failed to start (non-fatal): %s", e)

    active_bots, h2h = _wait_for_minimum_rating_pool(
        active_bots,
        ratings,
        h2h,
        bot_stats,
        save_num=int(_recovered_save_num),
        verbose=args.verbose,
        once=args.once,
    )
    if not running or len(active_bots) < MIN_RATING_POOL_BOTS:
        log.info("Rating daemon stopped while waiting for a schedulable pool.")
        return

    # Build initial match queue
    match_queue = deque()
    matches = pick_matches(active_bots, h2h, ratings, n_picks=n_workers * 2)
    for a, b in matches:
        match_queue.append(_internal_match_job(a, b, bot_path(a), bot_path(b), n_pairs))

    # Eval round manager for deterministic evaluation cycles
    eval_round_mgr = EvalRoundManager()

    import multiprocessing as _mp
    mp_ctx = _mp.get_context("spawn")
    executor = ProcessPoolExecutor(max_workers=n_workers, mp_context=mp_ctx)
    in_flight = {}  # future -> (bot_a, bot_b)

    # The daemon owns only current-pool strength scheduling. Precommit and
    # official certification have their own identity-bound direct runners.
    while len(in_flight) < n_workers and match_queue:
        m = match_queue.popleft()
        if m[0] not in active_bots or m[1] not in active_bots:
            continue
        fut = executor.submit(run_single_match, m)
        in_flight[fut] = (m[0], m[1])

    games_since_save = 0
    last_save_time = time.time()
    last_parent_check = time.time()
    last_heartbeat_time = 0.0
    save_num = int(_recovered_save_num)
    total_matches = 0
    MAX_POOL_RECOVERIES = 3
    recovery_count = 0
    played_bots_this_cycle = set()
    last_bot_refresh_time = time.time()
    evaluation_commit_failed = False

    try:
        # H4 (2026-06-29): track priority_eval.json mtime so a newly-committed bot's
        # priority signal takes effect without waiting for the current match_queue
        # (potentially hundreds of daemon matches) to drain. When the file's mtime
        # changes, queued internal matches are cleared so the next refill
        # re-reads the updated priority. Initialized to the current mtime so a
        # fresh start does not immediately discard the seed queue.
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
                    # rewritten (new commit), drop queued matches so the next
                    # pick_matches call uses the new priority bot.
                    try:
                        if PRIORITY_EVAL_FILE.exists():
                            _mt = os.path.getmtime(PRIORITY_EVAL_FILE)
                            if _mt != _priority_eval_mtime:
                                _dropped = len(match_queue)
                                if _dropped > 0:
                                    match_queue.clear()
                                    _priority_bot_now = _load_priority_eval()
                                    log.info(
                                        "H4: priority_eval.json changed (mtime %.0f→%.0f); "
                                        "dropped %d queued match(es); "
                                        "priority_bot=%s",
                                        _priority_eval_mtime, _mt, _dropped,
                                        _priority_bot_now,
                                    )
                                _priority_eval_mtime = _mt
                    except OSError:
                        pass

                    if not in_flight:
                        if not match_queue:
                            for ma, mb in pick_matches(
                                active_bots, h2h, ratings, n_picks=n_workers * 2
                            ):
                                match_queue.append(
                                    _internal_match_job(
                                        ma, mb, bot_path(ma), bot_path(mb), n_pairs
                                    )
                                )
                        while len(in_flight) < n_workers and match_queue:
                            m = match_queue.popleft()
                            if m[0] not in active_bots or m[1] not in active_bots:
                                continue
                            future = executor.submit(run_single_match, m)
                            in_flight[future] = (m[0], m[1])
                        if time.time() - last_heartbeat_time >= 5:
                            _write_heartbeat(
                                activity_state="scheduling_matches",
                                active_bot_count=len(active_bots),
                            )
                            last_heartbeat_time = time.time()
                        if not in_flight:
                            active_bots, h2h = _wait_for_minimum_rating_pool(
                                active_bots,
                                ratings,
                                h2h,
                                bot_stats,
                                save_num=save_num,
                                verbose=args.verbose,
                                once=args.once,
                            )
                            if not running:
                                break
                            if len(active_bots) < MIN_RATING_POOL_BOTS:
                                running = False
                                break
                        continue

                    done, _ = wait(in_flight.keys(), timeout=POLL_TIMEOUT, return_when=FIRST_COMPLETED)

                    for fut in done:
                        a, b = in_flight.pop(fut)
                        from evaluation_data_identity import current_evaluation_digest

                        if current_evaluation_digest(RESULTS_DIR) != daemon_evaluation_identity_digest:
                            log.error(
                                "Evaluation identity changed with an internal match in flight; "
                                "discarding result and stopping stale daemon run"
                            )
                            try:
                                _discard_staged_match(fut.result())
                            except Exception:
                                pass
                            running = False
                            break
                        # Skip results for bots that have been reaped
                        if a not in active_bots or b not in active_bots:
                            try:
                                _discard_staged_match(fut.result())
                            except Exception as e:
                                log.debug("Reaped bot result error: %s", e)
                            continue
                        result = fut.result()
                        try:
                            n = admit_internal_match_result(
                                result,
                                ratings,
                                h2h,
                                bot_stats,
                                verbose=args.verbose,
                            )
                        except Exception:
                            evaluation_commit_failed = True
                            raise
                        games_since_save += n
                        total_matches += 1
                        if n > 0:
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
                                        match_queue.append(
                                            _internal_match_job(
                                                ea, eb, bot_path(ea), bot_path(eb), n_pairs
                                            )
                                        )
                                    if args.verbose:
                                        log.info("Eval round triggered: %d pairs queued", len(eval_pairs))
                        except Exception as er_err:
                            log.warning("Eval round tracking error (non-fatal): %s", er_err)

                        # Replenish from the current-pool native match queue.
                        if match_queue and executor is not None:
                            m = match_queue.popleft()
                            if m[0] not in active_bots or m[1] not in active_bots:
                                continue
                            new_fut = executor.submit(run_single_match, m)
                            in_flight[new_fut] = (m[0], m[1])
                        elif executor is not None:
                            # Refill queue when empty
                            matches = pick_matches(active_bots, h2h, ratings, n_picks=n_workers * 2)
                            for ma, mb in matches:
                                match_queue.append(
                                    _internal_match_job(
                                        ma, mb, bot_path(ma), bot_path(mb), n_pairs
                                    )
                                )
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
                                try:
                                    save_cycle(
                                        ratings,
                                        h2h,
                                        bot_stats,
                                        stats,
                                        save_num,
                                        active_bots,
                                        played_bots=played_bots_this_cycle,
                                        verbose=args.verbose,
                                    )
                                except Exception:
                                    evaluation_commit_failed = True
                                    raise
                                games_since_save = 0
                                played_bots_this_cycle = set()
                                last_save_time = now
                    except Exception as e:
                        evaluation_commit_failed = True
                        log.critical("Evaluation cycle commit failed; stopping daemon: %s", e)
                        raise

                    # Refresh process health while the rating loop is active.
                    now_hb = time.time()
                    if now_hb - last_heartbeat_time >= 5:
                        _write_heartbeat(
                            activity_state="scheduling_matches",
                            active_bot_count=len(active_bots),
                        )
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
                        reap_fresh = _consume_reap_signal()
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
                                            match_queue.appendleft(
                                                _internal_match_job(
                                                    _a,
                                                    _b,
                                                    bot_path(_a),
                                                    bot_path(_b),
                                                    n_pairs,
                                                )
                                            )
                                            _prepended += 1
                                    if _prepended:
                                        log.info("Reap refresh: %d new-bot pairs prepended to queue (reap-bug fix)", _prepended)
                                except Exception as _rp_err:
                                    log.warning("Reap re-pick failed (non-fatal): %s", _rp_err)
                            if games_since_save > 0:
                                save_num += 1
                                try:
                                    save_cycle(
                                        ratings,
                                        h2h,
                                        bot_stats,
                                        stats,
                                        save_num,
                                        active_bots,
                                        played_bots=played_bots_this_cycle,
                                        verbose=args.verbose,
                                    )
                                except Exception:
                                    evaluation_commit_failed = True
                                    raise
                                games_since_save = 0
                                played_bots_this_cycle = set()
                                last_save_time = time.time()
                            if args.verbose:
                                log.info("Reap signal processed, active bots: %d", len(active_bots))
                    except Exception as e:
                        if evaluation_commit_failed:
                            raise
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
                                    if m[0] not in removed and m[1] not in removed
                                )
                                for fut in list(in_flight):
                                    fa, fb = in_flight[fut]
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

            except (BrokenProcessPool, ConnectionRefusedError, OSError) as e:
                # An OSError raised by replay admission or durable cycle
                # publication is a transaction failure, not a recoverable
                # ProcessPool transport fault.  Continuing here would run with
                # partially-mutated in-memory ratings after disk admission
                # failed.
                if evaluation_commit_failed:
                    raise
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
                for fut in list(in_flight):
                    try:
                        _discard_staged_match(fut.result(timeout=1))
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
                    match_queue = deque()
                    matches = pick_matches(active_bots, h2h, ratings, n_picks=n_workers * 2)
                    for a, b in matches:
                        match_queue.append(
                            _internal_match_job(
                                a, b, bot_path(a), bot_path(b), n_pairs
                            )
                        )
                    while len(in_flight) < n_workers and match_queue:
                        m = match_queue.popleft()
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

        # Final save.  A clean shutdown with no uncommitted strength samples is
        # not a rating period: advancing it would decay RD purely because the
        # process restarted.  Dirty samples, however, must commit or make the
        # daemon exit non-zero so a supervisor cannot mistake data loss for a
        # clean stop.
        final_save_error = None
        try:
            if not evaluation_commit_failed and games_since_save > 0:
                save_cycle(ratings, h2h, bot_stats, stats, save_num + 1, active_bots,
                           played_bots=played_bots_this_cycle, verbose=args.verbose)
        except Exception as e:
            final_save_error = e
            log.error("Final save failed: %s", e)
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
        if final_save_error is not None:
            raise RuntimeError("dirty evaluation state failed final commit") from final_save_error


if __name__ == "__main__":
    main()
